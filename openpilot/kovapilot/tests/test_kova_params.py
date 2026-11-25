from copy import deepcopy

import pytest

from openpilot.kovapilot.config.kova_params import (
  CRUISE_SYNC_KEY,
  CRUISE_SYNC_SHADOW_KEY,
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
  assert setting["defaultValue"] is False
  assert setting["risk"] == "control"
  assert setting["applyPolicy"] == "offroad"
  assert setting["availability"] == "pendingConsumer"


def test_document_merges_defaults_without_writing_storage():
  backend = MemoryBackend()
  document = KovaParamsStore(backend).document()

  assert document["revision"] == 0
  assert document["settings"][0]["value"] is False
  assert document["settings"][0]["applyStatus"] == "pendingConsumer"
  assert backend.put_calls == []


def test_offroad_update_is_revisioned_and_persisted_synchronously():
  backend = MemoryBackend()
  document = KovaParamsStore(backend).update("steering.always_on", True, 0, is_offroad=True)

  assert document["revision"] == 1
  assert document["settings"][0]["value"] is True
  assert document["settings"][0]["applyStatus"] == "pendingConsumer"
  assert backend.value == {
    "schemaVersion": 1,
    "revision": 1,
    "values": {
      "steering.always_on": True, "display.onroad_brightness_mode": "auto", "display.onroad_brightness_fixed": 50,
      "navigation.speed_camera_profile": 0,
      CRUISE_SYNC_KEY: False, CRUISE_SYNC_SHADOW_KEY: False,
    },
  }
  assert backend.put_calls[0][2] is True


def test_onroad_update_is_rejected_without_write():
  backend = MemoryBackend()

  with pytest.raises(KovaParamsApplyPolicyError, match="offroad"):
    KovaParamsStore(backend).update("steering.always_on", True, 0, is_offroad=False)

  assert backend.put_calls == []


def test_stale_revision_is_rejected_without_second_write():
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  store.update("steering.always_on", True, 0, is_offroad=True)

  with pytest.raises(KovaParamsRevisionConflict, match="current 1"):
    store.update("steering.always_on", False, 0, is_offroad=True)

  assert len(backend.put_calls) == 1


@pytest.mark.parametrize("value", [1, "true", None, {}])
def test_wrong_value_type_is_rejected(value):
  with pytest.raises(KovaParamsValidationError, match="expected boolean"):
    KovaParamsStore(MemoryBackend()).update("steering.always_on", value, 0, is_offroad=True)


def test_unknown_setting_is_rejected():
  with pytest.raises(KovaParamsValidationError, match="unknown setting"):
    KovaParamsStore(MemoryBackend()).update("steering.unknown", True, 0, is_offroad=True)


def test_corrupt_stored_state_fails_closed_without_overwrite():
  backend = MemoryBackend({"schemaVersion": 1, "revision": 0, "values": {"removed.setting": True}})

  with pytest.raises(KovaParamsValidationError, match="invalid revision or value set"):
    KovaParamsStore(backend).document()

  assert backend.put_calls == []


def test_new_catalog_setting_uses_default_without_eager_storage_write():
  backend = MemoryBackend({"schemaVersion": 1, "revision": 4, "values": {}})

  document = KovaParamsStore(backend).document()

  assert document["revision"] == 4
  assert document["settings"][0]["value"] is False
  assert backend.put_calls == []
