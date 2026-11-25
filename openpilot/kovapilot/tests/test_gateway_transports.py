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
  git("update-ref", "refs/remotes/origin/main", head)
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
    def run(self, *args, check=True):
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

  sm = Sample(carState=SimpleNamespace(vEgo=10., steeringAngleDeg=4., vCruise=72., gearShifter="drive",
                                      leftBlinker=True, rightBlinker=False, brakePressed=False))
  scene = road_scene(sm, 1_000_000_001)
  assert scene["setSpeedMps"] == 20
  assert scene["gear"] == "drive" and scene["leftBlinker"]
  assert "engaged" not in scene and "plannedPath" not in scene
  assert road_scene(sm, 2_000_000_000) is None
