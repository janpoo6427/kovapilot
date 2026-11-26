import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from openpilot.kovapilot.config.kova_params import AUTO_ENGAGE_KEY, KovaParamsError, KovaParamsStore, ParamsKovaParamsBackend


def read_auto_engage(params) -> bool:
  try:
    return KovaParamsStore(ParamsKovaParamsBackend(params)).values()[AUTO_ENGAGE_KEY] == 1
  except (KovaParamsError, RuntimeError, OSError):
    return False


def status_path() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.shm_path()) / f"kova-auto-engage{os.getenv('OPENPILOT_PREFIX', '')}.json"


def read_auto_engage_status() -> dict | None:
  try:
    value = json.loads(status_path().read_text())
  except FileNotFoundError:
    return None
  if not isinstance(value, dict) or type(value.get("session")) is not int:
    raise ValueError("invalid auto-engage session")
  return value


def applied_auto_engage(pid: int | None, now_ns: int) -> dict | None:
  if pid is None:
    return None
  try:
    status = read_auto_engage_status()
  except (OSError, ValueError):
    return None
  if (status is None or status.get("pid") != pid or type(status.get("time")) is not int or
      not 0 <= now_ns - status["time"] <= 2_000_000_000 or type(status.get("value")) is not int or status["value"] not in (0, 1)):
    return None
  allowed = ("waiting", "awaitingAuthority", "active", "paused", "manualRequired", "unsupported", "off")
  if any(status.get(key) not in allowed for key in ("lateral", "longitudinal")):
    return None
  return {key: status[key] for key in ("value", "lateral", "longitudinal")}


def publish_auto_engage_status(session: int, now_ns: int, requested: bool, lateral: str, longitudinal: str) -> None:
  path = status_path()
  with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
    temporary = Path(stream.name)
    try:
      json.dump({"session": session, "pid": os.getpid(), "time": now_ns, "value": int(requested),
                 "lateral": lateral, "longitudinal": longitudinal}, stream)
      stream.flush()
      temporary.replace(path)
    finally:
      temporary.unlink(missing_ok=True)
