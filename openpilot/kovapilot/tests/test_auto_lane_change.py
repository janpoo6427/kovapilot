"""KOVA lane change mode (steering.lane_change_mode) and navigation turn signal (navigation.auto_blinker)."""
import json
import random
from types import SimpleNamespace

import numpy as np
import pytest

from opendbc.car.hyundai.values import HyundaiFlags
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.lateral import auto_lane_change as alc_mod
from openpilot.kovapilot.lateral.auto_lane_change import (AUTO_LANE_CHANGE_MIN_SPEED, BLINDSPOT_CLEAR_TIME, BLINDSPOT_CLEAR_TIME_NO_RADAR,
                                                         MODE_AUTO, MODE_NUDGE, MODE_OFF, AutoLaneChange, AutoState, BlindSpotSource,
                                                         ModelLaneSource, RadarTrackSource, SideReport, lane_centre_offset,
                                                         return_curvature, side_lane_clear)
from openpilot.kovapilot.navigation.nav_blinker import LEFT, RIGHT, NavBlinker, nav_blinker_output_supported, parse_guidance, start_distance
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper

LCS = log.LaneChangeState
LCD = log.LaneChangeDirection
DM = log.DriverMonitoringState
LANE_WIDTH = 3.6
X = np.linspace(0.0, 100.0, 33)


def car_params(bsm=True, brand="hyundai", radar=False):
  return SimpleNamespace(brand=brand, passive=False, dashcamOnly=False, notCar=False, flags=HyundaiFlags.HAS_BSM.value if bsm else 0,
                         radarUnavailable=not radar)


def no_tracks():
  # radarTracks with no points and no error
  return SimpleNamespace(points=[], errors=SimpleNamespace(canError=False, radarFault=False, wrongConfig=False,
                                                           radarUnavailableTemporary=False))


def lane(y):
  return SimpleNamespace(x=X, y=np.full_like(X, y))


def model(offset=0.0, left_lane=True, right_lane=True):
  """modelV2-like lanes for a car `offset` m right of its lane centre (lateral positions grow to the right)."""
  ys = [(-1.5 * LANE_WIDTH - offset), (-0.5 * LANE_WIDTH - offset), (0.5 * LANE_WIDTH - offset), (1.5 * LANE_WIDTH - offset)]
  edges = [ys[0] - (0.5 if left_lane else -LANE_WIDTH + 0.5), ys[3] + (0.5 if right_lane else -LANE_WIDTH + 0.5)]
  probs = [0.9 if left_lane else 0.05, 0.9, 0.9, 0.9 if right_lane else 0.05]
  return SimpleNamespace(laneLines=[lane(y) for y in ys], laneLineProbs=probs, roadEdges=[lane(y) for y in edges])


class Drive:
  """DesireHelper + AutoLaneChange at the model rate with a simple lane-change world."""

  def __init__(self, enabled=True, bsm=True, cp=None, sources=None, mode=None, radar=True):
    # radar: a car whose radar is used, with no radar objects (the blind spot must have been clear 1 s); False: a car
    # without radar data (2 s)
    self.radar = radar
    self.left_lane = True
    self.alc = AutoLaneChange(cp or car_params(bsm, radar=radar), enabled=enabled, mode=mode)
    if sources is not None:
      self.alc.sources = sources
    self.dh = DesireHelper()
    self.cs = SimpleNamespace(vEgo=25.0, leftBlinker=False, rightBlinker=False, steeringPressed=False, steeringTorque=0.0,
                              leftBlindspot=False, rightBlindspot=False, blindspotFresh=bsm, brakePressed=False)
    self.cc = SimpleNamespace(enabled=True, latActive=True)
    self.dm = SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)
    self.offset = 0.0  # car position right of the current lane centre
    self.lane_change_prob = 0.0
    self.states = []

  def step(self, n=1):
    for _ in range(n):
      m = model(self.offset, left_lane=self.left_lane)
      sm = {"carState": self.cs, "carControl": self.cc, "driverMonitoringState": self.dm}
      if self.radar:
        sm["radarTracks"] = no_tracks()
      self.alc.update(sm, m, self.dh.lane_change_state, self.lane_change_prob)
      self.dh.update(self.cs, self.cc.latActive, self.lane_change_prob, self.alc)
      self.alc.publish_state(self.dh.lane_change_state)
      self.states.append((self.dh.lane_change_state, self.dh.lane_change_direction, self.alc.state))
    return self.dh.lane_change_state

  def seconds(self, t):
    return self.step(int(round(t / DT_MDL)))

  def blinker(self, side):
    self.cs.leftBlinker, self.cs.rightBlinker = side == "left", side == "right"


