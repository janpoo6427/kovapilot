import os
from pathlib import Path

from openpilot.kovapilot.config.cruise_sync_settings import publish_applied_setting, read_applied_setting
from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY, KovaParamsError, KovaParamsStore, ParamsKovaParamsBackend


def read_curve_slowdown(params) -> bool:
  try:
    return KovaParamsStore(ParamsKovaParamsBackend(params)).values()[CURVE_SLOWDOWN_KEY] is True
  except (KovaParamsError, RuntimeError, OSError):
    return False


def _applied_path() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.shm_path()) / f"kova-plannerd-curve-slowdown{os.getenv('OPENPILOT_PREFIX', '')}.json"


def publish_applied_curve_slowdown(enabled: bool, *, supported: bool) -> None:
  publish_applied_setting(_applied_path(), enabled if supported else "unsupported")


def applied_curve_slowdown(pid: int | None) -> bool | str | None:
  value = read_applied_setting(_applied_path(), pid)
  return value if type(value) is bool or value == "unsupported" else None
