import shutil
import subprocess

import pytest

from openpilot.kovapilot.config.git_update_request import KEY
from openpilot.system.updated import updated


@pytest.mark.parametrize("remote_moved", [False, True])
def test_link_stages_only_the_previewed_commit(tmp_path, monkeypatch, remote_moved):
  def git(directory, *args):
    return subprocess.check_output(["git", *args], cwd=directory, text=True).strip()

  def commit(message):
    git(origin, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", message)
    return git(origin, "rev-parse", "HEAD")

  origin = tmp_path / "origin"
  origin.mkdir()
  git(origin, "init", "-q", "-b", "main")
  target = commit("previous")
  before = commit("live")
  live = tmp_path / "live"
  overlay = tmp_path / "overlay"
  finalized = tmp_path / "finalized"
  git(tmp_path, "clone", "-q", str(origin), str(live))
  git(tmp_path, "clone", "-q", str(origin), str(overlay))
  if remote_moved:
    commit("changed upstream after preview")

  class Params:
    def __init__(self):
      self.data = {"IsOffroad": True}

    def get(self, key):
      return self.data.get(key)

    def get_bool(self, key):
      return bool(self.data.get(key))

    def put(self, key, value, *, block):
      self.data[key] = value

    put_bool = put

  params = Params()
  updater = object.__new__(updated.Updater)
  updater.params = params
  monkeypatch.setattr(updated, "BASEDIR", str(live))
  monkeypatch.setattr(updated, "OVERLAY_MERGED", str(overlay))
  monkeypatch.setattr(updated, "FINALIZED", str(finalized))
  monkeypatch.setattr(updated, "AGNOS", False)
  monkeypatch.setattr(updated, "init_overlay", lambda: None)
  monkeypatch.setattr(updated, "set_consistent_flag", lambda _: None)

  def finalize(*, link_requested):
    assert link_requested
    shutil.copytree(overlay, finalized)
    (finalized / ".overlay_consistent").touch()

  monkeypatch.setattr(updated, "finalize_update", finalize)
  job = {"operation": "reset", "branch": "main", "branchHead": before, "commit": target,
         "beforeHead": before, "beforeBranch": "main", "phase": "queued"}
  updater.process_link_update(job)
  assert git(live, "rev-parse", "HEAD") == before
  if remote_moved:
    assert params.get(KEY)["phase"] == "failed"
    assert not params.get_bool("UpdateAvailable")
    assert not finalized.exists()
  else:
    assert params.get(KEY)["phase"] == "staged"
    assert params.get_bool("UpdateAvailable")
    assert git(finalized, "rev-parse", "HEAD") == target
    updater.process_link_update(params.get(KEY))
    assert params.get(KEY)["phase"] == "staged"
