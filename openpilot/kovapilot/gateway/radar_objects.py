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

On cars that send the stock rear cells (carState.sideCells, Hyundai CAN-FD CCNC_0x162) a car in the left or right rear
cell is added while the cells are trusted (side_cells_trusted: stock HDA on, frames fresh; janpoo 2026-11-28 "4. 응").
The cell distance is approximate (no speed, ends at 25.0 m), so these are marked approximate and placed simply: one
REAR_CELL_VEHICLE_LENGTH car whose front is the cell distance behind an REAR_CELL_EGO_LENGTH ego, in the middle of the
next lane (REAR_CELL_LEFT), relative speed 0, no moving flag. Their ids are negative (radar track ids never are).
"""
import math

import numpy as np

from openpilot.kovapilot.config.parked_setting import fresh_message
from openpilot.kovapilot.lateral.auto_lane_change import (RADAR_HOLD_FRAMES, RADAR_TO_CAMERA, radar_object_stationary,
                                                          radar_points, rear_cell, side_cells_trusted, side_radar_objects)
from openpilot.kovapilot.longitudinal.radar_cut_in import CUT_IN_MAX_DIST

LANE_EGO, LANE_LEFT, LANE_RIGHT = "ego", "left", "right"
SIDE_LANES = ((0, LANE_LEFT), (1, LANE_RIGHT))  # auto_lane_change side order
REAR_CELL_IDS = (-1, -2)  # left, right rear cell
REAR_CELL_EGO_LENGTH = 5.0  # m, approximate: the app measures from the front bumper, the cell from the rear
REAR_CELL_VEHICLE_LENGTH = 4.6  # m, the app's default vehicle length
REAR_CELL_LEFT = 3.5  # m from the ego centre to the middle of the next lane (the raw cell lateral is not used)


def rear_cell_objects(sm, now_ns) -> list[dict]:
  """Cars in the stock rear cells for `objects`; empty without fresh carState or trusted cells."""
  if not fresh_message(sm, now_ns, "carState"):
    return []
  car = sm["carState"]
  if not side_cells_trusted(car):
    return []
  objects = []
  for side, lane in SIDE_LANES:
    cell = rear_cell(car, side)
    distance = float(cell.distance)
    if not cell.detected or not math.isfinite(distance) or distance < 0.0:
      continue
    objects.append({"id": REAR_CELL_IDS[side], "kind": "vehicle",
                    "distanceMeters": round(-(REAR_CELL_EGO_LENGTH + distance + REAR_CELL_VEHICLE_LENGTH), 2),
                    "leftMeters": REAR_CELL_LEFT if side == 0 else -REAR_CELL_LEFT, "relativeSpeedMps": 0.0,
                    "lengthMeters": REAR_CELL_VEHICLE_LENGTH, "lane": lane, "isLead": False, "approximate": True})
  return objects


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
    """The `objects` list, or None to leave it out (no current, error free radar tracks and no rear cell car). Never
    raises."""
    try:
      objects = self._objects(sm, now_ns)
    except Exception:
      self.reset()
      objects = None
    try:
      rear = rear_cell_objects(sm, now_ns)
    except Exception:
      rear = []
    if not rear:
      return objects
    return sorted((objects or []) + rear, key=lambda o: o["distanceMeters"])

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
