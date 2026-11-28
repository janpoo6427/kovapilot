from math import isfinite

from openpilot.kovapilot.config.parked_setting import fresh_message
from openpilot.kovapilot.config.personality_weights import LEAD_RESTART_REACTION, personality_value
from openpilot.kovapilot.longitudinal.lead_restart import LeadObservation, LeadRestartResponse


class LeadRestartRuntime:
  def __init__(self):
    self.response: LeadRestartResponse | None = None

  def select(self, personality) -> bool:
    """Follow the driving personality. False: no lead restart limit for it (the planner as it is); the departure
    state starts over the next time a personality with a reaction is selected."""
    reaction = personality_value(LEAD_RESTART_REACTION, personality)
    if reaction is None:
      self.response = None
    elif self.response is None:
      self.response = LeadRestartResponse(reaction)
    else:
      self.response.set_reaction(reaction)
    return self.response is not None

  @staticmethod
  def _observation(sm, now_ns: int) -> LeadObservation | None:
    if not fresh_message(sm, now_ns, "radarState"):
      return None
    radar = sm["radarState"]
    if any(radar.radarErrors.to_dict().values()):
      return None
    lead = radar.leadOne
    if (not lead.present or not lead.radar or lead.radarTrackId < 0 or
        not all(isfinite(value) for value in (lead.dRel, lead.yRel, lead.vLead, lead.modelProb)) or
        not 0.0 < lead.dRel <= 20.0 or abs(lead.yRel) > 1.0 or lead.modelProb < 0.5):
      return None
    return LeadObservation(lead.radarTrackId, lead.dRel, lead.vLead, sm.logMonoTime["radarState"])

  def constrain(self, sm, *, now_ns: int, reset_state: bool, should_stop: bool, target_accel: float,
                previous_accel: float, dt: float) -> tuple[float, bool]:
    state, control = sm["carState"], sm["carControl"]
    manual_resume = any(button.pressed and str(button.type) in ("accelCruise", "resumeCruise", "setCruise") for button in state.buttonEvents)
    allowed = (
      not reset_state and all(fresh_message(sm, now_ns, service) for service in ("carState", "carControl", "controlsState", "selfdriveState", "modelV2"))
      and sm["selfdriveState"].active and control.enabled and control.longActive and not control.cruiseControl.cancel
      and not sm["controlsState"].forceDecel and state.canValid and str(state.gearShifter) == "drive" and isfinite(state.vEgo)
      and not (state.brakePressed or state.regenBraking or state.parkingBrake or state.brakeHoldActive or state.gasPressed or state.accFaulted or manual_resume)
    )
    standing = state.standstill and abs(state.vEgo) < 0.1
    return self.response.constrain(
      self._observation(sm, now_ns) if allowed else None, allowed=allowed, standing=standing,
      should_stop=should_stop, target_accel=target_accel, previous_accel=previous_accel, now_ns=now_ns, dt=dt,
    )
