from math import isfinite

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.kovapilot.config.auto_engage_settings import publish_auto_engage_status, read_auto_engage, read_auto_engage_status
from openpilot.kovapilot.config.parked_setting import fresh_message, stopped_in_park
from openpilot.kovapilot.engagement.auto_engage import AutoEngage, AutoEngageInputs
from openpilot.kovapilot.engagement.pedal_resume import PedalResume, PedalResumeInputs
from openpilot.kovapilot.vehicle.pedal_resume import pedal_resume_enabled
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX
from openpilot.selfdrive.selfdrived.events import ET, EVENTS


IGNORED_MODES = (car.CarParams.SafetyModel.silent, car.CarParams.SafetyModel.noOutput)
ButtonType = car.CarState.ButtonEvent.Type
# The driver turning cruise off on purpose locks the automatic start: CANCEL, MAIN, and LFA when it turns
# always-on lateral off. LFA turning always-on lateral on keeps the start waiting (janpoo 11/27 23:12).
MANUAL_SELECTION_BUTTONS = (ButtonType.cancel, ButtonType.mainCruise)
SET_RESUME_BUTTONS = (ButtonType.accelCruise, ButtonType.decelCruise, ButtonType.setCruise, ButtonType.resumeCruise)
ENTRY_BLOCKING_EVENTS = (ET.NO_ENTRY, ET.SOFT_DISABLE, ET.IMMEDIATE_DISABLE, ET.USER_DISABLE)
# the automatic start is made at the accelerator press, so the press itself does not block it
ACCELERATOR_OVERRIDE = log.OnroadEvent.EventName.gasPressedOverride
# braking (also regen) before the start is normal driving: it does not lock the start
PEDAL_PRESSED = log.OnroadEvent.EventName.pedalPressed
# The saved preference is re-read while driving. Turning it off stops any later
# automatic start at once; turning it on only arms the next departure from Park.
PREFERENCE_POLL_NS = 1_000_000_000


def contains_except(events, event_type: str, ignored) -> bool:
  return any(event_type in EVENTS.get(name, {}) for name in events.names if name != ignored)


def has_control_authority(cp, pandas) -> bool:
  configs = cp.safetyConfigs
  if not configs or len(pandas) < len(configs):
    return False
  active_pandas = 0
  for index, panda in enumerate(pandas):
    if panda.safetyRxChecksInvalid or len(panda.faults):
      return False
    if index >= len(configs):
      if panda.safetyModel not in IGNORED_MODES:
        return False
      continue
    config = configs[index]
    if (panda.safetyModel != config.safetyModel or panda.safetyParam != config.safetyParam or
        panda.alternativeExperience != cp.alternativeExperience):
      return False
    if panda.safetyModel not in IGNORED_MODES:
      active_pandas += 1
      if not panda.controlsAllowed:
        return False
  return active_pandas > 0


