from pathlib import Path
from types import SimpleNamespace

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.kovapilot.config.kova_params import (
  CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, KOVA_PARAMS_KEY, KovaParamsStore, KovaParamsValidationError, ParamsKovaParamsBackend,
)
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import MIN_PRESS_SAMPLES
from openpilot.selfdrive.car import card
from openpilot.selfdrive.car.cruise import VCruiseHelper


@pytest.mark.parametrize('blocker', [None, 'invalid_controls', 'user_disable', 'can_timeout'])
def test_card_publishes_synced_setpoints_only_on_valid_release(monkeypatch, blocker):
  instance = object.__new__(card.Car)
  instance.CP = car.CarParams(openpilotLongitudinalControl=True)
  instance.v_cruise_helper = VCruiseHelper(instance.CP)
  instance.v_cruise_helper.initialize_v_cruise(car.CarState(), 80)
  instance.cruise_accelerator_sync = CruiseAcceleratorSyncBridge(instance.v_cruise_helper)
  instance.cruise_sync_settings = (True, False, False)
  instance.is_metric = True
  instance.can_rcv_cum_timeout_counter = 0
  instance.initialized_prev = True
  instance.can_sock = None
  instance.CC_prev = car.CarControl(enabled=True)

  class SubMaster(dict):
    checks_ok = True

    def update(self, timeout):
      pass

    def all_checks(self, services):
      return self.checks_ok

  instance.sm = SubMaster(carControl=instance.CC_prev, onroadEvents=[])
  instance.RI = SimpleNamespace(update=lambda _: None)
  monkeypatch.setattr(card, 'can_capnp_to_list', lambda _: [])
  monkeypatch.setattr(card, 'REPLAY', False)

  def step(speed_kph, pressed, *, long_active=False, can_received=True):
    instance.sm['carControl'].longActive = long_active
    state = car.CarState(canValid=True, gasPressed=pressed, vEgo=speed_kph * CV.KPH_TO_MS,
                         cruiseState={'available': True})
    instance.CI = SimpleNamespace(update=lambda _: state)
    monkeypatch.setattr(card.messaging, 'drain_sock_raw', lambda *args, **kwargs: [b'CAN'] if can_received else [])
    return instance.state_update()[0]

  assert step(80.0, False, long_active=True).vCruise == 80.0
  for _ in range(MIN_PRESS_SAMPLES):
    assert step(82.0, True).vCruise == 80.0

  if blocker == 'invalid_controls':
    instance.sm.checks_ok = False
  elif blocker == 'user_disable':
    instance.sm['onroadEvents'] = [log.OnroadEvent(userDisable=True)]
  state = step(93.0, False, can_received=blocker != 'can_timeout')
  expected = 93.0 if blocker is None else 80.0
  assert state.vCruise == state.vCruiseCluster == expected

  instance.sm.checks_ok = True
  instance.sm['onroadEvents'] = []
  assert step(95.0, False, long_active=True).vCruise == expected


def test_cruise_config_preserves_legacy_then_uses_shared_settings(tmp_path):
  params = Params(tmp_path.as_posix())
  instance = object.__new__(card.Car)
  instance.params = params
  instance.kova_params = KovaParamsStore(ParamsKovaParamsBackend(params))
  gateway_store = KovaParamsStore(ParamsKovaParamsBackend(Params(tmp_path.as_posix())))

  instance._read_cruise_sync_settings(starting=True)
  assert instance.cruise_sync_settings == (False, False, False)

  params.put(KOVA_PARAMS_KEY, {
    "schemaVersion": 1,
    "revision": 3,
    "values": {"display.onroad_brightness_fixed": 75, "navigation.speed_camera_profile": 2},
  }, block=True)
  params.put_bool("CruiseAcceleratorSync", True, block=True)
  params.put_bool("CruiseAcceleratorSyncStockShadow", True, block=True)
  gateway_store.initialize_storage()
  instance._read_cruise_sync_settings(starting=True)
  assert instance.cruise_sync_settings == (True, True, False)

  gateway_store.update(CRUISE_SYNC_KEY, 0, 3, is_offroad=False)
  instance._read_cruise_sync_settings()
  assert instance.cruise_sync_settings == (True, True, False)
  instance._read_cruise_sync_settings(starting=True)
  assert instance.cruise_sync_settings == (False, True, False)
  state = params.get(KOVA_PARAMS_KEY)
  assert state["revision"] == 4
  assert all(type(value) is int for value in state["values"].values())
  assert state["values"][CRUISE_SYNC_KEY] == 0
  assert state["values"][CRUISE_SYNC_SHADOW_KEY] == 1
  assert state["values"]["display.onroad_brightness_fixed"] == 75
  assert state["values"]["navigation.speed_camera_profile"] == 2
  assert params.get_bool("CruiseAcceleratorSync") is True

  gateway_store.update(CRUISE_SYNC_SHADOW_KEY, 0, 4, is_offroad=False)
  instance._read_cruise_sync_settings()
  assert instance.cruise_sync_settings == (False, False, False)


def test_storage_initialization_preserves_an_invalid_native_params_file(tmp_path):
  params = Params(tmp_path.as_posix())
  path = Path(params.get_param_path(KOVA_PARAMS_KEY))
  path.write_text("null", encoding="utf-8")

  with pytest.raises(KovaParamsValidationError, match="could not be decoded"):
    KovaParamsStore(ParamsKovaParamsBackend(params)).initialize_storage()

  assert path.read_text(encoding="utf-8") == "null"


def test_card_publishes_the_set_speed_while_the_pedal_is_held(monkeypatch):
  instance = object.__new__(card.Car)
  instance.CP = car.CarParams(openpilotLongitudinalControl=True)
  instance.v_cruise_helper = VCruiseHelper(instance.CP)
  instance.v_cruise_helper.initialize_v_cruise(car.CarState(), 60)
  instance.cruise_accelerator_sync = CruiseAcceleratorSyncBridge(instance.v_cruise_helper)
  instance.cruise_sync_settings = (True, False, False)
  instance.is_metric = True
  instance.can_rcv_cum_timeout_counter = 0
  instance.initialized_prev = True
  instance.can_sock = None
  instance.CC_prev = car.CarControl(enabled=True)

  class SubMaster(dict):
    def update(self, timeout):
      pass

    def all_checks(self, services):
      return True

  instance.sm = SubMaster(carControl=instance.CC_prev, onroadEvents=[])
  instance.RI = SimpleNamespace(update=lambda _: None)
  monkeypatch.setattr(card, "can_capnp_to_list", lambda _: [])
  monkeypatch.setattr(card, "REPLAY", False)
  monkeypatch.setattr(card.messaging, "drain_sock_raw", lambda *args, **kwargs: [b"CAN"])

  def step(speed_kph, pressed, *, long_active=False):
    instance.sm["carControl"].longActive = long_active
    state = car.CarState(canValid=True, gasPressed=pressed, vEgo=speed_kph * CV.KPH_TO_MS, cruiseState={"available": True})
    instance.CI = SimpleNamespace(update=lambda _: state)
    return instance.state_update()[0]

  assert step(60.0, False, long_active=True).vCruise == 60.0
  for _ in range(MIN_PRESS_SAMPLES):
    assert step(60.0, True).vCruise == 60.0
  published = [step(speed, True) for speed in (65.0, 71.0, 70.0)]
  assert [(s.vCruise, s.vCruiseCluster) for s in published] == [(65.0, 65.0), (71.0, 71.0), (71.0, 71.0)]
  released = step(69.0, False)
  assert released.vCruise == released.vCruiseCluster == 71.0
