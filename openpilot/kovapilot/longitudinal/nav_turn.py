"""Navigation turn slowdown (cruise.nav_turn_slowdown): turn speeds from turn-by-turn guidance.

The only turn input is the carrot modified TMAP app over WebSocket v2, which navcamd publishes as
kovaNavigation: the TMAP turn code and distance of the current and next guide points. The stock
navigation CAN frames navcamd reads carry no turn-by-turn guidance (vehicle_navi), and the app sends
no turn angle or curvature, so the turn speed comes from the turn type (TURN_RADIUS_M) as
sqrt(lateral acceleration x radius), with the vision curve lateral acceleration, deceleration, jerk
and response (curve_slowdown). Like carrot update_auto_turn, the turn speed is reached a safe time
(carrot safe_sec, 2 s) before the guide point; forks and exits are left to the road speed limit,
as carrot uses nRoadLimitSpeed for them.
"""
from math import isfinite, sqrt

from openpilot.kovapilot.longitudinal.curve_slowdown import (LATERAL_ACCEL_MPS2, CurveApproach, CurveEstimate, CurveLimits,
                                                           _approach_speed, _braking_distance)


NAVIGATION_SERVICE = "kovaNavigation"
GUIDANCE_TIMEOUT_NS = 1_000_000_000  # navcamd publishes kovaNavigation every 200 ms
MAX_TURN_DISTANCE_M = 5000.0
TURN_SAFE_S = 2.0
# TMAP turn codes (carrot carrot_serv._update_tbt) and the turn radius assumed for each, m.
TURN_RADIUS_M = {
  **dict.fromkeys((12, 13), 15.0),  # left, right: 19.7 km/h, carrot AutoTurnControlSpeedTurn 20
  **dict.fromkeys((16, 19), 10.0),  # sharp left, sharp right: 16.1 km/h
  14: 6.0,  # U-turn: 12.5 km/h
  **dict.fromkeys(range(131, 143), 15.0),  # roundabout: 19.7 km/h
}


def turn_speed(turn_type: int, min_speed_mps: float = 0.0) -> float | None:
  radius = TURN_RADIUS_M.get(turn_type)
  return None if radius is None else max(sqrt(LATERAL_ACCEL_MPS2 * radius), min_speed_mps)


def turn_points(navigation, min_speed_mps: float = 0.0) -> list[tuple[float, float]] | None:
  """(distance m, turn speed m/s) of the current and next guide turns.

  None: no usable guidance (no WebSocket v2 app). [] : the app reports no turn ahead (not guiding,
  off route, or guide points that are not turns)."""
  try:
    if str(navigation.source) != "carrotV2":
      return None
    if not navigation.guidanceActive or navigation.offRoute or not navigation.current.valid:
      return []
    points = []
    distance = float(navigation.current.distanceM)
    if not isfinite(distance) or distance < 0:
      return None
    speed = turn_speed(navigation.current.turnType, min_speed_mps)
    if speed is not None and distance <= MAX_TURN_DISTANCE_M:
      points.append((distance, speed))
    following = navigation.next
    if following.valid:
      gap = float(following.distanceM)  # counted from the current guide point
      speed = turn_speed(following.turnType, min_speed_mps)
      if speed is not None and isfinite(gap) and gap >= 0 and distance + gap <= MAX_TURN_DISTANCE_M:
        points.append((distance + gap, speed))
    return points
  except (AttributeError, TypeError, ValueError):
    return None


def estimate_turns(points: list[tuple[float, float]] | None, *, ego_mps: float, ego_accel_mps2: float, cruise_mps: float,
                   limits: CurveLimits) -> CurveEstimate:
  """The lowest approach speed over the turns ahead, as a vision curve estimate for CurveLimiter."""
  if points is None or not all(map(isfinite, (ego_mps, ego_accel_mps2, cruise_mps))) or cruise_mps <= 0:
    return CurveEstimate(None, False)
  positive_accel = max(0.0, ego_accel_mps2)
  approach = None
  for distance, speed in points:
    if speed >= cruise_mps:
      continue
    at_distance = max(0.0, distance - speed * TURN_SAFE_S)
    cap = _approach_speed(at_distance, speed, cruise_mps, positive_accel, limits)
    if approach is None or cap < approach.approach_speed_mps:
      feasible = ego_mps <= speed or _braking_distance(ego_mps, speed, positive_accel, limits) <= at_distance
      approach = CurveApproach(at_distance, speed, cap, feasible)
  return CurveEstimate(approach, True)
