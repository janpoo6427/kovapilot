"""Pedal resume panda flag (ALT_EXP_PEDAL_RESUME), chosen once per vehicle session.

card sets the flag in CarParams.alternativeExperience before pandad configures panda safety when
engagement.auto_engage is on, so a changed setting reaches panda from the next start. With the flag,
panda allows controls when the driver releases the brake or the accelerator while cruise is available.
"""
from opendbc.car.hyundai.values import HyundaiFlags
from opendbc.safety import ALTERNATIVE_EXPERIENCE

IGNORED_SAFETY_MODELS = ("noOutput", "silent")


def pedal_resume_supported(CP) -> bool:
  if (CP.brand != "hyundai" or not CP.flags & HyundaiFlags.CANFD or not CP.openpilotLongitudinalControl or CP.pcmCruise or
      CP.passive or CP.dashcamOnly or CP.notCar or len(CP.safetyConfigs) == 0):
    return False
  models = [config.safetyModel for config in CP.safetyConfigs]
  return models[-1] == "hyundaiCanfd" and all(model in IGNORED_SAFETY_MODELS for model in models[:-1])


def pedal_resume_enabled(CP) -> bool:
  return bool(CP.alternativeExperience & ALTERNATIVE_EXPERIENCE.PEDAL_RESUME)


def apply_pedal_resume(CP, requested: bool) -> bool:
  """Set or clear the flag on a CarParams builder; returns whether it is set."""
  enabled = requested and pedal_resume_supported(CP)
  if enabled:
    CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.PEDAL_RESUME
  else:
    CP.alternativeExperience &= ~ALTERNATIVE_EXPERIENCE.PEDAL_RESUME
  return enabled
