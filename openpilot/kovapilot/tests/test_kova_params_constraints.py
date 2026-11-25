import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openpilot.kovapilot.config.kova_params import (
  KOVA_PARAMS_KEY,
  KovaParamsReadOnlyError,
  KovaParamsStore,
  KovaParamsValidationError,
  load_bundled_catalog,
  validate_catalog,
)
from openpilot.kovapilot.gateway.app import create_app


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "kova_params_e2e.json"


def _fixture_catalog():
  return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class MemoryBackend:
  def __init__(self):
    self.value = None
    self.put_calls = 0

  def get(self, key):
    assert key == KOVA_PARAMS_KEY
    return deepcopy(self.value)

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    assert block is True
    self.value = deepcopy(value)
    self.put_calls += 1


def _setting(catalog, key):
  return next(item for item in catalog["settings"] if item["key"] == key)


def test_fixture_and_bundled_catalogs_validate():
  validate_catalog(_fixture_catalog())
  validate_catalog(load_bundled_catalog())


@pytest.mark.parametrize(("key", "value"), [
  ("e2e.level", -1),
  ("e2e.level", 11),
  ("e2e.gain", 0.49),
  ("e2e.gain", 2.01),
])
def test_out_of_range_values_are_rejected_without_write(key, value):
  backend = MemoryBackend()
  store = KovaParamsStore(backend, _fixture_catalog())
  with pytest.raises(KovaParamsValidationError, match="out of range"):
    store.update(key, value, 0, is_offroad=True)
  assert backend.put_calls == 0


@pytest.mark.parametrize(("key", "value"), [
  ("e2e.level", 0),
  ("e2e.level", 10),
  ("e2e.gain", 0.5),
  ("e2e.gain", 2),
  ("e2e.mode", "lively"),
])
def test_in_range_values_are_accepted(key, value):
  store = KovaParamsStore(MemoryBackend(), _fixture_catalog())
  document = store.update(key, value, 0, is_offroad=True)
  assert _setting(document, key)["value"] == value
  assert document["revision"] == 1


def test_enum_requires_exact_option_type():
  catalog = _fixture_catalog()
  setting = _setting(catalog, "e2e.mode")
  setting.update({"options": [0, 1], "defaultValue": 0})
  setting.pop("optionTitles")
  store = KovaParamsStore(MemoryBackend(), catalog)
  for value in (False, True, 1.0, "1"):
    with pytest.raises(KovaParamsValidationError, match="expected enum"):
      store.update("e2e.mode", value, 0, is_offroad=True)
  assert _setting(store.update("e2e.mode", 1, 0, is_offroad=True), "e2e.mode")["value"] == 1


def test_enum_rejects_value_outside_options():
  store = KovaParamsStore(MemoryBackend(), _fixture_catalog())
  with pytest.raises(KovaParamsValidationError, match="expected enum"):
    store.update("e2e.mode", "turbo", 0, is_offroad=True)


def test_read_only_fixture_setting_is_rejected():
  with pytest.raises(KovaParamsReadOnlyError):
    KovaParamsStore(MemoryBackend(), _fixture_catalog()).update("e2e.build_label", "x", 0, is_offroad=True)


@pytest.mark.parametrize("mutate", [
  lambda s: s.update({"minimum": 5, "maximum": 1}),
  lambda s: s.update({"minimum": 0.5}),
  lambda s: s.update({"step": 0}),
  lambda s: s.update({"maximum": True}),
  lambda s: s.update({"defaultValue": 42}),
  lambda s: s.update({"unit": "steps"}),
])
def test_invalid_integer_constraints_fail_catalog_validation(mutate):
  catalog = _fixture_catalog()
  mutate(_setting(catalog, "e2e.level"))
  with pytest.raises(KovaParamsValidationError):
    validate_catalog(catalog)


@pytest.mark.parametrize("mutate", [
  lambda s: s.update({"options": []}),
  lambda s: s.update({"options": ["calm", "calm", "normal"]}),
  lambda s: s.update({"options": [True, "normal"]}),
  lambda s: s["optionTitles"].pop("calm"),
])
def test_invalid_enum_constraints_fail_catalog_validation(mutate):
  catalog = _fixture_catalog()
  mutate(_setting(catalog, "e2e.mode"))
  with pytest.raises(KovaParamsValidationError):
    validate_catalog(catalog)


def test_constraints_on_wrong_type_fail_catalog_validation():
  catalog = _fixture_catalog()
  _setting(catalog, "steering.always_on")["maximum"] = 1
  with pytest.raises(KovaParamsValidationError, match="only valid for integer or number"):
    validate_catalog(catalog)


async def _with_client(test_case, *, offroad):
  state = {"offroad": offroad}
  app = create_app(KovaParamsStore(MemoryBackend(), _fixture_catalog()), lambda: state["offroad"])
  async with TestClient(TestServer(app)) as client:
    await test_case(client, state)


def test_read_reports_device_offroad_state():
  async def test_case(client, state):
    response = await client.get("/v1/kova-params")
    assert response.status == 200
    assert (await response.json())["device"] == {"offroad": True, "parked": False}
    state["offroad"] = False
    assert (await (await client.get("/v1/kova-params")).json())["device"] == {"offroad": False, "parked": False}

  asyncio.run(_with_client(test_case, offroad=True))


def test_onroad_allows_immediate_but_rejects_offroad_setting():
  async def test_case(client, state):
    response = await client.post("/v1/kova-params/update", json={"key": "steering.always_on", "value": True, "expectedRevision": 0})
    assert response.status == 403
    assert (await response.json())["error"]["code"] == "update_forbidden"

    response = await client.post("/v1/kova-params/update", json={"key": "e2e.level", "value": 4, "expectedRevision": 0})
    body = await response.json()
    assert response.status == 200
    assert body["revision"] == 1
    assert body["device"] == {"offroad": False, "parked": False}
    assert _setting(body, "e2e.level")["value"] == 4

  asyncio.run(_with_client(test_case, offroad=False))


def test_range_unknown_key_and_read_only_errors_over_http():
  async def test_case(client, state):
    cases = [
      ({"key": "e2e.level", "value": 99, "expectedRevision": 0}, 400, "invalid_value"),
      ({"key": "e2e.gain", "value": "fast", "expectedRevision": 0}, 400, "invalid_value"),
      ({"key": "e2e.missing", "value": 1, "expectedRevision": 0}, 400, "invalid_value"),
      ({"key": "e2e.build_label", "value": "x", "expectedRevision": 0}, 403, "update_forbidden"),
    ]
    for payload, status, code in cases:
      response = await client.post("/v1/kova-params/update", json=payload)
      assert response.status == status, payload
      assert (await response.json())["error"]["code"] == code
    assert (await (await client.get("/v1/kova-params")).json())["revision"] == 0

  asyncio.run(_with_client(test_case, offroad=True))
