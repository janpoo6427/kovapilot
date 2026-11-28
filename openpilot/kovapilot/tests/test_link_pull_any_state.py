"""KovaLink fetch and pull at any vehicle state (janpoo6427 2026-11-28 20:06).

Pull = take the newest commit of the branch the comma is on. Only that skips
the Park/offroad check; a branch switch and a reset keep it.
"""

import asyncio
import shutil
import subprocess

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.git_update_request import KEY as GIT_UPDATE_KEY, is_pull
from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.git_maintenance import GitApplyBlocked, GitRepository
from openpilot.system.updated import updated

A, B = "a" * 40, "b" * 40


def test_pull_is_only_a_switch_to_the_installed_branch():
  job = {"operation": "switch", "branch": "main", "commit": B, "branchHead": B, "beforeHead": A, "beforeBranch": "main"}
  assert is_pull(job)
  assert not is_pull({**job, "branch": "next"})
  assert not is_pull({**job, "operation": "reset"})
  assert not is_pull({**job, "beforeBranch": None})
  assert not is_pull({k: v for k, v in job.items() if k != "beforeBranch"})
  assert not is_pull(None)


def _git(directory, *args):
  return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
                                 cwd=directory, text=True).strip()


@pytest.fixture
def checkout(tmp_path):
  """Live checkout on main one commit behind origin/main, plus origin/next."""
  origin = tmp_path / "origin"
  origin.mkdir()
  _git(origin, "init", "-q", "-b", "main")
  _git(origin, "commit", "--allow-empty", "-qm", "installed")
  installed = _git(origin, "rev-parse", "HEAD")
  _git(origin, "commit", "--allow-empty", "-qm", "newer")
  newest = _git(origin, "rev-parse", "HEAD")
  _git(origin, "branch", "next", installed)
  live = tmp_path / "live"
  _git(tmp_path, "clone", "-q", str(origin), str(live))
  _git(live, "reset", "-q", "--hard", installed)
  return live, installed, newest


def test_gateway_offers_pull_while_driving_but_not_switch_or_reset(checkout):
  live, installed, newest = checkout
  jobs = []
  # Driving: the full check (vehicle and updater) is off, the updater part is on.
  repo = GitRepository(live, maintenance_state=lambda: jobs[-1] if jobs else None, apply_allowed=lambda: False,
                       pull_allowed=lambda: True, enqueue=jobs.append)
  status = repo.status(True)
  assert status["mutationEnabled"] is False and status["mutationBlock"] == "parkedOrUpdaterBusy"
  assert status["pullEnabled"] is True
  assert repo.status(False)["pullEnabled"] is False
  pull = repo.plan("switch", "main", None, authorized=True)
  assert pull["commit"] == newest and pull["beforeHead"] == installed and pull["mutationEnabled"] is True
  assert repo.plan("switch", "next", None, authorized=True)["mutationEnabled"] is False
  assert repo.plan("reset", "main", newest, authorized=True)["mutationEnabled"] is False
  assert repo.plan("switch", "main", None, authorized=False)["mutationEnabled"] is False
  other = repo.plan("switch", "next", None, authorized=True)
  with pytest.raises(GitApplyBlocked):
    repo.apply("switch", "next", None, other["planDigest"])
  reset = repo.plan("reset", "main", newest, authorized=True)
  with pytest.raises(GitApplyBlocked):
    repo.apply("reset", "main", newest, reset["planDigest"])
  assert jobs == []
  assert repo.apply("switch", "main", None, pull["planDigest"])["maintenance"]["phase"] == "queued"
  assert jobs[0]["commit"] == newest and jobs[0]["beforeBranch"] == "main"
  assert _git(live, "rev-parse", "HEAD") == installed
  # One request at a time, pull included.
  assert repo.status(True)["pullEnabled"] is False


def test_gateway_pull_still_needs_an_idle_updater(checkout):
  live, _, _ = checkout
  repo = GitRepository(live, apply_allowed=lambda: False, pull_allowed=lambda: False, enqueue=lambda job: None)
  assert repo.status(True)["pullEnabled"] is False
  assert repo.plan("switch", "main", None, authorized=True)["mutationEnabled"] is False


