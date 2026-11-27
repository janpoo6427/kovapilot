"""One automatic start per driving session, at the driver accelerator press.

The start is requested at the moment the accelerator is pressed (janpoo 11/27 22:28), not at its release:
panda (ALT_EXP_PEDAL_RESUME) allows controls on that press, and its heartbeat check ends the authority again
within about 3 s when openpilot does not engage. Engaging with the pedal still held starts in the normal
accelerator override, so openpilot takes over the speed after the release.

From ignition on the start waits (gray cruise display) and is made on the first press in Drive, also when
pulling away from a stop (janpoo 11/27 23:12). Park must have been seen in this session; reversing or Neutral
before Drive keeps it waiting. Braking before or while stopped in Drive is normal driving and does not lock it;
the driver turning cruise off on purpose does (the runtime reports it as hard_stop).

Driver monitoring is not an input (janpoo 11/28 00:23), and steering does not block the start. The system
conditions must hold for 1 s before the press in any gear, so a press right after shifting into Drive starts.
"""

from dataclasses import dataclass


READY_NS = 1_000_000_000              # the system conditions are stable this long before the press
# After the press, the start waits this long while the pedal stays pressed for panda controlsAllowed and the
# moment conditions (brake released, no entry block, the plan no longer stopping once the car rolls). It stays
# below the panda heartbeat check (about 3 s), which ends the authority of a press that did not engage.
LAUNCH_WINDOW_NS = 1_500_000_000


@dataclass(frozen=True)
class AutoEngageInputs:
  now_ns: int
  parked: bool = False
  driving: bool = False
  ready: bool = False                 # system conditions, stable for READY_NS before the press (any gear)
  entry_ok: bool = False              # moment conditions (pedals, buttons, entry events, plan) at the start
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

    # Park seen in this session arms the start; Reverse or Neutral before Drive keep it armed
    if inputs.parked:
      self.park_observed = True
      self.driver_departed = False
    elif not inputs.driving:
      self.driver_departed = False
    elif self.park_observed and gas_press:
      self.driver_departed = True

    # the system conditions count in any gear: shifting P, R, N, D does not restart the 1 s
    if not inputs.ready:
      self.ready_since = None
      self.pending_until = None
      return False
    if self.ready_since is None:
      self.ready_since = inputs.now_ns
    if not inputs.driving or not self.park_observed:
      self.pending_until = None
      return False
    if gas_press and inputs.now_ns - self.ready_since >= READY_NS:
      self.pending_until = inputs.now_ns + LAUNCH_WINDOW_NS
    if self.pending_until is None:
      return False
    if inputs.now_ns > self.pending_until or not inputs.gas_pressed:
      self.pending_until = None
      return False
    if not (inputs.authority and inputs.entry_ok):
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
