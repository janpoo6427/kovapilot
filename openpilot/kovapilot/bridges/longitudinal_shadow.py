from dataclasses import dataclass
from math import isfinite

from openpilot.kovapilot.longitudinal.shadow import SpeedCandidate, curve_candidate, is_fresh, select_candidate


@dataclass(frozen=True)
class ShadowObservation:
  model_mono_time_ns: int
  reason: str
  cruise_speed_mps: float | None = None
  candidate: SpeedCandidate | None = None
  selected: bool = False


class LongitudinalShadow:
  """Read recorded plannerd input messages and return diagnostic observations only."""

  def __init__(self, cruise_max_kph: float, cruise_unset_kph: float, kph_to_mps: float):
    self.cruise_max_kph = cruise_max_kph
    self.cruise_unset_kph = cruise_unset_kph
    self.kph_to_mps = kph_to_mps
    self.supported = False
    self.inputs = {}

  def update(self, event) -> ShadowObservation | None:
    service = event.which()
    if service == "carParams":
      supported = event.valid and event.carParams.openpilotLongitudinalControl and not event.carParams.dashcamOnly
      if supported != self.supported:
        self.inputs.clear()
      self.supported = supported
    elif service in ("carState", "carControl", "controlsState"):
      self.inputs[service] = event
    elif service == "modelV2":
      return self.observe_model(event)
    return None

  def observe_model(self, event) -> ShadowObservation:
    now_ns = event.logMonoTime
    if not self.supported:
      return ShadowObservation(now_ns, "longitudinal_unavailable")
    if not event.valid:
      return ShadowObservation(now_ns, "invalid_model")

    for service in ("carState", "carControl", "controlsState"):
      sample = self.inputs.get(service)
      if sample is None or not sample.valid or not is_fresh(now_ns, sample.logMonoTime):
        return ShadowObservation(now_ns, f"invalid_or_stale_{service}")

    state = self.inputs["carState"].carState
    control = self.inputs["carControl"].carControl
    if not state.canValid:
      return ShadowObservation(now_ns, "invalid_can")
    cancel_pressed = any(button.pressed and button.type == "cancel" for button in state.buttonEvents)
    if state.brakePressed or state.gasPressed or cancel_pressed or control.cruiseControl.override:
      return ShadowObservation(now_ns, "driver_override")
    if not control.longActive:
      return ShadowObservation(now_ns, "longitudinal_inactive")
    if self.inputs["controlsState"].controlsState.forceDecel:
      return ShadowObservation(now_ns, "forced_deceleration")

    cruise_kph = state.vCruise
    if not isfinite(cruise_kph) or cruise_kph <= 0 or cruise_kph == self.cruise_unset_kph:
      return ShadowObservation(now_ns, "cruise_unavailable")
    cruise_mps = min(cruise_kph, self.cruise_max_kph) * self.kph_to_mps
    model = event.modelV2
    candidate = curve_candidate(now_ns, model.position.t, model.velocity.x, model.orientationRate.z)
    if candidate is None:
      return ShadowObservation(now_ns, "no_curve_candidate", cruise_mps)

    selected = select_candidate(now_ns, cruise_mps, (candidate,)) is not None
    reason = "curve_below_cruise" if selected else "cruise_is_lower_or_equal"
    return ShadowObservation(now_ns, reason, cruise_mps, candidate, selected)
