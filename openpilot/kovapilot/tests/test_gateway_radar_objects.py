"""Radar vehicles in the KovaLink telemetry frame (`objects`, kovapilot/gateway/radar_objects.py)."""

import math
from types import SimpleNamespace

from openpilot.kovapilot.gateway.radar_objects import LANE_EGO, LANE_LEFT, LANE_RIGHT, RadarObjects, point_lane
from openpilot.kovapilot.gateway.telemetry import road_scene
from openpilot.kovapilot.lateral.auto_lane_change import RADAR_HOLD_FRAMES, RADAR_STATIONARY_SPEED
from openpilot.kovapilot.longitudinal.radar_cut_in import CUT_IN_MAX_DIST

NOW = 1_000_000_001
V_EGO = 20.


def _line(y0, n=33):
  xs = [192 * (i / 32) ** 2 for i in range(n)]
  return SimpleNamespace(x=xs, y=[y0] * n, z=[1.2] * n)


def _model(lines=True):
  # modelV2 y points right: current lane lines at 1.8 m left and 1.7 m right, a 3.5 m lane
  return SimpleNamespace(frameId=7, position=_line(0.), laneLines=[_line(-5.3), _line(-1.8), _line(1.7), _line(5.2)] if lines else [],
                         laneLineProbs=[0.6, 0.9, 0.9, 0.6], roadEdges=[], roadEdgeStds=[])


def _point(track_id, d_rel, y_rel, v_rel=0.):
  return SimpleNamespace(trackId=track_id, dRel=d_rel, yRel=y_rel, vRel=v_rel, measured=True)


def _errors(**bad):
  return SimpleNamespace(**({"canError": False, "radarFault": False, "wrongConfig": False, "radarUnavailableTemporary": False} | bad))


def _lead(track_id=-1, present=True, radar=True):
  return SimpleNamespace(present=present, radar=radar, radarTrackId=track_id, dRel=30., yRel=0., vRel=0.)


def _sm(points, model=None, lead_one=None, lead_two=None, errors=None, radar_fresh=True, updated=True, v_ego=V_EGO):
  services = ("carState", "selfdriveState", "modelV2", "radarTracks", "radarState")

  class Sample(dict):
    valid = dict.fromkeys(services, True)
    alive = dict.fromkeys(services, True)
    logMonoTime = dict.fromkeys(services, 1_000_000_000)

  sample = Sample(
    carState=SimpleNamespace(vEgo=v_ego, steeringAngleDeg=0., vCruiseCluster=100., gearShifter="drive", leftBlinker=False,
                             rightBlinker=False, brakePressed=False, leftBlindspot=False, rightBlindspot=False),
    selfdriveState=SimpleNamespace(enabled=True),
    modelV2=model if model is not None else _model(),
    radarTracks=SimpleNamespace(points=points, errors=errors or _errors()),
    radarState=SimpleNamespace(leadOne=lead_one or _lead(present=False), leadTwo=lead_two or _lead(present=False)),
  )
  sample.updated = dict.fromkeys(services, updated)
  if not radar_fresh:
    sample.alive = dict(Sample.alive, radarTracks=False)
  return sample


def _run(tracker, sm, times=RADAR_HOLD_FRAMES):
  scene = None
  for _ in range(times):
    scene = road_scene(sm, NOW, None, tracker)
  return scene


def test_moving_side_lane_vehicles_are_sent_with_lane_and_app_coordinates():
  # 2 m past the left line (radar yRel left positive), and 2 m past the right line
  sm = _sm([_point(5, 12.3, 3.8, -1.25), _point(9, 25., -3.7, 2.)])
  objects = _run(RadarObjects(), sm)["objects"]
  assert objects == [
    {"id": 5, "kind": "vehicle", "distanceMeters": 12.3, "leftMeters": 3.8, "relativeSpeedMps": -1.25, "lane": LANE_LEFT,
     "moving": True, "isLead": False},
    {"id": 9, "kind": "vehicle", "distanceMeters": 25., "leftMeters": -3.7, "relativeSpeedMps": 2., "lane": LANE_RIGHT,
     "moving": True, "isLead": False},
  ]


def test_a_track_counts_after_radar_hold_frames_messages_in_a_row():
  tracker = RadarObjects()
  sm = _sm([_point(5, 12., 3.8)])
  for _ in range(RADAR_HOLD_FRAMES - 1):
    assert road_scene(sm, NOW, None, tracker)["objects"] == []
  assert [o["id"] for o in road_scene(sm, NOW, None, tracker)["objects"]] == [5]
  # a message without the track drops it, it has to be seen again
  road_scene(_sm([]), NOW, None, tracker)
  assert road_scene(sm, NOW, None, tracker)["objects"] == []


