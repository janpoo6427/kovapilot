import asyncio
import ipaddress
from copy import deepcopy

from aiohttp import ClientSession, TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.cruise_sync_settings import read_cruise_sync_settings
from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, CURVE_SLOWDOWN_KEY, KOVA_PARAMS_KEY, KovaParamsStore
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway import lan
from openpilot.kovapilot.gateway.git_maintenance import GitRepository
from openpilot.kovapilot.gateway.native_longitudinal import KEY as NATIVE_LONG_KEY, PARAM as NATIVE_LONG_PARAM
from openpilot.kovapilot.gateway.native_longitudinal import NativeLongitudinal, vehicle_dependency_conditions
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


def test_gateway_reports_saved_and_applied_dependency_states_without_rewriting_preferences():
  from opendbc.car.structs import car

  class VehicleParams:
    values = {}

    def get(self, key):
      return self.values.get(key)

    def get_bool(self, key):
      return bool(self.get(key))

  vehicle = VehicleParams()
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  app = create_app(store, lambda: True, dependency_conditions=lambda: vehicle_dependency_conditions(vehicle))

  async def check():
    async with TestClient(TestServer(app)) as client:
      async def setting(key):
        response = await client.get("/v1/kova-params")
        document = await response.json()
        return next(item for item in document["settings"] if item["key"] == key)

      async def update(key, value, revision):
        response = await client.post("/v1/kova-params/update", json={"key": key, "value": value, "expectedRevision": revision})
        assert response.status == 200
        return await response.json()

      def state(item, condition):
        return next(d["state"] for d in item["dependencies"] if d["condition"] == condition)

      assert state(await setting(CRUISE_SYNC_KEY), "openpilotLongitudinalApplied") == "unknown"
      assert (await setting("display.onroad_brightness_fixed"))["dependencies"][0]["state"] == "unmet"
      assert (await update("display.onroad_brightness_mode", 2, 0))["revision"] == 1
      assert (await setting("display.onroad_brightness_fixed"))["dependencies"][0]["state"] == "met"
      assert (await update(CRUISE_SYNC_KEY, 1, 1))["revision"] == 2

      cp = car.CarParams.new_message(alphaLongitudinalAvailable=True, pcmCruise=True)
      vehicle.values.update(CarParams=cp.to_bytes(), AlphaLongitudinalEnabled=True)
      cruise = await setting(CRUISE_SYNC_KEY)
      assert state(cruise, "openpilotLongitudinalApplied") == "pendingRestart"
      assert state(cruise, "openpilotCruiseSpeedOwner") == "pendingRestart"

      cp = car.CarParams.new_message(alphaLongitudinalAvailable=True, openpilotLongitudinalControl=True, pcmCruise=False)
      vehicle.values.update(CarParams=cp.to_bytes(), DisengageOnAccelerator=True)
      cruise = await setting(CRUISE_SYNC_KEY)
      assert state(cruise, "openpilotLongitudinalApplied") == "met"
      assert state(cruise, "openpilotCruiseSpeedOwner") == "met"
      assert state(cruise, "acceleratorDisengagementOff") == "unmet"
      assert state(await setting("navigation.speed_camera_profile"), "verifiedCameraInput") == "pendingConsumer"

      cp = car.CarParams.new_message(pcmCruise=True)
      vehicle.values["CarParams"] = cp.to_bytes()
      cruise = await setting(CRUISE_SYNC_KEY)
      assert state(cruise, "openpilotLongitudinalApplied") == "unsupported"
      assert state(await setting(CRUISE_SYNC_SHADOW_KEY), "stockCruiseSpeedOwner") == "met"
      assert cruise["value"] == 1
      assert all("state" not in entry for entry in next(s for s in store.document()["settings"] if s["key"] == CRUISE_SYNC_KEY)["dependencies"])
      assert (await update(CRUISE_SYNC_KEY, 0, 2))["revision"] == 3

  asyncio.run(check())