def test_starts_one_second_after_blinker():
  d = Drive()
  d.blinker("left")
  d.seconds(0.2)  # lane seen for 0.2 s, blind spot clear for 1 s still counting
  assert d.dh.lane_change_state == LCS.preLaneChange
  d.seconds(0.6)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.state in (AutoState.pending, AutoState.waiting)
  d.seconds(0.25)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  assert d.dh.desire == log.Desire.laneChangeLeft
  start = next(i for i, s in enumerate(d.states) if s[0] == LCS.laneChangeStarting)
  assert start * DT_MDL == pytest.approx(1.0, abs=DT_MDL)


def test_nudge_mode_needs_the_nudge():
  # 1 (on steering input): no automatic start; the nudge starts once the side lane is clear
  d = Drive(mode=MODE_NUDGE)
  d.blinker("left")
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert all(s[2] in (AutoState.none, AutoState.waiting) for s in d.states) and d.states[-1][2] == AutoState.none
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting


@pytest.mark.parametrize("bsm", [True, False])
def test_off_mode_keeps_the_lane(bsm):
  # 0 (off): openpilot does not change lanes; turn signal and nudge keep the lane (janpoo 2026-11-28 17:15)
  d = Drive(mode=MODE_OFF, bsm=bsm)
  d.blinker("left")
  d.seconds(2.0)
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.seconds(3.0)
  assert all(s[0] == LCS.off and s[1] == LCD.none and s[2] == AutoState.none for s in d.states)
  assert d.dh.desire == log.Desire.none


def test_nudge_mode_side_lane_check():
  # 1: the nudge needs the same side-lane check as the automatic start (here the left lane is not seen); once started it
  # is the driver lane change as in stock: braking or a blind-spot warning before crossing do not cancel it
  d = Drive(mode=MODE_NUDGE)
  d.left_lane = False
  d.seconds(1.0)
  d.blinker("left")
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.seconds(2.0)
  assert d.dh.lane_change_state == LCS.preLaneChange and d.alc.state == AutoState.waiting
  d.left_lane = True
  d.seconds(0.3)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.cs.steeringPressed, d.cs.steeringTorque = False, 0.0
  d.lane_change_prob = 0.9
  d.offset = -0.8
  d.cs.brakePressed = True
  d.step(2)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.cs.brakePressed = False
  d.cs.leftBlindspot = True
  d.step(3)
  assert d.dh.lane_change_state == LCS.laneChangeStarting and not d.alc.returning


def test_cruise_off_never_automatic():
  # decision 1: always-on steering without openpilot engaged keeps the nudge
  d = Drive()
  d.cc.enabled = False
  d.blinker("right")
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.block_reason(LCD.right) == "not_engaged"


@pytest.mark.parametrize("kph,starts", [(29, False), (31, True)])
def test_speed(kph, starts):
  d = Drive()
  d.cs.vEgo = kph * CV.KPH_TO_MS
  d.blinker("left")
  d.seconds(1.5)
  assert (d.dh.lane_change_state == LCS.laneChangeStarting) == starts
  if not starts:  # below the stock 32 km/h too: no lane change state at all
    assert d.dh.lane_change_state == LCS.off


def test_min_speed_constant():
  assert AUTO_LANE_CHANGE_MIN_SPEED == pytest.approx(30 / 3.6)


@pytest.mark.parametrize("bsm,fresh,nudge_starts", [(False, False, True), (True, False, False)])
def test_no_or_stale_blindspot_nudge_only(bsm, fresh, nudge_starts):
  # decision 3: no blind-spot monitor: nudge only; decision 6: its frame stopped: in 1 and 2 not even the nudge starts
  d = Drive(bsm=bsm)
  d.cs.blindspotFresh = fresh
  d.blinker("left")
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.state == (AutoState.none if nudge_starts else AutoState.waiting)
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.step()
  assert (d.dh.lane_change_state == LCS.laneChangeStarting) == nudge_starts


