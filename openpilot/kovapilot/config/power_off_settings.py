"""Automatic comma power-off time chosen in KovaLink.

The value is the number of minutes the comma may stay on after the vehicle
is switched off. 0 means "always on": the offroad time limit is skipped, but
the low car battery voltage and empty battery estimate limits still apply.
"""

import math
from typing import Any

from openpilot.kovapilot.config.kova_params import KOVA_PARAMS_KEY

AUTO_POWER_OFF_KEY = "power.auto_power_off_minutes"
AUTO_POWER_OFF_ALWAYS_ON = 0
AUTO_POWER_OFF_STEP_MIN = 30
AUTO_POWER_OFF_MAX_MIN = 24 * 60
AUTO_POWER_OFF_OPTIONS = (AUTO_POWER_OFF_ALWAYS_ON,) + tuple(
  range(AUTO_POWER_OFF_STEP_MIN, AUTO_POWER_OFF_MAX_MIN + 1, AUTO_POWER_OFF_STEP_MIN))
AUTO_POWER_OFF_DEFAULT_MIN = AUTO_POWER_OFF_MAX_MIN
# hardwared checks shutdown about twice a second; the setting is read at most this often.
AUTO_POWER_OFF_READ_INTERVAL_S = 10.


def stored_auto_power_off_minutes(params: Any) -> int | None:
  """The saved value if it is one of the allowed options, otherwise None."""
  try:
    state = params.get(KOVA_PARAMS_KEY)
  except Exception:
    return None
  values = state.get("values") if isinstance(state, dict) else None
  value = values.get(AUTO_POWER_OFF_KEY) if isinstance(values, dict) else None
  # Exact int only: JSON true or 60.0 is not an allowed option.
  if type(value) is not int or value not in AUTO_POWER_OFF_OPTIONS:
    return None
  return value


def auto_power_off_limit_s(params: Any, default_s: float) -> float:
  """Offroad seconds before power-off; default_s when nothing valid is saved."""
  minutes = stored_auto_power_off_minutes(params)
  if minutes is None:
    return default_s
  if minutes == AUTO_POWER_OFF_ALWAYS_ON:
    return math.inf
  return minutes * 60.
