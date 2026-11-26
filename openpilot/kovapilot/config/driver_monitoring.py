from dataclasses import dataclass
from enum import IntEnum


SESSION_DISABLED_PARAM = "DriverMonitoringSessionDisabled"


class DriverMonitoringDisableReason(IntEnum):
  NONE = 0
  SESSION = 1
  BUILD_CONFIG = 2


@dataclass(frozen=True)
class DriverMonitoringConfig:
  enabled: bool = True
  allow_session_disable_gesture: bool = True
  cancel_press_count: int = 3
  cancel_window_seconds: float = 3.0
  input_freshness_seconds: float = 0.25
  cancel_echo_window_seconds: float = 0.15


# Turning monitoring off for every build is a source-level decision. The only runtime
# switch is the parked three-cancel gesture: it toggles SESSION_DISABLED_PARAM, which
# stays set across ignition cycles and reboots until the same gesture turns monitoring
# back on. It must not be exposed through profiles, remote settings, or the on-device UI.
DRIVER_MONITORING_ENABLED = True
ALLOW_SESSION_DISABLE_GESTURE = True

DRIVER_MONITORING_CONFIG = DriverMonitoringConfig(
  enabled=DRIVER_MONITORING_ENABLED,
  allow_session_disable_gesture=ALLOW_SESSION_DISABLE_GESTURE,
)


def disable_reason(params, config: DriverMonitoringConfig = DRIVER_MONITORING_CONFIG) -> DriverMonitoringDisableReason:
  if not config.enabled:
    return DriverMonitoringDisableReason.BUILD_CONFIG
  if params.get_bool(SESSION_DISABLED_PARAM):
    return DriverMonitoringDisableReason.SESSION
  return DriverMonitoringDisableReason.NONE


def monitoring_enabled(params, config: DriverMonitoringConfig = DRIVER_MONITORING_CONFIG) -> bool:
  return disable_reason(params, config) == DriverMonitoringDisableReason.NONE
