"""KOVA distance button levels. A car whose own cruise gap has four levels (CAN-FD camera SCC, rlog 0000079c segment 49,
2026-11-27: each gap press stepped the stock SCC_CONTROL DISTANCE_SETTING 4 -> 3 -> 2 -> 1 -> 4) gets a fourth, farthest
level and follows the car level one to one. Cars that never report 4 keep the three stock levels unchanged."""
import pytest

from openpilot.cereal import log
from openpilot.kovapilot.config.longitudinal_personality import (
  DEFAULT_PERSONALITY, FOUR_LEVEL_CAR_PARAM, CruiseGapLevels, clamp_personality, normalize_longitudinal_personality,
)
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import get_jerk_factor, get_T_FOLLOW
from openpilot.selfdrive.controls.tests.test_following_distance import desired_follow_distance, run_following_distance_simulation

P = log.LongitudinalPersonality
CAR = "HYUNDAI_TEST_CAR"


class FakeParams:
  def __init__(self, values=None):
    self.values = dict(values or {})
    self.puts = []

  def get(self, key, return_default=False):
    return self.values.get(key)

  def put(self, key, value, block=False):
    self.puts.append((key, value))
    self.values[key] = value


def test_enum_appends_more_relaxed():
  # old logs keep their meaning: the stock values are unchanged, the new one is last
  assert (int(P.aggressive), int(P.standard), int(P.relaxed), int(P.moreRelaxed)) == (0, 1, 2, 3)
  assert normalize_longitudinal_personality(3) == 3


def test_follow_times():
  assert get_T_FOLLOW(P.moreRelaxed) == 2.0
  assert (get_T_FOLLOW(P.relaxed), get_T_FOLLOW(P.standard), get_T_FOLLOW(P.aggressive)) == (1.75, 1.45, 1.25)
  assert get_jerk_factor(P.moreRelaxed) == get_jerk_factor(P.relaxed) == 1.0
  assert get_jerk_factor(P.aggressive) == 0.5


@pytest.mark.parametrize("speed", [0, 10, 35])
def test_more_relaxed_following_distance(speed):
  v_lead = float(speed)
  steady = run_following_distance_simulation(v_lead, personality=P.moreRelaxed)
  correct = desired_follow_distance(v_lead, v_lead, get_T_FOLLOW(P.moreRelaxed))
  assert abs(steady - correct) <= 0.2 * correct + (0.5 if v_lead > 0.0 else 1.15)
  if speed > 0:
    assert steady > run_following_distance_simulation(v_lead, personality=P.relaxed)


def test_three_level_car_is_unchanged():
  params = FakeParams()
  gaps = CruiseGapLevels(params, CAR)
  assert gaps.levels == 3
  # the car reports 0..3 (three level car or stock SCC off): never followed, nothing remembered
  for gap in (0, 3, 2, 1, 3, 0, 1, 2, 3):
    assert gaps.update(gap) is None
  assert gaps.levels == 3 and params.puts == []
  # distance button: the stock three level cycle
  for p in range(3):
    assert gaps.next_personality(p) == (p - 1) % 3
  assert [clamp_personality(p, 3) for p in (0, 1, 2)] == [0, 1, 2]
  assert clamp_personality(3, 3) == DEFAULT_PERSONALITY  # like the stock three values, which rejected 3


def test_four_detected_once_and_remembered():
  params = FakeParams()
  gaps = CruiseGapLevels(params, CAR)
  assert gaps.update(2) is None
  assert gaps.update(4) == int(P.moreRelaxed)  # first 4: four levels, follow the car
  assert gaps.levels == 4 and params.puts == [(FOUR_LEVEL_CAR_PARAM, CAR)]
  assert gaps.update(4) is None  # unchanged car level: no new follow
  # one to one: car level n -> personality n - 1
  assert [gaps.update(g) for g in (3, 2, 1, 4)] == [2, 1, 0, 3]
  # stock SCC off (0) does not change anything, nor forget four levels; coming back follows again
  assert gaps.update(0) is None and gaps.levels == 4
  assert gaps.update(4) == 3
  assert params.puts == [(FOUR_LEVEL_CAR_PARAM, CAR)]
  # distance button on a four level car: 3 -> 2 -> 1 -> 0 -> 3, the same direction as the car
  assert [gaps.next_personality(p) for p in (3, 2, 1, 0)] == [2, 1, 0, 3]
  assert clamp_personality(3, 4) == 3


