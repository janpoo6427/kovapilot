import pytest

from openpilot.cereal import log
from openpilot.kovapilot.bridges.driver_monitoring import disabled_state_packet, monitoring_is_disabled
from openpilot.kovapilot.config.driver_monitoring import (DriverMonitoringConfig, DriverMonitoringDisableReason,
                                                          disable_reason)
from openpilot.kovapilot.engagement.dm_cancel_gesture import CancelPressSequence


class FakeParams:
  def __init__(self, session_disabled: bool = False):
    self.session_disabled = session_disabled

  def get_bool(self, key: str) -> bool:
    assert key == "DriverMonitoringSessionDisabled"
    return self.session_disabled


class FakeSubMaster:
  def __init__(self, disable_reason, valid=True):
    self.valid = valid
    self.state = type("State", (), {"disableReason": disable_reason})()

  def all_checks(self, services):
    assert services == ["driverMonitoringState"]
    return self.valid

  def __getitem__(self, service):
    assert service == "driverMonitoringState"
    return self.state


def press(sequence: CancelPressSequence, timestamp: float) -> bool:
  triggered = sequence.update(timestamp, [("cancel", True)])
  sequence.update(timestamp + 0.01, [("cancel", False)])
  return triggered


@pytest.mark.parametrize(
  ("config", "session_disabled", "expected"),
  [
    (DriverMonitoringConfig(), False, DriverMonitoringDisableReason.NONE),
    (DriverMonitoringConfig(), True, DriverMonitoringDisableReason.SESSION),
    (DriverMonitoringConfig(enabled=False), False, DriverMonitoringDisableReason.BUILD_CONFIG),
    (DriverMonitoringConfig(enabled=False), True, DriverMonitoringDisableReason.BUILD_CONFIG),
  ],
)
def test_disable_reason(config, session_disabled, expected):
  assert disable_reason(FakeParams(session_disabled), config) == expected


def test_three_cancel_presses_trigger_within_window():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  assert press(sequence, 12.9)


def test_cancel_window_expires():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  assert not press(sequence, 13.1)


def test_held_cancel_counts_once():
  sequence = CancelPressSequence()
  assert not sequence.update(10.0, [("cancel", True)])
  assert not sequence.update(10.1, [("cancel", True)])
  assert not sequence.update(10.2, [("cancel", True)])


def test_non_cancel_button_resets_sequence():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  assert not sequence.update(11.5, [("accelCruise", True)])
  assert not press(sequence, 12.0)


def test_invalid_timestamp_resets_sequence():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  assert not sequence.update(float("nan"), [("cancel", True)])
  assert not press(sequence, 12.0)


def test_time_reversal_does_not_complete_sequence():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  assert not press(sequence, 9.0)


def test_software_cancel_echo_is_rejected():
  sequence = CancelPressSequence()
  assert not press(sequence, 10.0)
  assert not press(sequence, 11.0)
  sequence.record_cancel_request(12.0)
  assert not press(sequence, 12.05)
  assert not press(sequence, 12.5)


def test_sequence_rearms_after_trigger():
  sequence = CancelPressSequence()
  for timestamp in (10.0, 11.0):
    assert not press(sequence, timestamp)
  assert press(sequence, 12.0)
  for timestamp in (20.0, 21.0):
    assert not press(sequence, timestamp)
  assert press(sequence, 22.0)


@pytest.mark.parametrize("reason", [DriverMonitoringDisableReason.SESSION,
                                    DriverMonitoringDisableReason.BUILD_CONFIG])
def test_disabled_state_packet_is_neutral(reason):
  packet = disabled_state_packet(reason, rhd=False)
  state = packet.driverMonitoringState
  assert packet.valid
  assert state.disableReason != log.DriverMonitoringState.DisableReason.none
  assert str(state.alertLevel) == "none"
  assert not state.lockout
  assert not state.noResponseForceDecel
  assert state.visionPolicyState.awarenessPercent == 100
  assert state.wheeltouchPolicyState.awarenessPercent == 100


def test_monitoring_is_disabled_requires_valid_fresh_state():
  reason = log.DriverMonitoringState.DisableReason.session
  assert monitoring_is_disabled(FakeSubMaster(reason))
  assert not monitoring_is_disabled(FakeSubMaster(reason, valid=False))
  assert not monitoring_is_disabled(FakeSubMaster(log.DriverMonitoringState.DisableReason.none))
