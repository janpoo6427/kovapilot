"""Onroad screen brightness modes stored in the kova_params contract.

The upstream auto-brightness pipeline (camera exposure, CIE curve, 10 s filter,
backlight floor and UI dimming below it) stays unchanged. This module only maps
the filtered upstream value to the user-selected onroad target.
"""
from dataclasses import dataclass

from openpilot.kovapilot.config.kova_params import (BRIGHTNESS_MODE_KEY, BRIGHTNESS_MODE_AUTO, BRIGHTNESS_MODE_AUTO_DARK,
                                                 BRIGHTNESS_MODE_FIXED, KOVA_PARAMS_KEY)

MODE_KEY = BRIGHTNESS_MODE_KEY
FIXED_KEY = "display.onroad_brightness_fixed"

MODE_AUTO = BRIGHTNESS_MODE_AUTO
MODE_AUTO_DARK = BRIGHTNESS_MODE_AUTO_DARK
MODE_FIXED = BRIGHTNESS_MODE_FIXED
MODES = (MODE_AUTO, MODE_AUTO_DARK, MODE_FIXED)

FIXED_MIN = 10
FIXED_MAX = 100
DEFAULT_FIXED = 50

# Auto (dark) keeps the upstream daylight end (100%) and lowers the night floor.
# Values below the backlight floor are produced by the UI dim overlay, not the backlight.
AUTO_DARK_MIN = 10.0


@dataclass(frozen=True)
class DisplayBrightnessSettings:
  mode: int = MODE_AUTO
  fixed: int = DEFAULT_FIXED


DEFAULT_SETTINGS = DisplayBrightnessSettings()


def settings_from_values(values) -> DisplayBrightnessSettings:
  """Read display settings from KovaParams values; invalid entries fall back to defaults."""
  if not isinstance(values, dict):
    return DEFAULT_SETTINGS

  mode = values.get(MODE_KEY, MODE_AUTO)
  if type(mode) is not int or mode not in MODES:
    mode = MODE_AUTO

  fixed = values.get(FIXED_KEY, DEFAULT_FIXED)
  if type(fixed) is not int or not FIXED_MIN <= fixed <= FIXED_MAX:
    fixed = DEFAULT_FIXED

  return DisplayBrightnessSettings(mode=mode, fixed=fixed)


def load_display_brightness(params) -> DisplayBrightnessSettings:
  """Display-only reader for the UI. Never raises and never writes Params."""
  try:
    state = params.get(KOVA_PARAMS_KEY)
  except Exception:
    return DEFAULT_SETTINGS
  if not isinstance(state, dict):
    return DEFAULT_SETTINGS
  return settings_from_values(state.get("values"))


def onroad_brightness(filtered: float, settings: DisplayBrightnessSettings, *, onroad: bool,
                      auto_valid: bool, alert_active: bool, auto_min: float) -> float:
  """Map the filtered upstream brightness (0-100) to the selected onroad target.

  filtered: upstream value after its first-order filter, before the backlight floor.
  auto_valid: the road camera light estimate is live (upstream light_sensor >= 0).
  alert_active: an onroad alert is shown; the target never goes below upstream auto then.
  auto_min: upstream BRIGHTNESS_MIN, the lowest value upstream auto produces.
  """
  if not onroad or settings.mode == MODE_AUTO:
    return filtered

  if settings.mode == MODE_AUTO_DARK:
    if not auto_valid:
      return filtered
    span = 100.0 - auto_min
    level = (filtered - auto_min) / span if span > 0 else 1.0
    level = min(max(level, 0.0), 1.0)
    target = AUTO_DARK_MIN + level * (100.0 - AUTO_DARK_MIN)
  elif settings.mode == MODE_FIXED:
    target = float(min(max(settings.fixed, FIXED_MIN), FIXED_MAX))
  else:
    return filtered

  if alert_active:
    target = max(target, filtered)
  return target
