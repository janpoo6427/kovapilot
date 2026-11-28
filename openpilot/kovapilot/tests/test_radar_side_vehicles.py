"""화면 주변 차량: 레이더를 쓰는 차는 옆 차로의 레이더 물체를 앞차 막대처럼 도로 위에 그린다(차선 변경·끼어들기와 같은 기준).

Onroad side vehicles: cars whose radar is used draw the side-lane radar objects on the road like the lead bars
(same checks as the radar lane change block and the radar cut-in).
"""
import importlib.util
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from openpilot.kovapilot.display import radar_vehicles as rv
from openpilot.kovapilot.display.radar_vehicles import RadarSideVehicles, radar_used
from openpilot.kovapilot.lateral.auto_lane_change import RADAR_HOLD_FRAMES, RADAR_STATIONARY_SPEED
from openpilot.kovapilot.longitudinal.radar_cut_in import CUT_IN_MAX_DIST
from openpilot.kovapilot.tests.test_auto_lane_change import LANE_WIDTH, model
from openpilot.kovapilot.tests.test_radar_lane_change import point, tracks
from openpilot.selfdrive.ui.mici.onroad import model_renderer as mr
from openpilot.selfdrive.ui.ui_state import UIStatus

V_EGO = 20.0
LEFT_Y, RIGHT_Y = LANE_WIDTH, -LANE_WIDTH  # radar yRel grows to the left


class FakeSM(dict):
  def __init__(self, data, alive=True, updated=True):
    super().__init__(data)
    self.alive = dict.fromkeys(data, alive)
    self.updated = dict.fromkeys(data, updated)


def sm_with(points, alive=True, updated=True, **errors):
  return FakeSM({"radarTracks": tracks(points, **errors)}, alive=alive, updated=updated)


def run(src, points, n=RADAR_HOLD_FRAMES, v_ego=V_EGO, has_radar=True, lead_ids=(), m=None, **kw):
  out = []
  for _ in range(n):
    out = src.update(sm_with(points, **kw), m or model(), v_ego, has_radar, lead_ids)
  return out


def test_side_lanes_only():
  pts = [point(1, 20.0, LEFT_Y, 0.0), point(2, 20.0, 0.0, 0.0), point(3, 20.0, RIGHT_Y, 0.0), point(4, 20.0, 2 * LANE_WIDTH + 0.5, 0.0)]
  got = run(RadarSideVehicles(), pts)
  assert sorted((v.track_id, v.side) for v in got) == [(1, 0), (3, 1)]  # own lane and two lanes over are not drawn here
  left = next(v for v in got if v.track_id == 1)
  assert left.d_rel == pytest.approx(20.0) and left.y_rel == pytest.approx(LEFT_Y)


def test_needs_hold_frames_and_drops_missing_track():
  src = RadarSideVehicles()
  pts = [point(1, 20.0, LEFT_Y, 0.0)]
  assert run(src, pts, n=RADAR_HOLD_FRAMES - 1) == []
  assert [v.track_id for v in run(src, pts, n=1)] == [1]
  # a message without the track drops it (radard), it must build up again
  assert run(src, [], n=1) == []
  assert run(src, pts, n=RADAR_HOLD_FRAMES - 1) == []
  assert [v.track_id for v in run(src, pts, n=1)] == [1]


def test_counts_messages_not_ui_frames():
  src = RadarSideVehicles()
  pts = [point(1, 20.0, LEFT_Y, 0.0)]
  run(src, pts, n=1)
  # the UI draws several frames per radar message; frames without a new message do not count
  assert run(src, pts, n=10, updated=False) == []
  assert run(src, pts, n=RADAR_HOLD_FRAMES - 1) != []


@pytest.mark.parametrize("v_rel, drawn", [(-V_EGO, False), (-V_EGO + RADAR_STATIONARY_SPEED - 0.1, False),
                                          (-V_EGO + RADAR_STATIONARY_SPEED + 0.1, True), (0.0, True), (5.0, True)])
def test_moving_traffic_only(v_rel, drawn):
  assert bool(run(RadarSideVehicles(), [point(1, 20.0, LEFT_Y, v_rel)])) == drawn


@pytest.mark.parametrize("d_rel, drawn", [(0.5, True), (CUT_IN_MAX_DIST, True), (CUT_IN_MAX_DIST + 0.5, False), (80.0, False)])
def test_distance(d_rel, drawn):
  assert bool(run(RadarSideVehicles(), [point(1, d_rel, RIGHT_Y, 0.0)])) == drawn


@pytest.mark.parametrize("errors", [{"canError": True}, {"radarFault": True}, {"wrongConfig": True}, {"radarUnavailableTemporary": True}])
def test_radar_errors_draw_nothing(errors):
  src = RadarSideVehicles()
  pts = [point(1, 20.0, LEFT_Y, 0.0)]
  assert run(src, pts) != []
  assert run(src, pts, n=1, **errors) == []
  assert run(src, pts, n=RADAR_HOLD_FRAMES - 1) == []  # starts counting again


def test_stale_or_no_radar_draws_nothing():
  pts = [point(1, 20.0, LEFT_Y, 0.0)]
  assert run(RadarSideVehicles(), pts, alive=False) == []
  assert run(RadarSideVehicles(), pts, has_radar=False) == []
  assert RadarSideVehicles().update(FakeSM({}), model(), V_EGO, True) == []


def test_radar_lead_not_drawn_twice():
  pts = [point(1, 20.0, LEFT_Y, 0.0), point(3, 25.0, RIGHT_Y, 0.0)]
  assert [v.track_id for v in run(RadarSideVehicles(), pts, lead_ids={1})] == [3]


