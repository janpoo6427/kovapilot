#!/usr/bin/env python3
"""Mac-side consumer for the redacted driver-monitoring shadow stream."""

import argparse
import json
import sys

from openpilot.kovapilot.config.driver_monitoring import DRIVER_MONITORING_CONFIG, DriverMonitoringDisableReason
from openpilot.kovapilot.engagement.dm_session_disable import (DriverMonitoringSessionGesture,
                                                               SessionDisableContext)


def main() -> int:
  parser = argparse.ArgumentParser(description="Evaluate read-only DM session-disable shadow events")
  parser.add_argument("--stop-after-trigger", action="store_true")
  args = parser.parse_args()

  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
  observed_presses = 0
  rejected_contexts = 0

  for line in sys.stdin:
    try:
      event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
      continue

    if event.get("kind") == "cancel_request":
      gesture.record_cancel_request(float(event["event_time"]))
      continue
    if event.get("kind") == "ready":
      print("shadow_ready read_only_subscribers=true", flush=True)
      continue
    if event.get("kind") != "car_state":
      continue

    buttons = [(str(button_type), bool(pressed)) for button_type, pressed in event.get("buttons", [])]
    observed_presses += sum(button_type == "cancel" and pressed for button_type, pressed in buttons)
    context = SessionDisableContext(
      now=float(event["now"]),
      event_time=float(event["event_time"]),
      can_valid=bool(event.get("packet_valid") and event.get("can_valid")),
      selfdrive_valid=bool(event.get("selfdrive_valid")),
      selfdrive_enabled=bool(event.get("selfdrive_enabled")),
      dm_lockout=not bool(event.get("dm_valid")) or bool(event.get("dm_lockout")),
      dm_alert_three=bool(event.get("dm_alert_three")),
      disable_reason=DriverMonitoringDisableReason.NONE,
    )
    if not gesture.allowed(context):
      rejected_contexts += 1
    if gesture.update(context, buttons):
      print("WOULD_DISABLE_SESSION=true", flush=True)
      if args.stop_after_trigger:
        break

  print(f"shadow_summary cancel_presses={observed_presses} rejected_contexts={rejected_contexts}", flush=True)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
