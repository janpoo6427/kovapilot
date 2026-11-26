import asyncio
import hashlib
import io
import json
import os
import stat
import tarfile
from collections import namedtuple

import pytest

from openpilot.kovapilot.gateway import tailscale_install
from openpilot.kovapilot.gateway.lan import LanAccess
from openpilot.kovapilot.gateway.tailscale import INSTALL_RETRY, Tailscale
from openpilot.kovapilot.gateway.tailscale_install import InstallError, choose_directory, install


NAME = "tailscale_9.9.9_arm64"
DAEMON = b"#!/bin/sh\nexec sleep 30\n"
CLI = b"#!/bin/sh\necho '{\"BackendState\": \"NeedsLogin\"}'\n"


def archive(members=None) -> bytes:
  members = {"tailscale": CLI, "tailscaled": DAEMON, "systemd/tailscaled.service": b"[Unit]\n"} if members is None else members
  out = io.BytesIO()
  with tarfile.open(fileobj=out, mode="w:gz") as tar:
    for name, data in members.items():
      info = tarfile.TarInfo(f"{NAME}/{name}")
      info.size = len(data)
      info.mode = 0o755
      tar.addfile(info, io.BytesIO(data))
  return out.getvalue()


def served(data: bytes, calls=None):
  def fetch(url, timeout):
    if calls is not None:
      calls.append(url)
    return io.BytesIO(data)
  return fetch


def run(target, scratch, data, expected=None, **kwargs):
  install(target, scratch, served(data, kwargs.pop("calls", None)), url="https://pkgs.example/x.tgz",
          expected=expected or hashlib.sha256(data).hexdigest(), name=NAME, **kwargs)


def leftovers(parent):
  return sorted(p.name for p in parent.iterdir() if p.name.startswith(".tailscale-install."))


def test_installs_the_two_binaries_owner_only(tmp_path):
  target = tmp_path / "persist" / "kova" / "tailscale"
  run(target, tmp_path / "data", archive())
  assert sorted(p.name for p in target.iterdir()) == ["tailscale", "tailscaled"]
  assert stat.S_IMODE(target.stat().st_mode) == 0o700
  for name, data in (("tailscale", CLI), ("tailscaled", DAEMON)):
    assert (target / name).read_bytes() == data
    assert stat.S_IMODE((target / name).stat().st_mode) == 0o700
  assert Tailscale(target, LanAccess(None), lambda: True, dict, dict).installed()
  assert list((tmp_path / "data").iterdir()) == []


@pytest.mark.parametrize("data,expected,reason", [
  (archive(), "0" * 64, "checksum mismatch"),
  (archive({"tailscale": CLI}), None, "unexpected archive layout"),
  (b"not a tarball", None, "unexpected archive layout"),
])
def test_bad_archive_leaves_nothing(tmp_path, data, expected, reason):
  target = tmp_path / "kova" / "tailscale"
  with pytest.raises(InstallError, match=reason):
    run(target, tmp_path / "data", data, expected)
  assert not target.exists()
  assert leftovers(target.parent) == []


def test_symlinked_member_is_refused(tmp_path):
  out = io.BytesIO()
  with tarfile.open(fileobj=out, mode="w:gz") as tar:
    info = tarfile.TarInfo(f"{NAME}/tailscale")
    info.type = tarfile.SYMTYPE
    info.linkname = "/bin/sh"
    tar.addfile(info)
    info = tarfile.TarInfo(f"{NAME}/tailscaled")
    info.size = len(DAEMON)
    tar.addfile(info, io.BytesIO(DAEMON))
  target = tmp_path / "kova" / "tailscale"
  with pytest.raises(InstallError):
    run(target, tmp_path / "data", out.getvalue())
  assert not target.exists()
  assert leftovers(target.parent) == []


def test_download_errors_and_size_limit(tmp_path, monkeypatch):
  target = tmp_path / "kova" / "tailscale"

  def offline(url, timeout):
    raise OSError("no route to host")

  with pytest.raises(InstallError, match="download failed"):
    install(target, tmp_path / "data", offline, expected="0" * 64, name=NAME)
  monkeypatch.setattr(tailscale_install, "MAX_ARCHIVE_BYTES", 10)
  with pytest.raises(InstallError, match="too large"):
    run(target, tmp_path / "data", archive())
  assert not target.exists()