def test_blindspot_blocks_then_waits_one_second():
  d = Drive()
  d.cs.leftBlindspot = True
  d.blinker("left")
  d.seconds(2.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  d.cs.leftBlindspot = False
  d.seconds(0.9)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.state == AutoState.waiting
  d.seconds(0.2)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_no_radar_blindspot_waits_two_seconds():
  # cars without radar data (janpoo 2026-11-28 17:18): the blind spot must have been clear 2 s, not 1 s
  assert (BLINDSPOT_CLEAR_TIME, BLINDSPOT_CLEAR_TIME_NO_RADAR) == (1.0, 2.0)
  d = Drive(radar=False)
  d.cs.leftBlindspot = True
  d.blinker("left")
  d.seconds(2.0)
  d.cs.leftBlindspot = False
  d.seconds(1.9)
  assert d.dh.lane_change_state == LCS.preLaneChange and d.alc.state == AutoState.waiting
  d.seconds(0.2)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_no_radar_nudge_waits_two_seconds():
  d = Drive(mode=MODE_NUDGE, radar=False)
  d.cs.leftBlindspot = True
  d.blinker("left")
  d.seconds(1.0)
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0
  d.cs.leftBlindspot = False
  d.seconds(1.9)
  assert d.dh.lane_change_state == LCS.preLaneChange and d.alc.state == AutoState.waiting
  d.seconds(0.2)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


def test_no_radar_wait_runs_with_the_start_delay():
  # the 2 s count from the blind spot clearing, at the same time as the delay after the turn signal: clear long before
  # the turn signal, the start comes after the delay alone
  d = Drive(radar=False)
  d.seconds(3.0)
  first = len(d.states)
  d.blinker("left")
  d.seconds(2.0)
  start = next(i for i, s in enumerate(d.states) if i >= first and s[0] == LCS.laneChangeStarting)
  assert (start - first + 1) * DT_MDL == pytest.approx(1.0, abs=DT_MDL)


def drive_into_change(d, side="left"):
  d.blinker(side)
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.lane_change_prob = 0.9


def test_blindspot_during_change_returns():
  # decision 4: before crossing, a blind-spot warning on the target side cancels and steers back
  d = Drive()
  drive_into_change(d)
  d.offset = -0.8  # moved 0.8 m left of the original lane centre
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting
  d.cs.leftBlindspot = True
  d.step()
  assert d.dh.lane_change_state == LCS.off and d.alc.returning and d.alc.state == AutoState.returning
  k = d.alc.return_curvature(model(d.offset), d.cs.vEgo)
  assert k is not None and k < 0  # back to the right
  # back in the lane and the model has finished: control goes back to the model
  d.offset, d.lane_change_prob = 0.1, 0.0
  d.seconds(1.1)
  assert not d.alc.returning and d.alc.state == AutoState.none
  assert d.alc.return_curvature(model(d.offset), d.cs.vEgo) is None
  d.cs.leftBlindspot = False
  d.seconds(3.0)  # same blinker: no second automatic lane change
  assert d.dh.lane_change_state == LCS.off


def test_after_crossing_finishes_in_new_lane():
  d = Drive()
  drive_into_change(d)
  for offset in (-0.6, -1.2, -1.7):
    d.offset = offset
    d.step()
  d.offset = LANE_WIDTH - 1.9  # the model relabels: now 1.7 m right of the new lane centre... 1.9 left of crossing
  d.step()
  assert d.alc.crossed
  d.cs.leftBlindspot = True  # now the lane beyond the new one
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting and not d.alc.returning


@pytest.mark.parametrize("cancel", ["brake", "opposite", "disengage", "stale"])
def test_cancel_during_change_returns(cancel):
  d = Drive()
  drive_into_change(d, "right")
  d.offset = 0.5
  d.step()
  if cancel == "brake":
    d.cs.brakePressed = True
  elif cancel == "opposite":
    d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0  # left torque during a right lane change
  elif cancel == "disengage":
    d.cc.enabled = False  # always-on steering keeps latActive
  else:
    d.cs.blindspotFresh = False
  d.step()
  assert d.dh.lane_change_state == LCS.off and d.alc.returning


def test_lateral_pause_before_crossing_returns_when_steering_resumes():
  # always-on steering pauses while braking: DesireHelper drops the lane change, the return waits for steering
  d = Drive()
  drive_into_change(d)
  d.offset = -0.6
  d.step()
  d.cc.latActive = False
  d.step()
  assert d.dh.lane_change_state == LCS.off and d.alc.returning
  d.cc.latActive = True
  d.step()
  assert d.alc.return_curvature(model(d.offset), d.cs.vEgo) < 0


def test_alerts():
  from openpilot.selfdrive.selfdrived.events import auto_lane_change_alert

  def text(state, direction=LCD.left):
    meta = SimpleNamespace(kovaAutoLaneChange=state, laneChangeDirection=direction)
    alert = auto_lane_change_alert(None, None, {"modelV2": SimpleNamespace(meta=meta)}, True, 0, None)
    return alert.alert_text_1, alert.alert_text_2, alert.audible_alert
  assert text(AutoState.pending) == ("Changing Lanes Left Automatically", "", log.SelfdriveState.AudibleAlert.none)
  assert text(AutoState.waiting, LCD.right)[0] == "Waiting for Right Lane to Clear"
  assert text(AutoState.returning) == ("Lane Change Canceled", "Returning to Lane", log.SelfdriveState.AudibleAlert.prompt)


def test_nudge_direction_still_works_during_auto():
  d = Drive()
  drive_into_change(d)
  d.cs.steeringPressed, d.cs.steeringTorque = True, 1.0  # same direction: keeps going
  d.offset = -0.5
  d.step()
  assert d.dh.lane_change_state == LCS.laneChangeStarting


@pytest.mark.parametrize("disabled,level,starts", [
  (DM.DisableReason.session, DM.AlertLevel.two, True),  # decision 5: monitoring turned off
  (DM.DisableReason.none, DM.AlertLevel.none, True),
  (DM.DisableReason.none, DM.AlertLevel.one, False),  # monitoring on and warning
])
def test_driver_monitoring(disabled, level, starts):
  d = Drive()
  d.dm.disableReason, d.dm.alertLevel = disabled, level
  d.blinker("left")
  d.seconds(1.5)
  assert (d.dh.lane_change_state == LCS.laneChangeStarting) == starts


def test_one_per_blinker():
  d = Drive()
  drive_into_change(d)
  d.lane_change_prob = 0.0
  d.seconds(0.6)  # model done, blinker still on
  assert d.dh.lane_change_state == LCS.preLaneChange
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.block_reason(LCD.left) == "used"
  d.blinker(None)
  d.step(2)
  d.blinker("left")
  d.seconds(1.1)
  assert d.dh.lane_change_state == LCS.laneChangeStarting


@pytest.mark.parametrize("what", ["brake", "opposite"])
def test_brake_or_opposite_before_start_cancels_for_this_blinker(what):
  d = Drive()
  d.blinker("left")
  d.seconds(0.5)
  if what == "brake":
    d.cs.brakePressed = True
  else:
    d.cs.steeringPressed, d.cs.steeringTorque = True, -1.0
  d.step()
  d.cs.brakePressed, d.cs.steeringPressed, d.cs.steeringTorque = False, False, 0.0
  d.seconds(3.0)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.block_reason(LCD.left) == "cancelled"


def test_short_tap_does_nothing():
  d = Drive()
  d.blinker("right")
  d.seconds(0.6)
  d.blinker(None)
  d.seconds(2.0)
  assert all(s[0] != LCS.laneChangeStarting for s in d.states)


def test_no_side_lane_waits():
  d = Drive()
  d.blinker("left")
  d.alc.model_lane.seen_frames = [0, 0]
  for _ in range(40):
    m = model(0.0, left_lane=False)
    sm = {"carState": d.cs, "carControl": d.cc, "driverMonitoringState": d.dm}
    d.alc.update(sm, m, d.dh.lane_change_state)
    d.dh.update(d.cs, True, 0.0, d.alc)
  assert d.dh.lane_change_state == LCS.preLaneChange
  assert d.alc.block_reason(LCD.left) == ModelLaneSource.name + "_wait"


def random_world(seed, frames=4000):
  rng = random.Random(seed)
  rows = []
  cs = {"vEgo": 20.0, "leftBlinker": False, "rightBlinker": False, "steeringPressed": False, "steeringTorque": 0.0,
        "leftBlindspot": False, "rightBlindspot": False, "blindspotFresh": True, "brakePressed": False}
  lat = True
  prob = 0.0
  for _ in range(frames):
    if rng.random() < 0.03:
      cs["leftBlinker"], cs["rightBlinker"] = rng.choice([(False, False), (True, False), (False, True), (True, True)])
    if rng.random() < 0.05:
      cs["steeringPressed"] = rng.random() < 0.3
      cs["steeringTorque"] = rng.uniform(-2, 2)
    if rng.random() < 0.03:
      cs["leftBlindspot"], cs["rightBlindspot"] = rng.random() < 0.3, rng.random() < 0.3
    if rng.random() < 0.02:
      cs["vEgo"] = rng.uniform(0, 35)
    if rng.random() < 0.01:
      lat = rng.random() < 0.9
    if rng.random() < 0.05:
      prob = rng.choice([0.0, 0.01, 0.5, 0.9])
    cs["brakePressed"] = rng.random() < 0.01
    rows.append((SimpleNamespace(**cs), lat, prob, rng.choice([True, False]), rng.uniform(-2, 2)))
  return rows


def run_sequence(rows, alc, check=True):
  dh = DesireHelper()
  out = []
  for cs, lat, prob, engaged, offset in rows:
    if alc is not None:
      sm = {"carState": cs, "carControl": SimpleNamespace(enabled=engaged, latActive=lat),
            "driverMonitoringState": SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)}
      alc.update(sm, model(offset), dh.lane_change_state, prob)
    dh.update(cs, lat, prob, alc)
    state = alc.publish_state(dh.lane_change_state) if alc is not None else None
    if alc is not None and check:
      assert state == AutoState.none
      assert alc.return_curvature(model(offset), cs.vEgo) is None
    out.append((dh.lane_change_state, dh.lane_change_direction, dh.desire, round(dh.lane_change_timer, 6)))
  return out


