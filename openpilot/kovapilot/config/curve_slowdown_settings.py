import os
from pathlib import Path

from openpilot.kovapilot.config.cruise_sync_settings import publish_applied_setting, read_applied_setting
from openpilot.kovapilot.config.kova_params import (CURVE_SLOWDOWN_KEY, NAV_TURN_SLOWDOWN_KEY, KovaParamsError, KovaParamsStore,
                                                   ParamsKovaParamsBackend)
from openpilot.kovapilot.config.parked_setting import fresh_message, parked_for_settings


CURVE_SETTING_POLL_NS = 500_000_000


def read_curve_slowdown(params, key: str = CURVE_SLOWDOWN_KEY) -> bool:
  try:
    return KovaParamsStore(ParamsKovaParamsBackend(params)).values()[key] == 1
  except (KovaParamsError, RuntimeError, OSError):
    return False


class CurveSlowdownPreference:
  """Apply a saved change only while stationary in Park with control released.

  key: cruise.curve_slowdown (vision curves) or cruise.nav_turn_slowdown (navigation turns), the two
  switches of the curve slowdown item."""

  def __init__(self, params, key: str = CURVE_SLOWDOWN_KEY):
    self.params = params
    self.key = key
    self.enabled = False
    self.next_poll_ns = 0

  def poll(self, sm, now_ns: int) -> bool | None:
    if now_ns < self.next_poll_ns or not parked_for_settings(sm, now_ns) or not fresh_message(sm, now_ns, "carControl"):
      return None
    control = sm["carControl"]
    if control.enabled or control.latActive or control.longActive or sm["selfdriveState"].active:
      return None
    self.next_poll_ns = now_ns + CURVE_SETTING_POLL_NS
    enabled = read_curve_slowdown(self.params, self.key)
    if enabled == self.enabled:
      return None
    self.enabled = enabled
    return enabled


def _applied_path(key: str = CURVE_SLOWDOWN_KEY) -> Path:
  from openpilot.common.hardware.hw import Paths
  name = "curve-slowdown" if key == CURVE_SLOWDOWN_KEY else "nav-turn-slowdown"
  return Path(Paths.shm_path()) / f"kova-plannerd-{name}{os.getenv('OPENPILOT_PREFIX', '')}.json"


def publish_applied_curve_slowdown(enabled: bool, *, supported: bool, key: str = CURVE_SLOWDOWN_KEY) -> None:
  publish_applied_setting(_applied_path(key), int(enabled) if supported else "unsupported")


def applied_curve_slowdown(pid: int | None, key: str = CURVE_SLOWDOWN_KEY) -> int | str | None:
  value = read_applied_setting(_applied_path(key), pid)
  return value if value in (0, 1, "unsupported") else None


def applied_nav_turn_slowdown(pid: int | None) -> int | str | None:
  return applied_curve_slowdown(pid, NAV_TURN_SLOWDOWN_KEY)
