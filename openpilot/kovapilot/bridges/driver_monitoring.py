from openpilot.cereal import log
import openpilot.cereal.messaging as messaging

from openpilot.kovapilot.config.driver_monitoring import DriverMonitoringDisableReason


_CEREAL_DISABLE_REASON = {
  DriverMonitoringDisableReason.NONE: log.DriverMonitoringState.DisableReason.none,
  DriverMonitoringDisableReason.SESSION: log.DriverMonitoringState.DisableReason.session,
  DriverMonitoringDisableReason.BUILD_CONFIG: log.DriverMonitoringState.DisableReason.buildConfig,
}


def set_disable_reason(packet, reason: DriverMonitoringDisableReason) -> None:
  packet.driverMonitoringState.disableReason = _CEREAL_DISABLE_REASON[reason]


def disabled_state_packet(reason: DriverMonitoringDisableReason, rhd: bool, valid: bool = True):
  if reason == DriverMonitoringDisableReason.NONE:
    raise ValueError("disabled packet requires a disable reason")

  packet = messaging.new_message("driverMonitoringState", valid=valid)
  state = packet.driverMonitoringState
  state.disableReason = _CEREAL_DISABLE_REASON[reason]
  state.lockout = False
  state.lockoutCount = 0
  state.lockoutMinutesRemaining = 0
  state.alert3Count = 0
  state.noResponseCount = 0
  state.noResponseForceDecel = False
  state.alwaysOn = False
  state.alwaysOnLockout = False
  state.alertLevel = "none"
  state.activePolicy = "wheeltouch"
  state.isRHD = rhd
  state.visionPolicyState.awarenessPercent = 100
  state.visionPolicyState.awarenessStep = 0
  state.visionPolicyState.isDistracted = False
  state.visionPolicyState.distractedTypes.pose = False
  state.visionPolicyState.distractedTypes.eye = False
  state.visionPolicyState.distractedTypes.phone = False
  state.visionPolicyState.distractedTypes.sleep = False
  state.visionPolicyState.uncertainOffroadAlertPercent = 0
  state.wheeltouchPolicyState.awarenessPercent = 100
  state.wheeltouchPolicyState.awarenessStep = 0
  state.wheeltouchPolicyState.driverInteracting = False
  return packet


def monitoring_is_disabled(sm) -> bool:
  return (sm.all_checks(["driverMonitoringState"]) and
          sm["driverMonitoringState"].disableReason != log.DriverMonitoringState.DisableReason.none)
