import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, CRUISE_SYNC_SHADOW_KEY, KovaParamsError, KovaParamsStore


def read_cruise_sync_settings(store: KovaParamsStore) -> tuple[bool, bool]:
  try:
    values = store.values()
  except (KovaParamsError, RuntimeError):
    return False, False
  return values[CRUISE_SYNC_KEY], values[CRUISE_SYNC_SHADOW_KEY]


def _applied_path() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.shm_path()) / f"kova-card-cruise-sync{os.getenv('OPENPILOT_PREFIX', '')}.json"


def publish_applied_cruise_sync(enabled: bool) -> None:
  path = _applied_path()
  with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
    temporary = Path(stream.name)
    try:
      json.dump({"pid": os.getpid(), "value": enabled}, stream)
      stream.flush()
      temporary.replace(path)
    finally:
      temporary.unlink(missing_ok=True)


def applied_cruise_sync(card_pid: int | None) -> bool | None:
  if card_pid is None or card_pid <= 0:
    return None
  try:
    data = json.loads(_applied_path().read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if isinstance(data, dict) and data.get("pid") == card_pid and type(data.get("value")) is bool:
    return data["value"]
  return None
