import subprocess
from pathlib import Path

import pytest

from openpilot.kovapilot.gateway import bootstrap, runtime


def test_runtime_stages_locked_packages_and_reuses_them(tmp_path, monkeypatch):
  directory = tmp_path / "gateway" / "python"
  downloads = []
  run = subprocess.run

  def fake_run(command, **kwargs):
    if command[0] != "uv":
      return run(command, **kwargs)
    downloads.append(command)
    assert not runtime.runtime_ready(directory)
    if command[1] == "export":
      assert "kovalink-gateway" in command
      kwargs["stdout"].write(b"locked packages")
    else:
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
    if command[1] == "export":
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


@pytest.mark.parametrize("offroad", [True, False])
def test_preparation_retries_offroad_and_never_installs_onroad(monkeypatch, offroad):
  class Params:
    def get_bool(self, name):
      assert name == "IsOffroad"
      return offroad

  attempts = []
  sleeps = []

  def install():
    attempts.append(1)
    if len(attempts) == 1:
      raise OSError("network unavailable")

  monkeypatch.setattr(bootstrap, "install_runtime", install)
  monkeypatch.setattr(bootstrap.time, "sleep", sleeps.append)
  bootstrap.prepare_offroad(Params())
  assert len(attempts) == (2 if offroad else 0)
  assert sleeps == ([60] if offroad else [])
