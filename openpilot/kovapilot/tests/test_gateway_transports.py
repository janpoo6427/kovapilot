import asyncio
import subprocess
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.git_maintenance import GitRepository
from openpilot.kovapilot.gateway.telemetry import road_scene


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
