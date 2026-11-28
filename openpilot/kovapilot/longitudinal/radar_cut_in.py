"""Early cut-in lead from the car radar.

A radar track that is entering our path from a side lane is used as the lead before the driving model reports it,
so the normal longitudinal planner (MPC) starts following it earlier. radard only uses it when it is nearer than the
lead it already has. Nothing changes without radar tracks, with faulted radar data, while a lane change is running,
or below CUT_IN_MIN_EGO_SPEED (radard keeps its own low speed radar lead there).

Lateral positions are measured from the model plan (modelV2.position) at the object distance, so curves and the
driver steering within the lane do not look like the object moving. Radar yRel grows to the left, modelV2 y to the
right; the radar is RADAR_TO_CAMERA ahead of the model frame origin.
"""
from collections import deque

import numpy as np

from openpilot.cereal import log

RADAR_TO_CAMERA = 1.52  # m, as selfdrive/controls/radard.py

CUT_IN_MIN_EGO_SPEED = 4.0  # m/s, radard V_EGO_STATIONARY: slower, its low speed radar lead handles objects in front
CUT_IN_MIN_LEAD_SPEED = 2.0  # m/s over ground: moving traffic only (no guard rails, poles or parked cars)
CUT_IN_MAX_DIST = 40.0  # m: radar angle noise and the plan stay within a lane level position up to here (logs: 50 m more false leads)
CUT_IN_LATERAL_MARGIN = 0.8  # m: the object centre is at most this far outside our half lane width (a 0.9 m half car reaches the line)
CUT_IN_MIN_INWARD_SPEED = 0.4  # m/s towards our path
CUT_IN_MAX_INWARD_SPEED = 3.0  # m/s: faster is crossing traffic or a track jump, not a cut-in
CUT_IN_WINDOW = 10  # model frames (0.5 s) of one track for the lateral speed fit
CUT_IN_MAX_GAP = 0.12  # s: a track missing longer restarts its history
CUT_IN_HOLD = 6  # model frames (0.3 s) the track must keep entering before it is used
LANE_LINE_PROB_MIN = 0.5  # both current lane lines must be this sure to take the lane width from them
HALF_WIDTH_DEFAULT = 1.75  # m, half lane width without lane lines
HALF_WIDTH_MIN, HALF_WIDTH_MAX = 1.25, 2.25  # m

LaneChangeState = log.LaneChangeState


def radar_errors_present(errors) -> bool:
  return bool(errors.canError or errors.radarFault or errors.wrongConfig or errors.radarUnavailableTemporary)


def path_half_width(model, x: float) -> float:
  lines, probs = model.laneLines, model.laneLineProbs
  if len(lines) < 4 or len(probs) < 4 or min(probs[1], probs[2]) < LANE_LINE_PROB_MIN or not len(lines[1].x) or not len(lines[2].x):
    return HALF_WIDTH_DEFAULT
  width = abs(float(np.interp(x, lines[2].x, lines[2].y)) - float(np.interp(x, lines[1].x, lines[1].y)))
  return float(np.clip(width / 2, HALF_WIDTH_MIN, HALF_WIDTH_MAX))


class RadarCutIn:
  """Keeps a short lateral history per radar track and selects the nearest track entering our path."""
  def __init__(self):
    self.reset()

  def reset(self) -> None:
    self.history: dict[int, deque] = {}
    self.entering: dict[int, int] = {}
    self.selected: int | None = None

  def _inward_speed(self, history) -> float | None:
    """m/s towards our path (least squares over CUT_IN_WINDOW frames of the signed offset), None until the window is full"""
    if len(history) < CUT_IN_WINDOW:
      return None
    t = np.array([h[0] for h in history])
    offset = np.array([h[1] for h in history])
    side = 1.0 if offset[-1] >= 0.0 else -1.0
    return -side * float(np.polyfit(t - t[-1], offset, 1)[0])

  def update(self, now: float, tracks, model, v_ego: float, errors) -> int | None:
    """tracks: radard tracks (trackId -> Track). Returns the trackId to use as a cut-in lead, or None."""
    if (not tracks or errors is None or radar_errors_present(errors) or v_ego < CUT_IN_MIN_EGO_SPEED or
        model.meta.laneChangeState != LaneChangeState.off or len(model.position.x) == 0):
      self.reset()
      return None

    plan_x, plan_y = model.position.x, model.position.y
    for track_id in list(self.history):
      if track_id not in tracks:
        self.history.pop(track_id)
        self.entering.pop(track_id, None)

    state = {}
    for track_id, track in tracks.items():
      x = track.dRel + RADAR_TO_CAMERA
      signed_offset = -track.yRel - float(np.interp(x, plan_x, plan_y))  # m, right of our path +
      offset = abs(signed_offset)
      half_width = path_half_width(model, x)
      history = self.history.setdefault(track_id, deque(maxlen=CUT_IN_WINDOW))
      if history and now - history[-1][0] > CUT_IN_MAX_GAP:
        history.clear()
      history.append((now, signed_offset))
      state[track_id] = (offset, half_width)

      inward = self._inward_speed(history)
      entering = (0.0 < track.dRel <= CUT_IN_MAX_DIST and v_ego + track.vRel >= CUT_IN_MIN_LEAD_SPEED and
                  offset <= half_width + CUT_IN_LATERAL_MARGIN and inward is not None and
                  CUT_IN_MIN_INWARD_SPEED <= inward <= CUT_IN_MAX_INWARD_SPEED)
      self.entering[track_id] = self.entering.get(track_id, 0) + 1 if entering else 0

    if self.selected is not None:
      track = tracks.get(self.selected)
      if track is None:
        self.selected = None
      else:
        # kept while it stays at our path and does not move away again (a lead leaving our lane is not held)
        offset, half_width = state[self.selected]
        inward = self._inward_speed(self.history[self.selected])
        if (offset > half_width + CUT_IN_LATERAL_MARGIN or not 0.0 < track.dRel <= CUT_IN_MAX_DIST or
            (inward is not None and inward < -CUT_IN_MIN_INWARD_SPEED)):
          self.selected = None

    candidates = [tracks[i] for i, n in self.entering.items() if n >= CUT_IN_HOLD]
    if candidates:
      nearest = min(candidates, key=lambda c: c.dRel)
      if self.selected is None or nearest.dRel < tracks[self.selected].dRel:
        self.selected = nearest.identifier
    return self.selected
