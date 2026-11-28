"""Always-on lateral turned off by a gear change (janpoo 11/28, 79f segment 99).

D to N (39488.238 s) to R (39488.269 s): in N the car events add wrongGear (SOFT_DISABLE), which turned
always-on lateral off with "TAKE CONTROL" and warningSoft. A gear change out of Drive (N, P or R) alone now
gives the small "Always-on Steering Off" with the short disengage sound. A real fault at the same moment
(another SOFT_DISABLE or IMMEDIATE_DISABLE, panda refusing our steering, driver monitoring) still warns.
"""
import pytest

from openpilot.cereal import log
from openpilot.kovapilot.tests.test_always_lateral_runtime import Drive
from openpilot.selfdrive.selfdrived.events import EVENTS, ET

EventName = log.OnroadEvent.EventName
GEARS = ["neutral", "park", "reverse"]


def _gear(d, gear, *events):
  d.cs.gearShifter = gear
  return d.step(EventName.wrongGear, *events)


def test_79f_99_drive_neutral_reverse_is_a_small_alert():
  d = Drive()
  d.cruise_engage_then_cancel()
  d.cs.steeringPressed = True
  d.cs.brakePressed = True
  d.step(EventName.pedalPressed, EventName.steerOverride, n=30)
  assert d.rt.engaged
  assert _gear(d, "neutral", EventName.pedalPressed, EventName.steerOverride) == [EventName.alwaysLateralDisengaged]
  assert not d.rt.engaged
  assert _gear(d, "reverse", EventName.reverseGear, EventName.pedalPressed, EventName.steerOverride) == []
  assert d.step(EventName.wrongGear, EventName.reverseGear, n=50) == []


@pytest.mark.parametrize("gear", GEARS)
def test_gear_change_alone_is_a_small_alert(gear):
  d = Drive()
  d.cruise_engage_then_cancel()
  assert _gear(d, gear) == [EventName.alwaysLateralDisengaged]
  assert not d.rt.engaged
  alert = EVENTS[EventName.alwaysLateralDisengaged][ET.PERMANENT]
  assert alert.alert_size == log.SelfdriveState.AlertSize.small
  assert alert.audible_alert == log.SelfdriveState.AudibleAlert.disengage


@pytest.mark.parametrize("gear", GEARS)
@pytest.mark.parametrize("fault", [EventName.controlsMismatch, EventName.canError, EventName.commIssue, EventName.steerUnavailable,
                                   EventName.doorOpen, EventName.seatbeltNotLatched])
def test_gear_change_with_a_real_fault_warns(gear, fault):
  d = Drive()
  d.cruise_engage_then_cancel()
  assert _gear(d, gear, fault) == [EventName.alwaysLateralUnavailable]
  assert not d.rt.engaged


@pytest.mark.parametrize("gear", GEARS)
def test_gear_change_with_driver_monitoring_lockout_warns(gear):
  d = Drive()
  d.cruise_engage_then_cancel()
  d.sm["driverMonitoringState"].lockout = True
  assert _gear(d, gear) == [EventName.alwaysLateralUnavailable]


@pytest.mark.parametrize("gear", GEARS)
def test_gear_change_while_panda_refuses_our_steering_warns(gear):
  d = Drive()
  d.cruise_engage_then_cancel()
  d.panda(100)
  d.sm["pandaStates"][0].safetyTxBlocked = 200
  d.sm.updated["pandaStates"] = True
  assert _gear(d, gear) == [EventName.alwaysLateralUnavailable]


def test_wrong_gear_without_a_gear_change_warns():
  # an unknown gear (signal fault) is not the driver leaving Drive
  d = Drive()
  d.cruise_engage_then_cancel()
  assert _gear(d, "unknown") == [EventName.alwaysLateralUnavailable]


def test_lfa_off_and_cruise_engaged_unchanged():
  d = Drive()
  d.cruise_engage_then_cancel()
  assert d.step(lfa=True) == [EventName.alwaysLateralDisengaged]
  # openpilot engaged: its own alerts apply, always-on lateral adds none
  d = Drive()
  d.enabled = True
  d.step()
  assert _gear(d, "neutral") == []
