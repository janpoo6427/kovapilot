"""KOVA: the stock rear cells (carState.sideCells, Hyundai CAN-FD CCNC_0x162) block a lane change like the blind spot.

janpoo 2026-11-28 23:35 / 11-29 01:07: rear cell within 3 m, or closing in to 3 m within 3 s, for 0.15 s; trusted only
while the stock HDA is on (HDA_ICON >= 1) and both frames are fresh; cars without the cells unchanged.

KOVA: 뒤 칸 차선 변경 막기(3m·3초·0.15초). 순정 HDA 켜짐(HDA_ICON>=1)·0.2초 신선할 때만 믿고, 칸 없는 차는 그대로."""
from types import SimpleNamespace

import pytest

from opendbc.car.structs import car
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.lateral.auto_lane_change import (MODE_AUTO, MODE_NUDGE, MODE_OFF, REAR_CELL_HOLD_FRAMES,
                                                         REAR_CELL_HORIZON, REAR_CELL_NEAR_DIST, REAR_CELL_RATE_FRAMES,
                                                         AutoLaneChange, AutoState, RearCellSource, rear_cell_blocks,
                                                         rear_cell_closing, side_cells_trusted)
from openpilot.kovapilot.tests.test_auto_lane_change import (LCD, LCS, LANE_WIDTH, DM, Drive, car_params, model,
                                                            random_world)
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper


def cell(detected=False, distance=25.0):
  return SimpleNamespace(detected=detected, distance=distance, status=1 if detected else 0, lateral=0.0)


def cells(available=True, fresh=True, hda=2, left=None, right=None):
  return SimpleNamespace(available=available, fresh=fresh, hdaIcon=hda, leftRear=left or cell(), rightRear=right or cell(),
                         leftFront=cell(), rightFront=cell())


def test_constants():
  assert (REAR_CELL_NEAR_DIST, REAR_CELL_HORIZON, REAR_CELL_HOLD_FRAMES) == (3.0, 3.0, 3)
  assert REAR_CELL_HOLD_FRAMES * DT_MDL == pytest.approx(0.15)
  assert REAR_CELL_RATE_FRAMES * DT_MDL == pytest.approx(0.5)


def test_schema_field():
  cs = car.CarState.new_message()
  assert not cs.sideCells.available and not cs.sideCells.fresh and cs.sideCells.hdaIcon == 0
  assert not cs.sideCells.leftRear.detected and not cs.sideCells.rightRear.detected
  assert not side_cells_trusted(cs)


@pytest.mark.parametrize("available,fresh,hda,trusted", [
  (True, True, 1, True), (True, True, 2, True), (True, True, 3, True),
  (True, True, 0, False),   # stock HDA off: the camera leaves the cells empty
  (True, False, 2, False),  # a frame older than 0.2 s
  (False, True, 2, False),  # no flag
])
def test_trusted(available, fresh, hda, trusted):
  assert side_cells_trusted(SimpleNamespace(sideCells=cells(available, fresh, hda))) == trusted


def test_trusted_without_field():
  assert not side_cells_trusted(SimpleNamespace(vEgo=10.0))


def test_near_dist():
  assert rear_cell_blocks([3.0])
  assert rear_cell_blocks([0.5])
  assert not rear_cell_blocks([3.1])
  assert not rear_cell_blocks([])


def test_closing_needs_the_full_window():
  approaching = [12.0 - 0.2 * i for i in range(REAR_CELL_RATE_FRAMES)]  # 4 m/s, one frame short
  assert rear_cell_closing(approaching) == 0.0 and not rear_cell_blocks(approaching)
  approaching.append(approaching[-1] - 0.2)
  assert rear_cell_closing(approaching) == pytest.approx(4.0)
  assert rear_cell_blocks(approaching)  # 10.0 m - 4 m/s * 3 s < 3 m


def test_three_second_horizon():
  def window(end, speed):
    return [end + speed * DT_MDL * (REAR_CELL_RATE_FRAMES - i) for i in range(REAR_CELL_RATE_FRAMES + 1)]
  assert rear_cell_blocks(window(15.0, 4.0))  # reaches 3 m in exactly 3 s
  assert not rear_cell_blocks(window(15.2, 4.0))  # 3.05 s
  assert not rear_cell_blocks(window(10.0, -2.0))  # falling back
  assert not rear_cell_blocks(window(25.0, 0.0))  # the 25.0 m end value, no closing


def run_source(frames):
  src = RearCellSource()
  out = []
  for c in frames:
    src.update(SimpleNamespace(sideCells=c))
    out.append((src.available, src.occupied[0], src.occupied[1]))
  return src, out


def test_hold_frames():
  near = cells(left=cell(True, 2.0))
  _, out = run_source([near] * 5)
  assert [o[1] for o in out] == [False, False, True, True, True]
  assert not any(o[2] for o in out)
  # one empty frame resets the hold
  _, out = run_source([near, near, cells(), near, near, near])
  assert [o[1] for o in out] == [False, False, False, False, False, True]


def test_closing_car_occupied_after_0_65_s():
  frames = [cells(right=cell(True, 15.0 - 0.3 * i)) for i in range(20)]  # 6 m/s from 15 m
  _, out = run_source(frames)
  first = [o[2] for o in out].index(True)
  assert first == REAR_CELL_RATE_FRAMES + REAR_CELL_HOLD_FRAMES - 1  # frame 13 (0.65 s)
  assert not any(o[1] for o in out)


@pytest.mark.parametrize("bad", [cells(hda=0, left=cell(True, 1.0)), cells(fresh=False, left=cell(True, 1.0)),
                                 cells(available=False, left=cell(True, 1.0))])
