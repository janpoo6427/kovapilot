#!/usr/bin/env python3
"""Read-only comma-side source for driver-monitoring shadow validation.

Run from the installed openpilot root through stdin. This process only creates
subscriber sockets and writes a redacted JSONL stream to stdout.
"""

import json
import os
from pathlib import Path
import site
import time


def emit(payload) -> None:
  print(json.dumps(payload, separators=(",", ":")), flush=True)


def add_known_capnp_path() -> None:
  """Fallback for installations whose system Python omits the fixed runtime package path."""
  for root in (Path("/data/openpilot"), Path("/data/pythonpath"), Path("/usr/local/venv"), Path("/usr/local/lib")):
    if not root.is_dir():
      continue
    for package in root.rglob("capnp/__init__.py"):
      site.addsitedir(str(package.parent.parent))
      return


def main() -> None:
  add_known_capnp_path()
  import openpilot.cereal.messaging as messaging
  from openpilot.kovapilot.config.driver_monitoring import DRIVER_MONITORING_CONFIG
  from openpilot.kovapilot.config.parked_setting import parked_for_settings

  duration = max(1.0, min(float(os.getenv("DM_SHADOW_DURATION", "60")), 600.0))
  deadline = time.monotonic() + duration
  state = messaging.SubMaster(["carState", "selfdriveState", "driverMonitoringState"], frequency=20)
  car_state = messaging.sub_sock("carState", conflate=False)
  car_control = messaging.sub_sock("carControl", conflate=False)
  last_parked = False
  emit({"kind": "ready"})

  while time.monotonic() < deadline:
    state.update(50)
    now_ns = time.monotonic_ns()
    now = now_ns / 1e9
    freshness = DRIVER_MONITORING_CONFIG.input_freshness_seconds
    selfdrive_fresh = (state.all_checks(["selfdriveState"]) and
                       0 <= now - state.logMonoTime["selfdriveState"] / 1e9 < freshness)
    parked_now = selfdrive_fresh and parked_for_settings(state, now_ns)
    if last_parked and not parked_now:
      emit({"kind": "reset"})
      last_parked = False

    for packet in messaging.drain_sock(car_control, wait_for_one=False):
      if packet.valid and packet.carControl.cruiseControl.cancel:
        emit({"kind": "cancel_request", "event_time": packet.logMonoTime / 1e9})

    dm = state["driverMonitoringState"]
    for packet in messaging.drain_sock(car_state, wait_for_one=False):
      car = packet.carState
      event_time = packet.logMonoTime / 1e9
      buttons = [[str(button.type), bool(button.pressed)] for button in car.buttonEvents]
      # The stdin program also runs against the preceding deployed version.
      parked = bool(parked_now and packet.valid and car.canValid and 0 <= now - event_time < freshness and
                    car.standstill and str(car.gearShifter) == "park" and abs(car.vEgo) < 0.1)
      if not buttons and parked == last_parked:
        continue
      last_parked = parked
      emit({
        "kind": "car_state",
        "now": now,
        "event_time": event_time,
        "packet_valid": bool(packet.valid),
        "can_valid": bool(car.canValid),
        "parked": parked,
        "selfdrive_valid": bool(selfdrive_fresh),
        "selfdrive_enabled": bool(state["selfdriveState"].enabled),
        "dm_valid": bool(state.all_checks(["driverMonitoringState"])),
        "dm_lockout": bool(getattr(dm, "lockout", False)),
        "dm_alert_three": str(getattr(dm, "alertLevel", "none")) == "three",
        "buttons": buttons,
      })


if __name__ == "__main__":
  main()
