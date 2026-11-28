from dataclasses import dataclass


STABLE_STOP_NS = 500_000_000
DEPARTURE_CONFIRM_NS = 250_000_000
MIN_DEPARTURE_M = 0.8
MIN_DEPARTURE_MPS = 0.35
MAX_STOPPED_LEAD_SPEED_MPS = 0.25



@dataclass(frozen=True)
class LeadObservation:
  track_id: int
  distance_m: float
  speed_mps: float
  sample_ns: int


class LeadRestartResponse:
  """Every reaction shares the same departure evidence. Only the reaction (hold ns, ramp m/s^3) changes; it comes
  from the driving personality (kovapilot/config/personality_weights.py LEAD_RESTART_REACTION)."""
  def __init__(self, reaction: tuple[int, float]):
    self.set_reaction(reaction)
    self.reset()

  def set_reaction(self, reaction: tuple[int, float]) -> None:
    self.delay_ns, self.jerk = reaction

  def reset(self) -> None:
    self.track: LeadObservation | None = None
    self.stopped_since: int | None = None
    self.moving_since: int | None = None
    self.baseline_distance: float | None = None
    self.resume_since: int | None = None
    self.armed = False

  def _observe(self, lead: LeadObservation, standing: bool) -> None:
    if self.track is None or lead.track_id != self.track.track_id:
      self.stopped_since = self.moving_since = self.resume_since = None
      self.baseline_distance = None
    elif lead.sample_ns <= self.track.sample_ns:
      return
    self.track = lead
    if standing and abs(lead.speed_mps) <= MAX_STOPPED_LEAD_SPEED_MPS:
      if self.stopped_since is None:
        self.stopped_since = lead.sample_ns
      if lead.sample_ns - self.stopped_since >= STABLE_STOP_NS:
        self.armed = True
        self.baseline_distance = lead.distance_m
      self.moving_since = None
    else:
      self.stopped_since = None
      if self.baseline_distance is not None and lead.distance_m - self.baseline_distance >= MIN_DEPARTURE_M and lead.speed_mps >= MIN_DEPARTURE_MPS:
        if self.moving_since is None:
          self.moving_since = lead.sample_ns
      else:
        self.moving_since = None

  def constrain(self, lead: LeadObservation | None, *, allowed: bool, standing: bool, should_stop: bool,
                target_accel: float, previous_accel: float, now_ns: int, dt: float) -> tuple[float, bool]:
    if not allowed or (not standing and self.resume_since is None):
      self.reset()
      return target_accel, should_stop
    if lead is None:
      self.track = None
      self.stopped_since = self.moving_since = self.baseline_distance = self.resume_since = None
    else:
      self._observe(lead, standing)
    if not self.armed:
      return target_accel, should_stop
    if should_stop or target_accel <= 0.0:
      self.resume_since = None
      return target_accel, should_stop or standing
    if standing and self.moving_since is None:
      self.resume_since = None

    if self.resume_since is None:
      confirmed = self.track is not None and self.moving_since is not None and self.track.sample_ns - self.moving_since >= DEPARTURE_CONFIRM_NS
      if not confirmed:
        return min(target_accel, 0.0), should_stop or standing
      self.resume_since = now_ns

    if standing and now_ns - self.resume_since < self.delay_ns:
      return min(target_accel, 0.0), True

    limit = max(previous_accel, 0.0) + self.jerk * dt
    if not standing and 0.0 < target_accel <= limit:
      self.reset()
    return min(target_accel, limit), False
