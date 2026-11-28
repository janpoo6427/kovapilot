import asyncio
import subprocess
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.git_maintenance import GitRepository
from openpilot.kovapilot.gateway.telemetry import parked_for_settings, road_scene


def test_kovalink_transports(tmp_path):
  def git(*args):
    return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

  git("init", "-q", "-b", "main")
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture")
  head = git("rev-parse", "HEAD")
  git("remote", "add", "origin", ".")
  repo = GitRepository(tmp_path)

  async def run():
    async def signal(offer):
      assert offer == {"sdp": "v=0\r\n", "cameras": ["road"], "enabled": True, "raw": False,
                       "bridge_services_in": [], "bridge_services_out": []}
      return {"sdp": "v=0\r\n", "type": "answer"}

    scene = {"monotonicTimeNanos": 42, "speedMps": 5}
    app = create_app(KovaParamsStore(None), lambda: True, telemetry=lambda: scene, camera=signal, git=repo)
    async with TestClient(TestServer(app)) as client:
      async with client.ws_connect("/v1/telemetry") as ws:
        assert await ws.receive_json(timeout=1) == scene
      response = await client.post("/v1/stream", json={
        "sdp": "v=0\r\n", "cameras": ["road"], "raw": True, "bridge_services_in": ["carControl"],
      })
      assert await response.json() == {"sdp": "v=0\r\n", "type": "answer"}
      assert (await client.post("/v1/stream", json={"sdp": "v=0", "cameras": ["driver"]})).status == 400
      assert (await (await client.get("/v1/git/status")).json())["head"] == head
      assert (await (await client.get("/v1/git/branches")).json())["branches"] == [{"name": "main", "commit": head}]
      assert (await (await client.get("/v1/git/log?limit=1")).json())["entries"][0]["subject"] == "fixture"
      plan = await (await client.post("/v1/git/reset", json={"branch": "main", "commit": head})).json()
      assert plan["commit"] == head and plan["mutationEnabled"] is False
      assert (await client.post("/v1/git/reset", json={"branch": "main", "commit": "0" * 40})).status == 400
      assert (await client.post("/v1/git/apply", json={"planDigest": plan["planDigest"]})).status == 403
      assert (await client.get("/v1/git/status", headers={"Origin": "https://example.invalid"})).status == 403
      assert (await client.get("/v1/git/status", headers={"Host": "example.invalid"})).status == 403
    assert git("rev-parse", "HEAD") == head
    assert git("status", "--porcelain") == ""

  asyncio.run(run())


def test_git_status_and_preview_return_commits_when_worktree_scan_times_out(tmp_path):
  head = "1" * 40
  target = "2" * 40

  class SlowStatusRepository(GitRepository):
    def run(self, *args, **kwargs):
      if args[0] == "fetch":
        return ""
      if args[0] == "status":
        raise subprocess.TimeoutExpired(args, 3)
      if args[0] == "symbolic-ref":
        return "master-jh\n"
      if args[0] == "rev-parse":
        return "origin/master-jh\n" if args[-1] == "@{upstream}" else head
      if args[0] == "for-each-ref":
        return f"master-jh\t{head}\t\nother\t{target}\t\n"
      if args[0] == "diff":
        assert args[-3:] == (head, target, "--")
        return "changed-file\0"
      raise AssertionError(args)

  async def run():
    app = create_app(KovaParamsStore(None), lambda: True, git=SlowStatusRepository(tmp_path))
    async with TestClient(TestServer(app)) as client:
      response = await client.get("/v1/git/status")
      assert response.status == 200
      status = await response.json()
      assert status["branch"] == "master-jh" and status["head"] == head
      assert status["dirtyKnown"] is False
      assert status["dirty"] is True
      response = await client.post("/v1/git/switch", json={"branch": "other"})
      assert response.status == 200
      plan = await response.json()
      assert plan["beforeHead"] == head and plan["commit"] == target
      assert plan["changedFileCount"] == 1
      assert plan["dirtyKnown"] is False and plan["dirty"] is True
      assert plan["mutationEnabled"] is False
      assert (await client.post("/v1/git/apply", json={})).status == 403

  asyncio.run(run())


