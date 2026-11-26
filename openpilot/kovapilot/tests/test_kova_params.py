from copy import deepcopy

import pytest

from openpilot.kovapilot.config import cruise_sync_settings
from openpilot.kovapilot.config.kova_params import (
  AUTO_ENGAGE_KEY,
  BRIGHTNESS_MODE_KEY,
  CRUISE_SYNC_KEY,
  CRUISE_SYNC_SHADOW_KEY,
  CURVE_SLOWDOWN_KEY,
  LEAD_RESTART_KEY,
  KOVA_PARAMS_KEY,
  KovaParamsApplyPolicyError,
  KovaParamsRevisionConflict,
  KovaParamsStore,
  KovaParamsValidationError,
  load_bundled_catalog,
)


class MemoryBackend:
  def __init__(self, value=None):
    self.value = deepcopy(value)
    self.put_calls = []

  def get(self, key):
    assert key == KOVA_PARAMS_KEY
    return deepcopy(self.value)

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    self.value = deepcopy(value)
    self.put_calls.append((key, deepcopy(value), block))


def test_bundled_catalog_defines_safe_always_steering_sample():
  catalog = load_bundled_catalog()
  setting = next(item for item in catalog["settings"] if item["key"] == "steering.always_on")

  assert catalog["defaultLocale"] == "ko"
  assert set(catalog["locales"]) == {"ko", "en"}
  assert setting["title"] == {"ko": "항시 조향", "en": "Always-on steering"}
  assert setting["defaultValue"] == 0
  assert setting["risk"] == "control"
  assert setting["applyPolicy"] == "offroad"
  assert setting["availability"] == "pendingConsumer"


def test_document_merges_defaults_without_writing_storage():
  backend = MemoryBackend()
  document = KovaParamsStore(backend).document()

  assert document["revision"] == 0
  assert document["settings"][0]["value"] == 0
  assert document["settings"][0]["applyStatus"] == "pendingConsumer"
  assert backend.put_calls == []


def test_storage_initialization_saves_legacy_values_as_numbers_once():
  legacy = {
    "steering.always_on": True,
    CRUISE_SYNC_KEY: False,
    CRUISE_SYNC_SHADOW_KEY: True,
    CURVE_SLOWDOWN_KEY: True,
    BRIGHTNESS_MODE_KEY: "auto_dark",
    "navigation.speed_camera_profile": 2,
    "display.onroad_brightness_fixed": 75,
  }
  backend = MemoryBackend({"schemaVersion": 1, "revision": 4, "values": legacy})
  store = KovaParamsStore(backend)

  with pytest.raises(KovaParamsValidationError):
    store.values()
  assert backend.put_calls == []

  store.initialize_storage()
  values = {
    "steering.always_on": 1, CRUISE_SYNC_KEY: 0, CRUISE_SYNC_SHADOW_KEY: 1, CURVE_SLOWDOWN_KEY: 1, LEAD_RESTART_KEY: 0,
    AUTO_ENGAGE_KEY: 0,
    BRIGHTNESS_MODE_KEY: 1, "navigation.speed_camera_profile": 2, "display.onroad_brightness_fixed": 75,
  }
  assert backend.value == {"schemaVersion": 1, "revision": 4, "values": values}
  assert all(type(value) is int for value in backend.value["values"].values())
  assert store.values() == values
  store.initialize_storage()
  assert len(backend.put_calls) == 1
  assert backend.put_calls[0][2] is True

  saved = store.update("display.onroad_brightness_fixed", 80, 4, is_offroad=False)
  assert saved["revision"] == 5
  assert all(type(setting["defaultValue"]) is int and type(setting["value"]) is int for setting in saved["settings"])
  assert backend.value["values"] == values | {"display.onroad_brightness_fixed": 80}
  assert all(type(value) is int for value in backend.value["values"].values())


def test_storage_initialization_saves_numeric_defaults_for_a_fresh_install():
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  store.initialize_storage()
  assert backend.value == {"schemaVersion": 1, "revision": 0, "values": store.values()}
  assert all(type(value) is int for value in backend.value["values"].values())


