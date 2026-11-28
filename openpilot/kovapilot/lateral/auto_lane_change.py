"""KOVA auto lane change (steering.auto_lane_change).

Off (the default) leaves the stock openpilot DesireHelper exactly as it is: the driver turns on a blinker and nudges
the wheel. On, a lane change also starts without the nudge a start delay after the driver turns on one blinker (by driving
personality, kovapilot/config/personality_weights.py LANE_CHANGE_START_DELAY_S: 0.7 / 1.0 / 1.3 / 1.5 s for distance
levels 1-4, standard 1.0 s; the blind-spot and model lane checks below do not follow the personality) when

  * openpilot is engaged (cruise; not always-on steering alone),
  * the speed is at least AUTO_LANE_CHANGE_MIN_SPEED,
  * the side lane is clear by every side-lane source (side_lane_clear): the car's blind-spot monitor is present, its
    frame is fresh and that side has been clear for BLINDSPOT_CLEAR_TIME, and the model has seen a lane there for
    SIDE_LANE_HOLD, and on cars whose radar is used (CarParams.radarUnavailable False) the radar has seen no object in
    that lane near or closing (RadarTrackSource) for RADAR_CLEAR_TIME,
  * driver monitoring is off, or on and not warning,
  * the driver has not braked or steered the other way since the blinker came on, and this blinker has not already
    started an automatic lane change (one automatic lane change per blinker).

Cars without a blind-spot monitor, or whose blind-spot frame stops, keep the nudge only. During an automatic lane
change, a blind-spot warning or a radar object on the target side, braking, steering the other way, disengaging or a
stale blind-spot frame cancels it: before the car has crossed into the new lane it steers back to the centre of the
original lane (return_curvature); once it has crossed, the lane change is left to finish in the new lane.

The radar object also blocks the driver's nudge exactly as a blind-spot warning does in stock openpilot (DesireHelper
stays in preLaneChange, selfdrived shows the stock laneChangeBlocked alert through modelV2.meta.kovaLaneChangeBlocked),
whether or not the automatic lane change setting is on. Cars without radar tracks keep everything as before. The front
radar sees the side lane ahead of and beside the front of the car only; behind it is the blind-spot monitor's.
"""
from dataclasses import dataclass

import numpy as np

from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.config.personality_weights import LANE_CHANGE_START_DELAY_S, personality_value

AUTO_LANE_CHANGE_KEY = "steering.auto_lane_change"
AutoState = log.KovaAutoLaneChange
LaneChangeDirection = log.LaneChangeDirection
LaneChangeState = log.LaneChangeState
DmDisableReason = log.DriverMonitoringState.DisableReason

AUTO_LANE_CHANGE_MIN_SPEED = 30 * CV.KPH_TO_MS  # one constant, to be tuned after car tests
BLINDSPOT_CLEAR_TIME = 1.0  # s the blind spot must have been clear
SIDE_LANE_MIN_WIDTH = 2.5  # m from the current lane line to the next line and to the road edge
SIDE_LANE_HOLD = 0.2  # s
SIDE_LANE_LOOKAHEAD = 1.0  # s ahead
SIDE_EDGE_LOOKAHEAD = 2.0  # s ahead, road edge only
SETTING_POLL_FRAMES = int(1.0 / DT_MDL)

# Side-lane radar objects (radarTracks). Values from the 2026-11-28 IONIQ 9 drive 0000079f (front radar group 3): of the
# 14 lane changes the driver made, the tightest side-lane objects were 3.5 m ahead moving away at 2.6 m/s and 10 m ahead
# closing at 2.0 m/s; cars beside the front of the car (0-2 m) and slow traffic closing at 7-11 m/s were blocked.
RADAR_TO_CAMERA = 1.52  # m, the radar is ahead of the model frame origin (selfdrive/controls/radard.py)
RADAR_LANE_MIN = 0.5  # m: the object centre is at least this far past the current lane line towards that side
RADAR_LANE_EXTRA = 1.0  # m: and at most the current lane width plus this past the line
RADAR_LANE_WIDTH_MIN, RADAR_LANE_WIDTH_MAX = 2.5, 4.5  # m, clip of the current lane width used for the side lane
RADAR_NEAR_DIST = 3.0  # m from the front bumper to the object rear (0 = beside the car)
RADAR_HORIZON = 3.0  # s: blocks when nearer than RADAR_NEAR_DIST now or, at its relative speed, within this time
RADAR_HOLD_FRAMES = 3  # model frames (0.15 s) one track must keep blocking before it counts
RADAR_CLEAR_TIME = 1.0  # s the side must have been clear of radar objects before an automatic start, as the blind spot

