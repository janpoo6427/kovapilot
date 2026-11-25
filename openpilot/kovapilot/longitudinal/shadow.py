from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt

MAX_INPUT_AGE_NS = 250_000_000
CURVE_LOOKAHEAD_S = 6.0
CURVE_LATERAL_ACCEL_MPS2 = 2.8  # Initial observation threshold shared with upstream CurveSpeed.


@dataclass(frozen=True)
class SpeedCandidate:
  source: str
  speed_mps: float
  observed_at_ns: int


def is_fresh(now_ns: int, observed_at_ns: int) -> bool:
  return 0 <= now_ns - observed_at_ns <= MAX_INPUT_AGE_NS


def curve_candidate(observed_at_ns: int, times_s: Sequence[float], speeds_mps: Sequence[float],
                    yaw_rates_radps: Sequence[float]) -> SpeedCandidate | None:
  if len(times_s) != len(speeds_mps) or len(times_s) != len(yaw_rates_radps):
    return None
  samples = list(zip(times_s, speeds_mps, yaw_rates_radps, strict=True))
  if not samples or any(not all(map(isfinite, sample)) for sample in samples):
    return None

  curvature = max((abs(yaw_rate) / speed for time_s, speed, yaw_rate in samples
                   if 0 < time_s <= CURVE_LOOKAHEAD_S and speed >= 1.0), default=0.0)
  if curvature <= 0:
    return None
  speed_cap = sqrt(CURVE_LATERAL_ACCEL_MPS2 / curvature)
  return SpeedCandidate("model_curve", speed_cap, observed_at_ns) if isfinite(speed_cap) else None


def select_candidate(now_ns: int, cruise_speed_mps: float,
                     candidates: Iterable[SpeedCandidate]) -> SpeedCandidate | None:
  if not isfinite(cruise_speed_mps) or cruise_speed_mps <= 0:
    return None
  valid = (candidate for candidate in candidates if is_fresh(now_ns, candidate.observed_at_ns)
           and 0 < candidate.speed_mps < cruise_speed_mps)
  return min(valid, key=lambda candidate: (candidate.speed_mps, candidate.source), default=None)
