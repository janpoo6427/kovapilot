from pathlib import Path

from opendbc.car.structs import car

from openpilot.cereal import log
from openpilot.kovapilot.bridges.driver_monitoring import disabled_state_packet, monitoring_is_disabled
from openpilot.kovapilot.config.driver_monitoring import (DRIVER_MONITORING_CONFIG, DriverMonitoringDisableReason,
                                                          disable_reason)
from openpilot.kovapilot.engagement.dm_session_disable import (DriverMonitoringSessionGesture,
                                                               SessionDisableContext)
from openpilot.system.manager.process_config import driver_monitoring_model, driverview


class MemoryParams:
  def __init__(self):
    self.values: dict[str, bool] = {}

  def get_bool(self, key: str) -> bool:
    return self.values.get(key, False)

  def put_bool(self, key: str, value: bool) -> None:
    self.values[key] = value


class DriverMonitoringSubMaster:
  def __init__(self, state, valid=True):
    self.state = state
    self.valid = valid

  def all_checks(self, services):
    assert services == ["driverMonitoringState"]
    return self.valid

  def __getitem__(self, service):
    assert service == "driverMonitoringState"
    return self.state


def context(timestamp: float, *, enabled=False, valid=True, reason=DriverMonitoringDisableReason.NONE):
  return SessionDisableContext(
    now=timestamp + 0.05,
    event_time=timestamp,
    can_valid=True,
    selfdrive_valid=valid,
    selfdrive_enabled=enabled,
    dm_lockout=False,
    dm_alert_three=False,
    disable_reason=reason,
  )


def press(gesture: DriverMonitoringSessionGesture, timestamp: float, **kwargs) -> bool:
  triggered = gesture.update(context(timestamp, **kwargs), [("cancel", True)])
  gesture.update(context(timestamp + 0.01, **kwargs), [("cancel", False)])
  return triggered


def test_disengaged_three_press_flow_stops_only_model_and_publishes_neutral_state():
  params = MemoryParams()
  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
  CP = car.CarParams.new_message()

  assert driver_monitoring_model(True, params, CP)
  assert driverview(True, params, CP)
  assert not press(gesture, 10.0)
  assert not press(gesture, 11.0)
  assert press(gesture, 12.0)

  params.put_bool("DriverMonitoringSessionDisabled", True)
  reason = disable_reason(params)
  assert reason == DriverMonitoringDisableReason.SESSION
  assert not driver_monitoring_model(True, params, CP)
  assert driverview(True, params, CP)

  packet = disabled_state_packet(reason, rhd=False)
  state = packet.driverMonitoringState
  assert state.disableReason == log.DriverMonitoringState.DisableReason.session
  assert str(state.alertLevel) == "none"
  assert not state.lockout
  assert not state.noResponseForceDecel
  assert monitoring_is_disabled(DriverMonitoringSubMaster(state))


def test_engaged_or_invalid_selfdrive_never_disables_session():
  for kwargs in ({"enabled": True}, {"valid": False}):
    gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
    assert not press(gesture, 10.0, **kwargs)
    assert not press(gesture, 11.0, **kwargs)
    assert not press(gesture, 12.0, **kwargs)


def test_lockout_alert_and_existing_disable_reset_partial_sequence():
  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
  assert not press(gesture, 10.0)
  assert not press(gesture, 11.0)

  blocked = context(11.5)
  blocked = SessionDisableContext(**{**blocked.__dict__, "dm_lockout": True})
  assert not gesture.update(blocked, [])
  assert not press(gesture, 12.0)

  disabled = context(13.0, reason=DriverMonitoringDisableReason.SESSION)
  assert not gesture.update(disabled, [("cancel", True)])


def test_shadow_source_has_no_publish_or_params_write_path():
  source = (Path(__file__).parents[3] / "tools/kovapilot/dm_shadow_source.py").read_text()
  for prohibited in ("PubMaster", "pub_sock", "sendcan", "Params", "put_bool", "remove("):
    assert prohibited not in source
