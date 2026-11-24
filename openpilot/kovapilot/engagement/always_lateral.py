"""Offline model of independent lane-centering engagement."""

from dataclasses import dataclass
from enum import Enum, auto


class LateralState(Enum):
  DISABLED = auto()
  ACTIVE = auto()
  DRIVER_OVERRIDE = auto()


@dataclass(frozen=True)
class LateralInputs:
  mode_enabled: bool = False
  engage_requested: bool = False  # A new, explicit driver request.
  cancel_requested: bool = False
  lateral_permitted: bool = False  # Independent authorization and operating limits.
  safety_disable: bool = False
  driver_steering: bool = False
  driver_release_confirmed: bool = False


def next_lateral_state(state: LateralState, inputs: LateralInputs) -> LateralState:
  """Model intent while independent permission is valid.

  Brake and cruise state belong to longitudinal control. If either causes
  lateral permission to be withdrawn, the model disarms and needs a new request.
  """
  if not inputs.mode_enabled or inputs.cancel_requested or inputs.safety_disable or not inputs.lateral_permitted:
    return LateralState.DISABLED

  if state is LateralState.DISABLED and not inputs.engage_requested:
    return LateralState.DISABLED

  if inputs.driver_steering or (state is LateralState.DRIVER_OVERRIDE and not inputs.driver_release_confirmed):
    return LateralState.DRIVER_OVERRIDE

  return LateralState.ACTIVE
