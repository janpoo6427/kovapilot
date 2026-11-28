"""Stock HDA sync panda flag (ALT_EXP_STOCK_HDA_SYNC), chosen once per vehicle session.

card sets the flag in CarParams.alternativeExperience before pandad configures panda safety when
steering.stock_hda_sync is on (default on), so a changed setting applies from the next start. With the flag, the
Hyundai CAN FD angle steering controller presses MAIN or LFA towards the stock camera so the camera keeps its own
MAIN and LFA in step with openpilot (opendbc/car/hyundai/stock_hda_sync.py), and panda allows only those presses.
Off: nothing is pressed, as before.
"""
from opendbc.car.hyundai.stock_hda_sync import stock_hda_sync_supported as vehicle_supported
from opendbc.safety import ALTERNATIVE_EXPERIENCE

STOCK_HDA_SYNC_KEY = "steering.stock_hda_sync"
IGNORED_SAFETY_MODELS = ("noOutput", "silent")


def stock_hda_sync_supported(CP) -> bool:
  if CP.passive or CP.dashcamOnly or CP.notCar or len(CP.safetyConfigs) == 0 or not vehicle_supported(CP):
    return False
  models = [config.safetyModel for config in CP.safetyConfigs]
  return models[-1] == "hyundaiCanfd" and all(model in IGNORED_SAFETY_MODELS for model in models[:-1])


def read_stock_hda_sync(store) -> bool:
  from openpilot.kovapilot.config.kova_params import KovaParamsError
  try:
    return store.values()[STOCK_HDA_SYNC_KEY] == 1
  except (KovaParamsError, RuntimeError, KeyError, OSError):
    return False


def apply_stock_hda_sync(CP, requested: bool) -> bool:
  """Set or clear the flag on a CarParams builder; returns whether it is set."""
  enabled = requested and stock_hda_sync_supported(CP)
  if enabled:
    CP.alternativeExperience |= ALTERNATIVE_EXPERIENCE.STOCK_HDA_SYNC
  else:
    CP.alternativeExperience &= ~ALTERNATIVE_EXPERIENCE.STOCK_HDA_SYNC
  return enabled
