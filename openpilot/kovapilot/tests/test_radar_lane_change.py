"""Side-lane radar objects (radarTracks) block lane changes like the blind-spot monitor (kovapilot/lateral/auto_lane_change.py)."""
import importlib.util
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.lateral import auto_lane_change as alc_mod
from openpilot.kovapilot.lateral.auto_lane_change import (BLINDSPOT_CLEAR_TIME_NO_RADAR, MODE_AUTO, MODE_NUDGE, RADAR_CLEAR_TIME,
                                                         RADAR_HOLD_FRAMES, RADAR_STATIONARY_SPEED, AutoLaneChange, AutoState,
                                                         RadarTrackSource, lane_change_blocked_alert, radar_object_blocks,
                                                         side_radar_objects)
from openpilot.kovapilot.tests.test_auto_lane_change import (LANE_WIDTH, car_params, model, random_world, run_sequence)
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper

LCS = log.LaneChangeState
LCD = log.LaneChangeDirection
DM = log.DriverMonitoringState
LEFT_LANE_Y = LANE_WIDTH  # radar yRel (left positive) of the left lane centre for a car at its lane centre
RIGHT_LANE_Y = -LANE_WIDTH


def radar_car(bsm=True, radar=True):
  cp = car_params(bsm)
  cp.radarUnavailable = not radar
  return cp


def point(track_id, d_rel, y_rel, v_rel):
  return SimpleNamespace(trackId=track_id, dRel=d_rel, yRel=y_rel, vRel=v_rel)


def tracks(points, **errors):
  err = {"canError": False, "radarFault": False, "wrongConfig": False, "radarUnavailableTemporary": False}
  err.update(errors)
  return SimpleNamespace(points=list(points), errors=SimpleNamespace(**err))


class RadarDrive:
  """DesireHelper + AutoLaneChange at the model rate with radar points (modeld order)."""

  def __init__(self, enabled=True, radar=True, bsm=True, mode=None):
    self.alc = AutoLaneChange(radar_car(bsm, radar), enabled=enabled, mode=mode)
    self.dh = DesireHelper()
    self.cs = SimpleNamespace(vEgo=25.0, leftBlinker=False, rightBlinker=False, steeringPressed=False, steeringTorque=0.0,
                              leftBlindspot=False, rightBlindspot=False, blindspotFresh=bsm, brakePressed=False)
    self.cc = SimpleNamespace(enabled=True, latActive=True)
    self.dm = SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)
    self.points = []
    self.radar_errors = {}
    self.send_radar = True
    self.offset = 0.0
    self.lane_change_prob = 0.0
    self.blocked = []

  def step(self, n=1):
    for _ in range(n):
      sm = {"carState": self.cs, "carControl": self.cc, "driverMonitoringState": self.dm}
      if self.send_radar:
        sm["radarTracks"] = tracks(self.points, **self.radar_errors)
      self.alc.update(sm, model(self.offset), self.dh.lane_change_state, self.lane_change_prob)
      self.dh.update(self.cs, self.cc.latActive, self.lane_change_prob, self.alc)
      self.alc.publish_state(self.dh.lane_change_state)
      self.blocked.append(self.alc.lane_change_blocked(self.dh.lane_change_state, self.dh.lane_change_direction))
    return self.dh.lane_change_state

  def seconds(self, t):
    return self.step(int(round(t / DT_MDL)))

  def blinker(self, side):
    self.cs.leftBlinker, self.cs.rightBlinker = side == "left", side == "right"

  def nudge(self, side):
    self.cs.steeringPressed, self.cs.steeringTorque = True, (1.0 if side == "left" else -1.0)


@pytest.mark.parametrize("d_rel,v_rel,blocks", [
  (0.0, 5.0, True),  # beside the car, even when faster
  (2.9, 10.0, True),
  (3.1, 0.0, False),
  (3.5, 2.6, False),  # 79f-55: the driver changed lanes behind it
  (10.0, -2.0, False),  # 79f-24: 10 m closing at 2 m/s, 4 m left after 3 s
  (8.0, -2.0, True),
  (30.0, -7.6, False),  # 79f-72 slow traffic: from about 26 m at 8 m/s
  (26.0, -8.4, True),
  (60.0, -25.0, True),  # stopped object at 90 km/h
])
def test_block_rule(d_rel, v_rel, blocks):
  assert radar_object_blocks(d_rel, v_rel) == blocks