# Going back to the original lane
CROSSED_JUMP = 1.5  # m: the current-lane centre jumps by about a lane width when the model relabels the lines
CROSSED_OFFSET = 0.5  # m past the line towards the target lane
RETURN_LOOKAHEAD_TIME = 1.2  # s
RETURN_LOOKAHEAD_MIN, RETURN_LOOKAHEAD_MAX = 12.0, 40.0  # m
RETURN_MAX_LAT_ACCEL = 2.0  # m/s^2, below the controls' own limit
RETURN_DONE_OFFSET = 0.3  # m from the lane centre
RETURN_DONE_TIME = 1.0  # s
RETURN_DONE_PROB = 0.02  # model lane change probability, as DesireHelper
RETURN_TIME_MAX = 10.0  # s


def auto_lane_change_supported(CP) -> bool:
  """Vehicles whose blind-spot monitor is read by the car port (Hyundai, Kia, Genesis with HAS_BSM)."""
  if CP.brand != "hyundai" or CP.passive or CP.dashcamOnly or CP.notCar:
    return False
  from opendbc.car.hyundai.values import HyundaiFlags
  return bool(CP.flags & HyundaiFlags.HAS_BSM)


def read_auto_lane_change(params) -> bool:
  """Display and runtime reader: never raises, a missing or broken store means off."""
  try:
    from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend
    return KovaParamsStore(ParamsKovaParamsBackend(params)).values()[AUTO_LANE_CHANGE_KEY] == 1
  except Exception:
    return False


def current_personality(sm):
  """selfdriveState.personality once selfdrived has published it; None (standard) before that or without it."""
  seen = getattr(sm, "seen", None)
  try:
    if seen is not None and not seen["selfdriveState"]:
      return None
    return sm["selfdriveState"].personality
  except KeyError:
    return None


def _side(direction) -> int:
  return 0 if direction == LaneChangeDirection.left else 1


@dataclass(frozen=True)
class SideReport:
  """What one side-lane source says about one side. available False: the source has no current data (ignored,
  except that the blind-spot monitor is required). occupied True: it blocks a lane change to that side."""
  source: str
  available: bool
  occupied: bool = False
  ready: bool = True  # available and clear long enough to start


class BlindSpotSource:
  name = "blindspot"

  def __init__(self, supported: bool):
    self.supported = supported
    self.clear_frames = [0, 0]

  def update(self, CS) -> None:
    fresh = self.supported and CS.blindspotFresh
    for i, occupied in enumerate((CS.leftBlindspot, CS.rightBlindspot)):
      self.clear_frames[i] = self.clear_frames[i] + 1 if fresh and not occupied else 0

  def report(self, CS, side: int) -> SideReport:
    if not (self.supported and CS.blindspotFresh):
      return SideReport(self.name, False)
    occupied = (CS.leftBlindspot, CS.rightBlindspot)[side]
    return SideReport(self.name, True, occupied, not occupied and self.clear_frames[side] * DT_MDL >= BLINDSPOT_CLEAR_TIME)


