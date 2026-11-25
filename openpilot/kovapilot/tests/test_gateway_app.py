import asyncio
from copy import deepcopy

from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.cruise_sync_settings import read_cruise_sync_settings
from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, KOVA_PARAMS_KEY, KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app


class MemoryBackend:
  def __init__(self):
    self.value = None

  def get(self, key):
    assert key == KOVA_PARAMS_KEY
    return deepcopy(self.value)

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    assert block is True
    self.value = deepcopy(value)


async def _with_client(test_case, *, is_offroad=True):
  app = create_app(KovaParamsStore(MemoryBackend()), lambda: is_offroad)
  async with TestClient(TestServer(app)) as client:
    await test_case(client)


def test_gateway_updates_cruise_config_seen_by_another_store():
  backend = MemoryBackend()
  runtime = KovaParamsStore(backend)
  gateway = KovaParamsStore(backend)
  offroad = False

  async def check():
    nonlocal offroad
    async with TestClient(TestServer(create_app(gateway, lambda: offroad))) as client:
      result = await client.get("/v1/kova-params")
      assert result.status == 200
      settings = {setting["key"]: setting for setting in (await result.json())["settings"]}
      assert settings[CRUISE_SYNC_KEY]["value"] is False
      assert settings[CRUISE_SYNC_SHADOW_KEY]["value"] is False

      payload = {"key": CRUISE_SYNC_KEY, "value": True, "expectedRevision": 0}
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 403
      assert read_cruise_sync_settings(runtime) == (False, False)

      offroad = True
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 200
      assert read_cruise_sync_settings(runtime) == (True, False)

      payload = {"key": CRUISE_SYNC_SHADOW_KEY, "value": True, "expectedRevision": 1}
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 200
      assert read_cruise_sync_settings(runtime) == (True, True)

  asyncio.run(check())


def test_health_describes_loopback_security_stage():
  async def test_case(client):
    response = await client.get("/v1/health")
    assert response.status == 200
    assert await response.json() == {
      "status": "ok",
      "schemaVersion": 1,
      "bindScope": "loopback",
      "authentication": "localProcessOnly",
    }

  asyncio.run(_with_client(test_case))


def test_reads_localized_kova_params_document():
  async def test_case(client):
    response = await client.get("/v1/kova-params")
    document = await response.json()
    setting = document["settings"][0]
    assert response.status == 200
    assert document["revision"] == 0
    assert setting["key"] == "steering.always_on"
    assert setting["title"]["ko"] == "항시 조향"
    assert setting["value"] is False

  asyncio.run(_with_client(test_case))


def test_updates_setting_with_authoritative_offroad_state():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": True,
      "expectedRevision": 0,
    })
    document = await response.json()
    assert response.status == 200
    assert document["revision"] == 1
    assert document["settings"][0]["value"] is True
    assert document["settings"][0]["applyStatus"] == "pendingConsumer"

  asyncio.run(_with_client(test_case))


def test_rejects_update_while_onroad():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": True,
      "expectedRevision": 0,
    })
    assert response.status == 403
    assert (await response.json())["error"]["code"] == "update_forbidden"

  asyncio.run(_with_client(test_case, is_offroad=False))


def test_rejects_stale_revision():
  async def test_case(client):
    payload = {"key": "steering.always_on", "value": True, "expectedRevision": 0}
    assert (await client.post("/v1/kova-params/update", json=payload)).status == 200
    response = await client.post("/v1/kova-params/update", json=payload)
    assert response.status == 409
    assert (await response.json())["error"]["code"] == "revision_conflict"

  asyncio.run(_with_client(test_case))


def test_rejects_extra_fields_and_invalid_value_type():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": True,
      "expectedRevision": 0,
      "isOffroad": True,
    })
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "invalid_request"

    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": 1,
      "expectedRevision": 0,
    })
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "invalid_value"

  asyncio.run(_with_client(test_case))


def test_rejects_malformed_json():
  async def test_case(client):
    response = await client.post(
      "/v1/kova-params/update",
      data="{not-json",
      headers={"Content-Type": "application/json"},
    )
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "invalid_json"

  asyncio.run(_with_client(test_case))


def test_corrupt_settings_are_unavailable_without_details():
  async def test_case(client):
    response = await client.get("/v1/kova-params")
    assert response.status == 503
    assert await response.json() == {
      "error": {
        "code": "kova_params_unavailable",
        "message": "stored settings failed validation",
      },
    }

  backend = MemoryBackend()
  backend.value = {"schemaVersion": 999, "revision": 0, "values": {}}
  app = create_app(KovaParamsStore(backend), lambda: True)

  async def run_test():
    async with TestClient(TestServer(app)) as client:
      await test_case(client)

  asyncio.run(run_test())
