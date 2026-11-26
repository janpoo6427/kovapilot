from math import isfinite

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.kovapilot.config.auto_engage_settings import publish_auto_engage_status, read_auto_engage, read_auto_engage_status
from openpilot.kovapilot.config.parked_setting import fresh_message, stopped_in_park
from openpilot.kovapilot.engagement.auto_engage import AutoEngage, AutoEngageInputs
from openpilot.selfdrive.car.cruise import V_CRUISE_INITIAL, V_CRUISE_MAX
from openpilot.selfdrive.selfdrived.events import ET


IGNORED_MODES = (car.CarParams.SafetyModel.silent, car.CarParams.SafetyModel.noOutput)
ButtonType = car.CarState.ButtonEvent.Type
MANUAL_SELECTION_BUTTONS = (ButtonType.cancel, ButtonType.lkas, ButtonType.mainCruise)


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
    self.initial_cruise_speed = V_CRUISE_INITIAL
    self.policy = AutoEngage(seen_session=True)
    self.last_publish = 0
    self.last_status: tuple[str, str] | None = None

  def _start_session(self, session: int, now_ns: int) -> None:
    self.session = session
    self.requested = read_auto_engage(self.params)
    try:
      self.initial_cruise_speed = max(V_CRUISE_INITIAL, self.params.get("InitialCruiseSpeedKph", return_default=True))
      prior = read_auto_engage_status()
      self.policy = AutoEngage(seen_session=prior is not None and prior["session"] >= session)
      # Reserve this session before the first opportunity to enable. A restarted
      # process may never issue a new automatic request for the same session.
      publish_auto_engage_status(session, now_ns, self.requested, "waiting", "waiting")
    except (OSError, TypeError, ValueError):
      self.policy = AutoEngage(seen_session=True)
    self.last_status = None

  def update(self, cs, sm, events, *, initialized: bool, enabled: bool, now_ns: int) -> bool:
    device = sm["deviceState"]
    if (not sm.valid["deviceState"] or not sm.alive["deviceState"] or
        not 0 <= now_ns - sm.logMonoTime["deviceState"] <= 1_500_000_000 or
        not device.started or not 0 < device.startedMonoTime <= now_ns):
      if self.session:
        self.policy.locked = True
      return False
    if device.startedMonoTime != self.session:
      self._start_session(device.startedMonoTime, now_ns)

    request = False
    if not self.supported:
      status = ("unsupported", "unsupported")
    elif not self.requested:
      status = ("off", "off")
    else:
      request, status = self._update_supported(cs, sm, events, initialized, enabled, now_ns)
    if status != self.last_status or now_ns - self.last_publish >= 1_000_000_000:
      try:
        publish_auto_engage_status(self.session, now_ns, self.requested, *status)
      except OSError:
        self.policy.locked = True
        return False
      self.last_status = status
      self.last_publish = now_ns
    return request

  def _update_supported(self, cs, sm, events, initialized: bool, enabled: bool, now_ns: int) -> tuple[bool, tuple[str, str]]:
    driving = cs.gearShifter == car.CarState.GearShifter.drive
    manual_selection = any(button.type in MANUAL_SELECTION_BUTTONS for button in cs.buttonEvents)
    services = ("pandaStates", "driverMonitoringState", "longitudinalPlan", "carControl", "modelV2")
    fresh = all(fresh_message(sm, now_ns, service) for service in services)
    dm = sm["driverMonitoringState"]
    dm_ok = (dm.disableReason == log.DriverMonitoringState.DisableReason.none and not dm.lockout and
             not dm.alwaysOnLockout and dm.alertLevel == log.DriverMonitoringState.AlertLevel.none)
    authority = fresh and has_control_authority(self.cp, sm["pandaStates"])
    hard_stop = (manual_selection or (driving and (events.contains(ET.USER_DISABLE) or events.contains(ET.IMMEDIATE_DISABLE))) or
                 (enabled and (not authority or not dm_ok)))
    v_min = max(self.initial_cruise_speed * CV.KPH_TO_MS, self.cp.minEnableSpeed, self.cp.minSteerSpeed, 0.3)
    speed_ok = isfinite(cs.vEgo) and v_min <= cs.vEgo <= V_CRUISE_MAX * CV.KPH_TO_MS
    controls = sm["carControl"]
    ready = (
      initialized and authority and dm_ok and cs.canValid and cs.cruiseState.available and speed_ok and not cs.standstill
      and not (cs.brakePressed or cs.regenBraking or cs.brakeHoldActive or cs.parkingBrake or cs.accFaulted)
      and not (cs.steerFaultTemporary or cs.steerFaultPermanent or cs.steeringPressed or cs.buttonEvents)
      and not (sm["longitudinalPlan"].shouldStop or sm["longitudinalPlan"].fcw)
      and not any(events.contains(event_type) for event_type in (
        ET.NO_ENTRY, ET.PRE_ENABLE, ET.SOFT_DISABLE, ET.OVERRIDE_LATERAL, ET.OVERRIDE_LONGITUDINAL,
      ))
    )
    request = self.policy.request(AutoEngageInputs(
      now_ns=now_ns, parked=stopped_in_park(cs), driving=driving and cs.canValid, ready=ready,
      gas_pressed=cs.gasPressed, manual_engage=events.contains(ET.ENABLE), hard_stop=hard_stop, enabled=enabled,
    ))
    status = (("manualRequired", "manualRequired") if hard_stop else
              self.policy.statuses(enabled=enabled, lateral_active=fresh and controls.latActive, longitudinal_active=fresh and controls.longActive))
    if status == ("waiting", "waiting") and self.policy.driver_departed and not authority:
      status = ("awaitingAuthority", "awaitingAuthority")
    return request, status
