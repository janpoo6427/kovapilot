import os
from pathlib import Path

from openpilot.kovapilot.config.cruise_sync_settings import publish_applied_setting, read_applied_setting
from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY, KovaParamsError, KovaParamsStore, ParamsKovaParamsBackend
from openpilot.kovapilot.config.parked_setting import fresh_message, parked_for_settings


CURVE_SETTING_POLL_NS = 500_000_000


def read_curve_slowdown(params) -> bool:
  try:
    return KovaParamsStore(ParamsKovaParamsBackend(params)).values()[CURVE_SLOWDOWN_KEY] == 1
  except (KovaParamsError, RuntimeError, OSError):
    return False


class CurveSlowdownPreference:
  """Apply a saved change only while stationary in Park with control released."""

  def __init__(self, params):
    self.params = params
    self.enabled = False
    self.next_poll_ns = 0

  def poll(self, sm, now_ns: int) -> bool | None:
    if now_ns < self.next_poll_ns or not parked_for_settings(sm, now_ns) or not fresh_message(sm, now_ns, "carControl"):
      return None
    control = sm["carControl"]
    if control.enabled or control.latActive or control.longActive or sm["selfdriveState"].active:
      return None
    self.next_poll_ns = now_ns + CURVE_SETTING_POLL_NS
    enabled = read_curve_slowdown(self.params)
    if enabled == self.enabled:
      return None
    self.enabled = enabled
    return enabled


def _applied_path() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.shm_path()) / f"kova-plannerd-curve-slowdown{os.getenv('OPENPILOT_PREFIX', '')}.json"


def publish_applied_curve_slowdown(enabled: bool, *, supported: bool) -> None:
  publish_applied_setting(_applied_path(), int(enabled) if supported else "unsupported")


def applied_curve_slowdown(pid: int | None) -> int | str | None:
  value = read_applied_setting(_applied_path(), pid)
  return value if value in (0, 1, "unsupported") else None
