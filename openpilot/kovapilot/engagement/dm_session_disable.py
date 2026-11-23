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
    return (context.disable_reason == DriverMonitoringDisableReason.NONE and
            self.config.allow_session_disable_gesture and
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

    if not self.sequence.update(context.event_time, buttons):
      return False

    if context.selfdrive_valid and not context.selfdrive_enabled:
      return True

    self.reset()
    return False
