"""Distance-based vision curve limits for offline evaluation."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from math import hypot, isfinite, sqrt
from statistics import median

from openpilot.kovapilot.longitudinal.shadow import CURVE_LOOKAHEAD_S, MAX_INPUT_AGE_NS
from openpilot.kovapilot.navigation.camera_profile import SpeedRamp, speed_ramp


# Offline comparison values. Vehicle calibration is a separate step.
PREVIEW_DISTANCE_M = 180.0
MAX_PATH_SPEED_MPS = 85.0
MIN_MOVING_SPEED_MPS = 3.0
LATERAL_ACCEL_MPS2 = 2.0
DECEL_MPS2 = 1.2
JERK_MPS3 = 0.8
RESPONSE_S = 0.5
RECOVERY_ACCEL_MPS2 = 0.5
HOLD_NS = 350_000_000
CONFIRM_NS = 100_000_000


@dataclass(frozen=True)
class CurveApproach:
  distance_m: float
  turn_speed_mps: float
  approach_speed_mps: float
  target_feasible: bool


@dataclass(frozen=True)
class CurveEstimate:
  approach: CurveApproach | None
  complete: bool


def _braking_distance(start_mps: float, target_mps: float, positive_accel: float) -> float:
  # Reserve distance for response and to bring existing positive acceleration to zero.
  unwind_s = positive_accel / JERK_MPS3
  response_speed = start_mps + positive_accel * RESPONSE_S
  brake_speed = response_speed + positive_accel * unwind_s / 2
  reserve = (start_mps * RESPONSE_S + positive_accel * RESPONSE_S**2 / 2 +
             response_speed * unwind_s + positive_accel * unwind_s**2 / 3)
  ramp = speed_ramp(brake_speed, target_mps, DECEL_MPS2, max_jerk_mps3=JERK_MPS3)
  return reserve + ramp.distance_m


def _approach_speed(distance: float, target: float, cruise: float, accel: float) -> float:
  if _braking_distance(cruise, target, accel) <= distance:
    return cruise
  low, high = target, cruise
  for _ in range(18):
    mid = (low + high) / 2
    if _braking_distance(mid, target, accel) <= distance:
      low = mid
    else:
      high = mid
  return low


def estimate_curve(times_s: Sequence[float], x_m: Sequence[float], y_m: Sequence[float],
                   model_speeds_mps: Sequence[float], yaw_radps: Sequence[float], *,
                   ego_mps: float, ego_accel_mps2: float, cruise_mps: float) -> CurveEstimate:
  if (len({len(times_s), len(x_m), len(y_m), len(model_speeds_mps), len(yaw_radps)}) != 1 or
      not all(map(isfinite, (ego_mps, ego_accel_mps2, cruise_mps))) or ego_mps < MIN_MOVING_SPEED_MPS or cruise_mps <= 0):
    return CurveEstimate(None, False)

  distances: list[float] = []
  curvatures: list[float | None] = []
  distance = 0.0
  previous = None
  complete = False
  for t, x, y, speed, yaw in zip(times_s, x_m, y_m, model_speeds_mps, yaw_radps, strict=True):
    if not isfinite(t) or t < 0 or (previous is not None and t <= previous[0]):
      break
    if t > CURVE_LOOKAHEAD_S:
      complete = True
      break
    if not isfinite(x) or not isfinite(y) or (previous is None and t > 0.25):
      break
    if previous is not None:
      step = hypot(x - previous[1], y - previous[2])
      if step > MAX_PATH_SPEED_MPS * (t - previous[0]):
        break
      distance += step
    previous = (t, x, y)
    if distance > PREVIEW_DISTANCE_M:
      complete = True
      break
    distances.append(distance)
    curvatures.append(yaw / speed if isfinite(yaw) and isfinite(speed) and speed >= MIN_MOVING_SPEED_MPS else None)
    complete = t == CURVE_LOOKAHEAD_S or distance == PREVIEW_DISTANCE_M

  approach = None
  supported = False
  positive_accel = max(0.0, ego_accel_mps2)
  for i in range(len(curvatures) - 2):
    a, b, c = curvatures[i:i + 3]
    if a is None or b is None or c is None or distances[i + 2] - distances[i] < 1.0:
      continue
    supported = True
    curvature = abs(median((a, b, c)))
    if curvature <= LATERAL_ACCEL_MPS2 / cruise_mps**2:
      continue
    turn_speed = sqrt(LATERAL_ACCEL_MPS2 / curvature)
    # The middle point locates the supported curvature; the first point only
    # confirms it. This avoids moving a constraint to one isolated early sample.
    at_distance = distances[i + 1]
    cap = _approach_speed(at_distance, turn_speed, cruise_mps, positive_accel)
    if approach is None or cap < approach.approach_speed_mps:
      feasible = ego_mps <= turn_speed or _braking_distance(ego_mps, turn_speed, positive_accel) <= at_distance
      approach = CurveApproach(at_distance, turn_speed, cap, feasible)

  return CurveEstimate(approach, complete and supported and all(c is not None for c in curvatures))


class CurveLimiter:
  """Track a curve ceiling and confirm relief using new, complete geometry."""

  def __init__(self):
    self.cap_mps: float | None = None
    self.last_frame_ns: int | None = None
    self.last_geometry_ns: int | None = None
    self.relief: deque[tuple[int, float]] = deque()
    self.recovery: SpeedRamp | None = None
    self.recovery_started_ns = 0

  def update(self, now_ns: int, estimate: CurveEstimate, cruise_mps: float) -> float | None:
    if self.last_frame_ns is not None:
      if now_ns <= self.last_frame_ns:
        return self.cap_mps
      if now_ns - self.last_frame_ns > MAX_INPUT_AGE_NS:
        self.relief.clear()
        self.recovery = None
    self.last_frame_ns = now_ns
    if self.cap_mps is not None and self.cap_mps >= cruise_mps:
      self.cap_mps = self.recovery = None
      self.relief.clear()

    target = estimate.approach.approach_speed_mps if estimate.approach is not None else cruise_mps
    if estimate.complete or (target < cruise_mps and (self.cap_mps is None or target <= self.cap_mps)):
      self.last_geometry_ns = now_ns
    if target < cruise_mps and (self.cap_mps is None or target <= self.cap_mps):
      self.cap_mps = target
      self.relief.clear()
      self.recovery = None
    elif self.cap_mps is not None:
      if estimate.complete:
        if self.recovery is not None and target < self.recovery.target_mps:
          self.recovery = None
        self.relief.append((now_ns, target))
        while now_ns - self.relief[0][0] > MAX_INPUT_AGE_NS:
          self.relief.popleft()
        if len(self.relief) >= 3 and now_ns - self.relief[0][0] >= CONFIRM_NS:
          self._recover(now_ns, min(speed for _, speed in self.relief))
      else:
        self.relief.clear()
        if self.last_geometry_ns is not None and now_ns - self.last_geometry_ns >= HOLD_NS:
          self._recover(now_ns, target)
        elif self.recovery is not None:
          self.recovery = None

    return self.cap_mps

  def _recover(self, now_ns: int, target_mps: float) -> None:
    if self.cap_mps is None or self.cap_mps >= target_mps:
      return
    if (self.recovery is None or self.recovery.target_mps > target_mps or
        self.cap_mps >= self.recovery.target_mps):
      self.recovery = speed_ramp(self.cap_mps, target_mps, RECOVERY_ACCEL_MPS2, max_jerk_mps3=JERK_MPS3)
      self.recovery_started_ns = now_ns
    _, self.cap_mps = self.recovery.sample((now_ns - self.recovery_started_ns) / 1e9)
