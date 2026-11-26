"""Always-on lateral (steering.always_on): selfdrived runtime, vehicle support and gateway condition."""
from types import SimpleNamespace

import pytest

from opendbc.car.hyundai.values import HyundaiFlags
from opendbc.car.structs import car
from opendbc.safety import ALTERNATIVE_EXPERIENCE
from openpilot.cereal import log
from openpilot.kovapilot.bridges.always_lateral_runtime import DRIVER_RELEASE_FRAMES, TX_BLOCKED_LIMIT, AlwaysLateralRuntime
from openpilot.kovapilot.config.kova_params import KovaParamsError
from openpilot.kovapilot.gateway import native_longitudinal
from openpilot.kovapilot.vehicle.always_lateral import (always_on_lateral_enabled, always_on_lateral_supported, apply_always_on_lateral,
                                                        lfa_button_engages, read_always_on_steering)
from openpilot.selfdrive.selfdrived.events import Events

EventName = log.OnroadEvent.EventName
ALERTS = (EventName.alwaysLateralEngaged, EventName.alwaysLateralDisengaged, EventName.alwaysLateralUnavailable)


def _cp(*, canfd=True, always_on=True, brand="hyundai", model="hyundaiCanfd", steer="angle", **kwargs):
  flags = int(HyundaiFlags.CANFD | HyundaiFlags.CANFD_ANGLE_STEERING) if canfd else 0
  return car.CarParams.new_message(brand=brand, flags=flags, steerControlType=steer, safetyConfigs=[{"safetyModel": model}],
                                   alternativeExperience=ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL if always_on else 0, **kwargs)


class SM(dict):
  def __init__(self):
    super().__init__(driverMonitoringState=log.DriverMonitoringState.new_message(),
                     pandaStates=[log.PandaState.new_message(safetyModel="hyundaiCanfd")])
    self.updated = {"pandaStates": False}

  def all_checks(self, services=None):
    return True


class Drive:
  def __init__(self, cp=None):
    self.rt = AlwaysLateralRuntime(_cp() if cp is None else cp)
    self.sm = SM()
    self.cs = car.CarState.new_message(gearShifter="drive", vEgo=20.0)
    self.enabled = False

  def step(self, *event_names, lfa=False, n=1):
    added = []
    for _ in range(n):
      self.cs.buttonEvents = [{"type": "lkas", "pressed": True}] if lfa else []
      events = Events()
      for name in event_names:
        events.add(name)
      self.rt.update(self.cs, self.sm, events, self.enabled)
      added += [name for name in events.names if name in ALERTS]
    return added

  def panda(self, tx_blocked):
    self.sm["pandaStates"][0].safetyTxBlocked = tx_blocked
    self.sm.updated["pandaStates"] = True
    self.step()
    self.sm.updated["pandaStates"] = False

  def cruise_engage_then_cancel(self):
    self.enabled = True
    self.step()
    self.enabled = False
    return self.step()


def test_off_without_the_carparams_flag():
  d = Drive(_cp(always_on=False))
  d.enabled = True
  assert d.step() == []
  d.enabled = False
  assert d.step(lfa=True) == []
  assert not d.rt.engaged and not d.rt.active


def test_cruise_engagement_outlives_cruise_with_one_alert():
  d = Drive()
  assert not d.rt.engaged
  d.enabled = True
  assert d.step() == []  # openpilot owns lateral, no extra alert
  assert d.rt.engaged and d.rt.active
  d.enabled = False
  assert d.step() == [EventName.alwaysLateralEngaged]
  assert d.rt.engaged and d.rt.active
  assert d.step(n=50) == []


def test_brake_pauses_and_release_resumes():
  d = Drive()
  d.cruise_engage_then_cancel()
  d.cs.brakePressed = True
  assert d.step() == []
  assert d.rt.engaged and not d.rt.active
  d.cs.brakePressed = False
  d.step()
  assert d.rt.active
  d.cs.regenBraking = True
  d.step()
  assert not d.rt.active


