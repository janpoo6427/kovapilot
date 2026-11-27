"""First start at the launch (janpoo 11/27 23:12): from ignition on the start waits (gray), and the first accelerator
press in Drive engages, also pulling away from a stop, with the set speed starting at the speed of that moment and
following the held pedal. Braking, Reverse or Neutral before the start keep it waiting; the driver turning cruise off
on purpose (CANCEL, MAIN, LFA turning always-on lateral off) locks it."""
from types import SimpleNamespace

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.kovapilot.config import auto_engage_settings
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import MIN_PRESS_SAMPLES
from openpilot.kovapilot.tests.test_auto_engage import _runtime
from openpilot.selfdrive.car.cruise import V_CRUISE_INITIAL, V_CRUISE_MIN, VCruiseHelper

EN = log.OnroadEvent.EventName
GAS = EN.gasPressedOverride


def _stop_in_drive(runtime, *, brake_steps=20):
  # Park, then Drive held on the brake at a stop (selfdrived adds pedalPressed on the brake press)
  assert not runtime.step()
  runtime.cs.gearShifter = "drive"
  runtime.cs.brakePressed = True
  assert not runtime.step(EN.pedalPressed)
  for _ in range(brake_steps):
    assert not runtime.step()
  runtime.cs.brakePressed = False


def _status():
  status = auto_engage_settings.read_auto_engage_status()
  return status["lateral"], status["longitudinal"]


