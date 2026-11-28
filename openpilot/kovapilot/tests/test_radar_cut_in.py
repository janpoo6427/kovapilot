"""Radar tracks entering our path from a side lane lead before the model reports them (kovapilot/longitudinal/radar_cut_in.py,
selfdrive/controls/radard.py get_lead)."""
from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.cereal import log, messaging
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.longitudinal import radar_cut_in as rci
from openpilot.kovapilot.longitudinal.radar_cut_in import (CUT_IN_HOLD, CUT_IN_MAX_DIST, CUT_IN_MIN_EGO_SPEED, CUT_IN_WINDOW,
                                                          HALF_WIDTH_DEFAULT, RadarCutIn, path_half_width)
from openpilot.selfdrive.controls import radard
from openpilot.selfdrive.controls.radard import KalmanParams, RadarD, Track, get_lead

LCS = log.LaneChangeState
LANE = 3.5
HALF_WIDTH_DEFAULT_LINES = 1.75  # half of the 3.5 m lane in _model_msg
XS = np.linspace(0.0, 100.0, 33)


def model(curvature=0.0, lane_prob=0.9, lane_width=LANE, lane_change=LCS.off, lead=None):
  ys = 0.5 * curvature * XS ** 2
  lines = [SimpleNamespace(x=list(XS), y=list(ys + o)) for o in (-1.5 * lane_width, -0.5 * lane_width, 0.5 * lane_width, 1.5 * lane_width)]
  leads = [lead or SimpleNamespace(prob=0.0, x=[0.0], y=[0.0], v=[0.0], a=[0.0], xStd=[1.0], yStd=[1.0], vStd=[1.0])] * 3
  return SimpleNamespace(position=SimpleNamespace(x=list(XS), y=list(ys)), laneLines=lines,
                         laneLineProbs=[0.5, lane_prob, lane_prob, 0.5], meta=SimpleNamespace(laneChangeState=lane_change),
                         leadsV3=leads, velocity=SimpleNamespace(x=[]))


def errors(**kw):
  err = {"canError": False, "radarFault": False, "wrongConfig": False, "radarUnavailableTemporary": False}
  err.update(kw)
  return SimpleNamespace(**err)


def path_y(md, d_rel):
  """radar yRel (left +) of the plan at this radar distance"""
  return -float(np.interp(d_rel + rci.RADAR_TO_CAMERA, md.position.x, md.position.y))


class World:
  """One radar track per entry: (id, dRel, offset from the plan to the right (m), vRel); ego v_ego."""
  def __init__(self, v_ego=20.0, **model_kw):
    self.v_ego = v_ego
    self.model_kw = model_kw
    self.cut_in = RadarCutIn()
    self.tracks = {}
    self.t = 0.0
    self.errors = errors()

  def step(self, objects, **model_kw):
    md = model(**{**self.model_kw, **model_kw})
    for i in list(self.tracks):
      if i not in [o[0] for o in objects]:
        self.tracks.pop(i)
    for i, d, offset, v_rel in objects:
      track = self.tracks.setdefault(i, Track(i, self.v_ego + v_rel, KalmanParams(DT_MDL)))
      track.update(d, path_y(md, d) - offset, v_rel, self.v_ego + v_rel)
    self.t += DT_MDL
    return self.cut_in.update(self.t, self.tracks, md, self.v_ego, self.errors)


def entering(w, n, d=20.0, start=3.5, speed=-0.8, v_rel=-2.0, side=1.0, track_id=7, **model_kw):
  """Side-lane car moving towards our path at speed (m/s, negative = inward) for n frames; returns the selections."""
  out = []
  for k in range(n):
    offset = side * max(start + speed * DT_MDL * k, 0.0)
    out.append(w.step([(track_id, d + v_rel * DT_MDL * k, offset, v_rel)], **model_kw))
  return out


def first_selection(out):
  return next((k for k, s in enumerate(out) if s is not None), None)


@pytest.mark.parametrize("side", [1.0, -1.0])
def test_entering_side_car_is_selected_before_it_reaches_the_lane(side):
  w = World()
  out = entering(w, 60, side=side)
  k = first_selection(out)
  assert k is not None and out[k] == 7
  offset = 3.5 - 0.8 * DT_MDL * k
  assert offset > 1.75 - 0.01  # centre still outside our lane when it is selected
  assert all(s == 7 for s in out[k:])


def test_side_car_keeping_its_lane_is_never_selected():
  w = World()
  rng = np.random.default_rng(3)
  for k in range(400):
    assert w.step([(1, 15.0 + 5 * np.sin(k / 30), 3.5 + rng.normal(0.0, 0.1), -1.0), (2, 30.0, -3.4 + rng.normal(0.0, 0.1), 0.5)]) is None


def test_side_car_in_a_curve_is_not_an_entering_car():
  w = World(curvature=1 / 150)  # the side car follows the curve: its radar yRel changes, its offset from the plan does not
  for k in range(300):
    assert w.step([(1, 40.0 - 0.1 * k, 3.5, -2.0)]) is None