def side_lane_width(model, side: int, v_ego: float) -> tuple[float, float, float]:
  """(lane width, road edge distance, road edge distance farther ahead) in m on that side from the current lane line:
  the lane width is the distance to the next lane line or the road edge, whichever is nearer, SIDE_LANE_LOOKAHEAD
  ahead; the far edge distance is SIDE_EDGE_LOOKAHEAD ahead (carrot lane_math.calculate_lane_width). modelV2
  lateral positions grow to the right."""
  lines, edges = model.laneLines, model.roadEdges
  if len(lines) < 4 or len(edges) < 2:
    return 0.0, 0.0, 0.0
  inner, outer, edge = (lines[1], lines[0], edges[0]) if side == 0 else (lines[2], lines[3], edges[1])
  if len(inner.x) == 0 or len(outer.x) == 0 or len(edge.x) == 0:
    return 0.0, 0.0, 0.0
  x = max(v_ego * SIDE_LANE_LOOKAHEAD, 5.0)
  y_inner = float(np.interp(x, inner.x, inner.y))
  to_edge = abs(y_inner - float(np.interp(x, edge.x, edge.y)))
  width = min(abs(y_inner - float(np.interp(x, outer.x, outer.y))), to_edge)
  x_far = max(v_ego * SIDE_EDGE_LOOKAHEAD, 10.0)
  to_edge_far = abs(float(np.interp(x_far, inner.x, inner.y)) - float(np.interp(x_far, edge.x, edge.y)))
  return width, to_edge, to_edge_far


def side_lane_seen(model, side: int, v_ego: float) -> bool:
  """A lane wide enough to the next line and the edge, or a road edge far enough now and farther ahead (carrot
  side_state lane_available / edge_available). The outer line probability is not used: on the 2026-11-27 IONIQ 9
  drive it was 0.02-0.28 at 6 of 7 turn signals the driver used for lane changes."""
  width, to_edge, to_edge_far = side_lane_width(model, side, v_ego)
  return width > SIDE_LANE_MIN_WIDTH or (to_edge > SIDE_LANE_MIN_WIDTH and to_edge_far > SIDE_LANE_MIN_WIDTH)


class ModelLaneSource:
  name = "model_lane"

  def __init__(self):
    self.seen_frames = [0, 0]

  def update(self, model, v_ego: float) -> None:
    for side in (0, 1):
      self.seen_frames[side] = self.seen_frames[side] + 1 if side_lane_seen(model, side, v_ego) else 0

  def report(self, CS, side: int) -> SideReport:
    seen = self.seen_frames[side] * DT_MDL >= SIDE_LANE_HOLD
    return SideReport(self.name, True, False, seen)


def radar_points(sm):
  """radarTracks points when they are current and the radar reports no error; None otherwise."""
  try:
    tracks = sm["radarTracks"]
  except KeyError:
    return None
  alive = getattr(sm, "alive", None)
  if alive is not None and not alive.get("radarTracks", False):
    return None
  errors = tracks.errors
  if errors.canError or errors.radarFault or errors.wrongConfig or errors.radarUnavailableTemporary:
    return None
  return tracks.points


def side_radar_objects(points, model, side: int) -> list[tuple[int, float, float, float]]:
  """(trackId, dRel, vRel, lateral) of the points in the lane on that side of the current lane at their distance;
  lateral is m past the current lane line towards that side. Radar yRel grows to the left, modelV2 y to the right."""
  lines = model.laneLines
  if len(lines) < 4 or len(lines[1].x) == 0 or len(lines[2].x) == 0:
    return []
  towards = -1.0 if side == 0 else 1.0
  objects = []
  for point in points:
    x = point.dRel + RADAR_TO_CAMERA
    left, right = float(np.interp(x, lines[1].x, lines[1].y)), float(np.interp(x, lines[2].x, lines[2].y))
    width = float(np.clip(abs(right - left), RADAR_LANE_WIDTH_MIN, RADAR_LANE_WIDTH_MAX))
    lateral = towards * (-point.yRel - (left if side == 0 else right))
    if RADAR_LANE_MIN <= lateral <= width + RADAR_LANE_EXTRA:
      objects.append((point.trackId, point.dRel, point.vRel, lateral))
  return objects


def radar_object_blocks(d_rel: float, v_rel: float) -> bool:
  """Nearer than RADAR_NEAR_DIST now, or at this relative speed within RADAR_HORIZON (time to RADAR_NEAR_DIST)."""
  return d_rel < RADAR_NEAR_DIST or (v_rel < 0.0 and d_rel + v_rel * RADAR_HORIZON < RADAR_NEAR_DIST)