def test_gateway_without_pull_rule_keeps_the_full_check_for_pull(checkout):
  live, _, _ = checkout
  for allowed in (False, True):
    repo = GitRepository(live, apply_allowed=lambda: allowed, enqueue=lambda job: None)
    assert repo.status(True)["pullEnabled"] is allowed
    assert repo.plan("switch", "main", None, authorized=True)["mutationEnabled"] is allowed


def test_http_apply_takes_pull_while_driving_and_refuses_a_branch_switch(checkout):
  live, installed, newest = checkout
  jobs = []
  repo = GitRepository(live, maintenance_state=lambda: jobs[-1] if jobs else None, apply_allowed=lambda: False,
                       pull_allowed=lambda: True, enqueue=jobs.append)

  async def run():
    app = create_app(KovaParamsStore(None), lambda: False, git=repo)
    async with TestClient(TestServer(app)) as client:
      async with client.get("/v1/git/branches") as response:
        assert response.status == 200
      async with client.get("/v1/git/status") as response:
        status = await response.json()
      assert status["pullEnabled"] is True and status["mutationEnabled"] is False
      other = await (await client.post("/v1/git/plan", json={"operation": "switch", "branch": "next"})).json()
      async with client.post("/v1/git/apply", json={"operation": "switch", "branch": "next",
                                                     "planDigest": other["planDigest"]}) as response:
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "git_mutation_disabled"
      data = {"operation": "switch", "branch": "main"}
      plan = await (await client.post("/v1/git/plan", json=data)).json()
      assert plan["mutationEnabled"] is True
      async with client.post("/v1/git/apply", json=dict(data, planDigest=plan["planDigest"])) as response:
        assert response.status == 202
      assert jobs[0]["commit"] == newest and jobs[0]["beforeHead"] == installed

  asyncio.run(run())


class FakeParams:
  def __init__(self, **values):
    self.data = values

  def get(self, key):
    return self.data.get(key)

  def get_bool(self, key):
    return bool(self.data.get(key))

  def put(self, key, value, *, block):
    self.data[key] = value

  put_bool = put


def test_updater_stages_a_pull_while_driving_and_keeps_the_live_checkout(checkout, monkeypatch):
  live, installed, newest = checkout
  overlay = live.parent / "overlay"
  finalized = live.parent / "finalized"
  _git(live.parent, "clone", "-q", str(live.parent / "origin"), str(overlay))
  _git(overlay, "reset", "-q", "--hard", installed)
  params = FakeParams(IsOffroad=False)
  updater = object.__new__(updated.Updater)
  updater.params = params
  monkeypatch.setattr(updater, "link_maintenance_allowed", lambda: False)
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
  job = {"operation": "switch", "branch": "main", "branchHead": newest, "commit": newest,
         "beforeHead": installed, "beforeBranch": "main", "phase": "queued"}
  updater.process_link_update(job)
  assert params.get(GIT_UPDATE_KEY)["phase"] == "staged"
  assert params.get_bool("UpdateAvailable")
  assert _git(finalized, "rev-parse", "HEAD") == newest
  assert _git(live, "rev-parse", "HEAD") == installed


def test_updater_still_waits_for_park_to_switch_branches_or_reset(monkeypatch):
  for job in ({"operation": "switch", "branch": "next", "branchHead": B, "commit": B, "beforeHead": A, "beforeBranch": "main"},
              {"operation": "reset", "branch": "main", "branchHead": B, "commit": "c" * 40, "beforeHead": A,
               "beforeBranch": "main"}):
    job = {**job, "phase": "queued"}
    params = FakeParams(IsOffroad=False, **{GIT_UPDATE_KEY: job})
    updater = object.__new__(updated.Updater)
    updater.params = params
    monkeypatch.setattr(updater, "get_commit_hash", lambda path: A, raising=False)
    monkeypatch.setattr(updater, "get_branch", lambda path: "main", raising=False)
    monkeypatch.setattr(updater, "link_maintenance_allowed", lambda: False)
    monkeypatch.setattr(updated, "init_overlay", lambda: pytest.fail("must wait for Park or offroad"))
    updater.process_link_update(job)
    assert params.get(GIT_UPDATE_KEY)["phase"] == "queued"