def test_pulling_away_from_a_stop_engages_at_the_press(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  _stop_in_drive(runtime)
  assert _status() == ("waiting", "waiting")
  # the brake is released just before the press, the plan still stops while the car stands
  plan = runtime.sd.sm["longitudinalPlan"]
  plan.shouldStop = True
  assert not runtime.step()
  runtime.cs.gasPressed = True
  assert not runtime.step(GAS)
  # the car rolls: the plan no longer stops, and the start comes within the window of the same press
  runtime.cs.standstill = False
  runtime.cs.vEgo = 0.5
  plan.shouldStop = False
  assert runtime.step(GAS)


def test_press_at_standstill_engages_when_nothing_stops(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  _stop_in_drive(runtime)
  assert not runtime.step()
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_brake_before_and_in_drive_does_not_lock(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  runtime.cs.brakePressed = True
  assert not runtime.step(EN.pedalPressed)
  _stop_in_drive(runtime)
  for _ in range(3):
    runtime.cs.brakePressed = True
    assert not runtime.step(EN.pedalPressed)
    runtime.cs.brakePressed = False
    assert not runtime.step()
  assert _status() == ("waiting", "waiting")
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_reverse_and_neutral_before_drive_keep_waiting(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  assert not runtime.step()
  for gear in ("reverse", "neutral", "drive", "neutral", "reverse", "neutral"):
    runtime.cs.gearShifter = gear
    for _ in range(5):
      assert not runtime.step()
  _stop_in_drive(runtime)
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_drive_without_park_in_this_session_does_not_start(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  runtime.cs.gearShifter = "reverse"
  for _ in range(5):
    assert not runtime.step()
  runtime.cs.gearShifter = "drive"
  for _ in range(12):
    assert not runtime.step()
  runtime.cs.gasPressed = True
  for _ in range(20):
    assert not runtime.step(GAS)


def test_a_tap_released_before_the_start_does_not_engage_later(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  panda = runtime.sd.sm["pandaStates"][0]
  panda.controlsAllowed = False
  _stop_in_drive(runtime)
  runtime.cs.gasPressed = True
  assert not runtime.step(GAS)
  runtime.cs.gasPressed = False
  panda.controlsAllowed = True
  for _ in range(20):
    assert not runtime.step()


def test_cancel_and_main_lock_the_start(monkeypatch, tmp_path):
  for button, event in (("cancel", EN.buttonCancel), ("mainCruise", None)):
    (tmp_path / button).mkdir()
    runtime = _runtime(monkeypatch, tmp_path / button)
    _stop_in_drive(runtime)
    runtime.cs.buttonEvents = [{"type": button, "pressed": True}]
    assert not runtime.step(event)
    runtime.cs.buttonEvents = []
    for _ in range(12):
      assert not runtime.step()
    assert _status() == ("manualRequired", "manualRequired")
    runtime.cs.gasPressed = True
    for _ in range(20):
      assert not runtime.step(GAS)


def test_lfa_turning_always_on_lateral_on_keeps_waiting_and_off_locks(monkeypatch, tmp_path):
  for engaged_before, starts in ((False, True), (True, False)):
    (tmp_path / str(engaged_before)).mkdir()
    runtime = _runtime(monkeypatch, tmp_path / str(engaged_before))
    runtime.sd.always_lateral = SimpleNamespace(engaged=engaged_before, update=lambda *args: None)
    _stop_in_drive(runtime)
    runtime.cs.buttonEvents = [{"type": "lkas", "pressed": True}]
    assert not runtime.step()
    runtime.cs.buttonEvents = []
    for _ in range(12):
      assert not runtime.step()
    runtime.cs.gasPressed = True
    assert runtime.step(GAS) == starts, engaged_before


def card_setup(*, is_metric=True):
  # card order: update_v_cruise, initialize_v_cruise and initialize_on_accelerator on the engaging step, then the bridge
  helper = VCruiseHelper(car.CarParams(pcmCruise=False, openpilotLongitudinalControl=True))
  bridge = CruiseAcceleratorSyncBridge(helper)
  state = {"enabled_prev": False, "cs_prev": None}

  def step(speed_kph, pressed, *, enabled=True, buttons=()):
    CS = car.CarState.new_message(vEgo=speed_kph * CV.KPH_TO_MS, vEgoCluster=speed_kph * CV.KPH_TO_MS, gasPressed=pressed,
                                 standstill=speed_kph == 0, cruiseState={"available": True},
                                 buttonEvents=[{"type": b, "pressed": p} for b, p in buttons])
    CC = car.CarControl.new_message(enabled=enabled, longActive=enabled and not pressed)
    helper.update_v_cruise(CS, CC.enabled, is_metric=is_metric)
    if enabled and not state["enabled_prev"]:
      helper.initialize_v_cruise(state["cs_prev"], V_CRUISE_INITIAL)
      bridge.initialize_on_accelerator(state["cs_prev"], is_metric)
    bridge.update(CS, CC, inputs_valid=True, cruise_engaged=enabled and state["enabled_prev"], enabled=True,
                  stock_shadow=False, disengage_on_accelerator=False, is_metric=is_metric)
    state["enabled_prev"] = enabled
    state["cs_prev"] = CS
    return helper.v_cruise_kph

  return step


def test_set_speed_from_a_stop_starts_at_the_minimum_and_follows_the_pedal():
  step = card_setup()
  step(0.0, False, enabled=False)
  step(0.0, True, enabled=False)
  assert step(0.0, True) == V_CRUISE_MIN
  speed, seen = 0.0, []
  for _ in range(MIN_PRESS_SAMPLES + 500):
    speed += 0.05
    seen.append(step(speed, True))
  assert seen[-1] == round(speed)
  assert all(b >= a for a, b in zip(seen, seen[1:], strict=False))
  # released at about 25 km/h: kept
  assert step(speed, False) == round(speed)
  assert step(speed - 3, False) == round(speed)


def test_set_speed_on_a_press_starts_at_the_current_speed():
  for speed_kph, shown in ((23.4, 23), (5.0, V_CRUISE_MIN), (35.6, 36), (61.0, 61)):
    step = card_setup()
    step(speed_kph, False, enabled=False)
    step(speed_kph, True, enabled=False)
    assert step(speed_kph, True) == shown, speed_kph


def test_set_button_engagement_keeps_the_initial_cruise_speed():
  # the SET button while pressing keeps the initial cruise speed (at least 40 km/h), then follows the pedal
  step = card_setup()
  step(12.8, False, enabled=False)
  step(12.8, True, enabled=False)
  step(12.8, True, enabled=False, buttons=[("decelCruise", True)])
  step(12.8, True, enabled=False, buttons=[("decelCruise", False)])
  assert step(12.8, True) == V_CRUISE_INITIAL
  speed = 12.8
  while speed < 71.0:
    speed += 0.05
    step(speed, True)
  assert step(speed, True) == 71
  assert step(speed, False) == 71


def test_engagement_without_the_pedal_keeps_the_initial_cruise_speed():
  step = card_setup()
  step(20.0, False, enabled=False)
  assert step(20.0, False) == V_CRUISE_INITIAL


def test_imperial_set_speed_on_a_press_uses_the_mph_grid():
  step = card_setup(is_metric=False)
  step(30.0, False, enabled=False)
  step(30.0, True, enabled=False)
  assert step(30.0, True) == round(30.0 * CV.KPH_TO_MPH) * round(CV.MPH_TO_KPH, 1)