@pytest.mark.parametrize("seed", range(5))
def test_without_blindspot_monitor_matches_stock(seed):
  # vehicles without a blind-spot monitor keep the stock nudge in 1 and 2; with one, 2 never engaged equals 1
  rows = random_world(seed)
  stock = run_sequence(rows, None)
  assert {s[0] for s in stock} >= {LCS.off, LCS.preLaneChange, LCS.laneChangeStarting}
  for cp in (car_params(False), car_params(True, "toyota")):
    for mode in (MODE_NUDGE, MODE_AUTO):
      assert run_sequence(rows, AutoLaneChange(cp, mode=mode)) == stock
  rows_off = [(cs, lat, prob, False, off) for cs, lat, prob, _, off in rows]
  never_engaged = run_sequence(rows_off, AutoLaneChange(car_params(True), mode=MODE_AUTO), check=False)
  assert never_engaged == run_sequence(rows_off, AutoLaneChange(car_params(True), mode=MODE_NUDGE), check=False)


@pytest.mark.parametrize("seed", range(5))
def test_nudge_mode_starts_only_on_the_nudge(seed):
  rows = random_world(seed)
  out = run_sequence(rows, AutoLaneChange(car_params(True), mode=MODE_NUDGE), check=False)
  starts = [i for i in range(1, len(out)) if out[i][0] == LCS.laneChangeStarting and out[i - 1][0] == LCS.preLaneChange]
  assert starts
  for i in starts:
    cs = rows[i][0]
    assert cs.steeringPressed and (cs.steeringTorque > 0) == (out[i][1] == LCD.left)


