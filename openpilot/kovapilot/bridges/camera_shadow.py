from collections.abc import Callable
from dataclasses import asdict, dataclass
from math import isfinite

from openpilot.kovapilot.longitudinal.shadow import SpeedCandidate, select_candidate
from openpilot.kovapilot.navigation.camera_input import CameraInput, CameraInputs, MAX_CAMERA_AGE_NS
from openpilot.kovapilot.navigation.camera_profile import (
  PROFILE_ACCEL_MPS2, PROFILE_JERK_MPS3, RECOVERY_ACCEL_MPS2, SpeedRamp, speed_ramp, target_distance,
)


@dataclass
class Approach:
  initial_distance_m: float
  target_mps: float
  decel_limit_mps2: float = PROFILE_ACCEL_MPS2
  jerk_limit_mps3: float = PROFILE_JERK_MPS3
  response_time_s: float = 0.0
  response_distance_m: float = 0.0
  ramp: SpeedRamp | None = None
  start_distance_m: float | None = None
  progress_m: float = 0.0
  reached_distance_m: float | None = None
  overspeed_s: float = 0.0
  last_sample_ns: int | None = None
  was_over_target: bool = False
  last_cap_mps: float | None = None
  was_eligible: bool = False
  # The distance is an estimate (stock navigation): start on receipt and reach the target by the
  # margin before the estimated point, whatever the profile, using as little of decel_limit as fits.
  estimated_distance: bool = False

  def sample(self, profile: int, distance_m: float, ego_mps: float, now_ns: int, eligible: bool) -> dict:
    finish_m = target_distance(3 if self.estimated_distance else profile, self.initial_distance_m, self.target_mps)
    if self.last_sample_ns is not None and self.was_over_target:
      self.overspeed_s += min(0.25, (now_ns - self.last_sample_ns) / 1e9)
    self.last_sample_ns = now_ns
    self.was_over_target = ego_mps > self.target_mps
    if ego_mps <= self.target_mps and self.reached_distance_m is None:
      self.reached_distance_m = distance_m

    if self.ramp is None:
      reserve_m = ego_mps * self.response_time_s if ego_mps > self.target_mps else 0.0
      minimum = speed_ramp(max(ego_mps, self.target_mps), self.target_mps, self.decel_limit_mps2, max_jerk_mps3=self.jerk_limit_mps3)
      # Allow one model update of travel to avoid crossing the calculated start between frames.
      if profile == 1 or self.estimated_distance or distance_m <= finish_m + reserve_m + minimum.distance_m + ego_mps * 0.05:
        self.start_distance_m = distance_m
        self.response_distance_m = reserve_m
        self.ramp = speed_ramp(minimum.start_mps, self.target_mps, self.decel_limit_mps2, distance_m - finish_m - reserve_m,
                               max_jerk_mps3=self.jerk_limit_mps3)

    planned_finish = None
    phase = "waiting"
    self.last_cap_mps = None
    if self.ramp is not None:
      self.progress_m = max(self.progress_m, self.start_distance_m - distance_m)
      self.last_cap_mps = self.ramp.speed_at_distance(self.progress_m)
      planned_finish = self.start_distance_m - self.ramp.distance_m - self.response_distance_m
      phase = "holding" if self.progress_m >= self.ramp.distance_m else "approach"
    self.was_eligible = eligible
    return {
      "phase": phase, "initial_distance_m": self.initial_distance_m, "distance_m": distance_m,
      "target_mps": self.target_mps, "target_distance_m": finish_m, "start_distance_m": self.start_distance_m,
      "planned_arrival_m": planned_finish, "observed_arrival_m": self.reached_distance_m,
      "baseline_overspeed_s": self.overspeed_s, "speed_cap_mps": self.last_cap_mps,
      "target_feasible": planned_finish is None or planned_finish >= finish_m - 1e-6,
    }


@dataclass
class Recovery:
  source: str
  started_ns: int
  cap_mps: float
  ramp: SpeedRamp | None = None

  def sample(self, now_ns: int, cruise_mps: float, accel_mps2: float) -> float:
    if self.ramp is None or self.ramp.target_mps != cruise_mps:
      self.ramp = speed_ramp(min(self.cap_mps, cruise_mps), cruise_mps, accel_mps2)
      self.started_ns = now_ns
    self.cap_mps = self.ramp.sample((now_ns - self.started_ns) / 1e9)[1]
    return self.cap_mps