def test_slow_drift_below_the_inward_speed_is_ignored():
  assert first_selection(entering(World(), 200, speed=-0.2)) is None


def entering_through(w, n, d=20.0, start=6.0, speed=-5.0, v_rel=-2.0):
  # crosses our path and goes on to the other side (no stop at the path)
  return [w.step([(7, d, start + speed * DT_MDL * k, v_rel)]) for k in range(n)]


def test_lateral_speed_above_the_limit_is_ignored():
  assert first_selection(entering_through(World(), 50, speed=-5.0)) is None


def test_crossing_traffic_is_ignored():
  assert first_selection(entering_through(World(), 50, speed=-2.0, v_rel=-20.0)) is None


def test_lateral_speed_limit_is_the_only_reason_above():
  assert first_selection(entering_through(World(), 50, speed=-2.0)) is not None


@pytest.mark.parametrize("kw", [{"lane_change": LCS.preLaneChange}, {"lane_change": LCS.laneChangeStarting},
                                {"lane_change": LCS.laneChangeFinishing}])
def test_no_cut_in_while_a_lane_change_runs(kw):
  assert first_selection(entering(World(), 60, **kw)) is None


def test_lane_change_drops_a_selected_car():
  w = World()
  assert entering(w, 60)[-1] == 7
  assert w.step([(7, 18.0, 1.5, -2.0)], lane_change=LCS.laneChangeStarting) is None


@pytest.mark.parametrize("v_ego", [0.0, CUT_IN_MIN_EGO_SPEED - 0.1])
def test_no_cut_in_at_low_ego_speed(v_ego):
  w = World(v_ego=v_ego)
  assert first_selection(entering(w, 60, v_rel=0.0)) is None


def test_stationary_object_is_ignored():
  w = World(v_ego=20.0)
  assert first_selection(entering(w, 60, v_rel=-20.0, d=45.0)) is None


def test_far_object_is_ignored():
  assert first_selection(entering(World(), 60, d=CUT_IN_MAX_DIST + 5.0, v_rel=0.0)) is None


@pytest.mark.parametrize("err", ["canError", "radarFault", "wrongConfig", "radarUnavailableTemporary"])
def test_radar_errors_disable_and_reset(err):
  w = World()
  assert entering(w, 60)[-1] == 7
  w.errors = errors(**{err: True})
  assert w.step([(7, 18.0, 1.0, -2.0)]) is None
  assert w.cut_in.selected is None and not w.cut_in.history
  w.errors = None
  assert w.step([(7, 18.0, 1.0, -2.0)]) is None


def test_no_tracks_no_cut_in():
  w = World()
  assert w.step([]) is None


def test_selected_car_is_kept_inside_and_dropped_when_it_goes_back():
  w = World()
  assert entering(w, 70)[-1] == 7
  for _ in range(40):
    assert w.step([(7, 18.0, 0.3, -1.0)]) == 7  # now in our lane, no lateral motion
  for k in range(40):
    s = w.step([(7, 18.0, 0.3 + 0.1 * k, -1.0)])
  assert s is None


def test_lost_track_clears_the_selection():
  w = World()
  assert entering(w, 60)[-1] == 7
  assert w.step([(8, 40.0, 3.5, 0.0)]) is None
  assert 7 not in w.cut_in.history


def test_nearest_entering_car_wins():
  w = World()
  for k in range(60):
    s = w.step([(1, 30.0, 3.5 - 0.8 * DT_MDL * k, -1.0), (2, 15.0, -3.5 + 0.8 * DT_MDL * k, -1.0)])
  assert s == 2


def test_track_gap_restarts_the_history():
  w = World()
  entering(w, CUT_IN_WINDOW - 1)
  w.t += 0.5
  out = entering(w, CUT_IN_HOLD + 1, start=2.5)
  assert first_selection(out) is None


def test_half_width_from_lane_lines_or_default():
  assert path_half_width(model(lane_width=3.0), 20.0) == pytest.approx(1.5)
  assert path_half_width(model(lane_width=6.0), 20.0) == pytest.approx(2.25)
  assert path_half_width(model(lane_prob=0.2), 20.0) == HALF_WIDTH_DEFAULT


def _track(d, y, v_rel, v_ego=20.0, track_id=5):
  t = Track(track_id, v_ego + v_rel, KalmanParams(DT_MDL))
  t.update(d, y, v_rel, v_ego + v_rel)
  return t


def _vision_lead(prob, x, v):
  return SimpleNamespace(prob=prob, x=[x], y=[0.0], v=[v], a=[0.0], xStd=[1.0], yStd=[1.0], vStd=[1.0])


def test_get_lead_unchanged_without_cut_in():
  tracks = {1: _track(30.0, 0.0, -2.0, track_id=1), 2: _track(12.0, 3.0, -1.0, track_id=2)}
  lead = _vision_lead(0.9, 31.5, 18.0)
  assert get_lead(20.0, True, tracks, lead, 20.0, 0.9) == get_lead(20.0, True, tracks, lead, 20.0, 0.9, cut_in=None)
  assert get_lead(20.0, True, tracks, lead, 20.0, 0.9)["radarTrackId"] == 1