@pytest.mark.parametrize("seed", range(3))
def test_radar_source_without_data_changes_nothing(seed):
  # decision 10: the radar-track source exists but has no data, so decisions equal blind spot + model lane alone
  rows = random_world(seed)
  sources = lambda a: (a.blindspot, a.model_lane)  # noqa: E731
  with_radar, without = AutoLaneChange(car_params(), enabled=True), AutoLaneChange(car_params(), enabled=True)
  without.sources = sources(without)
  assert any(isinstance(s, RadarTrackSource) for s in with_radar.sources)
  dh_a, dh_b = DesireHelper(), DesireHelper()
  for cs, lat, prob, engaged, offset in rows:
    sm = {"carState": cs, "carControl": SimpleNamespace(enabled=engaged, latActive=lat),
          "driverMonitoringState": SimpleNamespace(disableReason=DM.DisableReason.none, alertLevel=DM.AlertLevel.none)}
    for a, dh in ((with_radar, dh_a), (without, dh_b)):
      a.update(sm, model(offset), dh.lane_change_state, prob)
      dh.update(cs, lat, prob, a)
      a.publish_state(dh.lane_change_state)
    assert (dh_a.lane_change_state, dh_a.lane_change_direction, with_radar.state, with_radar.returning) == \
           (dh_b.lane_change_state, dh_b.lane_change_direction, without.state, without.returning)


