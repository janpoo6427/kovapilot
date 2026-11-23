from dataclasses import dataclass

from openpilot.kovapilot.config.driver_monitoring import DriverMonitoringDisableReason


@dataclass(frozen=True)
class DriverMonitoringStatus:
  disable_reason: DriverMonitoringDisableReason

  @property
  def disabled(self) -> bool:
    return self.disable_reason != DriverMonitoringDisableReason.NONE
