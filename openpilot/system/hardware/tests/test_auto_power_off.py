import json
from pathlib import Path

import pytest

from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.kovapilot.config.kova_params import (CATALOG_PATH, KOVA_PARAMS_KEY, KovaParamsStore, KovaParamsValidationError,
                                                    ParamsKovaParamsBackend)
from openpilot.kovapilot.config.power_off_settings import (AUTO_POWER_OFF_DEFAULT_MIN, AUTO_POWER_OFF_KEY, AUTO_POWER_OFF_OPTIONS,
                                                           AUTO_POWER_OFF_READ_INTERVAL_S)
import openpilot.system.hardware.power_monitoring as power_monitoring
from openpilot.system.hardware.power_monitoring import (CAR_BATTERY_CAPACITY_uWh, DELAY_SHUTDOWN_TIME_S, MAX_TIME_OFFROAD_S,
                                                        VBATT_PAUSE_CHARGING, PowerMonitoring)

ssb = 0.


def mock_time_monotonic():
  global ssb
  ssb += 1.
  return ssb


def set_mock_time(value):
  global ssb
  ssb = value


LOW_VOLTAGE = (VBATT_PAUSE_CHARGING - 1) * 1e3
ALL_MINUTES = [0] + list(range(30, 24 * 60 + 1, 30))


def _catalog_setting():
  with CATALOG_PATH.open(encoding="utf-8") as f:
    catalog = json.load(f)
  return catalog, next(s for s in catalog["settings"] if s["key"] == AUTO_POWER_OFF_KEY)


def test_catalog_lists_always_on_and_every_30_minutes_up_to_24_hours():
  catalog, setting = _catalog_setting()
  assert list(AUTO_POWER_OFF_OPTIONS) == ALL_MINUTES
  assert len(ALL_MINUTES) == 49
  assert setting["options"] == ALL_MINUTES
  assert setting["type"] == "enum" and "control" not in setting
  assert setting["access"] == "readWrite" and setting["availability"] == "available"
  assert setting["applyPolicy"] == "immediate" and setting["savePolicy"] == "anytime"
  assert setting["groupId"] == "power" and any(g["id"] == "power" for g in catalog["groups"])
  titles = setting["optionTitles"]
  assert set(titles) == {str(m) for m in ALL_MINUTES}
  assert titles["0"]["ko"] == "항상 켜둠"
  assert titles["30"]["ko"] == "30분"
  assert titles["60"]["ko"] == "1시간"
  assert titles["90"]["ko"] == "1시간 30분"
  assert titles["1440"]["ko"] == "24시간"
  assert setting["title"]["ko"] == "자동 전원 끄기"


def test_default_is_24_hours_everywhere():
  _, setting = _catalog_setting()
  assert setting["defaultValue"] == AUTO_POWER_OFF_DEFAULT_MIN == 24 * 60
  assert MAX_TIME_OFFROAD_S == AUTO_POWER_OFF_DEFAULT_MIN * 60


class Env:
  def __init__(self, params):
    self.params = params
    self.store = KovaParamsStore(ParamsKovaParamsBackend(params))

  def save(self, minutes):
    revision = self.store.document()["revision"]
    self.store.update(AUTO_POWER_OFF_KEY, minutes, revision, is_offroad=False, is_parked=False)

  def put_raw(self, value):
    self.params.put(KOVA_PARAMS_KEY, {"schemaVersion": 1, "revision": 1, "values": {AUTO_POWER_OFF_KEY: value}}, block=True)


def _pm():
  pm = PowerMonitoring()
  pm.car_battery_capacity_uWh = CAR_BATTERY_CAPACITY_uWh
  return pm


def _first_shutdown(pm, start, limit_s, ignition=False):
  """should_shutdown just below and at limit_s offroad seconds."""
  set_mock_time(start + limit_s - 1)  # monotonic() returns start + limit_s
  before = pm.should_shutdown(ignition, True, start, True)
  set_mock_time(start + limit_s)      # monotonic() returns start + limit_s + 1
  after = pm.should_shutdown(ignition, True, start, True)
  return before, after


@pytest.fixture
def env(monkeypatch):
  monkeypatch.setattr("time.monotonic", mock_time_monotonic)
  monkeypatch.setattr(power_monitoring.HARDWARE, "get_current_power_draw", lambda: 0)
  with OpenpilotPrefix():
    yield Env(Params())


@pytest.mark.parametrize("minutes", ALL_MINUTES[1:])
def test_each_option_powers_off_after_its_time(env, minutes):
  env.save(minutes)
  assert _first_shutdown(_pm(), ssb, minutes * 60) == (False, True)


def test_nothing_saved_uses_24_hours(env):
  assert _first_shutdown(_pm(), ssb, 24 * 3600) == (False, True)


def test_saved_default_from_storage_initialization_is_24_hours(env):
  env.store.initialize_storage()
  assert env.params.get(KOVA_PARAMS_KEY)["values"][AUTO_POWER_OFF_KEY] == 24 * 60
  assert _first_shutdown(_pm(), ssb, 24 * 3600) == (False, True)


def test_always_on_does_not_power_off_by_time(env):
  env.save(0)
  pm = _pm()
  start = ssb
  for hours in (1, 24, 30, 24 * 30):
    set_mock_time(start + hours * 3600)
    assert not pm.should_shutdown(False, True, start, True)


