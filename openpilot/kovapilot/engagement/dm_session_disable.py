from dataclasses import dataclass

from openpilot.kovapilot.config.driver_monitoring import DriverMonitoringConfig, DriverMonitoringDisableReason
from openpilot.kovapilot.engagement.dm_cancel_gesture import CancelPressSequence


@dataclass(frozen=True)
class SessionDisableContext:
  now: float
  event_time: float
  can_valid: bool
  selfdrive_valid: bool
  selfdrive_enabled: bool
  parked: bool
  dm_lockout: bool
  dm_alert_three: bool
  disable_reason: DriverMonitoringDisableReason


class DriverMonitoringSessionGesture:
  """Pure policy wrapper shared by dmonitoringd, integration tests, and shadow tooling."""

  def __init__(self, config: DriverMonitoringConfig):
    self.config = config
    self.sequence = CancelPressSequence(
      required_presses=config.cancel_press_count,
      window_seconds=config.cancel_window_seconds,
      echo_window_seconds=config.cancel_echo_window_seconds,
    )

  def record_cancel_request(self, event_time: float) -> None:
    self.sequence.record_cancel_request(event_time)

  def reset(self) -> None:
    self.sequence.reset()

  def allowed(self, context: SessionDisableContext) -> bool:
    # The same gesture turns monitoring off (NONE) and back on (SESSION).
    # A build-level disable cannot be changed at runtime.
    return (context.disable_reason in (DriverMonitoringDisableReason.NONE, DriverMonitoringDisableReason.SESSION) and
            self.config.allow_session_disable_gesture and
            context.parked and
            context.selfdrive_valid and
            not context.selfdrive_enabled and
            not context.dm_lockout and
            not context.dm_alert_three)

  def update(self, context: SessionDisableContext, buttons: list[tuple[str, bool]]) -> bool:
    if not self.allowed(context):
      self.reset()
      return False

    fresh = (context.can_valid and
             0 <= context.now - context.event_time < self.config.input_freshness_seconds)
    if not fresh:
      self.reset()
      return False

    return self.sequence.update(context.event_time, buttons)


def toggled_session_disabled(reason: DriverMonitoringDisableReason) -> bool:
  """Value to store in SESSION_DISABLED_PARAM after a completed gesture."""
  if reason == DriverMonitoringDisableReason.BUILD_CONFIG:
    raise ValueError("build-level disable cannot be toggled")
  return reason == DriverMonitoringDisableReason.NONE
