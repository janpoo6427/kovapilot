"""Pedal resume (engagement.auto_engage on Hyundai CAN FD with openpilot longitudinal).

The comma-side start conditions follow carrot's automatic cruise start (selfdrive/car/cruise.py
_update_cruise_state) for the moments the driver releases a pedal; panda safety
(ALT_EXP_PEDAL_RESUME) allows controls only on those same pedal releases, seen on the car bus.
Starts that carrot makes without a driver pedal release (soft hold, forward collision distance,
desired speed, traffic light) are not made here.
"""

from dataclasses import dataclass

GAS_TOK_NS = 400_000_000              # carrot _gas_tok_timer: accelerator tap shorter than 0.4 s
AUTO_GAS_TOK_SPEED_KPH = 0            # carrot AutoGasTokSpeed default
AUTO_GAS_CANCEL_SPEED_KPH = 30        # carrot AutoGasCancelSpeed default
DRIVE_WAIT_NS = 20_000_000_000        # carrot autoCruiseControl_cancel_timer: 20 s after entering Drive
GAS_WHILE_BRAKING_NS = 5_000_000_000  # carrot "Cruise off (gas pressed while braking)" timer
MAX_STEERING_ANGLE_DEG = 70.0         # carrot AUTO_CRUISE_MAX_STEERING_ANGLE
LOW_SPEED_STEERING_ANGLE_DEG = 20.0   # carrot brake release below AutoGasTokSpeed
LOW_SPEED_LEAD_DISTANCE_M = 20.0      # carrot "Cruise on (lead car)"
AUTHORITY_WAIT_NS = 500_000_000       # wait for panda controlsAllowed after the release


@dataclass(frozen=True)
class PedalResumeInputs:
  now_ns: int
  enabled: bool
  driving: bool
  cruise_available: bool
  brake_pressed: bool
  gas_pressed: bool
  v_ego: float
  v_ego_cluster: float
  a_ego: float
  steering_angle_deg: float
  blinker: bool
  hold: bool                      # brake hold (AVH) or parking brake
  cancel_pressed: bool
  set_resume_released: bool
  lead_distance: float            # 0 when no lead
  disengage_on_accelerator: bool
  authority: bool                 # panda controls allowed
  entry_ok: bool                  # no NO_ENTRY and the existing entry checks pass


class PedalResume:
  def __init__(self):
    self.cancel_state = False
    self.drive_since: int | None = None
    self.blocked_until = 0
    self.brake_prev = False
    self.gas_prev = False
    self.gas_since: int | None = None
    self.pending_until: int | None = None

  def update(self, i: PedalResumeInputs) -> bool:
    if i.cancel_pressed:
      self.cancel_state = True
    if i.set_resume_released:
      self.cancel_state = False
      self.drive_since = None if not i.driving else i.now_ns - DRIVE_WAIT_NS
    if not i.driving:
      self.drive_since = None
    elif self.drive_since is None:
      self.drive_since = i.now_ns

    brake_released = self.brake_prev and not i.brake_pressed
    gas_released = self.gas_prev and not i.gas_pressed
    gas_tok = gas_released and self.gas_since is not None and i.now_ns - self.gas_since < GAS_TOK_NS
    if i.gas_pressed:
      if not self.gas_prev:
        self.gas_since = i.now_ns
      elif i.now_ns - self.gas_since > GAS_TOK_NS and i.a_ego < -0.5:
        self.blocked_until = i.now_ns + GAS_WHILE_BRAKING_NS
    self.brake_prev, self.gas_prev = i.brake_pressed, i.gas_pressed

    if i.enabled or i.brake_pressed or i.cancel_pressed or self._blocked(i):
      self.pending_until = None
      return False

    if (brake_released or gas_released) and self._starts(i, brake_released, gas_released, gas_tok):
      self.pending_until = i.now_ns + AUTHORITY_WAIT_NS
    if self.pending_until is None:
      return False
    if i.now_ns > self.pending_until:
      self.pending_until = None
      return False
    if not (i.authority and i.entry_ok):
      return False
    self.pending_until = None
    return True

  def _blocked(self, i: PedalResumeInputs) -> bool:
    return (not i.driving or not i.cruise_available or self.cancel_state or i.hold or
            abs(i.steering_angle_deg) >= MAX_STEERING_ANGLE_DEG or i.now_ns < self.blocked_until or
            self.drive_since is None or i.now_ns - self.drive_since < DRIVE_WAIT_NS)

  @staticmethod
  def _starts(i: PedalResumeInputs, brake_released: bool, gas_released: bool, gas_tok: bool) -> bool:
    v_kph = int(i.v_ego_cluster * 3.6 + 0.5)
    if gas_released:
      if gas_tok and not i.disengage_on_accelerator and v_kph >= AUTO_GAS_TOK_SPEED_KPH:
        return True
      if 0 < i.lead_distance < i.v_ego * 0.8:
        return i.v_ego < 1.0
      if v_kph < AUTO_GAS_CANCEL_SPEED_KPH or i.blinker:
        return False
      return not i.disengage_on_accelerator and v_kph >= AUTO_GAS_TOK_SPEED_KPH
    if brake_released and not i.blinker:
      if v_kph > AUTO_GAS_TOK_SPEED_KPH:
        return True
      return abs(i.steering_angle_deg) < LOW_SPEED_STEERING_ANGLE_DEG and 0 < i.lead_distance < LOW_SPEED_LEAD_DISTANCE_M
    return False

  def waiting(self) -> bool:
    return not self.cancel_state