def test_applied_cruise_report_matches_only_the_running_card(tmp_path, monkeypatch):
  monkeypatch.setattr(cruise_sync_settings, "_applied_path", lambda: tmp_path / "cruise.json")
  monkeypatch.setattr(cruise_sync_settings.os, "getpid", lambda: 1234)
  assert cruise_sync_settings.applied_cruise_sync(1234) is None
  cruise_sync_settings.publish_applied_cruise_sync(False)
  assert cruise_sync_settings.applied_cruise_sync(None) is None
  assert cruise_sync_settings.applied_cruise_sync(1235) is None
  value = cruise_sync_settings.applied_cruise_sync(1234)
  assert type(value) is int and value == 0
  monkeypatch.setattr(cruise_sync_settings.os, "getpid", lambda: 1235)
  cruise_sync_settings.publish_applied_cruise_sync(True)
  assert cruise_sync_settings.applied_cruise_sync(1234) is None
  value = cruise_sync_settings.applied_cruise_sync(1235)
  assert type(value) is int and value == 1


def test_offroad_update_is_revisioned_and_persisted_synchronously():
  backend = MemoryBackend()
  document = KovaParamsStore(backend).update("steering.always_on", 1, 0, is_offroad=True)

  assert document["revision"] == 1
  assert document["settings"][0]["value"] == 1
  assert document["settings"][0]["applyStatus"] == "pendingConsumer"
  assert backend.value == {
    "schemaVersion": 1,
    "revision": 1,
    "values": {
      "steering.always_on": 1, "display.onroad_brightness_mode": 0, "display.onroad_brightness_fixed": 50,
      "navigation.speed_camera_profile": 0,
      CRUISE_SYNC_KEY: 0, CRUISE_SYNC_SHADOW_KEY: 0,
      CURVE_SLOWDOWN_KEY: 0,
      LEAD_RESTART_KEY: 0,
      AUTO_ENGAGE_KEY: 0,
    },
  }
  assert backend.put_calls[0][2] is True


def test_onroad_update_is_rejected_without_write():
  backend = MemoryBackend()
  catalog = load_bundled_catalog()
  next(s for s in catalog["settings"] if s["key"] == "steering.always_on").pop("savePolicy")

  with pytest.raises(KovaParamsApplyPolicyError, match="offroad"):
    KovaParamsStore(backend, catalog).update("steering.always_on", 1, 0, is_offroad=False)

  assert backend.put_calls == []


def test_stale_revision_is_rejected_without_second_write():
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  store.update("steering.always_on", 1, 0, is_offroad=True)

  with pytest.raises(KovaParamsRevisionConflict, match="current 1"):
    store.update("steering.always_on", 0, 0, is_offroad=True)

  assert len(backend.put_calls) == 1


@pytest.mark.parametrize("value", [True, 2, 1.0, "true", None, {}])
def test_wrong_value_type_is_rejected(value):
  with pytest.raises(KovaParamsValidationError, match="expected enum"):
    KovaParamsStore(MemoryBackend()).update("steering.always_on", value, 0, is_offroad=True)


def test_unknown_setting_is_rejected():
  with pytest.raises(KovaParamsValidationError, match="unknown setting"):
    KovaParamsStore(MemoryBackend()).update("steering.unknown", True, 0, is_offroad=True)


def test_corrupt_stored_state_fails_closed_without_overwrite():
  backend = MemoryBackend({"schemaVersion": 1, "revision": 0, "values": {"removed.setting": True}})

  with pytest.raises(KovaParamsValidationError, match="invalid revision or value set"):
    KovaParamsStore(backend).document()

  with pytest.raises(KovaParamsValidationError, match="invalid revision or value set"):
    KovaParamsStore(backend).initialize_storage()

  assert backend.put_calls == []


def test_new_catalog_setting_uses_default_without_eager_storage_write():
  backend = MemoryBackend({"schemaVersion": 1, "revision": 4, "values": {}})

  document = KovaParamsStore(backend).document()

  assert document["revision"] == 4
  assert document["settings"][0]["value"] == 0
  assert backend.put_calls == []