def test_driver_steering_pauses_until_released():
  d = Drive()
  d.cruise_engage_then_cancel()
  d.cs.steeringPressed = True
  d.step(n=20)
  assert d.rt.engaged and not d.rt.active
  d.cs.steeringPressed = False
  d.step(n=DRIVER_RELEASE_FRAMES - 1)
  assert not d.rt.active
  d.step()
  assert d.rt.active


@pytest.mark.parametrize("gear", ["park", "reverse"])
def test_park_or_reverse_turns_it_off(gear):
  d = Drive()
  d.cruise_engage_then_cancel()
  d.cs.gearShifter = gear
  assert d.step() == [EventName.alwaysLateralDisengaged]
  assert not d.rt.engaged
  d.cs.gearShifter = "drive"
  d.step(n=10)
  assert not d.rt.engaged  # a new request is needed


def test_lfa_button_toggles_on_canfd():
  d = Drive()
  assert d.step(lfa=True) == [EventName.alwaysLateralEngaged]
  assert d.rt.engaged and d.rt.active
  assert d.step(lfa=True) == [EventName.alwaysLateralDisengaged]
  assert not d.rt.engaged
  # an entry block refuses the request, cruise-only blocks do not
  assert d.step(EventName.seatbeltNotLatched, lfa=True) == []
  assert not d.rt.engaged
  assert d.step(EventName.wrongCarMode, EventName.pedalPressed, lfa=True) == [EventName.alwaysLateralEngaged]


def test_lfa_button_is_not_an_engagement_on_can():
  # panda safety does not read the LFA button on CAN cars, so openpilot must not use it either
  d = Drive(_cp(canfd=False, model="hyundai", steer="torque"))
  assert d.step(lfa=True) == []
  assert not d.rt.engaged
  d.cruise_engage_then_cancel()
  assert d.rt.engaged
  assert d.step(lfa=False) == []


def test_lfa_while_openpilot_engaged_drops_it_after_cruise():
  d = Drive()
  d.enabled = True
  d.step()
  d.step(lfa=True)
  assert not d.rt.engaged
  d.enabled = False
  assert d.step() == []
  assert not d.rt.engaged


@pytest.mark.parametrize("event", [EventName.controlsMismatch, EventName.canError, EventName.commIssue, EventName.steerUnavailable,
                                   EventName.steerTempUnavailable, EventName.doorOpen, EventName.calibrationInvalid])
def test_system_faults_turn_it_off_with_take_control(event):
  d = Drive()
  d.cruise_engage_then_cancel()
  assert d.step(event) == [EventName.alwaysLateralUnavailable]
  assert not d.rt.engaged


@pytest.mark.parametrize("event", [EventName.accFaulted, EventName.radarFault, EventName.steerTempUnavailableSilent, EventName.cruiseDisabled,
                                   EventName.pedalPressed, EventName.buttonCancel, EventName.wrongCarMode])
def test_cruise_only_events_keep_it(event):
  d = Drive()
  d.cruise_engage_then_cancel()
  assert d.step(event) == []
  assert d.rt.engaged


def test_driver_monitoring_terminal_alert_or_lockout_turns_it_off():
  for field, value in (("alertLevel", "three"), ("lockout", True)):
    d = Drive()
    d.cruise_engage_then_cancel()
    setattr(d.sm["driverMonitoringState"], field, value)
    assert d.step() == [EventName.alwaysLateralUnavailable]
    assert not d.rt.engaged
    # and a new request is refused while it lasts
    d.step(lfa=True)
    assert not d.rt.engaged
  d = Drive()
  d.cruise_engage_then_cancel()
  d.sm["driverMonitoringState"].alertLevel = "two"
  d.step()
  assert d.rt.engaged
  # driver monitoring disabled by the KOVA option keeps the existing behavior
  d.sm["driverMonitoringState"].alertLevel = "three"
  d.sm["driverMonitoringState"].disableReason = "session"
  d.step()
  assert d.rt.engaged


