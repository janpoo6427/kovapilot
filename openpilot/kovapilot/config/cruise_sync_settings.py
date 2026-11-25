from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, KovaParamsError, KovaParamsStore


def read_cruise_sync_settings(store: KovaParamsStore) -> tuple[bool, bool]:
  try:
    values = store.values()
  except (KovaParamsError, RuntimeError):
    return False, False
  return values[CRUISE_SYNC_KEY], values[CRUISE_SYNC_SHADOW_KEY]
