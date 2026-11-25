import asyncio
import ipaddress
from copy import deepcopy

from aiohttp import ClientSession, TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.cruise_sync_settings import read_cruise_sync_settings
from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, CURVE_SLOWDOWN_KEY, KOVA_PARAMS_KEY, KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway import lan
from openpilot.kovapilot.gateway.runtime import prepare_tls_identity


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


def test_same_subnet_works_without_a_token_and_control_settings_remain_read_only(tmp_path, monkeypatch):
  directory = prepare_tls_identity(tmp_path / "identity")
  certificate = (directory / "server.pem").read_bytes()
  assert prepare_tls_identity(directory) == directory
  assert (directory / "server.pem").read_bytes() == certificate
  assert (directory / "server.key").stat().st_mode & 0o077 == 0
  assert not (directory / "token.sha256").exists()
  access, context = lan.load_lan_config(directory, require_token=False)
  access.address = ipaddress.IPv4Interface("127.0.0.1/32")
  monkeypatch.setattr(lan, "PRIVATE_NETWORKS", (access.address.network,))

  async def check():
    parked = False
    store = KovaParamsStore(MemoryBackend())
    app = create_app(store, lambda: False, parked_provider=lambda: parked, lan_access=access)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
      await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
      base = f"https://127.0.0.1:{runner.addresses[0][1]}"
      async with ClientSession(base, connector=TCPConnector(ssl=False)) as client:
        async with client.get("/v1/health") as response:
          assert response.status == 200
          assert (await response.json())["authentication"] == "localNetwork"
        async with client.get("/v1/health", headers={"Origin": "https://example.test"}) as response:
          assert response.status == 403
        async with client.get("/v1/kova-params") as response:
          settings = {setting["key"]: setting for setting in (await response.json())["settings"]}
          assert settings[CRUISE_SYNC_KEY]["access"] == "readOnly"
          assert settings[CURVE_SLOWDOWN_KEY]["access"] == "readOnly"
          assert settings["display.onroad_brightness_fixed"]["access"] == "readWrite"

        payload = {"key": CRUISE_SYNC_KEY, "value": 1, "expectedRevision": 0}
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 403
        payload.update(key=CURVE_SLOWDOWN_KEY)
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 403
        assert store.values()[CURVE_SLOWDOWN_KEY] == 0
        payload.update(key="display.onroad_brightness_fixed", value=65)
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 200
          settings = {setting["key"]: setting for setting in (await response.json())["settings"]}
          assert settings[CRUISE_SYNC_KEY]["access"] == "readOnly"
          assert settings["display.onroad_brightness_fixed"]["value"] == 65
        assert store.values()[CRUISE_SYNC_KEY] == 0
        payload.update(key=CRUISE_SYNC_SHADOW_KEY, value=1, expectedRevision=1)
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 200
          assert (await response.json())["device"]["parked"] is False
        parked = True
        async with client.get("/v1/kova-params") as response:
          assert (await response.json())["device"] == {"offroad": False, "parked": True}
        assert store.values()[CRUISE_SYNC_SHADOW_KEY] == 1
        payload.update(key=CRUISE_SYNC_KEY, value=1, expectedRevision=2)
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 403
        async with client.post("/v1/git/apply", json={}) as response:
          assert response.status == 403

        access.address = ipaddress.IPv4Interface("127.0.0.2/32")
        async with client.get("/v1/health") as response:
          assert response.status == 403
    finally:
      await runner.cleanup()

  asyncio.run(check())


def test_curve_slowdown_preference_is_saved_and_remains_pending():
  async def check(client):
    for revision, value in enumerate((1, 0)):
      payload = {"key": CURVE_SLOWDOWN_KEY, "value": value, "expectedRevision": revision}
      async with client.post("/v1/kova-params/update", json=payload) as response:
        assert response.status == 200
        document = await response.json()
        assert document["revision"] == revision + 1
        setting = next(s for s in document["settings"] if s["key"] == CURVE_SLOWDOWN_KEY)
        assert setting["type"] == "enum"
        assert setting["defaultValue"] == 0
        assert type(setting["value"]) is int and setting["value"] == value
        assert setting["applyStatus"] == "pendingConsumer"
        assert "appliedValue" not in setting

  asyncio.run(_with_client(check, is_offroad=False))


