"""Turn fresh plannerd inputs into an optional curve speed ceiling."""

from math import isfinite

from openpilot.kovapilot.longitudinal.curve_slowdown import (MIN_MOVING_SPEED_MPS, CurveEstimate, CurveLimiter, CurveLimits,
                                                           estimate_model_curve, lane_change_active)
from openpilot.kovapilot.longitudinal.shadow import is_fresh


CURVE_INPUTS = ("carState", "carControl", "controlsState", "selfdriveState", "modelV2")


class CurveRuntime:
  def __init__(self, limits: CurveLimits):
    self.limits = limits
    self.limiter = CurveLimiter(jerk_mps3=limits.jerk_mps3)
    self.big_model: bool | None = None

  def eligible(self, sm, now_ns: int, cruise_mps: float | None) -> bool:
    if cruise_mps is None or not all(is_fresh(now_ns, sm.logMonoTime[service]) for service in CURVE_INPUTS):
      return False
    state = sm["carState"]
    return (isfinite(state.vEgo) and isfinite(state.aEgo) and state.vEgo >= MIN_MOVING_SPEED_MPS and
            not state.standstill and not sm["carControl"].cruiseControl.cancel)

  def invalidate(self) -> None:
    self.limiter = CurveLimiter(jerk_mps3=self.limits.jerk_mps3)
    self.big_model = None

  def speed_cap(self, sm, cruise_mps: float) -> float | None:
    model = sm["modelV2"]
    if self.big_model is not None and self.big_model != bool(model.big):
      self.invalidate()
    self.big_model = bool(model.big)
    estimate = (CurveEstimate(None, False) if lane_change_active(model) else
                estimate_model_curve(model, sm["carState"], cruise_mps, self.limits))
    return self.limiter.update(sm.logMonoTime["modelV2"], estimate, cruise_mps)