class RadarTrackSource:
  """Side-lane objects from the car radar (radarTracks) on cars whose radar is used. A side is occupied while one track
  in that lane has been near or closing (radar_object_blocks) for RADAR_HOLD_FRAMES; ready once it has been clear for
  RADAR_CLEAR_TIME. Without radar, or with stale or faulted radar data, it has no data and changes no decision."""
  name = "radar_tracks"

  def __init__(self, has_radar: bool = False):
    self.has_radar = has_radar
    self.available = False
    self.hold: list[dict[int, int]] = [{}, {}]  # trackId -> model frames it has blocked in a row
    self.occupied = [False, False]
    self.clear_frames = [0, 0]

  def update(self, sm, model=None) -> None:
    points = radar_points(sm) if self.has_radar and model is not None else None
    self.available = points is not None
    for side in (0, 1):
      hold = {}
      if points is not None:
        for track_id, d_rel, v_rel, _ in side_radar_objects(points, model, side):
          if radar_object_blocks(d_rel, v_rel):
            hold[track_id] = self.hold[side].get(track_id, 0) + 1
      self.hold[side] = hold
      self.occupied[side] = any(frames >= RADAR_HOLD_FRAMES for frames in hold.values())
      self.clear_frames[side] = self.clear_frames[side] + 1 if self.available and not self.occupied[side] else 0

  def report(self, CS, side: int) -> SideReport:
    if not self.available:
      return SideReport(self.name, False)
    occupied = self.occupied[side]
    return SideReport(self.name, True, occupied, not occupied and self.clear_frames[side] * DT_MDL >= RADAR_CLEAR_TIME)


def side_lane_clear(reports) -> tuple[bool, str]:
  """Combine the sources for one side. (start allowed, reason). The blind-spot monitor must have data; any
  available source that says occupied blocks; every available source must be ready."""
  by_name = {report.source: report for report in reports}
  blindspot = by_name.get(BlindSpotSource.name)
  if blindspot is None or not blindspot.available:
    return False, "no_blindspot"
  for report in reports:
    if report.available and report.occupied:
      return False, report.source
  for report in reports:
    if report.available and not report.ready:
      return False, report.source + "_wait"
  return True, "clear"


def lane_change_blocked_alert(CS, meta) -> bool:
  """selfdrived laneChangeBlocked during preLaneChange: the stock blind-spot check, or the side radar (modeld)."""
  direction = meta.laneChangeDirection
  return bool((CS.leftBlindspot and direction == LaneChangeDirection.left) or
              (CS.rightBlindspot and direction == LaneChangeDirection.right) or meta.kovaLaneChangeBlocked)


def lane_centre_offset(model) -> float | None:
  """Lateral position (m, right positive) of the current lane centre at the car."""
  lines = model.laneLines
  if len(lines) < 4 or len(lines[1].y) == 0 or len(lines[2].y) == 0:
    return None
  return (float(lines[1].y[0]) + float(lines[2].y[0])) / 2.0


def return_curvature(model, v_ego: float) -> float | None:
  """Curvature (left positive) that steers to the centre of the current lane: pure pursuit to the lane centre
  RETURN_LOOKAHEAD_TIME ahead, limited to RETURN_MAX_LAT_ACCEL."""
  lines = model.laneLines
  if len(lines) < 4 or len(lines[1].x) == 0 or len(lines[2].x) == 0:
    return None
  look = float(np.clip(v_ego * RETURN_LOOKAHEAD_TIME, RETURN_LOOKAHEAD_MIN, RETURN_LOOKAHEAD_MAX))
  y = (float(np.interp(look, lines[1].x, lines[1].y)) + float(np.interp(look, lines[2].x, lines[2].y))) / 2.0
  curvature = -2.0 * y / look ** 2
  limit = RETURN_MAX_LAT_ACCEL / max(v_ego, 1.0) ** 2
  return float(np.clip(curvature, -limit, limit))


