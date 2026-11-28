"""Radar vehicles around the car for the KovaLink road view (telemetry `objects`).

The comma screen draws nothing new; the app road view already draws `objects` as vehicles. Only cars whose radar
tracks are current and error free send them: no radarTracks, stale tracks or a radar error leave `objects` out of the
frame, as before. Cars without radar publish empty radarTracks, so their list stays empty. The checks are the ones the
radar lane change block (kovapilot/lateral/auto_lane_change.py) and the radar cut-in
(kovapilot/longitudinal/radar_cut_in.py) already use, no new thresholds:
- the radard leads (radarState leadOne, leadTwo) matched to a radar track: own lane, always sent, leadOne as the lead
- other tracks only within CUT_IN_MAX_DIST (40 m), where the radar lateral position is good to a lane, moving over
  the ground (not radar_object_stationary, 2.0 m/s) and in RADAR_HOLD_FRAMES radarTracks messages in a row (0.15 s)
- lane: left or right as side_radar_objects (0.5 m past the current lane line up to the lane width plus 1.0 m), own
  lane between the current lane lines; tracks in neither (on a line) or without lane lines are not sent
Coordinates as the app frame: distanceMeters = radar dRel (front bumper to the vehicle rear), leftMeters = radar yRel
(left positive), relativeSpeedMps = radar vRel.
"""
import math

import numpy as np

from openpilot.kovapilot.config.parked_setting import fresh_message
from openpilot.kovapilot.lateral.auto_lane_change import (RADAR_HOLD_FRAMES, RADAR_TO_CAMERA, radar_object_stationary,
                                                          radar_points, side_radar_objects)
from openpilot.kovapilot.longitudinal.radar_cut_in import CUT_IN_MAX_DIST

LANE_EGO, LANE_LEFT, LANE_RIGHT = "ego", "left", "right"
SIDE_LANES = ((0, LANE_LEFT), (1, LANE_RIGHT))  # auto_lane_change side order


def _lane_lines(model):
  lines = getattr(model, "laneLines", None) if model is not None else None
  if lines is None or len(lines) < 4 or len(lines[1].x) == 0 or len(lines[2].x) == 0:
    return None
  return lines


def point_lane(point, model) -> str | None:
  """ego, left, right or None (on a lane line, or no current lane lines)."""
  lines = _lane_lines(model)
  if lines is None:
    return None
  x = point.dRel + RADAR_TO_CAMERA
  left, right = float(np.interp(x, lines[1].x, lines[1].y)), float(np.interp(x, lines[2].x, lines[2].y))
  if min(left, right) <= -point.yRel <= max(left, right):  # radar yRel grows to the left, modelV2 y to the right
    return LANE_EGO
  for side, name in SIDE_LANES:
    if side_radar_objects([point], model, side):
      return name
  return None


def radar_lead_tracks(sm, now_ns) -> dict[int, bool]:
  """trackId -> True for leadOne, False for leadTwo, of the radard leads matched to a radar track."""
  if not fresh_message(sm, now_ns, "radarState"):
    return {}
  state = sm["radarState"]
  leads = {}
  for lead, is_lead in ((state.leadTwo, False), (state.leadOne, True)):
    if lead.present and lead.radar and lead.radarTrackId >= 0:
      leads[int(lead.radarTrackId)] = is_lead or leads.get(int(lead.radarTrackId), False)
  return leads


class RadarObjects:
  def __init__(self):
    self.seen: dict[int, int] = {}  # trackId -> radarTracks messages in a row

  def reset(self) -> None:
    self.seen = {}

  def __call__(self, sm, now_ns) -> list[dict] | None:
    """The `objects` list, or None to leave it out (no current, error free radar tracks). Never raises."""
    try:
      return self._objects(sm, now_ns)
    except Exception:
      self.reset()
      return None

  def _objects(self, sm, now_ns):
    points = radar_points(sm) if fresh_message(sm, now_ns, "radarTracks") else None
    if points is None:
      self.reset()
      return None
    updated = getattr(sm, "updated", None)
    if updated is None or updated.get("radarTracks", False):  # the telemetry loop may read one message twice
      self.seen = {int(p.trackId): self.seen.get(int(p.trackId), 0) + 1 for p in points}

    model = sm["modelV2"] if fresh_message(sm, now_ns, "modelV2") else None
    leads = radar_lead_tracks(sm, now_ns)
    v_ego = sm["carState"].vEgo
    objects = []
    for point in points:
      track_id = int(point.trackId)
      d_rel, y_rel, v_rel = float(point.dRel), float(point.yRel), float(point.vRel)
      if not (math.isfinite(d_rel) and math.isfinite(y_rel) and math.isfinite(v_rel)):
        continue
      moving = not radar_object_stationary(v_rel, v_ego)
      if track_id in leads:
        lane, is_lead = LANE_EGO, leads[track_id]
      else:
        if self.seen.get(track_id, 0) < RADAR_HOLD_FRAMES or d_rel > CUT_IN_MAX_DIST or not moving:
          continue
        lane, is_lead = point_lane(point, model), False
        if lane is None:
          continue
      objects.append({"id": track_id, "kind": "vehicle", "distanceMeters": round(d_rel, 2), "leftMeters": round(y_rel, 2),
                      "relativeSpeedMps": round(v_rel, 2), "lane": lane, "moving": moving, "isLead": is_lead})
    objects.sort(key=lambda o: o["distanceMeters"])
    return objects
