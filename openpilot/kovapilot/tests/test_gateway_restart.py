import asyncio
import subprocess

from aiohttp.test_utils import TestClient, TestServer

from openpilot.common.params import Params
from openpilot.kovapilot.config.git_update_request import KEY as GIT_UPDATE_KEY
from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.device_restart import REBOOT_KEY, DeviceRestart, RestartBlocked, restart_allowed
from openpilot.kovapilot.gateway.git_maintenance import GitRepository


class NoSettings:
  def get(self, key):
    return None

  def put(self, key, value, block=False):
    raise AssertionError("restart must not change settings")


class FakeParams:
  def __init__(self, **values):
    self.values = values

  def get_bool(self, key):
    return bool(self.values.get(key))

  def get(self, key):
    return self.values.get(key)


def test_restart_is_allowed_only_offroad_and_not_while_an_update_is_written():
  idle = {"IsOffroad": True, "UpdaterState": "idle"}
  assert restart_allowed(FakeParams(**idle))
  assert not restart_allowed(FakeParams(**idle), pc=True)
  assert not restart_allowed(FakeParams(**{**idle, "IsOffroad": False}))
  assert not restart_allowed(FakeParams(**{**idle, "UpdaterState": "finalizing update..."}))
  # The device's own Reboot button also works while an update downloads.
  assert restart_allowed(FakeParams(**{**idle, "UpdaterState": "downloading..."}))
  assert restart_allowed(FakeParams(IsOffroad=True))
  for phase, allowed in (("queued", False), ("staged", True), ("installed", True), ("failed", True)):
    assert restart_allowed(FakeParams(**idle, **{GIT_UPDATE_KEY: {"phase": phase}})) is allowed


def test_restart_needs_authorization_and_a_request_path():
  calls = []
  restart = DeviceRestart(lambda: True, lambda: calls.append(1))
  assert restart.enabled(True)
  assert not restart.enabled(False)
  assert not DeviceRestart(lambda: True).enabled(True)
  try:
    restart.restart(False)
  except RestartBlocked:
    pass
  else:
    raise AssertionError("an unauthorized restart must be refused")
  assert calls == []
  restart.restart(True)
  assert calls == [1]


def test_restart_request_sets_the_same_param_as_the_reboot_button():
  params = Params()
  params.remove(REBOOT_KEY)
  restart = DeviceRestart(lambda: True, lambda: params.put_bool(REBOOT_KEY, True, block=True))
  restart.restart(True)
  assert params.get_bool(REBOOT_KEY)
  params.remove(REBOOT_KEY)


def test_gateway_restart_endpoint_and_status_flag(tmp_path):
  subprocess.check_call(["git", "init", "-q", "-b", "main"], cwd=tmp_path)
  subprocess.check_call(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "--allow-empty", "-qm", "fixture"], cwd=tmp_path)
  state = {"allowed": False}
  calls = []
  restart = DeviceRestart(lambda: state["allowed"], lambda: calls.append(1))

  async def check():
    app = create_app(KovaParamsStore(NoSettings()), lambda: True, git=GitRepository(tmp_path), restart=restart)
    async with TestClient(TestServer(app)) as client:
      assert (await (await client.get("/v1/git/status")).json())["restartEnabled"] is False
      async with client.post("/v1/device/restart") as response:
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "restart_blocked"
      assert calls == []

      state["allowed"] = True
      assert (await (await client.get("/v1/git/status")).json())["restartEnabled"] is True
      # A web page on another origin cannot restart the comma.
      async with client.post("/v1/device/restart", headers={"Origin": "https://example.invalid"}) as response:
        assert response.status == 403
      assert calls == []
      async with client.post("/v1/device/restart") as response:
        assert response.status == 202
        assert await response.json() == {"restarting": True}
      assert calls == [1]

    # Without a restart path the status says so and the endpoint is unavailable.
    app = create_app(KovaParamsStore(NoSettings()), lambda: True, git=GitRepository(tmp_path))
    async with TestClient(TestServer(app)) as client:
      assert (await (await client.get("/v1/git/status")).json())["restartEnabled"] is False
      async with client.post("/v1/device/restart") as response:
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "restart_unavailable"

  asyncio.run(check())