def test_side_lane_clear_rules():
  bsm = lambda **kw: SideReport(BlindSpotSource.name, **kw)  # noqa: E731
  assert side_lane_clear([SideReport(BlindSpotSource.name, False), SideReport(ModelLaneSource.name, True)]) == (False, "no_blindspot")
  assert side_lane_clear([bsm(available=True, occupied=True, ready=False)]) == (False, BlindSpotSource.name)
  assert side_lane_clear([bsm(available=True), SideReport("radar_tracks", True, True)]) == (False, "radar_tracks")
  assert side_lane_clear([bsm(available=True), SideReport("radar_tracks", False, True)]) == (True, "clear")
  assert side_lane_clear([bsm(available=True), SideReport(ModelLaneSource.name, True, False, False)]) == (False, "model_lane_wait")
  assert RadarTrackSource().report(None, 0) == SideReport("radar_tracks", False)


def test_side_lane_rule():
  from openpilot.kovapilot.lateral.auto_lane_change import side_lane_seen
  m = model(0.0)
  m.laneLineProbs = [0.05, 0.9, 0.9, 0.05]  # outer lines faint, as on the 2026-11-27 drive
  assert side_lane_seen(m, 0, 25.0) and side_lane_seen(m, 1, 25.0)
  assert not side_lane_seen(model(0.0, left_lane=False), 0, 25.0)  # road edge 0.5 m beyond the line
  narrow = model(0.0)
  narrow.laneLines[0] = lane(narrow.laneLines[1].y[0] - 1.5)  # next line 1.5 m away, road edge 4.1 m
  assert side_lane_seen(narrow, 0, 25.0)  # the road edge is far enough now and 2 s ahead
  narrow.roadEdges[0] = lane(narrow.laneLines[1].y[0] - 2.0)
  assert not side_lane_seen(narrow, 0, 25.0)


def test_lane_geometry_helpers():
  assert lane_centre_offset(model(0.0)) == pytest.approx(0.0)
  assert lane_centre_offset(model(-1.0)) == pytest.approx(1.0)  # car 1 m left: centre 1 m to the right
  assert return_curvature(model(-1.0), 25.0) < 0  # steer right
  assert return_curvature(model(1.0), 25.0) > 0
  assert abs(return_curvature(model(-3.0), 10.0)) <= alc_mod.RETURN_MAX_LAT_ACCEL / 10.0 ** 2 + 1e-9


def test_supported_vehicles():
  assert alc_mod.auto_lane_change_supported(car_params(True))
  assert not alc_mod.auto_lane_change_supported(car_params(False))
  assert not alc_mod.auto_lane_change_supported(car_params(True, "toyota"))
  assert not AutoLaneChange(car_params(False), enabled=True).enabled


def test_reader_falls_back_to_the_default():
  class Broken:
    def get(self, *a, **k):
      raise RuntimeError("broken")
  assert alc_mod.read_lane_change_mode(Broken()) == alc_mod.MODE_DEFAULT == MODE_NUDGE


# ---- navigation turn signal (decision 8)
def test_gateway_conditions():
  from opendbc.car.hyundai.interface import CarInterface
  from opendbc.car.hyundai.tests import test_hyundai
  from opendbc.car.hyundai.values import CAR
  from openpilot.kovapilot.gateway.native_longitudinal import vehicle_dependency_conditions

  class Params:
    def __init__(self, raw):
      self.raw = raw

    def get(self, key):
      return self.raw if key == "CarParams" else None

    def get_bool(self, key):
      return False

  helper = test_hyundai.TestHyundaiFingerprint
  for bsm in (True, False):
    fingerprint = helper._ioniq9_fingerprint()
    if bsm:
      fingerprint[0][0x1BA] = 24
    CP = CarInterface.get_params(CAR.HYUNDAI_IONIQ_9, fingerprint, helper._ioniq9_fw(), False, False, False)
    conditions = vehicle_dependency_conditions(Params(CP.to_bytes()))
    assert conditions["blindSpotMonitor"] == ("met" if bsm else "unsupported")
    assert conditions["blinkerOutput"] == "unsupported"
  conditions = vehicle_dependency_conditions(Params(None))
  assert conditions["blindSpotMonitor"] == conditions["blinkerOutput"] == "unknown"