def test_a_message_read_twice_is_counted_once():
  tracker = RadarObjects()
  road_scene(_sm([_point(5, 12., 3.8)]), NOW, None, tracker)
  for _ in range(5):
    assert road_scene(_sm([_point(5, 12., 3.8)], updated=False), NOW, None, tracker)["objects"] == []


def test_standing_side_objects_and_far_ones_are_not_sent():
  standing = -V_EGO + 0.9 * RADAR_STATIONARY_SPEED  # 1.8 m/s over the ground
  sm = _sm([_point(1, 12., 3.8, standing), _point(2, CUT_IN_MAX_DIST + 0.5, 3.8), _point(3, CUT_IN_MAX_DIST, 3.8)])
  assert [o["id"] for o in _run(RadarObjects(), sm)["objects"]] == [3]


def test_own_lane_vehicle_between_the_lane_lines():
  sm = _sm([_point(4, 18., 0.3, -2.)])
  assert _run(RadarObjects(), sm)["objects"][0]["lane"] == LANE_EGO


def test_objects_on_a_lane_line_or_beyond_the_side_lane_are_not_sent():
  # 0.2 m past the left line (under RADAR_LANE_MIN) and far outside the lane on the right
  sm = _sm([_point(1, 15., 2.0), _point(2, 15., -1.7 - 3.5 - 1.2)])
  assert point_lane(_point(1, 15., 2.0), _model()) is None
  assert _run(RadarObjects(), sm)["objects"] == []


def test_radard_leads_are_sent_at_once_marked_and_also_when_standing_or_far():
  points = [_point(11, 60., 0.1, -V_EGO), _point(12, 75., -0.2, -V_EGO + 1.)]
  sm = _sm(points, lead_one=_lead(11), lead_two=_lead(12))
  objects = road_scene(sm, NOW, None, RadarObjects())["objects"]
  assert [(o["id"], o["lane"], o["isLead"], o["moving"]) for o in objects] == [(11, LANE_EGO, True, False), (12, LANE_EGO, False, False)]


def test_vision_only_leads_add_nothing():
  sm = _sm([_point(11, 60., 0.1, -V_EGO)], lead_one=_lead(11, radar=False))
  assert road_scene(sm, NOW, None, RadarObjects())["objects"] == []


def test_without_lane_lines_only_the_radar_leads_are_sent():
  sm = _sm([_point(11, 30., 0.1), _point(5, 12., 3.8)], model=_model(lines=False), lead_one=_lead(11))
  assert [o["id"] for o in _run(RadarObjects(), sm)["objects"]] == [11]


def test_cars_without_radar_send_an_empty_list():
  assert _run(RadarObjects(), _sm([]))["objects"] == []


def test_radar_errors_or_stale_tracks_leave_objects_out_and_restart_the_hold():
  tracker = RadarObjects()
  good = _sm([_point(5, 12., 3.8)])
  _run(tracker, good)
  for bad in (_sm([_point(5, 12., 3.8)], errors=_errors(radarFault=True)), _sm([_point(5, 12., 3.8)], errors=_errors(canError=True)),
              _sm([_point(5, 12., 3.8)], radar_fresh=False)):
    assert "objects" not in road_scene(bad, NOW, None, tracker)
  assert road_scene(good, NOW, None, tracker)["objects"] == []


def test_without_a_tracker_the_frame_is_unchanged():
  assert "objects" not in road_scene(_sm([_point(5, 12., 3.8)]), NOW, None)


def test_bad_values_are_skipped_not_raised():
  sm = _sm([_point(5, math.nan, 3.8), _point(6, 12., 3.8)])
  assert [o["id"] for o in _run(RadarObjects(), sm)["objects"]] == [6]

  class Broken(dict):
    valid = alive = {"carState": True, "radarTracks": True}
    logMonoTime = {"carState": 1_000_000_000, "radarTracks": 1_000_000_000}

  broken = Broken(carState=_sm([])["carState"])
  assert RadarObjects()(broken, NOW) is None


def test_the_cereal_telemetry_subscribes_the_radar():
  import inspect
  from openpilot.kovapilot.gateway import telemetry
  source = inspect.getsource(telemetry.CerealTelemetry.__init__)
  assert "\"radarTracks\"" in source and "\"radarState\"" in source and "RadarObjects()" in source
