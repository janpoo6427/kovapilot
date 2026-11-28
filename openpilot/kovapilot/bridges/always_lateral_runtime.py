"""Always-on lateral (steering.always_on): steering assist that outlives cruise.

Panda safety (ALT_EXP_ALWAYS_ON_LATERAL) allows lateral commands without cruise after a
driver engagement while the car moves and the RX checks are valid, also while the driver brakes.
This runtime owns the openpilot side, which must stay within that permission:

- arm: a cruise engagement (openpilot enabled rising edge), or the LFA button where the
  safety mode reads it (Hyundai/Kia/Genesis CAN FD). Refused while an entry block is present.
- disarm (needs a new request): LFA button again, P or R, a system or steering fault (as
  openpilot would soft or immediately disable), terminal driver monitoring alert or lockout,
  or panda safety blocking our steering frames.
- alert: turned off by the driver (LFA button, or a gear change to N, P or R) it shows the small
  "Always-on Steering Off" with the short disengage sound; any other cause, also a real fault at the
  same moment as a gear change, warns with "TAKE CONTROL" (janpoo 11/28).
- pause (resumes by itself): regen braking, driver steering until released, and in controlsd
  standstill and steering faults that openpilot only warns about. The brake pedal ends cruise
  (longitudinal) only; steering continues while the driver brakes.
"""
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.common.realtime import DT_CTRL
from openpilot.kovapilot.bridges.driver_monitoring import monitoring_is_disabled
from openpilot.kovapilot.engagement.always_lateral import LateralInputs, LateralState, next_lateral_state
from openpilot.kovapilot.vehicle.always_lateral import always_on_lateral_enabled, lfa_button_engages
from openpilot.selfdrive.selfdrived.events import ET, EVENTS

EventName = log.OnroadEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter
AlertLevel = log.DriverMonitoringState.AlertLevel

# Events about cruise and longitudinal control only, or handled as a pause.
NOT_LATERAL_EVENTS = frozenset((
  EventName.accFaulted, EventName.radarFault, EventName.radarTempUnavailable, EventName.cruiseDisabled,
  EventName.speedTooLow, EventName.belowEngageSpeed, EventName.wrongCarMode, EventName.wrongCruiseMode,
  EventName.resumeBlocked, EventName.buttonCancel, EventName.pedalPressed,
))
# Gears the driver selects to leave Drive: their own SOFT_DISABLE (wrongGear) is the gear change, not a fault.
GEAR_CHANGE_GEARS = (GearShifter.neutral, GearShifter.park, GearShifter.reverse)
GEAR_CHANGE_EVENTS = frozenset((EventName.wrongGear,))
# The driver must release the wheel this long before steering resumes.
DRIVER_RELEASE_FRAMES = int(0.5 / DT_CTRL)
# Panda safety blocking this many frames between two pandaStates while we steer means it
# does not allow our lateral control (a single frame can race a brake press).
TX_BLOCKED_LIMIT = 5


def has_event_type(events, event_type: str, ignored=frozenset()) -> bool:
  return any(event_type in EVENTS.get(name, {}) for name in events.names if name not in NOT_LATERAL_EVENTS and name not in ignored)


class AlwaysLateralRuntime:
  def __init__(self, CP):
    self.available = always_on_lateral_enabled(CP)
    self.lfa_button = self.available and lfa_button_engages(CP)
    self.state = LateralState.DISABLED
    self.released_frames = DRIVER_RELEASE_FRAMES
    self.enabled_prev = False
    self.owner_prev = False  # always-on lateral was the only lateral owner
    self.tx_blocked_prev: int | None = None
    self.output_prev = False
    self.active = False

  @property
  def engaged(self) -> bool:
    return self.state is not LateralState.DISABLED

  def _safety_rejected(self, sm, steering_alone: bool) -> bool:
    if not sm.updated['pandaStates']:
      return False
    blocked = sum(ps.safetyTxBlocked for ps in sm['pandaStates'])
    rejected = (self.tx_blocked_prev is not None and steering_alone and self.output_prev and
                blocked - self.tx_blocked_prev >= TX_BLOCKED_LIMIT)
    self.tx_blocked_prev = blocked
    self.output_prev = steering_alone
    return rejected

  def update(self, CS, sm, events, enabled: bool) -> None:
    """Call once per selfdrived step after the state machine; adds the alert events."""
    if not self.available:
      return

    lfa_pressed = self.lfa_button and any(be.type == ButtonType.lkas and be.pressed for be in CS.buttonEvents)
    cruise_engaged = enabled and not self.enabled_prev
    self.enabled_prev = enabled

    dm = sm['driverMonitoringState']
    dm_terminal = not monitoring_is_disabled(sm) and (dm.lockout or dm.alertLevel == AlertLevel.three)
    park_or_reverse = CS.gearShifter in (GearShifter.park, GearShifter.reverse)
    rejected = self._safety_rejected(sm, self.active and not enabled)
    system_fault = has_event_type(events, ET.IMMEDIATE_DISABLE) or has_event_type(events, ET.SOFT_DISABLE)
    # a gear change out of Drive with nothing else wrong at that moment (janpoo 11/28)
    gear_change_only = (CS.gearShifter in GEAR_CHANGE_GEARS and not (dm_terminal or rejected) and
                        not has_event_type(events, ET.IMMEDIATE_DISABLE, GEAR_CHANGE_EVENTS) and
                        not has_event_type(events, ET.SOFT_DISABLE, GEAR_CHANGE_EVENTS))

    toggle_off = lfa_pressed and self.engaged
    request = cruise_engaged or (lfa_pressed and not self.engaged and not has_event_type(events, ET.NO_ENTRY))

    self.released_frames = 0 if CS.steeringPressed else min(self.released_frames + 1, DRIVER_RELEASE_FRAMES)
    was_engaged = self.engaged
    self.state = next_lateral_state(self.state, LateralInputs(
      mode_enabled=not toggle_off,
      engage_requested=request,
      lateral_permitted=not (park_or_reverse or dm_terminal),
      safety_disable=system_fault or rejected,
      driver_steering=CS.steeringPressed,
      driver_release_confirmed=self.released_frames >= DRIVER_RELEASE_FRAMES,
    ))
    self.active = self.state is LateralState.ACTIVE and not CS.regenBraking

    # alerts only matter while openpilot is not engaged, otherwise its own alerts apply
    owner = self.engaged and not enabled
    if owner and not self.owner_prev:
      events.add(EventName.alwaysLateralEngaged)
    elif was_engaged and not self.engaged and not enabled:
      user_off = toggle_off or gear_change_only
      events.add(EventName.alwaysLateralDisengaged if user_off else EventName.alwaysLateralUnavailable)
    self.owner_prev = owner
