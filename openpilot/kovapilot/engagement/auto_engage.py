"""One automatic start per driving session, at the driver accelerator press.

The start is requested at the moment the accelerator is pressed (janpoo 11/27 22:28), not at its release:
panda (ALT_EXP_PEDAL_RESUME) allows controls on that press, and its heartbeat check ends the authority again
within about 3 s when openpilot does not engage. Engaging with the pedal still held starts in the normal
accelerator override, so openpilot takes over the speed after the release.
"""

from dataclasses import dataclass


READY_NS = 1_000_000_000              # the entry conditions are stable this long before the press
AUTHORITY_WAIT_NS = 500_000_000       # panda controlsAllowed from the press reaches selfdrived a little later


@dataclass(frozen=True)
class AutoEngageInputs:
  now_ns: int
  parked: bool = False
  driving: bool = False
  ready: bool = False                 # entry conditions except panda authority and the accelerator itself
  authority: bool = False             # panda controls allowed
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
    self.pending_until: int | None = None
    self.used = False
    self.was_enabled = False
    self.gas_pressed_prev = True

  def request(self, inputs: AutoEngageInputs) -> bool:
    if inputs.hard_stop or (self.was_enabled and not inputs.enabled):
      self.locked = True
    self.was_enabled = inputs.enabled
    if inputs.enabled or inputs.manual_engage:
      self.used = True
    gas_press = inputs.gas_pressed and not self.gas_pressed_prev
    self.gas_pressed_prev = inputs.gas_pressed
    if self.locked or self.used:
      self.pending_until = None
      return False

    if inputs.parked:
      self.park_observed = True
      self.driver_departed = False
    elif not inputs.driving:
      self.park_observed = False
      self.driver_departed = False
    elif self.park_observed and gas_press:
      self.driver_departed = True

    if not inputs.driving or not self.park_observed or not inputs.ready:
      self.ready_since = None
      self.pending_until = None
      return False
    if self.ready_since is None:
      self.ready_since = inputs.now_ns
    if gas_press and inputs.now_ns - self.ready_since >= READY_NS:
      self.pending_until = inputs.now_ns + AUTHORITY_WAIT_NS
    if self.pending_until is None:
      return False
    if inputs.now_ns > self.pending_until:
      self.pending_until = None
      return False
    if not inputs.authority:
      return False
    self.pending_until = None
    self.used = True
    return True

  def statuses(self, *, enabled: bool, lateral_active: bool, longitudinal_active: bool) -> tuple[str, str]:
    if enabled:
      return ("active" if lateral_active else "paused", "active" if longitudinal_active else "paused")
    if self.locked or self.used:
      return "manualRequired", "manualRequired"
    return "waiting", "waiting"