def test_remembered_only_for_the_same_car():
  assert CruiseGapLevels(FakeParams({FOUR_LEVEL_CAR_PARAM: CAR}), CAR).levels == 4
  assert CruiseGapLevels(FakeParams({FOUR_LEVEL_CAR_PARAM: CAR}), "OTHER_CAR").levels == 3
  assert CruiseGapLevels(FakeParams({FOUR_LEVEL_CAR_PARAM: ""}), "").levels == 3
  # a remembered car follows its first reported level right away
  gaps = CruiseGapLevels(FakeParams({FOUR_LEVEL_CAR_PARAM: CAR}), CAR)
  assert gaps.update(2) == 1


def selfdrived_with(levels_param=None, op_long=True, personality=1):
  from opendbc.car.structs import car
  from openpilot.selfdrive.selfdrived.events import Events
  from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
  sd = object.__new__(SelfdriveD)
  sd.CP = car.CarParams.new_message(openpilotLongitudinalControl=op_long, carFingerprint=CAR)
  sd.params = FakeParams({} if levels_param is None else {FOUR_LEVEL_CAR_PARAM: levels_param})
  sd.gap_levels = CruiseGapLevels(sd.params, sd.CP.carFingerprint)
  sd.personality = personality
  sd.events = Events()
  return sd


def frame(sd, gap, press=False):
  from opendbc.car.structs import car
  from openpilot.selfdrive.selfdrived.events import Events
  buttons = [{"type": "gapAdjustCruise", "pressed": False}] if press else []
  sd.events = Events()
  sd.update_personality(car.CarState.new_message(pcmCruiseGap=gap, buttonEvents=buttons))
  return sd.personality, log.OnroadEvent.EventName.personalityChanged in sd.events.names


def test_selfdrived_three_level_car_is_unchanged():
  sd = selfdrived_with()
  # the car level moves without a press: ignored, as before
  assert frame(sd, 3) == (1, False)
  assert frame(sd, 2) == (1, False)
  # each release steps one closer and wraps over three levels, an alert each time, the param written
  assert [frame(sd, 2, press=True) for _ in range(4)] == [(0, True), (2, True), (1, True), (0, True)]
  assert [v for k, v in sd.params.puts if k == "LongitudinalPersonality"] == [0, 2, 1, 0]
  assert FOUR_LEVEL_CAR_PARAM not in sd.params.values
  assert sd.gap_levels.levels == 3


def test_selfdrived_four_level_car_follows_the_car():
  sd = selfdrived_with()
  assert frame(sd, 4) == (3, True)  # first 4 seen: four levels, farthest like the car
  assert sd.params.values[FOUR_LEVEL_CAR_PARAM] == CAR
  assert frame(sd, 4, press=True) == (2, True)
  assert frame(sd, 3) == (2, False)  # the car follows the same press a few frames later: no second step
  assert frame(sd, 2) == (1, True)  # the car moved without a comma press: follow it
  assert frame(sd, 0) == (1, False)  # stock SCC off: keep
  assert [frame(sd, 0, press=True)[0] for _ in range(2)] == [0, 3]  # button alone over four levels


def test_selfdrived_stock_longitudinal_does_nothing():
  sd = selfdrived_with(op_long=False)
  assert frame(sd, 4, press=True) == (1, False)
  assert sd.gap_levels.levels == 3