def test_same_subnet_can_save_control_preferences_without_a_token(tmp_path, monkeypatch):
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
    applied_curve = 0
    store = KovaParamsStore(MemoryBackend())
    jobs = []
    app = create_app(store, lambda: False, parked_provider=lambda: parked, applied_cruise_provider=lambda: 0,
                     applied_curve_provider=lambda: applied_curve, lan_access=access,
                     git=GitRepository(tmp_path, apply_allowed=lambda: True, enqueue=jobs.append))
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
        async def read_settings(response):
          assert response.status == 200
          doc = await response.json()
          return doc, {setting["key"]: setting for setting in doc["settings"]}

        _, settings = await read_settings(await client.get("/v1/kova-params"))
        for key in (CRUISE_SYNC_KEY, CURVE_SLOWDOWN_KEY, "steering.always_on", "navigation.speed_camera_profile",
                    "display.onroad_brightness_fixed"):
          assert settings[key]["access"] == "readWrite"

        payload = {"key": CRUISE_SYNC_KEY, "value": 1, "expectedRevision": 0}
        async with client.post("/v1/kova-params/update", json=payload, headers={"Origin": "https://example.test"}) as response:
          assert response.status == 403
        assert store.values()[CRUISE_SYNC_KEY] == 0

        async def save(key, value, revision):
          return await read_settings(await client.post("/v1/kova-params/update", json={
            "key": key, "value": value, "expectedRevision": revision,
          }))

        doc, settings = await save(CRUISE_SYNC_KEY, 1, 0)
        assert doc["revision"] == 1
        assert settings[CRUISE_SYNC_KEY]["applyState"] == "pendingRestart"
        assert settings[CRUISE_SYNC_KEY]["appliedValue"] == 0

        doc, settings = await save(CURVE_SLOWDOWN_KEY, 1, 1)
        assert doc["device"] == {"offroad": False, "parked": False}
        assert settings[CURVE_SLOWDOWN_KEY]["applyState"] == "pendingParked"
        assert settings[CURVE_SLOWDOWN_KEY]["appliedValue"] == 0

        _, settings = await save("steering.always_on", 1, 2)
        assert settings["steering.always_on"]["applyStatus"] == "pendingConsumer"
        assert "appliedValue" not in settings["steering.always_on"]
        _, settings = await save("display.onroad_brightness_fixed", 65, 3)
        assert settings["display.onroad_brightness_fixed"]["value"] == 65
        await save(CRUISE_SYNC_SHADOW_KEY, 1, 4)
        assert all(store.values()[key] == 1 for key in (CRUISE_SYNC_KEY, CURVE_SLOWDOWN_KEY, "steering.always_on", CRUISE_SYNC_SHADOW_KEY))

        parked = True
        applied_curve = 1
        doc, settings = await read_settings(await client.get("/v1/kova-params"))
        assert doc["device"] == {"offroad": False, "parked": True}
        assert settings[CURVE_SLOWDOWN_KEY]["applyState"] == "applied"

        applied_curve = "unsupported"
        _, settings = await read_settings(await client.get("/v1/kova-params"))
        assert settings[CURVE_SLOWDOWN_KEY]["access"] == "readOnly"
        payload = {"key": CURVE_SLOWDOWN_KEY, "value": 0, "expectedRevision": 5}
        async with client.post("/v1/kova-params/update", json=payload) as response:
          assert response.status == 403
        assert store.values()[CURVE_SLOWDOWN_KEY] == 1
        async with client.post("/v1/git/apply", json={}) as response:
          assert response.status == 403
        assert not jobs

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


def test_native_longitudinal_exposes_numeric_preference_only_when_supported_and_parked():
  supported, parked, applied = False, False, False
  values = {NATIVE_LONG_PARAM: False}

  class Params:
    def get_bool(self, key):
      return values[key]

    def put_bool(self, key, value, *, block):
      assert block
      values[key] = value

  native = NativeLongitudinal(Params(), lambda: (supported, applied), lambda: parked)

  async def run():
    nonlocal supported, parked, applied
    app = create_app(KovaParamsStore(MemoryBackend()), lambda: False, native_longitudinal=native)
    async with TestClient(TestServer(app)) as client:
      payload = {"key": NATIVE_LONG_KEY, "value": 1, "expectedRevision": 0, "expectedValue": 0}
      async with client.get("/v1/kova-params") as response:
        item = next(s for s in (await response.json())["settings"] if s["key"] == NATIVE_LONG_KEY)
      assert item["access"] == "readOnly" and item["availability"] == "unsupported"
      assert (await client.post("/v1/kova-params/update", json=payload)).status == 403
      supported = True
      assert (await client.post("/v1/kova-params/update", json=payload)).status == 403
      parked = True
      assert (await client.post("/v1/kova-params/update", json=dict(payload, value=True))).status == 400
      async with client.post("/v1/kova-params/update", json=payload) as response:
        assert response.status == 200
        item = next(s for s in (await response.json())["settings"] if s["key"] == NATIVE_LONG_KEY)
      assert type(item["value"]) is int and item["value"] == 1 and item["appliedValue"] == 0
      assert item["applyState"] == "pendingRestart"
      assert values[NATIVE_LONG_PARAM] is True
      assert (await client.post("/v1/kova-params/update", json=payload)).status == 409
      applied = True
      async with client.get("/v1/kova-params") as response:
        item = next(s for s in (await response.json())["settings"] if s["key"] == NATIVE_LONG_KEY)
      assert item["applyState"] == "applied"

  asyncio.run(run())


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
