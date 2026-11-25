from dataclasses import dataclass
from math import isfinite

from openpilot.kovapilot.longitudinal.curve_slowdown import (CurveApproach, CurveEstimate, CurveLimiter, estimate_model_curve,
                                                           lane_change_active)
from openpilot.kovapilot.longitudinal.shadow import SpeedCandidate, curve_candidate, is_fresh


REQUIRED_INPUTS = ("carState", "carControl", "controlsState", "selfdriveState")


@dataclass(frozen=True)
class ShadowObservation:
  model_mono_time_ns: int
  reason: str
  cruise_speed_mps: float | None = None
  candidate: SpeedCandidate | None = None
  selected: bool = False
  approach: CurveApproach | None = None
  baseline_candidate: SpeedCandidate | None = None
  big_model: bool | None = None
  ego_speed_mps: float | None = None


def _blocked(service, message) -> str | None:
  if service == "carState":
    if not message.canValid:
      return "invalid_can"
    cancel = any(button.pressed and button.type == "cancel" for button in message.buttonEvents)
    if message.brakePressed or message.gasPressed or cancel:
      return "driver_override"
  elif service == "carControl":
    if message.cruiseControl.override or message.cruiseControl.cancel:
      return "driver_override"
    if not message.longActive:
      return "longitudinal_inactive"
  elif service == "controlsState" and message.forceDecel:
    return "forced_deceleration"
  elif service == "selfdriveState" and not message.enabled:
    return "longitudinal_inactive"
  return None


class LongitudinalShadow:
  """Read recorded plannerd input messages and return diagnostic observations only."""

  def __init__(self, cruise_max_kph: float, cruise_unset_kph: float, kph_to_mps: float, *,
               geometry_only: bool = False, curve_enabled: bool = True):
    self.cruise_max_kph = cruise_max_kph
    self.cruise_unset_kph = cruise_unset_kph
    self.kph_to_mps = kph_to_mps
    self.geometry_only = geometry_only
    self.curve_enabled = curve_enabled
    self.supported = False
    self.inputs = {}
    self.curve = CurveLimiter()
    self.big_model: bool | None = None
    self.last_model_ns: int | None = None

  def update(self, event) -> ShadowObservation | None:
    service = event.which()
    if service == "carParams":
      supported = event.valid and event.carParams.openpilotLongitudinalControl and not event.carParams.dashcamOnly
      if supported != self.supported:
        self.inputs.clear()
        self.curve = CurveLimiter()
      self.supported = supported
    elif service in REQUIRED_INPUTS:
      self.inputs[service] = event
      if not event.valid or _blocked(service, getattr(event, service)):
        self.curve = CurveLimiter()
    elif service == "modelV2":
      return self.observe_model(event)
    return None

  def reject(self, timestamp_ns: int, reason: str) -> ShadowObservation:
    self.curve = CurveLimiter()
    return ShadowObservation(timestamp_ns, reason)

  def observe_model(self, event) -> ShadowObservation:
    now_ns = event.logMonoTime
    repeated = self.last_model_ns is not None and now_ns <= self.last_model_ns
    if not repeated:
      self.last_model_ns = now_ns
    if not self.geometry_only and not self.supported:
      return self.reject(now_ns, "longitudinal_unavailable")

    for service in (("carState",) if self.geometry_only else REQUIRED_INPUTS):
      sample = self.inputs.get(service)
      if sample is None or not sample.valid or not is_fresh(now_ns, sample.logMonoTime):
        return self.reject(now_ns, f"invalid_or_stale_{service}")
      if not self.geometry_only and (reason := _blocked(service, getattr(sample, service))):
        return self.reject(now_ns, reason)

    state = self.inputs["carState"].carState
    if not state.canValid:
      return self.reject(now_ns, "invalid_can")
    cruise_kph = state.vCruise
    if not isfinite(cruise_kph) or not 0 < cruise_kph < self.cruise_unset_kph:
      return self.reject(now_ns, "cruise_unavailable")
    if not isfinite(state.vEgo) or not isfinite(state.aEgo) or state.vEgo < 0:
      return self.reject(now_ns, "invalid_vehicle_motion")
    if state.standstill:
      return self.reject(now_ns, "standstill")
    cruise_mps = min(cruise_kph, self.cruise_max_kph) * self.kph_to_mps
    if repeated:
      return ShadowObservation(now_ns, "repeated_model", cruise_mps, big_model=self.big_model, ego_speed_mps=state.vEgo)
    model = event.modelV2
    if event.valid and self.big_model != bool(model.big):
      self.big_model = bool(model.big)
      self.curve = CurveLimiter()

    estimate = CurveEstimate(None, False)
    baseline = None
    lane_change = False
    if event.valid:
      baseline = curve_candidate(now_ns, model.position.t, model.velocity.x, model.orientationRate.z)
      lane_change = lane_change_active(model)
      if not lane_change:
        estimate = estimate_model_curve(model, state, cruise_mps)
    cap = self.curve.update(now_ns, estimate, cruise_mps) if self.curve_enabled and not self.geometry_only else None
    candidate = SpeedCandidate("model_curve", cap, now_ns) if cap is not None and cap < cruise_mps else None
    selected = candidate is not None
    if not event.valid:
      reason = "invalid_model"
    elif lane_change:
      reason = "lane_change_geometry"
    elif not estimate.complete:
      reason = "incomplete_curve_geometry"
    elif self.geometry_only:
      reason = "geometry_only"
    elif not self.curve_enabled:
      reason = "curve_setting_disabled"
    else:
      reason = "curve_below_cruise" if selected else "no_curve_candidate"
    return ShadowObservation(now_ns, reason, cruise_mps, candidate, selected, estimate.approach, baseline,
                             self.big_model, state.vEgo)
