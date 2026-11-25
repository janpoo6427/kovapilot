"""Offline ACC and LFA selection and request model."""

from dataclasses import dataclass
from enum import Enum, auto

from openpilot.kovapilot.engagement.always_lateral import LateralInputs, LateralState, next_lateral_state


class CruiseState(Enum):
  UNKNOWN = auto()
  OFF = auto()
  STANDBY = auto()
  ACTIVE = auto()

  @property
  def selected(self) -> bool:
    return self in (CruiseState.STANDBY, CruiseState.ACTIVE)


@dataclass(frozen=True)
class EngagementInputs:
  cruise: CruiseState = CruiseState.UNKNOWN
  lfa_toggle_requested: bool = False  # A single confirmed button event.
  always_steering: bool = False
  braking: bool = False
  standstill: bool = True
  below_normal_steering_speed: bool = True
  lateral_permitted: bool = False  # Includes vehicle-specific limits.
  longitudinal_permitted: bool = False
  safety_disable: bool = False
  driver_steering: bool = False
  driver_release_confirmed: bool = False


@dataclass(frozen=True)
class EngagementState:
  cruise: CruiseState = CruiseState.UNKNOWN
  cruise_on_observed: bool = False
  lateral: LateralState = LateralState.DISABLED
  lateral_requested: bool = False
  longitudinal_requested: bool = False

  @property
  def lfa_selected(self) -> bool:
    return self.lateral is not LateralState.DISABLED


def next_engagement_state(state: EngagementState, inputs: EngagementInputs) -> EngagementState:
  cruise_turned_on = state.cruise is CruiseState.OFF and inputs.cruise.selected
  cruise_on_observed = inputs.cruise.selected and (state.cruise_on_observed or cruise_turned_on) and not inputs.safety_disable
  # An explicit LFA toggle takes priority when both inputs arrive together.
  lfa_selected = not state.lfa_selected if inputs.lfa_toggle_requested else state.lfa_selected or cruise_turned_on
  lateral = next_lateral_state(state.lateral, LateralInputs(
    mode_enabled=lfa_selected,
    engage_requested=inputs.lfa_toggle_requested or cruise_turned_on,
    lateral_permitted=inputs.lateral_permitted,
    safety_disable=inputs.safety_disable,
    driver_steering=inputs.driver_steering,
    driver_release_confirmed=inputs.driver_release_confirmed,
  ))
  below_normal_limits = inputs.braking or inputs.below_normal_steering_speed
  return EngagementState(
    cruise=inputs.cruise,
    cruise_on_observed=cruise_on_observed,
    lateral=lateral,
    lateral_requested=lateral is LateralState.ACTIVE and not inputs.standstill and
                      (inputs.always_steering or not below_normal_limits),
    longitudinal_requested=cruise_on_observed and inputs.cruise is CruiseState.ACTIVE and
                           inputs.longitudinal_permitted and not inputs.braking,
  )