def test_gateway_fetches_current_branch_tips_without_changing_checkout(tmp_path):
  remote = tmp_path / "remote"
  remote.mkdir()
  local = tmp_path / "device"

  def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()

  def commit(name):
    git(remote, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", name)
    return git(remote, "rev-parse", "HEAD")

  git(remote, "init", "-q", "-b", "main")
  installed = commit("installed")
  git(tmp_path, "clone", "-q", "--single-branch", str(remote), str(local))
  git(local, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/unrelated/*")
  git(remote, "switch", "-qc", "next")
  listed = commit("latest when listing")
  fetch_head = local / ".git" / "FETCH_HEAD"
  fetch_head.write_text("existing updater marker")

  async def run():
    app = create_app(KovaParamsStore(None), lambda: True, git=GitRepository(local))
    async with TestClient(TestServer(app)) as client:
      response = await client.get("/v1/git/branches")
      assert response.status == 200
      branches = {branch["name"]: branch["commit"] for branch in (await response.json())["branches"]}
      assert branches == {"main": installed, "next": listed}

      newer = commit("latest while preview opens")
      response = await client.post("/v1/git/plan", json={"operation": "switch", "branch": "next"})
      assert response.status == 200
      plan = await response.json()
      assert plan["commit"] == newer and plan["beforeHead"] == installed
      assert git(local, "rev-parse", "HEAD") == installed
      assert git(local, "status", "--porcelain") == ""
      assert fetch_head.read_text() == "existing updater marker"
      assert git(local, "for-each-ref", "refs/remotes/unrelated/") == ""

      git(remote, "switch", "-q", "main")
      git(remote, "branch", "-D", "next")
      response = await client.get("/v1/git/branches")
      assert response.status == 200
      assert (await response.json())["branches"] == [{"name": "main", "commit": installed}]

      git(local, "remote", "set-url", "origin", str(tmp_path / "missing"))
      for response in (await client.get("/v1/git/branches"),
                       await client.post("/v1/git/plan", json={"operation": "switch", "branch": "next"})):
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "git_unavailable"

  asyncio.run(run())


def test_authorized_git_apply_queues_pinned_target_and_does_not_change_live_checkout(tmp_path):
  def git(*args):
    return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

  git("init", "-q", "-b", "main")
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "main")
  installed = git("rev-parse", "HEAD")
  git("switch", "-qc", "next")
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "next")
  target = git("rev-parse", "HEAD")
  git("switch", "-q", "main")
  git("remote", "add", "origin", ".")
  jobs = []
  repo = GitRepository(tmp_path, maintenance_state=lambda: jobs[0] if jobs else None, apply_allowed=lambda: True, enqueue=jobs.append)

  async def run():
    app = create_app(KovaParamsStore(None), lambda: True, git=repo)
    async with TestClient(TestServer(app)) as client:
      data = {"operation": "switch", "branch": "next"}
      async with client.post("/v1/git/plan", json=data) as response:
        assert response.status == 200
        plan = await response.json()
      assert plan["commit"] == target and plan["mutationEnabled"]
      assert (await client.post("/v1/git/apply", json=dict(data, planDigest="0" * 64))).status == 409
      edit = tmp_path / "untracked"
      edit.touch()
      assert (await client.post("/v1/git/apply", json=dict(data, planDigest=plan["planDigest"]))).status == 403
      edit.unlink()
      assert not jobs
      async with client.post("/v1/git/apply", json=dict(data, planDigest=plan["planDigest"])) as response:
        assert response.status == 202
        assert (await response.json())["maintenance"]["phase"] == "queued"
      assert jobs[0]["beforeHead"] == installed
      assert jobs[0]["commit"] == jobs[0]["branchHead"] == target
      assert (await client.post("/v1/git/apply", json=dict(data, planDigest=plan["planDigest"]))).status == 403
      assert git("rev-parse", "HEAD") == installed

  asyncio.run(run())


def test_parked_configuration_requires_fresh_can_park_and_no_engagement():
  class Sample(dict):
    valid = {"carState": True, "selfdriveState": True}
    alive = valid.copy()
    logMonoTime = {"carState": 1_000_000_000, "selfdriveState": 1_000_000_000}

  car = SimpleNamespace(vEgo=0., canValid=True, standstill=True, gearShifter="park")
  selfdrive = SimpleNamespace(enabled=False)
  sm = Sample(carState=car, selfdriveState=selfdrive)
  assert parked_for_settings(sm, 1_010_000_000)
  car.gearShifter = "drive"
  assert not parked_for_settings(sm, 1_010_000_000)
  car.gearShifter = "park"
  selfdrive.enabled = True
  assert not parked_for_settings(sm, 1_010_000_000)
  selfdrive.enabled = False
  assert not parked_for_settings(sm, 1_600_000_000)


def test_scene_omits_stale_sources_and_converts_cruise_speed():
  class Sample(dict):
    valid = {"carState": True, "selfdriveState": True, "modelV2": True}
    alive = valid
    logMonoTime = {"carState": 1_000_000_000, "selfdriveState": 1, "modelV2": 1}

  sm = Sample(carState=SimpleNamespace(vEgo=10., steeringAngleDeg=4., vCruise=72., vCruiseCluster=72., gearShifter="drive",
                                      leftBlinker=True, rightBlinker=False, brakePressed=False))
  scene = road_scene(sm, 1_000_000_001)
  assert scene["setSpeedMps"] == 20
  assert scene["gear"] == "drive" and scene["leftBlinker"]
  assert "engaged" not in scene and "plannedPath" not in scene
  assert road_scene(sm, 2_000_000_000) is None