def test_always_on_still_powers_off_on_low_car_voltage(env):
  env.save(0)
  pm = _pm()
  start = ssb
  for _ in range(400):
    pm.calculate(LOW_VOLTAGE, False)
  assert pm.car_voltage_mV < VBATT_PAUSE_CHARGING * 1e3
  assert pm.should_shutdown(False, True, start, True)


def test_always_on_still_powers_off_on_empty_battery_estimate(env):
  env.save(0)
  pm = _pm()
  pm.car_battery_capacity_uWh = 0
  assert _first_shutdown(pm, ssb, DELAY_SHUTDOWN_TIME_S) == (False, True)


@pytest.mark.parametrize("minutes", [0, 30, 1440])
def test_never_powers_off_while_ignition_is_on(env, minutes):
  env.save(minutes)
  pm = _pm()
  pm.car_battery_capacity_uWh = 0
  start = ssb
  for offroad_s in (DELAY_SHUTDOWN_TIME_S + 1, 30 * 60 + 1, 48 * 3600):
    set_mock_time(start + offroad_s)
    assert not pm.should_shutdown(True, True, start, True)


@pytest.mark.parametrize("minutes", [0, 30])
def test_stays_on_at_least_5_minutes_after_switch_off(env, minutes):
  env.save(minutes)
  pm = _pm()
  pm.car_battery_capacity_uWh = 0
  assert _first_shutdown(pm, ssb, DELAY_SHUTDOWN_TIME_S) == (False, True)


def test_shortest_option_is_not_shorter_than_the_5_minute_floor():
  assert min(m for m in ALL_MINUTES if m) * 60 > DELAY_SHUTDOWN_TIME_S


@pytest.mark.parametrize("value", [45, 1441, 1470, -30, 3000, "60", 60.0, True, None, [60], {"m": 60}])
def test_invalid_saved_value_uses_24_hours(env, value):
  env.put_raw(value)
  assert _first_shutdown(_pm(), ssb, 24 * 3600) == (False, True)


@pytest.mark.parametrize("state", [[1, 2], {"values": "x"}, {"values": None}, {"values": {}}])
def test_unreadable_settings_use_24_hours(env, state):
  env.params.put(KOVA_PARAMS_KEY, state, block=True)
  assert _first_shutdown(_pm(), ssb, 24 * 3600) == (False, True)


def test_corrupt_settings_file_uses_24_hours(env):
  env.save(30)
  Path(env.params.get_param_path(KOVA_PARAMS_KEY)).write_bytes(b"{not json")
  assert _first_shutdown(_pm(), ssb, 24 * 3600) == (False, True)


def test_read_failure_uses_24_hours(env, monkeypatch):
  env.save(30)
  pm = _pm()
  def broken_get(*args, **kwargs):
    raise OSError("read failed")
  monkeypatch.setattr(pm.params, "get", broken_get)
  assert _first_shutdown(pm, ssb, 24 * 3600) == (False, True)


def test_disable_power_down_still_wins_over_short_time(env):
  env.save(30)
  env.params.put_bool("DisablePowerDown", True, block=True)
  pm = _pm()
  start = ssb
  set_mock_time(start + 48 * 3600)
  assert not pm.should_shutdown(False, True, start, True)


def test_changed_value_is_used_within_the_read_interval(env):
  env.save(24 * 60)
  pm = _pm()
  start = ssb
  set_mock_time(start + 3600)
  assert not pm.should_shutdown(False, True, start, True)
  env.save(30)
  set_mock_time(start + 3600 + AUTO_POWER_OFF_READ_INTERVAL_S)
  assert pm.should_shutdown(False, True, start, True)


def test_settings_are_not_read_on_every_check(env, monkeypatch):
  calls = []
  real = power_monitoring.auto_power_off_limit_s
  def counting(*args, **kwargs):
    calls.append(args)
    return real(*args, **kwargs)
  monkeypatch.setattr(power_monitoring, "auto_power_off_limit_s", counting)
  pm = _pm()
  start = ssb
  for _ in range(int(AUTO_POWER_OFF_READ_INTERVAL_S) // 2 - 1):
    pm.should_shutdown(False, True, start, True)  # each call advances the fake clock by 1 s
  assert len(calls) == 1


class MemoryBackend:
  def __init__(self):
    self.value = None

  def get(self, key):
    assert key == KOVA_PARAMS_KEY
    return self.value

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    self.value = json.loads(json.dumps(value))


@pytest.mark.parametrize("value", [45, 1441, 1470, -30, "60", 60.0, True, None])
def test_store_rejects_values_outside_the_list(value):
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  with pytest.raises(KovaParamsValidationError):
    store.update(AUTO_POWER_OFF_KEY, value, 0, is_offroad=False)
  assert backend.get(KOVA_PARAMS_KEY) is None

@pytest.mark.parametrize("value", ALL_MINUTES)
def test_store_accepts_every_listed_value_while_driving(value):
  store = KovaParamsStore(MemoryBackend())
  document = store.update(AUTO_POWER_OFF_KEY, value, 0, is_offroad=False, is_parked=False)
  assert next(s for s in document["settings"] if s["key"] == AUTO_POWER_OFF_KEY)["value"] == value