def test_lane_from_model_lines():
  # the car 1 m right of its lane centre: the left-lane object is 1 m farther left of the car, still the left lane
  got = run(RadarSideVehicles(), [point(1, 20.0, LEFT_Y + 1.0, 0.0)], m=model(1.0))
  assert [(v.track_id, v.side) for v in got] == [(1, 0)]


def test_radar_used():
  assert radar_used(SimpleNamespace(radarUnavailable=False))
  assert not radar_used(SimpleNamespace(radarUnavailable=True))
  assert not radar_used(None)


def test_ui_subscribes_radar_tracks():
  src = open(importlib.util.find_spec("openpilot.selfdrive.ui.ui_state").origin).read()
  assert "\"radarTracks\"," in src
  assert "RadarSideVehicles" in open(mr.__file__).read()
  assert rv.RadarSideVehicles is mr.RadarSideVehicles


# --- mici road view ---------------------------------------------------------------------------------------------

def lane(y):
  x = np.linspace(0.0, 100.0, 33)
  return SimpleNamespace(x=x, y=np.full_like(x, y), z=np.zeros_like(x))


def renderer_sm(points, radar_unavailable=False, source="cruise", lead_one=None, enabled=True):
  none = SimpleNamespace(present=False, radar=False, radarTrackId=-1, dRel=0.0, yRel=0.0)
  lead_one = lead_one or none
  sm = FakeSM({
    "radarTracks": tracks(points),
    "carParams": SimpleNamespace(radarUnavailable=radar_unavailable, openpilotLongitudinalControl=True),
    "radarState": SimpleNamespace(leadOne=lead_one, leadTwo=none),
    "modelV2": SimpleNamespace(laneLines=model().laneLines, laneLineProbs=model().laneLineProbs, leadsV3=[], velocity=SimpleNamespace(x=[]),
                               position=SimpleNamespace(x=[])),
    "selfdriveState": SimpleNamespace(enabled=enabled, engageable=enabled),
    "carState": SimpleNamespace(vEgo=V_EGO, brakePressed=False),
    "longitudinalPlan": SimpleNamespace(longitudinalPlanSource=source),
  })
  return sm


class FakeFade:
  def __init__(self, status=UIStatus.ENGAGED):
    self.status = status

  def weight(self, status):
    return 1.0 if status == self.status else 0.0


def make_renderer():
  r = mr.ModelRenderer()
  r._path.raw_points = np.column_stack((np.linspace(0.0, 100.0, 33), np.zeros(33), np.zeros(33)))
  # a simple pinhole looking down the road (x forward, y right, z down), 536x240
  r.set_transform(np.array([[0.0, 300.0, 0.0], [0.0, 0.0, 300.0], [1.0, 0.0, 0.0]]) + np.array([[268.0, 0, 0], [120.0, 0, 0], [0, 0, 0]]))
  r._path_offset_z = 1.22
  return r


def side_frames(r, sm, n=RADAR_HOLD_FRAMES + 120, status=UIStatus.ENGAGED):
  with patch.object(mr, "status_fade", FakeFade(status)):
    for i in range(n):
      sm.updated = dict.fromkeys(sm, i % 3 == 0)  # radar at 20 Hz, UI at 60 Hz
      r._update_side_vehicles(sm)


def draws(r):
  calls = []
  with patch.object(mr, "draw_polygon", side_effect=lambda rect, pts, color=None, **kw: calls.append((pts.copy(), color))):
    r._draw_side_vehicles()
  return calls


def test_renderer_draws_side_vehicles_left_and_right():
  r = make_renderer()
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0), point(2, 30.0, RIGHT_Y, 0.0), point(3, 20.0, 0.0, 0.0)])
  side_frames(r, sm)
  calls = draws(r)
  assert len(calls) == 2
  xs = sorted(float(pts[:, 0].mean()) for pts, _ in calls)
  assert xs[0] < 268.0 < xs[1]  # one left of the centre, one right
  for _, color in calls:
    assert 0 < color.a < 255 * 0.8  # dimmer than a lead bar


def test_renderer_no_radar_car_unchanged():
  r = make_renderer()
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0)], radar_unavailable=True)
  side_frames(r, sm)
  assert draws(r) == []


def test_renderer_fades_out_lost_track():
  r = make_renderer()
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0)])
  side_frames(r, sm)
  assert len(draws(r)) == 1
  sm["radarTracks"] = tracks([])
  side_frames(r, sm, n=300)
  assert draws(r) == []


def test_renderer_skips_radar_lead():
  r = make_renderer()
  lead = SimpleNamespace(present=True, radar=True, radarTrackId=1, dRel=20.0, yRel=LEFT_Y)
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0), point(2, 30.0, RIGHT_Y, 0.0)], lead_one=lead)
  with patch.object(mr, "status_fade", FakeFade()):
    r._update_lead_bars(sm)
  side_frames(r, sm)
  assert len(draws(r)) == 1


def test_renderer_hidden_when_unavailable():
  r = make_renderer()
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0)], enabled=False)
  side_frames(r, sm)
  assert draws(r) == []


def test_renderer_lane_test_on_new_data_only():
  r = make_renderer()
  sm = renderer_sm([point(1, 20.0, LEFT_Y, 0.0)])
  calls = []
  orig = r._side_vehicles.update

  def counted(*args, **kw):
    calls.append(1)
    return orig(*args, **kw)

  with patch.object(r._side_vehicles, "update", side_effect=counted):
    side_frames(r, sm, n=30)
  assert len(calls) == 10  # one in three UI frames has a new radar or model message
  assert len(draws(r)) == 1
