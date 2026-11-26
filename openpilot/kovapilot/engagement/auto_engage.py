"""One automatic start per driving session, using existing control authority."""

from dataclasses import dataclass


READY_NS = 1_000_000_000


@dataclass(frozen=True)
class AutoEngageInputs:
  now_ns: int
  parked: bool = False
  driving: bool = False
  ready: bool = False
  gas_pressed: bool = False
  manual_engage: bool = False
  hard_stop: bool = False
  enabled: bool = False


class AutoEngage:
  def __init__(self, *, seen_session: bool):
    self.locked = seen_session
    self.park_observed = False
    self.driver_departed = False
    self.ready_since: int | None = None
    self.used = False
    self.was_enabled = False
    self.gas_pressed_prev = True

  def request(self, inputs: AutoEngageInputs) -> bool:
    if inputs.hard_stop or (self.was_enabled and not inputs.enabled):
      self.locked = True
    self.was_enabled = inputs.enabled
    if inputs.enabled or inputs.manual_engage:
      self.used = True
    if self.locked or self.used:
      return False

    if inputs.parked:
      self.park_observed = True
      self.driver_departed = False
    elif not inputs.driving:
      self.park_observed = False
      self.driver_departed = False
    elif self.park_observed and inputs.gas_pressed and not self.gas_pressed_prev:
      self.driver_departed = True
    self.gas_pressed_prev = inputs.gas_pressed

    if not inputs.driving or not self.driver_departed or inputs.gas_pressed or not inputs.ready:
      self.ready_since = None
      return False
    if self.ready_since is None:
      self.ready_since = inputs.now_ns
    if inputs.now_ns - self.ready_since < READY_NS:
      return False
    self.used = True
    return True

  def statuses(self, *, enabled: bool, lateral_active: bool, longitudinal_active: bool) -> tuple[str, str]:
    if enabled:
      return ("active" if lateral_active else "paused", "active" if longitudinal_active else "paused")
    if self.locked or self.used:
      return "manualRequired", "manualRequired"
    return "waiting", "waiting"