def test_not_enough_space_leaves_nothing(tmp_path, monkeypatch):
  usage = namedtuple("usage", "total used free")
  monkeypatch.setattr(tailscale_install.shutil, "disk_usage", lambda path: usage(1, 1, 1024))
  target = tmp_path / "kova" / "tailscale"
  with pytest.raises(InstallError, match="not enough space"):
    run(target, tmp_path / "data", archive())
  assert not target.exists()
  assert leftovers(target.parent) == []


def test_existing_installation_is_not_touched(tmp_path):
  target = tmp_path / "tailscale"
  target.mkdir()
  calls = []
  run(target, tmp_path / "data", archive(), calls=calls)
  assert calls == []
  assert list(target.iterdir()) == []


def test_pinned_release_is_the_official_arm64_archive():
  assert tailscale_install.URL == f"https://pkgs.tailscale.com/stable/tailscale_{tailscale_install.VERSION}_arm64.tgz"
  assert tailscale_install.ARCHIVE == f"tailscale_{tailscale_install.VERSION}_arm64"
  assert len(tailscale_install.SHA256) == 64 and int(tailscale_install.SHA256, 16) >= 0


def test_failed_install_reports_and_backs_off(tmp_path):
  attempts = []

  def broken():
    attempts.append(1)
    raise InstallError("download failed")

  async def check():
    service = Tailscale(tmp_path / "tailscale", LanAccess(None), lambda: True, dict, dict, installer=broken)
    await service._install()
    assert service.current == {"state": "installFailed"}
    assert service.install_retry == INSTALL_RETRY * 2
    await service._install()
    assert len(attempts) == 1
    service.next_install = 0.0
    await service._install()
    assert len(attempts) == 2
    assert service.install_retry == INSTALL_RETRY * 4

  asyncio.run(check())


def test_status_answers_while_the_download_runs(tmp_path):
  async def check():
    started, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow():
      loop.call_soon_threadsafe(started.set)
      asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)
      raise InstallError("download failed")

    service = Tailscale(tmp_path / "tailscale", LanAccess(None), lambda: True, dict, dict, installer=slow)
    task = asyncio.create_task(service._run())
    try:
      await asyncio.wait_for(started.wait(), 5)
      assert service.current == {"state": "installing"}
      async with asyncio.timeout(1):
        async with service.lock:
          pass
      release.set()
      for _ in range(100):
        if service.current["state"] == "installFailed":
          break
        await asyncio.sleep(0.02)
      assert service.current == {"state": "installFailed"}
      await asyncio.sleep(0.1)
      assert service.current == {"state": "installFailed"}
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)

  asyncio.run(check())


def test_installed_binaries_start_and_become_ready(tmp_path):
  data = archive()
  target = tmp_path / "kova" / "tailscale"

  async def check():
    service = Tailscale(target, LanAccess(None), lambda: True, dict, dict,
                        installer=lambda: run(target, tmp_path / "data", data))
    task = asyncio.create_task(service._run())
    try:
      for _ in range(250):
        if service.current["state"] == "needsLogin":
          break
        await asyncio.sleep(0.02)
      assert service.current == {"state": "needsLogin"}
      assert service.daemon is not None and service.daemon.returncode is None
      status = json.loads((await service._command("status", "--json")) or "{}")
      assert status == {"BackendState": "NeedsLogin"}
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)
      if service.daemon is not None and service.daemon.returncode is None:
        service.daemon.kill()
        await service.daemon.wait()

  asyncio.run(check())


def test_without_installer_missing_binaries_stay_not_installed(tmp_path):
  async def check():
    service = Tailscale(tmp_path / "none", LanAccess(None), lambda: True, dict, dict)
    task = asyncio.create_task(service._run())
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert service.current == {"state": "notInstalled"}

  asyncio.run(check())
  assert not os.path.exists(tmp_path / "none")


def test_manual_persist_install_wins_else_data(tmp_path):
  manual, automatic = tmp_path / "persist" / "tailscale", tmp_path / "data" / "tailscale"
  assert choose_directory(manual, automatic) == automatic
  manual.mkdir(parents=True)
  assert choose_directory(manual, automatic) == manual
