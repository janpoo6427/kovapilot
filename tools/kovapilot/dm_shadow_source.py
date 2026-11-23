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

  duration = max(1.0, min(float(os.getenv("DM_SHADOW_DURATION", "60")), 600.0))
  deadline = time.monotonic() + duration
  state = messaging.SubMaster(["selfdriveState", "driverMonitoringState"], frequency=20)
  car_state = messaging.sub_sock("carState", conflate=False)
  car_control = messaging.sub_sock("carControl", conflate=False)
  emit({"kind": "ready"})

  while time.monotonic() < deadline:
    state.update(50)
    now = time.monotonic()

    for packet in messaging.drain_sock(car_control, wait_for_one=False):
      if packet.valid and packet.carControl.cruiseControl.cancel:
        emit({"kind": "cancel_request", "event_time": packet.logMonoTime / 1e9})

    dm = state["driverMonitoringState"]
    for packet in messaging.drain_sock(car_state, wait_for_one=False):
      buttons = [[str(button.type), bool(button.pressed)] for button in packet.carState.buttonEvents]
      if not buttons:
        continue
      emit({
        "kind": "car_state",
        "now": now,
        "event_time": packet.logMonoTime / 1e9,
        "packet_valid": bool(packet.valid),
        "can_valid": bool(packet.carState.canValid),
        "selfdrive_valid": bool(state.all_checks(["selfdriveState"])),
        "selfdrive_enabled": bool(state["selfdriveState"].enabled),
        "dm_valid": bool(state.all_checks(["driverMonitoringState"])),
        "dm_lockout": bool(getattr(dm, "lockout", False)),
        "dm_alert_three": str(getattr(dm, "alertLevel", "none")) == "three",
        "buttons": buttons,
      })


if __name__ == "__main__":
  main()
