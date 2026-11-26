import os
from pathlib import Path

from openpilot.kovapilot.config.cruise_sync_settings import publish_applied_setting, read_applied_setting
from openpilot.kovapilot.config.kova_params import KovaParamsError, KovaParamsStore, LEAD_RESTART_KEY, ParamsKovaParamsBackend
from openpilot.kovapilot.config.parked_setting import fresh_message, parked_for_settings


class LeadRestartPreference:
  def __init__(self, params):
    self.store = KovaParamsStore(ParamsKovaParamsBackend(params))
    self.profile = 0
    self.next_poll_ns = 0

  def poll(self, sm, now_ns: int) -> int | None:
    if now_ns < self.next_poll_ns or not parked_for_settings(sm, now_ns) or not fresh_message(sm, now_ns, "carControl"):
      return None
    control = sm["carControl"]
    if control.enabled or control.latActive or control.longActive or sm["selfdriveState"].active:
      return None
    self.next_poll_ns = now_ns + 500_000_000
    try:
      profile = self.store.values()[LEAD_RESTART_KEY]
    except (KovaParamsError, RuntimeError, OSError):
      profile = 0
    if profile == self.profile:
      return None
    self.profile = profile
    return profile


def _applied_path() -> Path:
  from openpilot.common.hardware.hw import Paths
  return Path(Paths.shm_path()) / f"kova-plannerd-lead-restart{os.getenv('OPENPILOT_PREFIX', '')}.json"


def publish_applied_lead_restart(profile: int) -> None:
  publish_applied_setting(_applied_path(), profile)


def applied_lead_restart(pid: int | None) -> int | None:
  value = read_applied_setting(_applied_path(), pid)
  return value if type(value) is int and value in (0, 1, 2, 3) else None