@pytest.mark.parametrize("d_rel,v_rel,v_ego,blocks", [
  (60.0, -25.0, 25.0, False),  # guard rail, tunnel wall or parked car 60 m ahead at 90 km/h: no longer blocks
  (20.0, -25.0 + 1.9, 25.0, False),  # standing still within the measured 2 m/s
  (20.0, -25.0 + 2.1, 25.0, True),  # 2.1 m/s over the ground: moving, closing in within 3 s still blocks
  (2.9, -25.0, 25.0, True),  # standing still within 3 m of the front bumper
  (0.0, -25.0, 25.0, True),  # standing still beside the car
  (26.0, -8.4, 25.0, True),  # moving traffic as before
  (10.0, -2.0, 25.0, False),
])
def test_block_rule_standing_object(d_rel, v_rel, v_ego, blocks):
  # janpoo 2026-11-28 17:11: a side-lane object standing still blocks only within 3 m of the front bumper
  assert RADAR_STATIONARY_SPEED == 2.0
  assert radar_object_blocks(d_rel, v_rel, v_ego) == blocks


@pytest.mark.parametrize("mode", [MODE_NUDGE, MODE_AUTO])
@pytest.mark.parametrize("d_rel,starts", [(40.0, True), (2.0, False)])
def test_standing_object_blocks_only_near(mode, d_rel, starts):
  d = RadarDrive(mode=mode)
  d.points = [point(7, d_rel, LEFT_LANE_Y, -d.cs.vEgo)]  # standing still in the left lane, at 25 m/s
  d.blinker("left")
  d.seconds(1.0)
  if mode == MODE_NUDGE:
    d.nudge("left")
  d.step()
  assert (d.dh.lane_change_state == LCS.laneChangeStarting) == starts
  assert any(d.blocked) != starts


def test_side_lane_position():
  m = model()
  pts = [point(1, 10.0, LEFT_LANE_Y, 0.0), point(2, 10.0, 0.0, 0.0), point(3, 10.0, RIGHT_LANE_Y, 0.0),
         point(4, 10.0, 2.0 * LANE_WIDTH + 0.5, 0.0), point(5, 10.0, 0.5 * LANE_WIDTH + 0.2, 0.0)]
  left = side_radar_objects(pts, m, 0)
  right = side_radar_objects(pts, m, 1)
  assert [o[0] for o in left] == [1]  # own-lane lead, two lanes over and on the line are not the left lane
  assert left[0][3] == pytest.approx(LANE_WIDTH / 2)
  assert [o[0] for o in right] == [3]
  # the car 1 m right of its lane centre: the same left-lane object is 1 m farther from the car, same lane
  assert [o[0] for o in side_radar_objects([point(1, 10.0, LEFT_LANE_Y + 1.0, 0.0)], model(1.0), 0)] == [1]


def test_auto_start_waits_for_radar_object_beside():
  d = RadarDrive()
  d.points = [point(7, 0.0, LEFT_LANE_Y, 1.0)]
  d.blinker("left")
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.block_reason(LCD.left) == "radar_tracks"
  assert d.alc.state == AutoState.none and d.blocked[-1]
  d.points = [point(7, 6.0, LEFT_LANE_Y, 1.0)]  # moved ahead out of the near zone
  d.seconds(RADAR_CLEAR_TIME - 0.1)
  assert d.dh.lane_change_state == LCS.preLaneChange and d.alc.state == AutoState.waiting
  assert not d.blocked[-1]
  d.seconds(0.2)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_other_side_does_not_block():
  d = RadarDrive()
  d.points = [point(7, 0.0, RIGHT_LANE_Y, 0.0)]
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