def test_get_lead_uses_a_nearer_cut_in_only():
  near = _track(12.0, 2.0, -1.0, track_id=2)
  tracks = {1: _track(30.0, 0.0, -2.0, track_id=1), 2: near}
  lead = _vision_lead(0.9, 31.5, 18.0)
  out = get_lead(20.0, True, tracks, lead, 20.0, 0.9, cut_in=near)
  assert out["radarTrackId"] == 2 and out["radar"] and out["modelProb"] == 0.0
  far = _track(40.0, 2.0, -1.0, track_id=3)
  assert get_lead(20.0, True, {**tracks, 3: far}, lead, 20.0, 0.9, cut_in=far)["radarTrackId"] == 1
  # no model lead at all: the cut-in car is the lead
  assert get_lead(20.0, True, tracks, _vision_lead(0.1, 50.0, 20.0), 20.0, 0.1, cut_in=near)["radarTrackId"] == 2


class FakeSM:
  def __init__(self):
    self.d = {}
    self.seen = {"modelV2": True}
    self.recv_frame = {"carState": 0}
    self.logMonoTime = {"modelV2": 0}

  def __getitem__(self, k):
    return self.d[k]

  def all_checks(self):
    return True


def _radar_data(points, **err):
  msg = messaging.new_message("radarTracks")
  rt = msg.radarTracks
  rt.init("points", len(points))
  for p, (i, d, y, v) in zip(rt.points, points, strict=True):
    p.trackId, p.dRel, p.yRel, p.vRel = i, d, y, v
  for k, v in err.items():
    setattr(rt.errors, k, v)
  return msg.as_reader().radarTracks


def _model_msg(lane_change="off", lead_prob=0.0, lead_x=60.0):
  msg = messaging.new_message("modelV2")
  md = msg.modelV2
  md.position.x = [float(x) for x in XS]
  md.position.y = [0.0] * len(XS)
  md.init("laneLines", 4)
  for line, o in zip(md.laneLines, (-5.25, -1.75, 1.75, 5.25), strict=True):
    line.x = [float(x) for x in XS]
    line.y = [o] * len(XS)
  md.laneLineProbs = [0.5, 0.9, 0.9, 0.5]
  md.meta.laneChangeState = lane_change
  md.init("leadsV3", 3)
  for lead in md.leadsV3:
    lead.prob = lead_prob
    lead.x, lead.y, lead.v, lead.a = [lead_x], [0.0], [18.0], [0.0]
    lead.xStd, lead.yStd, lead.vStd = [1.0], [1.0], [1.0]
  return msg.as_reader().modelV2


def _run_radard(frames, v_ego=20.0, lane_change="off", lead_prob=0.0, **err):
  rd = RadarD(0.1)
  sm = FakeSM()
  cs = messaging.new_message("carState")
  cs.carState.vEgo = v_ego
  sm.d["carState"] = cs.as_reader().carState
  out = []
  for k, points in enumerate(frames):
    sm.d["modelV2"] = _model_msg(lane_change, lead_prob)
    sm.logMonoTime["modelV2"] = int(k * DT_MDL * 1e9)
    sm.recv_frame["carState"] += 1
    rd.update(sm, _radar_data(points, **err))
    out.append(rd.radar_state.leadOne)
  return out


def _cut_in_frames(n=60):
  # left side car at 20 m moving in at 0.8 m/s (radar yRel left +)
  return [[(7, 20.0, 3.5 - 0.8 * DT_MDL * k, -2.0)] for k in range(n)]


def test_radard_leads_with_the_entering_car():
  leads = _run_radard(_cut_in_frames())
  k = next(i for i, ld in enumerate(leads) if ld.present)
  k_edge = int(np.ceil((3.5 - HALF_WIDTH_DEFAULT_LINES - rci.CUT_IN_LATERAL_MARGIN) / (0.8 * DT_MDL)))  # centre 0.5 m outside our lane
  assert k_edge <= k <= k_edge + CUT_IN_HOLD + 1
  assert leads[-1].radar and leads[-1].radarTrackId == 7 and leads[-1].modelProb == 0.0


@pytest.mark.parametrize("kw", [{"lane_change": "laneChangeStarting"}, {"v_ego": 2.0}, {"radarFault": True}, {"canError": True}])
def test_radard_without_cut_in_matches_stock(kw, monkeypatch):
  new = _run_radard(_cut_in_frames(), **kw)
  monkeypatch.setattr(radard.RadarCutIn, "update", lambda self, *a, **k: None)
  stock = _run_radard(_cut_in_frames(), **kw)
  assert [ld.to_dict() for ld in new] == [ld.to_dict() for ld in stock]


def test_radard_without_radar_tracks_matches_stock(monkeypatch):
  frames = [[] for _ in range(60)]
  new = _run_radard(frames, lead_prob=0.9)
  monkeypatch.setattr(radard.RadarCutIn, "update", lambda self, *a, **k: None)
  stock = _run_radard(frames, lead_prob=0.9)
  assert [ld.to_dict() for ld in new] == [ld.to_dict() for ld in stock]
  assert new[-1].present and not new[-1].radar