class AutoLaneChange:
  """State for DesireHelper. update() once per model frame before DesireHelper.update()."""

  def __init__(self, CP, params=None, enabled: bool | None = None):
    self.params = params
    self.supported = auto_lane_change_supported(CP)
    self.fixed = enabled
    self.setting = bool(enabled) if enabled is not None else False
    self.enabled = self.supported and self.setting
    self.poll_frame = 0
    self.blindspot = BlindSpotSource(self.supported)
    self.model_lane = ModelLaneSource()
    self.radar = RadarTrackSource(not getattr(CP, "radarUnavailable", True))
    self.sources = (self.blindspot, self.model_lane, self.radar)
    self.CS = None
    self.engaged = False
    self.dm_ok = False
    self.v_ego = 0.0
    self.blinker_frames = 0
    self.blinker_dir = LaneChangeDirection.none
    self.start_delay = personality_value(LANE_CHANGE_START_DELAY_S, None)  # s after the blinker comes on
    self.used = False  # this blinker started an automatic lane change
    self.cancelled = False  # braking or opposite steering since this blinker came on
    self.reason = ""
    # the automatic lane change in progress
    self.active_dir = LaneChangeDirection.none
    self.crossed = False
    self.prev_offset = None
    self.returning = False
    self.return_frames = 0
    self.centred_frames = 0
    self.lane_change_prob = 0.0
    self.state = AutoState.none

  # ---- inputs
  def poll_setting(self, lane_change_off: bool) -> None:
    if self.fixed is not None or self.params is None:
      return
    self.poll_frame += 1
    if self.poll_frame >= SETTING_POLL_FRAMES:
      self.poll_frame = 0
      self.setting = read_auto_lane_change(self.params)
    if lane_change_off:  # a change applies outside a lane change
      self.enabled = self.supported and self.setting

  def update(self, sm, model, lane_change_state, lane_change_prob: float = 0.0) -> None:
    CS = sm["carState"]
    self.CS = CS
    self.v_ego = CS.vEgo
    self.lane_change_prob = lane_change_prob
    self.poll_setting(lane_change_state == LaneChangeState.off and not self.returning)
    self.engaged = bool(sm["carControl"].enabled)
    self.start_delay = personality_value(LANE_CHANGE_START_DELAY_S, current_personality(sm))
    dm = sm["driverMonitoringState"]
    self.dm_ok = dm.disableReason != DmDisableReason.none or dm.alertLevel == log.DriverMonitoringState.AlertLevel.none
    for source in self.sources:
      if source is self.blindspot:
        source.update(CS)
      elif source is self.model_lane:
        source.update(model, CS.vEgo)
      else:
        source.update(sm, model)

    one_blinker = CS.leftBlinker != CS.rightBlinker
    direction = (LaneChangeDirection.left if CS.leftBlinker else LaneChangeDirection.right) if one_blinker else LaneChangeDirection.none
    if not one_blinker or direction != self.blinker_dir:
      self.blinker_frames, self.used, self.cancelled = 0, False, False
    self.blinker_dir = direction
    if one_blinker:
      self.blinker_frames += 1
      if CS.brakePressed or self.opposite_steer(direction):
        self.cancelled = True

    if self.active_dir != LaneChangeDirection.none:
      self.track_crossing(model)
    if self.returning:
      self.update_return(model, CS)

  def opposite_steer(self, direction) -> bool:
    CS = self.CS
    return bool(CS.steeringPressed) and ((direction == LaneChangeDirection.left and CS.steeringTorque < 0) or
                                         (direction == LaneChangeDirection.right and CS.steeringTorque > 0))

  def reports(self, direction):
    side = _side(direction)
    return [source.report(self.CS, side) for source in self.sources]

  # ---- DesireHelper hooks
  def radar_occupied(self, direction) -> bool:
    """The radar sees an object in the lane on that side: blocks the start like a blind-spot warning, nudge included."""
    return direction != LaneChangeDirection.none and self.radar.available and self.radar.occupied[_side(direction)]

  def lane_change_blocked(self, lane_change_state, direction) -> bool:
    """For modelV2.meta.kovaLaneChangeBlocked (the stock laneChangeBlocked alert) after DesireHelper.update()."""
    return lane_change_state == LaneChangeState.preLaneChange and self.radar_occupied(direction)

  def speed_min(self, default: float) -> float:
    return min(default, AUTO_LANE_CHANGE_MIN_SPEED) if self.enabled and self.engaged else default

  def start_allowed(self, direction) -> bool:
    self.reason = self.block_reason(direction)
    return self.reason == ""

  def block_reason(self, direction) -> str:
    if not self.enabled:
      return "off"
    if not self.engaged:
      return "not_engaged"
    if direction == LaneChangeDirection.none or direction != self.blinker_dir:
      return "no_blinker"
    if self.used:
      return "used"
    if self.cancelled:
      return "cancelled"
    if self.returning:
      return "returning"
    if self.v_ego < AUTO_LANE_CHANGE_MIN_SPEED:
      return "slow"
    if not self.dm_ok:
      return "driver_monitoring"
    clear, reason = side_lane_clear(self.reports(direction))
    if not clear:
      return reason
    if self.blinker_frames * DT_MDL < self.start_delay:
      return "delay"
    return ""

  def started(self, direction, model=None) -> None:
    self.used = True
    self.active_dir = direction
    self.crossed = False
    self.prev_offset = None

  def abort_reason(self, direction) -> str:
    """Why an automatic lane change in progress must stop ('' = keep going)."""
    if self.active_dir == LaneChangeDirection.none or self.crossed:
      return ""
    CS = self.CS
    if not self.engaged:
      return "not_engaged"
    if CS.brakePressed:
      return "brake"
    if self.opposite_steer(direction):
      return "opposite_steer"
    for report in self.reports(direction):
      if report.source == BlindSpotSource.name and not report.available:
        return "blindspot_stale"
      if report.available and report.occupied:
        return report.source
    return ""

  def begin_return(self) -> None:
    self.returning = True
    self.return_frames = 0
    self.centred_frames = 0

  def stopped(self) -> None:
    """DesireHelper dropped the lane change (lateral control paused or off, time limit): an automatic lane change
    that has not crossed goes back to the original lane once lateral control runs again."""
    if self.active_dir != LaneChangeDirection.none and not self.crossed:
      self.begin_return()
    self.finished()

  def finished(self) -> None:
    self.active_dir = LaneChangeDirection.none
    self.crossed = False
    self.prev_offset = None

  def reset(self) -> None:
    self.finished()
    self.returning = False

  # ---- progress
  def track_crossing(self, model) -> None:
    offset = lane_centre_offset(model)
    if offset is None:
      return
    towards = -1.0 if self.active_dir == LaneChangeDirection.left else 1.0  # target lane side, right positive
    # moving towards the target lane pushes the current-lane centre away from it; it jumps back by about a lane
    # width when the car crosses the line and the model relabels the lines
    jumped = self.prev_offset is not None and (offset - self.prev_offset) * towards > CROSSED_JUMP
    if jumped or offset * towards > CROSSED_OFFSET:
      self.crossed = True
    self.prev_offset = offset

  def update_return(self, model, CS) -> None:
    self.return_frames += 1
    offset = lane_centre_offset(model)
    centred = offset is not None and abs(offset) < RETURN_DONE_OFFSET and self.lane_change_prob < RETURN_DONE_PROB
    self.centred_frames = self.centred_frames + 1 if centred else 0
    if self.centred_frames * DT_MDL >= RETURN_DONE_TIME or self.return_frames * DT_MDL > RETURN_TIME_MAX:
      self.reset()

  def return_curvature(self, model, v_ego: float) -> float | None:
    return return_curvature(model, v_ego) if self.returning else None

  def publish_state(self, lane_change_state):
    """State for modelV2.meta.kovaAutoLaneChange after DesireHelper.update()."""
    self.state = self.display_state(lane_change_state)
    return self.state

  def display_state(self, lane_change_state):
    if self.returning:
      return AutoState.returning
    if lane_change_state != LaneChangeState.preLaneChange or not self.enabled:
      return AutoState.none
    reason = self.block_reason(self.blinker_dir)
    if reason in ("", "delay"):
      return AutoState.pending
    if reason in (BlindSpotSource.name + "_wait", ModelLaneSource.name + "_wait", RadarTrackSource.name + "_wait"):
      return AutoState.waiting
    return AutoState.none