@pytest.mark.parametrize("enabled", [True, False])
def test_nudge_blocked_like_blindspot(enabled):
  # stock openpilot: a blind-spot warning keeps the nudge from starting; the radar object does the same
  d = RadarDrive(enabled=enabled)
  d.points = [point(7, 1.0, RIGHT_LANE_Y, 0.5)]
  d.blinker("right")
  d.seconds(0.5)
  d.nudge("right")
  d.seconds(1.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.blocked[-1]
  d.points = []
  d.step(2)
  # 1 and 2: the side must have been clear RADAR_CLEAR_TIME before the nudge goes, as the automatic start
  assert d.dh.lane_change_state == LCS.preLaneChange and not d.blocked[-1]
  d.seconds(RADAR_CLEAR_TIME)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_nudge_started_is_not_stopped():
  # stock: the blind spot is not looked at after a nudge started; neither is the radar (the start waits 1 s as in 2)
  d = RadarDrive(enabled=False)
  d.blinker("left")
  d.seconds(1.0)
  d.nudge("left")
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.points = [point(7, 0.0, LEFT_LANE_Y, 0.0)]
  d.lane_change_prob = 0.9
  d.seconds(1.0)
  assert d.dh.lane_change_state == LCS.laneChangeStarting and not d.alc.returning


def test_object_during_auto_change_returns():
  d = RadarDrive()
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.lane_change_prob = 0.9
  d.offset = -0.8
  d.step()
  d.points = [point(7, 20.0, LEFT_LANE_Y + 0.8, -8.0)]  # closing fast in the target lane
  d.step(RADAR_HOLD_FRAMES - 1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.step()
  assert d.dh.lane_change_state == LCS.off and d.alc.returning and d.alc.state == AutoState.returning


def test_single_frame_return_ignored():
  d = RadarDrive()
  d.blinker("left")
  d.seconds(0.5)
  for i in range(8):  # a new track id each frame: never held
    d.points = [point(100 + i, 0.0, LEFT_LANE_Y, 0.0)]
    d.step()
  d.points = []
  d.seconds(0.6)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


@pytest.mark.parametrize("case", ["no_radar_car", "no_message", "canError", "radarFault", "temporary"])
def test_without_radar_data_waits_two_seconds(case):
  d = RadarDrive(radar=case != "no_radar_car")
  d.points = [point(7, 0.0, LEFT_LANE_Y, 0.0)]
  if case == "no_message":
    d.send_radar = False
  elif case == "canError":
    d.radar_errors = {"canError": True}
  elif case == "radarFault":
    d.radar_errors = {"radarFault": True}
  elif case == "temporary":
    d.radar_errors = {"radarUnavailableTemporary": True}
  d.blinker("left")
  d.seconds(1.1)
  # janpoo 2026-11-28 17:18: no radar object blocks, the blind spot must have been clear 2 s
  assert d.dh.lane_change_state == LCS.preLaneChange and d.alc.state == AutoState.waiting
  d.seconds(BLINDSPOT_CLEAR_TIME_NO_RADAR - 1.0)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  assert not any(d.blocked)


def test_stale_radar_with_submaster_alive():
  src = RadarTrackSource(True)

  class SM(dict):
    alive = {"radarTracks": False}
  sm = SM(radarTracks=tracks([point(1, 0.0, LEFT_LANE_Y, 0.0)]))
  for _ in range(5):
    src.update(sm, model())
  assert not src.available and src.report(None, 0) == alc_mod.SideReport("radar_tracks", False)
  SM.alive = {"radarTracks": True}
  for _ in range(5):
    src.update(sm, model())
  assert src.available and src.report(None, 0).occupied


@pytest.mark.parametrize("seed", range(3))
def test_radar_car_without_objects_is_unchanged(seed, monkeypatch):
  # a radar car with an empty radar: decisions equal the car without radar data, but for the blind-spot wait (1 s, 2 s)
  monkeypatch.setattr(alc_mod, "BLINDSPOT_CLEAR_TIME_NO_RADAR", alc_mod.BLINDSPOT_CLEAR_TIME)
  rows = random_world(seed)
  for mode in (MODE_NUDGE, MODE_AUTO):
    expected = run_sequence(rows, AutoLaneChange(car_params(True), mode=mode), check=False)
    dh = DesireHelper()
    alc = AutoLaneChange(radar_car(True, True), mode=mode)
    out = []
    for cs, lat, prob, engaged, offset in rows:
      sm = {"carState": cs, "carControl": SimpleNamespace(enabled=engaged, latActive=lat), "radarTracks": tracks([]),
            "driverMonitoringState": SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)}
      alc.update(sm, model(offset), dh.lane_change_state, prob)
      dh.update(cs, lat, prob, alc)
      alc.publish_state(dh.lane_change_state)
      out.append((dh.lane_change_state, dh.lane_change_direction, dh.desire, round(dh.lane_change_timer, 6)))
    assert out == expected
    assert alc.radar.available


def test_alert_helper():
  meta = lambda direction, radar=False: SimpleNamespace(laneChangeDirection=direction, kovaLaneChangeBlocked=radar)  # noqa: E731
  cs = lambda left=False, right=False: SimpleNamespace(leftBlindspot=left, rightBlindspot=right)  # noqa: E731
  assert lane_change_blocked_alert(cs(left=True), meta(LCD.left))
  assert not lane_change_blocked_alert(cs(left=True), meta(LCD.right))
  assert lane_change_blocked_alert(cs(right=True), meta(LCD.right))
  assert lane_change_blocked_alert(cs(), meta(LCD.right, True))
  assert not lane_change_blocked_alert(cs(), meta(LCD.left))


def test_wiring():
  meta = log.ModelDataV2.new_message().meta
  assert meta.kovaLaneChangeBlocked is False
  src = open(importlib.util.find_spec("openpilot.selfdrive.modeld.modeld").origin).read()
  assert "\"lateralDelay\", \"selfdriveState\", \"radarTracks\"]" in src
  assert "kovaLaneChangeBlocked = ALC.lane_change_blocked(DH.lane_change_state, DH.lane_change_direction)" in src
  src = open(importlib.util.find_spec("openpilot.selfdrive.selfdrived.selfdrived").origin).read()
  assert "if lane_change_blocked_alert(CS, self.sm[" in src
