import pytest

from openpilot.common.params import Params, ParamKeyFlag
from openpilot.kovapilot.config.driver_monitoring import (DRIVER_MONITORING_CONFIG, SESSION_DISABLED_PARAM,
                                                          DriverMonitoringConfig, DriverMonitoringDisableReason,
                                                          disable_reason)
from openpilot.kovapilot.engagement.dm_session_disable import (DriverMonitoringSessionGesture, SessionDisableContext,
                                                               toggled_session_disabled)


class MemoryParams:
  def __init__(self):
    self.values: dict[str, bool] = {}

  def get_bool(self, key: str) -> bool:
    return self.values.get(key, False)

  def put_bool(self, key: str, value: bool) -> None:
    self.values[key] = value


def context(timestamp: float, reason: DriverMonitoringDisableReason, **overrides) -> SessionDisableContext:
  values = {"now": timestamp + 0.05, "event_time": timestamp, "can_valid": True, "selfdrive_valid": True,
            "selfdrive_enabled": False, "parked": True, "dm_lockout": False, "dm_alert_three": False,
            "disable_reason": reason}
  values.update(overrides)
  return SessionDisableContext(**values)


def press(gesture, timestamp, reason, **overrides) -> bool:
  triggered = gesture.update(context(timestamp, reason, **overrides), [("cancel", True)])
  gesture.update(context(timestamp + 0.01, reason, **overrides), [("cancel", False)])
  return triggered


def three_presses(gesture, start, params, **overrides) -> bool:
  """Mirror the dmonitoringd gesture handling for one three-press sequence."""
  reason = disable_reason(params)
  for i in range(3):
    if press(gesture, start + i, reason, **overrides):
      params.put_bool(SESSION_DISABLED_PARAM, toggled_session_disabled(reason))
      return True
  return False


def test_same_gesture_turns_monitoring_off_then_on():
  params = MemoryParams()
  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)

  assert three_presses(gesture, 10.0, params)
  assert disable_reason(params) == DriverMonitoringDisableReason.SESSION

  assert three_presses(gesture, 20.0, params)
  assert disable_reason(params) == DriverMonitoringDisableReason.NONE

  assert three_presses(gesture, 30.0, params)
  assert disable_reason(params) == DriverMonitoringDisableReason.SESSION


@pytest.mark.parametrize("overrides", [
  {"selfdrive_enabled": True},
  {"selfdrive_valid": False},
  {"parked": False},
  {"can_valid": False},
  {"dm_lockout": True},
  {"dm_alert_three": True},
])
def test_turning_monitoring_back_on_needs_the_same_parked_conditions(overrides):
  params = MemoryParams()
  params.put_bool(SESSION_DISABLED_PARAM, True)
  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)

  assert not three_presses(gesture, 10.0, params, **overrides)
  assert disable_reason(params) == DriverMonitoringDisableReason.SESSION


def test_gesture_disabled_by_config_cannot_turn_monitoring_back_on():
  params = MemoryParams()
  params.put_bool(SESSION_DISABLED_PARAM, True)
  gesture = DriverMonitoringSessionGesture(DriverMonitoringConfig(allow_session_disable_gesture=False))

  assert not three_presses(gesture, 10.0, params)
  assert disable_reason(params) == DriverMonitoringDisableReason.SESSION


def test_build_config_disable_is_never_toggled():
  config = DriverMonitoringConfig(enabled=False)
  gesture = DriverMonitoringSessionGesture(config)
  for t in (10.0, 11.0, 12.0):
    assert not press(gesture, t, DriverMonitoringDisableReason.BUILD_CONFIG)
  with pytest.raises(ValueError):
    toggled_session_disabled(DriverMonitoringDisableReason.BUILD_CONFIG)


@pytest.mark.parametrize(("reason", "expected"), [
  (DriverMonitoringDisableReason.NONE, True),
  (DriverMonitoringDisableReason.SESSION, False),
])
def test_toggled_value(reason, expected):
  assert toggled_session_disabled(reason) is expected


def test_disable_survives_manager_start_and_ignition_clears():
  params = Params()
  params.put_bool(SESSION_DISABLED_PARAM, True, block=True)
  try:
    params.clear_all(ParamKeyFlag.CLEAR_ON_MANAGER_START)
    params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)
    params.clear_all(ParamKeyFlag.CLEAR_ON_ONROAD_TRANSITION)
    params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)
    assert params.get_bool(SESSION_DISABLED_PARAM)
  finally:
    params.remove(SESSION_DISABLED_PARAM)
  assert not params.get_bool(SESSION_DISABLED_PARAM)
