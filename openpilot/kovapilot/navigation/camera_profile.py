from dataclasses import dataclass
from math import sqrt


CAMERA_PROFILE_KEY = "navigation.speed_camera_profile"
# Initial offline comparison values. These are not vehicle calibrations.
PROFILE_ACCEL_MPS2 = 1.2
PROFILE_JERK_MPS3 = 0.8
RECOVERY_ACCEL_MPS2 = {1: 0.35, 2: 0.55, 3: 0.8}


@dataclass(frozen=True)
class SpeedRamp:
  start_mps: float
  target_mps: float
  ramp_s: float
  hold_s: float
  jerk_mps3: float

  @property
  def duration_s(self) -> float:
    return 2 * self.ramp_s + self.hold_s

  @property
  def distance_m(self) -> float:
    return (self.start_mps + self.target_mps) / 2 * self.duration_s

  def sample(self, time_s: float) -> tuple[float, float]:
    """Return traveled distance and cap speed at a time along the ramp."""
    if time_s >= self.duration_s:
      return self.distance_m, self.target_mps
    speed = self.start_mps
    accel = distance = 0.0
    for phase_s, jerk in ((self.ramp_s, self.jerk_mps3), (self.hold_s, 0.0), (self.ramp_s, -self.jerk_mps3)):
      span = min(max(time_s, 0.0), phase_s)
      distance += speed * span + accel * span**2 / 2 + jerk * span**3 / 6
      speed += accel * span + jerk * span**2 / 2
      accel += jerk * span
      time_s -= span
    return distance, speed

  def speed_at_distance(self, traveled_m: float) -> float:
    if traveled_m <= 0:
      return self.start_mps
    if traveled_m >= self.distance_m:
      return self.target_mps
    low, high = 0.0, self.duration_s
    for _ in range(24):
      mid = (low + high) / 2
      if self.sample(mid)[0] < traveled_m:
        low = mid
      else:
        high = mid
    return self.sample(high)[1]


def speed_ramp(start_mps: float, target_mps: float, max_accel_mps2: float, distance_m: float = 0.0) -> SpeedRamp:
  delta = abs(target_mps - start_mps)
  if delta == 0.0:
    return SpeedRamp(start_mps, target_mps, 0.0, 0.0, 0.0)
  ramp_s = min(max_accel_mps2 / PROFILE_JERK_MPS3, sqrt(delta / PROFILE_JERK_MPS3))
  hold_s = max(0.0, delta / (PROFILE_JERK_MPS3 * ramp_s) - ramp_s)
  min_distance = (start_mps + target_mps) / 2 * (2 * ramp_s + hold_s)
  stretch = max(1.0, distance_m / min_distance)
  jerk = PROFILE_JERK_MPS3 if target_mps > start_mps else -PROFILE_JERK_MPS3
  return SpeedRamp(start_mps, target_mps, ramp_s * stretch, hold_s * stretch, jerk / stretch**2)


def target_distance(profile: int, initial_distance_m: float, target_mps: float) -> float:
  margin_m = max(20.0, target_mps * 2.0)
  # Early starts on receipt. Aim for the first quarter of the approach in shadow.
  fraction = {1: 0.75, 2: 0.5, 3: 0.0}[profile]
  return max(margin_m, fraction * initial_distance_m)
