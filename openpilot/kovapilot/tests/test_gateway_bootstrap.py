import hashlib
import subprocess
import tomllib
from pathlib import Path

import pytest

from openpilot.kovapilot.gateway import bootstrap, runtime


def test_bundled_wheels_have_their_locked_hashes():
  with (Path(runtime.BASEDIR) / "uv.lock").open("rb") as source:
    lock = tomllib.load(source)
  locked = {wheel["url"].rsplit("/", 1)[-1]: wheel["hash"] for package in lock["package"] for wheel in package.get("wheels", [])}
  wheels = list(runtime.DEVICE_WHEELS.glob("*.whl"))
  assert wheels
  for wheel in wheels:
    with wheel.open("rb") as file:
      assert locked[wheel.name] == f"sha256:{hashlib.file_digest(file, 'sha256').hexdigest()}"


def test_runtime_stages_locked_packages_and_reuses_them(tmp_path, monkeypatch):
  directory = tmp_path / "gateway" / "python"
  downloads = []
  run = subprocess.run

  def fake_run(command, **kwargs):
    if command[0] != "uv":
      return run(command, **kwargs)
    downloads.append(command)
    assert not runtime.runtime_ready(directory)
    assert "--offline" in command
    if "export" in command:
      assert "kovalink-gateway" in command
      kwargs["stdout"].write(b"locked packages")
    else:
      assert "--no-index" in command
      assert command[command.index("--find-links") + 1] == str(runtime.DEVICE_WHEELS)
      assert "--require-hashes" in command
      target = Path(command[command.index("--target") + 1])
      package = target / "aiohttp"
      package.mkdir(parents=True)
      (package / "__init__.py").touch()
      (package / "web.py").touch()

  monkeypatch.setattr(runtime.subprocess, "run", fake_run)
  runtime.install_runtime(directory)
  assert runtime.runtime_ready(directory)
  assert len(downloads) == 2
  runtime.install_runtime(directory)
  assert len(downloads) == 2


def test_failed_install_does_not_publish_a_runtime(tmp_path, monkeypatch):
  directory = tmp_path / "gateway" / "python"

  def fake_run(command, **kwargs):
    if "export" in command:
      kwargs["stdout"].write(b"locked packages")
      return
    target = Path(command[command.index("--target") + 1])
    (target / "aiohttp").mkdir(parents=True)
    raise subprocess.TimeoutExpired(command, timeout=1)

  monkeypatch.setattr(runtime.subprocess, "run", fake_run)
  with pytest.raises(subprocess.TimeoutExpired):
    runtime.install_runtime(directory)
  assert not directory.exists()
  assert {file.name for file in directory.parent.iterdir()} == {".runtime.lock"}


def test_preparation_retries_until_local_install_succeeds(monkeypatch):
  attempts = []
  sleeps = []

  def install():
    attempts.append(1)
    if len(attempts) == 1:
      raise OSError("temporary package directory unavailable")

  monkeypatch.setattr(bootstrap, "install_runtime", install)
  monkeypatch.setattr(bootstrap.time, "sleep", sleeps.append)
  bootstrap.prepare_runtime()
  assert len(attempts) == 2
  assert sleeps == [60]
