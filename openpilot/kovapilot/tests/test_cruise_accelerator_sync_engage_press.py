"""Cruise engaged on an accelerator press: the set speed starts at the speed of that moment and follows
the speed while the pedal stays held, then keeps the value reached (janpoo 11/27 22:28)."""
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import MIN_PRESS_SAMPLES
from openpilot.selfdrive.car.cruise import V_CRUISE_INITIAL, VCruiseHelper


def setup(*, sync_enabled=True):
  helper = VCruiseHelper(car.CarParams(pcmCruise=False, openpilotLongitudinalControl=True))
  bridge = CruiseAcceleratorSyncBridge(helper)
  state = {"enabled_prev": False}

  def step(speed_kph, pressed, *, enabled=True, buttons=(), is_metric=True):
    CS = car.CarState.new_message(vEgo=speed_kph * CV.KPH_TO_MS, vEgoCluster=speed_kph * CV.KPH_TO_MS, gasPressed=pressed,
                                 cruiseState={"available": True}, buttonEvents=[{"type": b, "pressed": p} for b, p in buttons])
    CC = car.CarControl.new_message(enabled=enabled, longActive=enabled and not pressed)
    helper.update_v_cruise(CS, CC.enabled, is_metric=is_metric)
    if enabled and not state["enabled_prev"]:
      helper.initialize_v_cruise(CS, V_CRUISE_INITIAL)
    bridge.update(CS, CC, inputs_valid=True, cruise_engaged=enabled and state["enabled_prev"], enabled=sync_enabled,
                  stock_shadow=False, disengage_on_accelerator=False, is_metric=is_metric)
    state["enabled_prev"] = enabled
    return helper.v_cruise_kph, helper.v_cruise_cluster_kph

  return helper, step


def test_engaging_on_the_press_follows_the_speed_while_held_then_keeps_it():
  helper, step = setup()
  for _ in range(20):
    step(55.0, False, enabled=False)
  step(55.2, True, enabled=False)
  assert step(55.4, True) == (55, 55)
  seen = []
  speed = 55.4
  for _ in range(MIN_PRESS_SAMPLES + 300):
    speed += 0.05
    seen.append(step(speed, True)[0])
  assert seen[MIN_PRESS_SAMPLES - 2] == 55
  assert seen[-1] == round(speed)
  # the first follow goes straight to the rounded speed once it is 1 km/h above the engaged speed, then 1 km/h steps
  first = next(i for i, v in enumerate(seen) if v != 55)
  assert seen[first] - 55 <= 2
  assert all(b - a in (0, 1) for a, b in zip(seen[first:], seen[first + 1:], strict=False))
  final = step(speed, False)
  assert final == (round(speed), round(speed))
  for _ in range(50):
    speed -= 0.1
    assert step(speed, False) == final


def test_engaging_below_the_minimum_set_speed_starts_at_the_minimum_then_follows_past_it():
  helper, step = setup()
  step(32.0, False, enabled=False)
  step(32.0, True, enabled=False)
  assert step(32.0, True) == (V_CRUISE_INITIAL, V_CRUISE_INITIAL)
  speed = 32.0
  while speed < 39.5:
    speed += 0.05
    assert step(speed, True)[0] == V_CRUISE_INITIAL
  while speed < 46.0:
    speed += 0.05
    step(speed, True)
  assert step(speed, False)[0] == 46


def test_set_speed_follow_stops_at_the_upper_limit():
  helper, step = setup()
  step(140.0, False, enabled=False)
  step(140.0, True, enabled=False)
  step(140.0, True)
  speed = 140.0
  for _ in range(MIN_PRESS_SAMPLES + 200):
    speed += 0.05
    step(speed, True)
  assert step(speed, False)[0] == 145


def test_engaged_with_the_set_button_during_the_press_keeps_the_existing_rule():
  helper, step = setup()
  step(55.0, False, enabled=False)
  step(55.0, True, enabled=False)
  step(55.0, True, enabled=False, buttons=[("decelCruise", True)])
  step(55.0, True, enabled=False, buttons=[("decelCruise", False)])
  start = step(55.0, True)[0]
  speed = 55.0
  for _ in range(MIN_PRESS_SAMPLES + 200):
    speed += 0.05
    assert step(speed, True)[0] == start


def test_sync_setting_off_keeps_the_engaged_speed():
  helper, step = setup(sync_enabled=False)
  step(55.0, False, enabled=False)
  step(55.0, True, enabled=False)
  start = step(55.0, True)[0]
  speed = 55.0
  for _ in range(MIN_PRESS_SAMPLES + 200):
    speed += 0.05
    assert step(speed, True)[0] == start