class CameraShadow:
  def __init__(self, profile: int, *, decel_limit_mps2: float = PROFILE_ACCEL_MPS2, jerk_limit_mps3: float = PROFILE_JERK_MPS3,
               response_time_s: float = 0.0, estimated_distance_sources: dict[str, float] | None = None):
    self.profile = profile
    self.decel_limit_mps2 = decel_limit_mps2
    # Sources whose distance is an estimate (e.g. distance-less stock navigation warnings), with the
    # deceleration their approaches may plan; other sources use decel_limit_mps2 and the profile.
    self.estimated_distance_sources = dict(estimated_distance_sources or {})
    self.jerk_limit_mps3 = jerk_limit_mps3
    self.response_time_s = response_time_s
    self.inputs = CameraInputs()
    self.approaches: dict[str, Approach] = {}
    self.initial_distances: dict[str, float] = {}
    self.recoveries: dict[str, Recovery] = {}
    self.passed: set[str] = set()
    self.rejected: set[tuple[str, str]] = set()

  def update(self, event: CameraInput) -> None:
    reason = self.inputs.put(event)
    if reason is not None:
      self.rejected.add((event.source, reason))
      return
    if (event.event_id not in self.passed and event.state == "present" and event.road_match
        and event.received_ns - event.observed_ns <= MAX_CAMERA_AGE_NS):
      self.initial_distances.setdefault(event.event_id, event.distance_m)

  def observe(self, now_ns: int, ego_mps: float | None, cruise_mps: float | None,
              curve: SpeedCandidate | None, *, traveled_since: Callable[[int], float] | None = None) -> dict:
    eligible = cruise_mps is not None
    current, excluded = self.inputs.current(now_ns)
    excluded.extend({"source": source, "reason": reason} for source, reason in sorted(self.rejected))
    self.rejected.clear()
    report: dict = {"profile": self.profile, "eligible": eligible, "cameras": [], "excluded": excluded, "selected_candidate": None}
    candidates = [curve] if curve is not None else []
    if self.profile == 0:
      report["reason"] = "profile_off"
      return report

    active_ids = set()
    for event_id, sources in sorted(current.items()):
      winner = min(sources, key=lambda event: (-event.observed_ns, event.state != "passed", event.source))
      if event_id in self.passed:
        continue
      if any(self._conflicts(winner, event, ego_mps or 0.0) for event in sources):
        excluded.extend({"source": event.source, "reason": "conflicting_camera"} for event in sources)
        continue
      excluded.extend({"source": event.source, "reason": "duplicate_camera"} for event in sources if event is not winner)
      if winner.state == "passed":
        plan = self.approaches.get(event_id)
        if plan is not None and plan.was_eligible and plan.last_cap_mps is not None and eligible:
          self.recoveries[event_id] = Recovery(winner.source, now_ns, plan.last_cap_mps)
        self.passed.add(event_id)
        self.initial_distances.pop(event_id, None)
        report["cameras"].append({"source": winner.source, "phase": "passed"})
        continue

      if ego_mps is None or not isfinite(ego_mps) or ego_mps < 0:
        excluded.append({"source": winner.source, "reason": "vehicle_speed_unavailable"})
        continue
      initial_m = self.initial_distances[event_id]
      plan = self.approaches.get(event_id)
      if plan is None or plan.target_mps != winner.target_mps:
        plan = self.approaches[event_id] = Approach(initial_m, winner.target_mps, self.decel_limit(winner.source),
                                                    self.jerk_limit_mps3, self.response_time_s,
                                                    estimated_distance=winner.source in self.estimated_distance_sources)
      active_ids.add(event_id)
      distance_m = max(0.0, winner.distance_m - traveled_since(winner.observed_ns)) if traveled_since else winner.distance_m
      result = plan.sample(self.profile, distance_m, ego_mps, now_ns, eligible)
      report["cameras"].append({"source": winner.source, **result})
      if result["speed_cap_mps"] is not None:
        candidates.append(SpeedCandidate(f"camera:{winner.source}", result["speed_cap_mps"], now_ns))

    self.approaches = {event_id: self.approaches[event_id] for event_id in active_ids}
    if not eligible:
      self.recoveries.clear()
    for event_id, recovery in list(self.recoveries.items()):
      cap = recovery.sample(now_ns, cruise_mps, RECOVERY_ACCEL_MPS2[self.profile])
      report["cameras"].append({"source": recovery.source, "phase": "recovery", "speed_cap_mps": cap})
      candidates.append(SpeedCandidate(f"camera_recovery:{recovery.source}", cap, now_ns))
      if cap >= cruise_mps:
        del self.recoveries[event_id]

    if eligible:
      selected = select_candidate(now_ns, cruise_mps, candidates)
      report["selected_candidate"] = asdict(selected) if selected is not None else None
    return report

  def decel_limit(self, source: str) -> float:
    return self.estimated_distance_sources.get(source, self.decel_limit_mps2)

  @staticmethod
  def _conflicts(winner: CameraInput, other: CameraInput, ego_mps: float) -> bool:
    if winner.state == other.state == "passed":
      return False
    # Allow reports from different times to differ by the distance traveled between samples.
    tolerance_m = 25.0 + abs(winner.observed_ns - other.observed_ns) / 1e9 * max(0.0, ego_mps)
    if abs(winner.distance_m - other.distance_m) > tolerance_m:
      return True
    return winner.state == other.state == "present" and abs(winner.target_mps - other.target_mps) > 0.5
