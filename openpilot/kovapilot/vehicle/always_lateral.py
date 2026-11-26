"""Always-on lateral (steering.always_on): which vehicles support it, and the CarParams flag.

The choice is made once per vehicle session: card sets ALT_EXP_ALWAYS_ON_LATERAL in
CarParams.alternativeExperience before pandad configures panda safety, so a changed
setting applies from the next start.
"""
from opendbc.car.hyundai.values import HyundaiFlags
from opendbc.safety import ALTERNATIVE_EXPERIENCE

ALWAYS_ON_STEERING_KEY = "steering.always_on"
# Panda safety modes whose steering checks and engagement (cruise, CAN FD LFA button) are covered
# by the always-on lateral safety tests.
SUPPORTED_SAFETY_MODELS = ("hyundai", "hyundaiLegacy", "hyundaiCanfd")
IGNORED_SAFETY_MODELS = ("noOutput", "silent")


def always_on_lateral_supported(CP) -> bool:
  if CP.brand != "hyundai" or CP.passive or CP.dashcamOnly or CP.notCar or len(CP.safetyConfigs) == 0:
    return False
  models = [config.safetyModel for config in CP.safetyConfigs]
  return models[-1] in SUPPORTED_SAFETY_MODELS and all(model in IGNORED_SAFETY_MODELS for model in models[:-1]) and \
    CP.steerControlType in ("torque", "angle")


def lfa_button_engages(CP) -> bool:
  # panda safety reads the LFA button only on CAN FD
  return CP.brand == "hyundai" and bool(CP.flags & HyundaiFlags.CANFD)


def always_on_lateral_enabled(CP) -> bool:
  return bool(CP.alternativeExperience & ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL)


def read_always_on_steering(store) -> bool:
  from openpilot.kovapilot.config.kova_params import KovaParamsError
  try:
    return store.values()[ALWAYS_ON_STEERING_KEY] == 1
  except (KovaParamsError, RuntimeError, KeyError):
    return False


def apply_always_on_lateral(CP, requested: bool) -> bool:
  """Set or clear the flag on a CarParams builder; returns whether it is set."""
  enabled = requested and always_on_lateral_supported(CP)
  if enabled:
    CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL
  else:
    CP.alternativeExperience &= ~ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL
  return enabled
