"""Road speed limit as kovaCameraEvents (navigation.road_speed_limit_profile).

The limit comes from the carrot-compatible app over WebSocket v2 (speed.road_limit_kph) while the
app is connected, else from the stock navigation CAN (carrot carrot_serv: CS.speedLimit only while
no navigation app is connected). Hyundai CAN-FD 0x4A3 SPEED_LIMIT is the road limit whatever its
MapSource (carrot carstate); CAN 0x544 SpeedLim_Nav_Clu is used only with the camera bit, as carrot.

No source says where a lower limit starts, so a lower limit is handled like a distance-less stock
navigation camera warning: a virtual endpoint of limit x 6 s from where it is first reported
(vehicle_navi.CAMERA_DISTANCE_TIME_S), planned with the stock navigation deceleration
(camera_profile.STOCK_NAVI_DECEL_MPS2), then held while the limit lasts. Unlike a camera, a road
limit is never passed, so the endpoint is moved out to the distance that deceleration needs from
the current speed when 6 s is too short; the car then always settles at the limit. A higher,
unknown or stale limit reports the old one passed, so plannerd returns with the profile recovery.
"""
from dataclasses import dataclass, field

from openpilot.kovapilot.config.kova_params import ROAD_LIMIT_PROFILE_KEY
from openpilot.kovapilot.navigation.camera_profile import STOCK_NAVI_DECEL_MPS2, speed_ramp
from openpilot.kovapilot.navigation.carrot_link import MAX_LIMIT_KPH, MIN_LIMIT_KPH
from openpilot.kovapilot.navigation.vehicle_navi import CAMERA_DISTANCE_TIME_S, PASSED_REPORT_NS


ROAD_LIMIT_KEY = ROAD_LIMIT_PROFILE_KEY
ROAD_LIMIT_SOURCE = "road_limit"
# Room for the plannerd approach: its smallest cruise jerk (longitudinal_planner min(J_CRUISE_VALS)),
# a response reserve and the camera finish margin (camera_profile.target_distance, profile 3).
PLANNER_JERK_MPS3 = 0.6
RESPONSE_RESERVE_S = 2.0


def endpoint_distance(limit_kph: float, ego_mps: float) -> float:
  target = limit_kph / 3.6
  needed = 0.0
  if ego_mps > target:
    needed = (speed_ramp(ego_mps, target, STOCK_NAVI_DECEL_MPS2, max_jerk_mps3=PLANNER_JERK_MPS3).distance_m +
              ego_mps * RESPONSE_RESERVE_S + max(20.0, target * 2.0))
  return max(target * CAMERA_DISTANCE_TIME_S, needed)


def valid_limit(limit_kph) -> float | None:
  if limit_kph is None:
    return None
  limit = float(limit_kph)
  return limit if MIN_LIMIT_KPH <= limit <= MAX_LIMIT_KPH else None


@dataclass
class RoadLimit:
  event_id: str
  limit_kph: float
  target_odo_m: float


@dataclass
class RoadLimitTracker:
  session: str
  source: str = ROAD_LIMIT_SOURCE
  current: RoadLimit | None = None
  retired: list[tuple[RoadLimit, str, int]] = field(default_factory=list)
  next_id: int = 0
  sequence: int = 0

  def _retire(self, state: str, now_ns: int) -> None:
    if self.current is not None:
      self.retired.append((self.current, state, now_ns))
      self.current = None

  def update(self, limit_kph: float | None, now_ns: int, odo_m: float, ego_mps: float = 0.0) -> None:
    """limit_kph None: no valid, fresh road limit."""
    limit = valid_limit(limit_kph)
    if limit is None:
      self._retire("passed", now_ns)
      return
    if self.current is not None and self.current.limit_kph != limit:
      # Higher: return with the profile recovery. Lower: a new approach from the current speed.
      self._retire("passed" if limit > self.current.limit_kph else "clear", now_ns)
    if self.current is None:
      self.next_id += 1
      self.current = RoadLimit(f"road-{self.next_id}", limit, odo_m + endpoint_distance(limit, ego_mps))

  def step(self, now_ns: int, odo_m: float) -> list[dict]:
    events = []
    keep = []
    for limit, state, retired_ns in self.retired:
      if state == "clear":
        events.append(self._event(limit, now_ns, "clear", 0.0))
      elif now_ns - retired_ns <= PASSED_REPORT_NS:
        events.append(self._event(limit, now_ns, "passed", 0.0))
        keep.append((limit, state, retired_ns))
    self.retired = keep
    if self.current is not None:
      events.append(self._event(self.current, now_ns, "present", max(0.0, self.current.target_odo_m - odo_m)))
    return events

  def _event(self, limit: RoadLimit, now_ns: int, state: str, distance_m: float) -> dict:
    self.sequence += 1
    return {"source": self.source, "session": self.session, "eventId": limit.event_id, "sequence": self.sequence,
            "observedMonoTime": now_ns, "state": state, "roadMatch": True, "verifiedSinglePoint": True,
            "distanceM": float(distance_m), "targetMps": float(limit.limit_kph / 3.6) if state != "clear" else 0.0}
