"""Turn kovaNavigation guidance into an optional navigation turn speed ceiling for plannerd."""

from math import isfinite

from openpilot.kovapilot.bridges.curve_runtime import CURVE_INPUTS
from openpilot.kovapilot.longitudinal.curve_slowdown import MIN_MOVING_SPEED_MPS, CurveLimiter, CurveLimits
from openpilot.kovapilot.longitudinal.nav_turn import GUIDANCE_TIMEOUT_NS, estimate_turns, turn_points
from openpilot.kovapilot.longitudinal.shadow import MAX_INPUT_AGE_NS, is_fresh


class NavTurnRuntime:
  def __init__(self, limits: CurveLimits):
    self.limits = limits
    self.limiter = CurveLimiter(jerk_mps3=limits.jerk_mps3)
    self.navigation = None
    self.received_ns: int | None = None
    # Wheel distance since the last guidance: kovaNavigation comes every 200 ms, plannerd runs at 20 Hz.
    self.odo_m = 0.0
    self.anchor_odo_m = 0.0
    self.last_ns: int | None = None

  def receive(self, navigation, received_ns: int) -> None:
    self.navigation, self.received_ns = navigation, received_ns
    self.anchor_odo_m = self.odo_m

  def eligible(self, sm, now_ns: int, cruise_mps: float | None) -> bool:
    # The same vehicle conditions as the vision curve (curve_runtime.CurveRuntime.eligible).
    if cruise_mps is None or not all(is_fresh(now_ns, sm.logMonoTime[service]) for service in CURVE_INPUTS):
      return False
    state = sm["carState"]
    return (isfinite(state.vEgo) and isfinite(state.aEgo) and state.vEgo >= MIN_MOVING_SPEED_MPS and
            not state.standstill and not sm["carControl"].cruiseControl.cancel)

  def invalidate(self) -> None:
    self.limiter = CurveLimiter(jerk_mps3=self.limits.jerk_mps3)

  def speed_cap(self, now_ns: int, ego_mps: float, ego_accel_mps2: float, cruise_mps: float) -> float | None:
    if self.last_ns is not None and 0 < now_ns - self.last_ns <= MAX_INPUT_AGE_NS and isfinite(ego_mps) and ego_mps > 0:
      self.odo_m += ego_mps * (now_ns - self.last_ns) / 1e9
    self.last_ns = now_ns
    fresh = self.received_ns is not None and 0 <= now_ns - self.received_ns <= GUIDANCE_TIMEOUT_NS
    points = turn_points(self.navigation, self.limits.min_turn_speed_mps) if fresh else None
    if points:
      travelled = self.odo_m - self.anchor_odo_m
      points = [(max(0.0, distance - travelled), speed) for distance, speed in points]
    estimate = estimate_turns(points, ego_mps=ego_mps, ego_accel_mps2=ego_accel_mps2, cruise_mps=cruise_mps, limits=self.limits)
    return self.limiter.update(now_ns, estimate, cruise_mps)