def test_gateway_updates_cruise_config_seen_by_another_store():
  backend = MemoryBackend()
  runtime = KovaParamsStore(backend)
  gateway = KovaParamsStore(backend)
  effective = None

  async def check():
    nonlocal effective
    async with TestClient(TestServer(create_app(gateway, lambda: False, applied_cruise_provider=lambda: effective))) as client:
      result = await client.get("/v1/kova-params")
      assert result.status == 200
      settings = {setting["key"]: setting for setting in (await result.json())["settings"]}
      assert settings[CRUISE_SYNC_KEY]["value"] == 0
      assert settings[CRUISE_SYNC_KEY]["applyState"] == "awaitingStart"
      assert settings[CRUISE_SYNC_SHADOW_KEY]["value"] == 0

      payload = {"key": CRUISE_SYNC_KEY, "value": 1, "expectedRevision": 0}
      effective = 0
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 200
      settings = {setting["key"]: setting for setting in (await response.json())["settings"]}
      assert settings[CRUISE_SYNC_KEY]["applyState"] == "pendingRestart"
      assert type(settings[CRUISE_SYNC_KEY]["appliedValue"]) is int
      assert settings[CRUISE_SYNC_KEY]["appliedValue"] == 0
      assert read_cruise_sync_settings(runtime) == (True, False)

      payload = {"key": CRUISE_SYNC_SHADOW_KEY, "value": 1, "expectedRevision": 1}
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 200
      assert read_cruise_sync_settings(runtime) == (True, True)
      effective = 1
      result = await client.get("/v1/kova-params")
      settings = {setting["key"]: setting for setting in (await result.json())["settings"]}
      assert settings[CRUISE_SYNC_KEY]["applyState"] == "applied"

  asyncio.run(check())


def test_curve_slowdown_reports_planner_application_and_unsupported_vehicle():
  store = KovaParamsStore(MemoryBackend())
  applied = None

  async def check():
    nonlocal applied
    app = create_app(store, lambda: False, applied_curve_provider=lambda: applied)
    async with TestClient(TestServer(app)) as client:
      async def setting(response):
        assert response.status == 200
        return next(s for s in (await response.json())["settings"] if s["key"] == CURVE_SLOWDOWN_KEY)

      payload = {"key": CURVE_SLOWDOWN_KEY, "value": 1, "expectedRevision": 0}
      pending = await setting(await client.get("/v1/kova-params"))
      assert pending["applyState"] == "awaitingStart"
      applied = 0
      saved = await setting(await client.post("/v1/kova-params/update", json=payload))
      assert saved["applyPolicy"] == "parked"
      assert saved["applyState"] == "pendingParked"
      assert saved["value"] == 1 and saved["appliedValue"] == 0
      applied = 1
      active = await setting(await client.get("/v1/kova-params"))
      assert active["applyState"] == "applied"
      applied = "unsupported"
      unsupported = await setting(await client.get("/v1/kova-params"))
      assert unsupported["applyStatus"] == "unsupported"
      assert unsupported["access"] == "readOnly"
      assert "appliedValue" not in unsupported
      payload.update(value=0, expectedRevision=1)
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == 403
      assert store.values()[CURVE_SLOWDOWN_KEY] == 1

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
    assert setting["value"] == 0

  asyncio.run(_with_client(test_case))


def test_updates_setting_with_authoritative_offroad_state():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": 1,
      "expectedRevision": 0,
    })
    document = await response.json()
    assert response.status == 200
    assert document["revision"] == 1
    assert document["settings"][0]["value"] == 1
    assert document["settings"][0]["applyStatus"] == "pendingConsumer"

  asyncio.run(_with_client(test_case))


def test_saves_pending_consumer_while_onroad():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": 1,
      "expectedRevision": 0,
    })
    assert response.status == 200
    setting = next(s for s in (await response.json())["settings"] if s["key"] == "steering.always_on")
    assert setting["applyStatus"] == "pendingConsumer"
    assert setting["value"] == 1

  asyncio.run(_with_client(test_case, is_offroad=False))


def test_rejects_stale_revision():
  async def test_case(client):
    payload = {"key": "steering.always_on", "value": 1, "expectedRevision": 0}
    assert (await client.post("/v1/kova-params/update", json=payload)).status == 200
    response = await client.post("/v1/kova-params/update", json=payload)
    assert response.status == 409
    assert (await response.json())["error"]["code"] == "revision_conflict"

  asyncio.run(_with_client(test_case))


def test_rejects_extra_fields_and_invalid_value_type():
  async def test_case(client):
    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": 1,
      "expectedRevision": 0,
      "isOffroad": True,
    })
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "invalid_request"

    response = await client.post("/v1/kova-params/update", json={
      "key": "steering.always_on",
      "value": True,
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