def test_catalog_entries():
  catalog = json.loads(open(__file__.rsplit("/tests/", 1)[0] + "/config/kova_params.json").read())
  settings = {s["key"]: s for s in catalog["settings"]}
  for key, group, condition, options, default, control in (
      ("steering.lane_change_mode", "steering", "blindSpotMonitor", [0, 1, 2], 1, None),
      ("navigation.auto_blinker", "navigation", "blinkerOutput", [0, 1], 0, "switch")):
    s = settings[key]
    assert (s["groupId"], s["options"], s["defaultValue"], s.get("control")) == (group, options, default, control)
    assert [d["condition"] for d in s["dependencies"]] == [condition]
  assert "steering.auto_lane_change" not in settings
  titles = settings["steering.lane_change_mode"]["optionTitles"]
  assert [titles[str(i)]["ko"] for i in range(3)] == ["끔", "조향 개입 시", "자동"]


def packet(**fields):
  return json.dumps({"nRoadLimitSpeed": 100, **fields}).encode()


def test_guidance_parsing():
  g = parse_guidance(packet(nTBTTurnType=101, nTBTDist=300))
  assert g is not None and g.side == RIGHT and g.distance_m == 300
  assert parse_guidance(packet(nTBTTurnType=7, nTBTDist=150)).side == LEFT
  assert parse_guidance(packet(nTBTTurnType=12, nTBTDist=100)) is None  # an intersection turn is not a lane change
  assert parse_guidance(packet(nTBTTurnType=101, nTBTDist=0)) is None
  assert parse_guidance(json.dumps({"nTBTTurnType": 101, "nTBTDist": 300}).encode()) is None
  assert parse_guidance(b"\xff") is None
  assert parse_guidance(packet(nTBTTurnType=True, nTBTDist=300)) is None
  assert start_distance(100) == 350 and start_distance(30) == 160


def test_no_vehicle_has_blinker_output():
  assert not nav_blinker_output_supported(car_params(True))


def test_guidance_to_turn_signal_to_lane_change():
  # a vehicle that could light the lamp (none yet): guidance -> request -> lamp -> the automatic lane change conditions
  nav = NavBlinker()
  d = Drive()
  now = 0
  nav.receive(parse_guidance(packet(nTBTTurnType=101, nTBTDist=500)), now)
  assert nav.request(now, d.cs) is None  # 500 m: farther than 350 m at 100 km/h
  for dist in range(340, 0, -1):
    now += int(DT_MDL * 1e9)
    nav.receive(parse_guidance(packet(nTBTTurnType=101, nTBTDist=dist)), now)
    d.blinker(nav.request(now, d.cs))
    d.step()
    if d.dh.lane_change_state == LCS.laneChangeStarting:
      break
  assert d.dh.lane_change_state == LCS.laneChangeStarting and d.dh.lane_change_direction == LCD.right
  assert (340 - dist) * DT_MDL == pytest.approx(1.0, abs=2 * DT_MDL)


def test_guidance_canceled_by_driver_and_timeout():
  nav = NavBlinker()
  cs = SimpleNamespace(leftBlinker=False, rightBlinker=False, steeringPressed=False, steeringTorque=0.0)
  nav.receive(parse_guidance(packet(nTBTTurnType=7, nTBTDist=100)), 0)
  assert nav.request(0, cs) == LEFT
  cs.steeringPressed, cs.steeringTorque = True, -1.0
  assert nav.request(1, cs) is None
  cs.steeringPressed = False
  nav.receive(parse_guidance(packet(nTBTTurnType=7, nTBTDist=90)), 2)
  assert nav.request(2, cs) is None  # stays canceled for this guidance
  nav.receive(parse_guidance(packet(nTBTTurnType=6, nTBTDist=90)), 3)
  assert nav.request(3, cs) == RIGHT
  assert nav.request(3 + 2_000_000_001, cs) is None  # app went quiet