class AutoEngageRuntime:
  def __init__(self, cp, params):
    self.supported = cp.openpilotLongitudinalControl and not (cp.pcmCruise or cp.dashcamOnly or cp.passive or cp.notCar)
    self.cp = cp
    self.params = params
    self.requested = False
    self.session = 0
    self.policy = AutoEngage(seen_session=True)
    self.last_publish = 0
    self.last_status: tuple[str, str] | None = None
    self.next_preference_read = 0
    # pedal resume: re-engage on the driver's brake release or accelerator press (panda ALT_EXP_PEDAL_RESUME)
    self.pedal = PedalResume() if self.supported and pedal_resume_enabled(cp) else None
    self.disengage_on_accelerator = False

  def _start_session(self, session: int, now_ns: int) -> None:
    self.session = session
    self.requested = read_auto_engage(self.params)
    self.next_preference_read = now_ns + PREFERENCE_POLL_NS
    try:
      prior = read_auto_engage_status()
      self.policy = AutoEngage(seen_session=prior is not None and prior["session"] >= session)
      # Reserve this session before the first opportunity to enable. A restarted
      # process may never issue a new automatic request for the same session.
      publish_auto_engage_status(session, now_ns, self.requested, "waiting", "waiting")
    except (OSError, TypeError, ValueError):
      self.policy = AutoEngage(seen_session=True)
    self.last_status = None

  def update(self, cs, sm, events, *, initialized: bool, enabled: bool, now_ns: int, lateral_engaged: bool = False) -> bool:
    device = sm["deviceState"]
    if (not sm.valid["deviceState"] or not sm.alive["deviceState"] or
        not 0 <= now_ns - sm.logMonoTime["deviceState"] <= 1_500_000_000 or
        not device.started or not 0 < device.startedMonoTime <= now_ns):
      if self.session:
        self.policy.locked = True
      return False
    if device.startedMonoTime != self.session:
      self._start_session(device.startedMonoTime, now_ns)
    elif now_ns >= self.next_preference_read:
      self.requested = read_auto_engage(self.params)
      self.next_preference_read = now_ns + PREFERENCE_POLL_NS

    request = False
    pedal_request = self.pedal is not None and self._update_pedal(cs, sm, events, initialized, enabled, now_ns)
    if not self.supported:
      status = ("unsupported", "unsupported")
    elif not self.requested:
      status = ("off", "off")
    else:
      request, status = self._update_supported(cs, sm, events, initialized, enabled, now_ns, lateral_engaged)
      request = request or pedal_request
      if self.pedal is not None and not enabled and status == ("manualRequired", "manualRequired") and self.pedal.waiting():
        status = ("waiting", "waiting")
    if status != self.last_status or now_ns - self.last_publish >= 1_000_000_000:
      try:
        publish_auto_engage_status(self.session, now_ns, self.requested, *status)
      except OSError:
        self.policy.locked = True
        return False
      self.last_status = status
      self.last_publish = now_ns
    return request

  def _update_pedal(self, cs, sm, events, initialized: bool, enabled: bool, now_ns: int) -> bool:
    try:
      lead = sm["radarState"].leadOne
      lead_distance = lead.dRel if lead.present and fresh_message(sm, now_ns, "radarState") else 0.0
    except KeyError:
      lead_distance = 0.0
    authority = fresh_message(sm, now_ns, "pandaStates") and has_control_authority(self.cp, sm["pandaStates"])
    entry_ok = initialized and cs.canValid and not any(events.contains(event_type) for event_type in ENTRY_BLOCKING_EVENTS)
    return self.pedal.update(PedalResumeInputs(
      now_ns=now_ns, enabled=enabled, driving=cs.gearShifter == car.CarState.GearShifter.drive and cs.canValid,
      cruise_available=cs.cruiseState.available, brake_pressed=cs.brakePressed, gas_pressed=cs.gasPressed,
      v_ego=cs.vEgo, v_ego_cluster=cs.vEgoCluster, a_ego=cs.aEgo, steering_angle_deg=cs.steeringAngleDeg,
      blinker=cs.leftBlinker or cs.rightBlinker, hold=cs.brakeHoldActive or cs.parkingBrake,
      cancel_pressed=any(button.type == ButtonType.cancel for button in cs.buttonEvents),
      set_resume_released=any(button.type in SET_RESUME_BUTTONS and not button.pressed for button in cs.buttonEvents),
      lead_distance=lead_distance, disengage_on_accelerator=self.disengage_on_accelerator,
      authority=authority, entry_ok=entry_ok,
    ))

  def _update_supported(self, cs, sm, events, initialized: bool, enabled: bool, now_ns: int,
                        lateral_engaged: bool) -> tuple[bool, tuple[str, str]]:
    driving = cs.gearShifter == car.CarState.GearShifter.drive
    # lateral_engaged: always-on lateral before this step, so an LFA press now turns it off
    manual_selection = any(button.type in MANUAL_SELECTION_BUTTONS or (button.type == ButtonType.lkas and lateral_engaged)
                           for button in cs.buttonEvents)
    services = ("pandaStates", "driverMonitoringState", "longitudinalPlan", "carControl", "modelV2")
    fresh = all(fresh_message(sm, now_ns, service) for service in services)
    dm = sm["driverMonitoringState"]
    dm_ok = (dm.disableReason == log.DriverMonitoringState.DisableReason.none and not dm.lockout and
             not dm.alwaysOnLockout and dm.alertLevel == log.DriverMonitoringState.AlertLevel.none)
    authority = fresh and has_control_authority(self.cp, sm["pandaStates"])
    # a brake press disables an engaged openpilot as before; before the start it does not lock
    user_disable = events.contains(ET.USER_DISABLE) if enabled else contains_except(events, ET.USER_DISABLE, PEDAL_PRESSED)
    hard_stop = (manual_selection or (driving and (user_disable or events.contains(ET.IMMEDIATE_DISABLE))) or
                 (enabled and (not authority or not dm_ok)))
    # any speed from a stop on (no 40 km/h floor, also at standstill, janpoo 11/27 23:12); the car minimum stays
    speed_ok = isfinite(cs.vEgo) and self.cp.minEnableSpeed <= cs.vEgo <= V_CRUISE_MAX * CV.KPH_TO_MS
    controls = sm["carControl"]
    # system conditions, stable for 1 s before the press
    ready = (
      initialized and fresh and dm_ok and cs.canValid and cs.cruiseState.available
      and not (cs.accFaulted or cs.steerFaultTemporary or cs.steerFaultPermanent)
    )
    # moment conditions at the start itself, shortly after the press: a brake released just before the press
    # does not block it, and neither does the stopping plan of a car still at standstill once it rolls
    entry_ok = (
      speed_ok
      and not (cs.brakePressed or cs.regenBraking or cs.brakeHoldActive or cs.parkingBrake)
      and not (cs.steeringPressed or cs.buttonEvents)
      and not (sm["longitudinalPlan"].shouldStop or sm["longitudinalPlan"].fcw)
      and not any(events.contains(event_type) for event_type in (
        ET.NO_ENTRY, ET.PRE_ENABLE, ET.SOFT_DISABLE, ET.OVERRIDE_LATERAL,
      ))
      and not contains_except(events, ET.OVERRIDE_LONGITUDINAL, ACCELERATOR_OVERRIDE)
    )
    request = self.policy.request(AutoEngageInputs(
      now_ns=now_ns, parked=stopped_in_park(cs), driving=driving and cs.canValid, ready=ready, entry_ok=entry_ok,
      authority=authority,
      gas_pressed=cs.gasPressed, manual_engage=events.contains(ET.ENABLE), hard_stop=hard_stop, enabled=enabled,
    ))
    status = (("manualRequired", "manualRequired") if hard_stop else
              self.policy.statuses(enabled=enabled, lateral_active=fresh and controls.latActive, longitudinal_active=fresh and controls.longActive))
    if status == ("waiting", "waiting") and self.policy.driver_departed and not authority:
      status = ("awaitingAuthority", "awaitingAuthority")
    return request, status