def test_untrusted_never_blocks(bad):
  src, out = run_source([bad] * 10)
  assert not any(o[0] or o[1] or o[2] for o in out)
  assert not src.report(None, 0).available


def test_losing_trust_clears():
  near = cells(left=cell(True, 2.0))
  src, out = run_source([near] * 4 + [cells(hda=0, left=cell(True, 2.0))])
  assert out[3][1] and not out[4][1] and not src.available


def test_empty_cell_is_not_a_clear_side():
  src, _ = run_source([cells()] * 30)
  report = src.report(None, 0)
  assert report.available and not report.occupied and report.ready  # follows the other sources, adds no wait


class CellDrive(Drive):
  def __init__(self, side_cells=None, **kw):
    super().__init__(**kw)
    self.cs.sideCells = side_cells if side_cells is not None else cells()


def test_rear_cell_blocks_the_automatic_start():
  d = CellDrive(cells(left=cell(True, 2.0)))
  d.blinker("left")
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.rear_cell_occupied(LCD.left) and not d.alc.rear_cell_occupied(LCD.right)
  assert d.alc.lane_change_blocked(d.dh.lane_change_state, d.dh.lane_change_direction)  # the stock blocked alert
  d.cs.sideCells = cells()  # the car has passed
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting  # no extra wait from the cells


def test_rear_cell_other_side_does_not_block():
  d = CellDrive(cells(right=cell(True, 1.0)))
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_rear_cell_blocks_the_nudge():
  d = CellDrive(cells(left=cell(True, 2.0)), mode=MODE_NUDGE)
  d.blinker("left")
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.seconds(2.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  d.cs.sideCells = cells()
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_off_mode_unchanged():
  d = CellDrive(cells(left=cell(True, 2.0)), mode=MODE_OFF)
  d.blinker("left")
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.seconds(2.0)
  assert d.dh.lane_change_state == LCS.off


@pytest.mark.parametrize("side_cells", [cells(hda=0, left=cell(True, 2.0)), cells(fresh=False, left=cell(True, 2.0))])
def test_hda_off_or_stale_starts_as_before(side_cells):
  d = CellDrive(side_cells)
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_rear_cell_before_crossing_returns():
  d = CellDrive()
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.lane_change_prob = 0.9
  d.offset = -0.8
  d.step()
  d.cs.sideCells = cells(left=cell(True, 2.0))
  d.step(REAR_CELL_HOLD_FRAMES - 1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.step()
  assert d.dh.lane_change_state == LCS.off and d.alc.returning and d.alc.state == AutoState.returning


def test_rear_cell_after_crossing_finishes():
  d = CellDrive()
  d.blinker("left")
  d.seconds(1.1)
  d.lane_change_prob = 0.9
  for offset in (-0.6, -1.2, -1.7):
    d.offset = offset
    d.step()
  d.offset = LANE_WIDTH - 1.9
  d.step()
  assert d.alc.crossed
  d.cs.sideCells = cells(left=cell(True, 2.0))
  d.seconds(0.5)
  assert d.dh.lane_change_state == LCS.laneChangeStarting and not d.alc.returning


def run_world(rows, alc, side_cells_for):
  dh = DesireHelper()
  out = []
  for i, (cs, lat, prob, engaged, offset) in enumerate(rows):
    cs = SimpleNamespace(**vars(cs))
    c = side_cells_for(i)
    if c is not None:
      cs.sideCells = c
    sm = {"carState": cs, "carControl": SimpleNamespace(enabled=engaged, latActive=lat),
          "driverMonitoringState": SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)}
    alc.update(sm, model(offset), dh.lane_change_state, prob)
    dh.update(cs, lat, prob, alc)
    out.append((dh.lane_change_state, dh.lane_change_direction, alc.publish_state(dh.lane_change_state), alc.returning,
                alc.lane_change_blocked(dh.lane_change_state, dh.lane_change_direction),
                alc.rear_cell_occupied(dh.lane_change_direction)))
  return out


def random_cells(seed, trusted):
  import random
  rng = random.Random(seed + 100)
  state = {"c": cells()}

  def at(_):
    if rng.random() < 0.05:
      left = cell(rng.random() < 0.5, rng.uniform(0.0, 25.0))
      right = cell(rng.random() < 0.5, rng.uniform(0.0, 25.0))
      if trusted:
        state["c"] = cells(left=left, right=right)
      else:
        state["c"] = rng.choice([cells(hda=0, left=left, right=right), cells(fresh=False, left=left, right=right),
                                 cells(available=False, left=left, right=right)])
    return state["c"]
  return at


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("mode", [MODE_NUDGE, MODE_AUTO])
def test_untrusted_or_empty_cells_change_nothing(seed, mode):
  # HDA off, stale or no flag (any cell values), or trusted but empty cells: the same as a car without the cells
  rows = random_world(seed)
  base = run_world(rows, AutoLaneChange(car_params(), mode=mode), lambda i: None)
  assert run_world(rows, AutoLaneChange(car_params(), mode=mode), random_cells(seed, trusted=False)) == base
  assert run_world(rows, AutoLaneChange(car_params(), mode=mode), lambda i: cells()) == base


@pytest.mark.parametrize("seed", range(3))
def test_trusted_cells_only_add_blocks(seed):
  # with trusted cells the decisions change, and no lane change starts in a frame where its rear cell blocks
  rows = random_world(seed)
  base = run_world(rows, AutoLaneChange(car_params(), mode=MODE_AUTO), lambda i: None)
  new = run_world(rows, AutoLaneChange(car_params(), mode=MODE_AUTO), random_cells(seed, trusted=True))
  assert new != base
  for i in range(1, len(rows)):
    if new[i][0] == LCS.laneChangeStarting and new[i - 1][0] == LCS.preLaneChange:
      assert not new[i][5]
  assert any(o[5] for o in new)
