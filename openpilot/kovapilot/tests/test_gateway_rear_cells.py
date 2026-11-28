"""KOVA: cars in the stock rear cells (carState.sideCells) in the KovaLink telemetry `objects` (radar_objects.py).

janpoo 2026-11-28 "4. 응": sent only while the cells are trusted (stock HDA on, frames fresh); approximate placement.
KOVA: 뒤 칸 차를 로드뷰 objects에 더함(믿을 때만, 자리는 어림)."""
import json
from types import SimpleNamespace

import pytest

from openpilot.kovapilot.gateway.radar_objects import (REAR_CELL_EGO_LENGTH, REAR_CELL_IDS, REAR_CELL_LEFT,
                                                       REAR_CELL_VEHICLE_LENGTH, LANE_LEFT, LANE_RIGHT, RadarObjects,
                                                       rear_cell_objects)
from openpilot.kovapilot.tests.test_gateway_radar_objects import NOW, _point, _run, _sm


def _cell(detected=False, distance=25.0):
  return SimpleNamespace(detected=detected, distance=distance)


def _cells(available=True, fresh=True, hda=2, left=None, right=None):
  return SimpleNamespace(available=available, fresh=fresh, hdaIcon=hda, leftRear=left or _cell(), rightRear=right or _cell(),
                         leftFront=_cell(True, 3.0), rightFront=_cell(True, 3.0))


def _with_cells(sm, cells):
  sm["carState"].sideCells = cells
  return sm


def test_rear_left_car_is_sent_approximate():
  sm = _with_cells(_sm([]), _cells(left=_cell(True, 2.0)))
  objects = _run(RadarObjects(), sm)["objects"]
  assert objects == [{"id": REAR_CELL_IDS[0], "kind": "vehicle",
                      "distanceMeters": round(-(REAR_CELL_EGO_LENGTH + 2.0 + REAR_CELL_VEHICLE_LENGTH), 2),
                      "leftMeters": REAR_CELL_LEFT, "relativeSpeedMps": 0.0, "lengthMeters": REAR_CELL_VEHICLE_LENGTH,
                      "lane": LANE_LEFT, "isLead": False, "approximate": True}]
  json.dumps(objects)


def test_both_sides_and_radar_together_sorted():
  sm = _with_cells(_sm([_point(5, 12.3, 3.8, -1.25)]), _cells(left=_cell(True, 10.0), right=_cell(True, 0.5)))
  objects = _run(RadarObjects(), sm)["objects"]
  assert [o["id"] for o in objects] == [REAR_CELL_IDS[0], REAR_CELL_IDS[1], 5]
  assert objects[1]["lane"] == LANE_RIGHT and objects[1]["leftMeters"] == -REAR_CELL_LEFT
  assert [o["distanceMeters"] for o in objects] == sorted(o["distanceMeters"] for o in objects)


def test_front_cells_are_not_sent():
  sm = _with_cells(_sm([]), _cells())
  assert _run(RadarObjects(), sm)["objects"] == []


@pytest.mark.parametrize("cells", [_cells(hda=0, left=_cell(True, 2.0)), _cells(fresh=False, left=_cell(True, 2.0)),
                                   _cells(available=False, left=_cell(True, 2.0))])
def test_untrusted_cells_change_nothing(cells):
  points = [_point(5, 12.3, 3.8, -1.25), _point(9, 25., -3.7, 2.)]
  assert _run(RadarObjects(), _with_cells(_sm(points), cells)) == _run(RadarObjects(), _sm(points))
  assert _run(RadarObjects(), _with_cells(_sm(points, radar_fresh=False), cells)) == _run(RadarObjects(), _sm(points, radar_fresh=False))
  assert "objects" not in _run(RadarObjects(), _with_cells(_sm(points, radar_fresh=False), cells))


def test_without_radar_rear_cells_alone():
  sm = _with_cells(_sm([_point(5, 12.3, 3.8, -1.25)], radar_fresh=False), _cells(right=_cell(True, 4.0)))
  objects = _run(RadarObjects(), sm)["objects"]
  assert [o["id"] for o in objects] == [REAR_CELL_IDS[1]]


def test_stale_car_state_sends_no_rear_cells():
  sm = _with_cells(_sm([]), _cells(left=_cell(True, 2.0)))
  sm.alive = dict(sm.alive, carState=False)
  assert rear_cell_objects(sm, NOW) == []


def test_bad_distance_skipped():
  sm = _with_cells(_sm([]), _cells(left=_cell(True, float("nan")), right=_cell(True, -1.0)))
  assert rear_cell_objects(sm, NOW) == []
