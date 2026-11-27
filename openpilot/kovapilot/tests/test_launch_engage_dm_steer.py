"""First start without driver monitoring and while steering (janpoo 11/28 00:23).

- The first start (accelerator press in Drive) and what follows do not look at driver monitoring at all:
  disabled for the session or by the build, alerts, lockout flags, or no driverMonitoringState at all.
  Events that selfdrived itself raises (for example tooDistracted, NO_ENTRY) still block like a manual start.
- It also starts while the driver turns the wheel (steeringPressed, steerOverride, a large angle); the start goes
  into the override state as with the accelerator.
- The system conditions only need to hold for 1 s before the press, in any gear after Park, so a press right
  after shifting into Drive (79e: P, R, N, D at 23.5 s, press at 24.1 s) starts.
"""
import pytest

from openpilot.cereal import log
from openpilot.kovapilot.tests.test_auto_engage import _runtime
from openpilot.kovapilot.tests.test_launch_engage import _status, _stop_in_drive
from openpilot.selfdrive.selfdrived.state import State

EN = log.OnroadEvent.EventName
GAS = EN.gasPressedOverride
DM = log.DriverMonitoringState


def _dm(runtime, **fields):
  runtime.sd.sm["driverMonitoringState"] = DM.new_message(**fields)


def _steer_events(runtime):
  # selfdrived adds steerOverride while the driver steers (car events)
  def update_events(CS):
    if CS.steeringPressed:
      runtime.sd.events.add(EN.steerOverride)
  runtime.sd.update_events.side_effect = update_events


@pytest.mark.parametrize("fields", [
  {"disableReason": "session"},
  {"disableReason": "buildConfig"},
  {"alertLevel": "one"},
  {"alertLevel": "three"},
  {"lockout": True},
  {"alwaysOnLockout": True},
])
def test_driver_monitoring_state_does_not_block_the_first_start(monkeypatch, tmp_path, fields):
  runtime = _runtime(monkeypatch, tmp_path)
  _dm(runtime, **fields)
  _stop_in_drive(runtime)
  assert _status() == ("waiting", "waiting")
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_missing_driver_monitoring_messages_do_not_block_the_first_start(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  sm = runtime.sd.sm
  sm.valid["driverMonitoringState"] = sm.alive["driverMonitoringState"] = False
  _stop_in_drive(runtime)
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_driver_monitoring_after_the_start_does_not_change_the_status(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  _stop_in_drive(runtime)
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)
  for fields in ({"alertLevel": "three"}, {"disableReason": "session"}, {"lockout": True}):
    _dm(runtime, **fields)
    for _ in range(3):
      assert runtime.step(GAS)
      assert _status() == ("paused", "paused"), fields


def test_selfdrived_entry_blocks_from_driver_monitoring_still_block(monkeypatch, tmp_path):
  # selfdrived raises tooDistracted (NO_ENTRY) when monitoring is on and locked out: same as a manual start
  runtime = _runtime(monkeypatch, tmp_path)
  _stop_in_drive(runtime)
  runtime.cs.gasPressed = True
  for _ in range(20):
    assert not runtime.step(EN.tooDistracted)


@pytest.mark.parametrize("angle", [0.0, -532.0, 90.0])
def test_starts_while_the_driver_steers(monkeypatch, tmp_path, angle):
  runtime = _runtime(monkeypatch, tmp_path)
  _steer_events(runtime)
  _stop_in_drive(runtime)
  runtime.cs.steeringPressed = True
  runtime.cs.steeringAngleDeg = angle
  assert not runtime.step()
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)
  assert runtime.sd.state_machine.state == State.overriding
  # still overriding while steering or pressing, enabled once both are released
  runtime.cs.gasPressed = False
  assert runtime.step()
  assert runtime.sd.state_machine.state == State.overriding
  runtime.cs.steeringPressed = False
  assert runtime.step()
  assert runtime.sd.state_machine.state == State.enabled


def test_press_right_after_shifting_into_drive_starts(monkeypatch, tmp_path):
  # 79e: Park after ignition, then P, R, N, D and the press 0.6 s after Drive
  runtime = _runtime(monkeypatch, tmp_path)
  for _ in range(12):
    assert not runtime.step()
  for gear in ("reverse", "neutral", "drive"):
    runtime.cs.gearShifter = gear
    for _ in range(2):
      assert not runtime.step()
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_system_conditions_still_need_one_second_before_the_press(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  runtime.cs.cruiseState.available = False
  for _ in range(12):
    assert not runtime.step()
  runtime.cs.gearShifter = "drive"
  runtime.cs.cruiseState.available = True
  for _ in range(5):
    assert not runtime.step()
  runtime.cs.gasPressed = True
  for _ in range(20):
    assert not runtime.step(GAS)
