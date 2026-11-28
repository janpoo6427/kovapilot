"""Side-lane vehicles from the car radar for the onroad screen (mici road view).

On cars whose radar is used (CarParams.radarUnavailable False) the road view draws the radar objects in the lanes
next to ours as bars on the road, like the lead bars. The checks are the ones the radar lane change block
(kovapilot/lateral/auto_lane_change.py) and the radar cut-in (kovapilot/longitudinal/radar_cut_in.py) already use:
- current, error free radarTracks (radar_points); nothing is drawn while they are stale or faulted
- the lane on either side (side_radar_objects: 0.5 m past the current lane line up to the lane width plus 1.0 m)
- moving traffic only (not radar_object_stationary, 2.0 m/s over the ground): no guard rails, poles or parked cars
- within CUT_IN_MAX_DIST (40 m), where the radar lateral position is good to a lane
- a track must have been in RADAR_HOLD_FRAMES radarTracks messages in a row (0.15 s); like radard, a track missing
  from a message is dropped at once
The own lane keeps the lead bars (radarState); a radar lead drawn there is not drawn again. Cars without radar draw
nothing new.
"""
from typing import NamedTuple

from openpilot.kovapilot.lateral.auto_lane_change import RADAR_HOLD_FRAMES, radar_object_stationary, radar_points, side_radar_objects
from openpilot.kovapilot.longitudinal.radar_cut_in import CUT_IN_MAX_DIST

SIDES = (0, 1)  # left, right (auto_lane_change order)


class SideVehicle(NamedTuple):
  track_id: int
  d_rel: float  # m ahead of the radar
  y_rel: float  # m, left +
  side: int


def radar_used(car_params) -> bool:
  return car_params is not None and not getattr(car_params, "radarUnavailable", True)


class RadarSideVehicles:
  def __init__(self):
    self.seen: dict[int, int] = {}  # trackId -> radarTracks messages in a row

  def reset(self) -> None:
    self.seen = {}

  def update(self, sm, model, v_ego: float, has_radar: bool, lead_track_ids=()) -> list[SideVehicle]:
    points = radar_points(sm) if has_radar else None
    if points is None:
      self.reset()
      return []
    updated = getattr(sm, "updated", None)
    if updated is None or updated.get("radarTracks", False):  # the UI draws several frames per radar message
      self.seen = {p.trackId: self.seen.get(p.trackId, 0) + 1 for p in points}

    y_rel = {p.trackId: p.yRel for p in points}
    vehicles = []
    for side in SIDES:
      for track_id, d_rel, v_rel, _ in side_radar_objects(points, model, side):
        if (self.seen.get(track_id, 0) >= RADAR_HOLD_FRAMES and d_rel <= CUT_IN_MAX_DIST and
            not radar_object_stationary(v_rel, v_ego) and track_id not in lead_track_ids):
          vehicles.append(SideVehicle(int(track_id), float(d_rel), float(y_rel[track_id]), side))
    return vehicles