def test_panda_blocking_our_steering_turns_it_off():
  d = Drive()
  d.cruise_engage_then_cancel()
  d.panda(100)
  d.panda(100 + TX_BLOCKED_LIMIT - 1)  # a few frames can race a brake press
  assert d.rt.engaged
  d.panda(100 + 2 * TX_BLOCKED_LIMIT + 10)
  assert not d.rt.engaged


def test_panda_blocks_are_ignored_while_openpilot_is_engaged_or_paused():
  d = Drive()
  d.enabled = True
  d.step()
  d.panda(0)
  d.panda(500)
  d.panda(1000)
  assert d.rt.engaged
  d.enabled = False
  d.step()
  d.cs.brakePressed = True
  d.step()
  d.panda(1500)
  d.panda(2000)
  assert d.rt.engaged


def test_vehicle_support():
  assert always_on_lateral_supported(_cp())
  assert always_on_lateral_supported(_cp(canfd=False, model="hyundai", steer="torque"))
  assert always_on_lateral_supported(_cp(canfd=False, model="hyundaiLegacy", steer="torque"))
  assert always_on_lateral_supported(car.CarParams.new_message(brand="hyundai", steerControlType="torque",
                                     safetyConfigs=[{"safetyModel": "noOutput"}, {"safetyModel": "hyundaiCanfd"}]))
  assert not always_on_lateral_supported(car.CarParams.new_message(brand="hyundai", steerControlType="torque", safetyConfigs=[]))
  assert not always_on_lateral_supported(_cp(brand="toyota", model="toyota", steer="torque"))
  assert not always_on_lateral_supported(_cp(model="noOutput"))
  assert not always_on_lateral_supported(_cp(steer="curvature"))
  for field in ("passive", "dashcamOnly", "notCar"):
    assert not always_on_lateral_supported(_cp(**{field: True}))
  assert lfa_button_engages(_cp()) and not lfa_button_engages(_cp(canfd=False))
  assert not lfa_button_engages(_cp(brand="kia"))


def test_apply_flag_once_per_session():
  cp = _cp(always_on=False)
  cp.alternativeExperience = ALTERNATIVE_EXPERIENCE.ALLOW_AEB
  assert apply_always_on_lateral(cp, True)
  assert always_on_lateral_enabled(cp)
  assert cp.alternativeExperience == ALTERNATIVE_EXPERIENCE.ALLOW_AEB | ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL
  assert not apply_always_on_lateral(cp, False)
  assert cp.alternativeExperience == ALTERNATIVE_EXPERIENCE.ALLOW_AEB
  unsupported = _cp(always_on=False, passive=True)
  assert not apply_always_on_lateral(unsupported, True)
  assert unsupported.alternativeExperience == 0


def test_read_setting_fails_closed():
  assert read_always_on_steering(SimpleNamespace(values=lambda: {"steering.always_on": 1}))
  assert not read_always_on_steering(SimpleNamespace(values=lambda: {"steering.always_on": 0}))

  def broken():
    raise KovaParamsError("bad")
  assert not read_always_on_steering(SimpleNamespace(values=broken))
  assert not read_always_on_steering(SimpleNamespace(values=dict))


def test_gateway_condition_follows_carparams():
  class Params:
    def __init__(self, raw):
      self.raw = raw

    def get(self, key):
      return self.raw if key == "CarParams" else None

    def get_bool(self, key):
      return False

  condition = "independentLateralControl"
  assert native_longitudinal.vehicle_dependency_conditions(Params(None))[condition] == "unknown"
  assert native_longitudinal.vehicle_dependency_conditions(Params(b"bad"))[condition] == "unknown"
  assert native_longitudinal.vehicle_dependency_conditions(Params(_cp().to_bytes()))[condition] == "met"
  assert native_longitudinal.vehicle_dependency_conditions(Params(_cp(passive=True).to_bytes()))[condition] == "unsupported"
  assert native_longitudinal.vehicle_dependency_conditions(Params(_cp(brand="toyota").to_bytes()))[condition] == "unsupported"
