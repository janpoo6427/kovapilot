#!/usr/bin/env python3
import time

import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.realtime import DT_DMON, Ratekeeper, config_realtime_process
from openpilot.kovapilot.bridges.driver_monitoring import disabled_state_packet, set_disable_reason
from openpilot.kovapilot.config.driver_monitoring import (DRIVER_MONITORING_CONFIG, SESSION_DISABLED_PARAM,
                                                          DriverMonitoringDisableReason, disable_reason)
from openpilot.kovapilot.engagement.dm_session_disable import (DriverMonitoringSessionGesture,
                                                               SessionDisableContext)
from openpilot.selfdrive.monitoring.policy import DriverMonitoring


def dmonitoringd_thread():
  config_realtime_process([0, 1, 2, 3], 5)

  params = Params()
  pm = messaging.PubMaster(['driverMonitoringState'])
  sm = messaging.SubMaster(['driverStateV2', 'extrinsicsCalibration', 'carState', 'selfdriveState', 'modelV2'], frequency=1 / DT_DMON)
  car_state_sock = messaging.sub_sock('carState', conflate=False)
  car_control_sock = messaging.sub_sock('carControl', conflate=False)

  DM = DriverMonitoring(rhd_saved=params.get_bool("IsRhdDetected"), always_on=params.get_bool("AlwaysOnDM"))
  demo_mode = False
  session_gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
  rk = Ratekeeper(1 / DT_DMON, print_delay_threshold=None)
  reason = disable_reason(params)

  # Keep publishing at 20 Hz even when dmonitoringmodeld is intentionally stopped.
  while True:
    sm.update(0)
    now = time.monotonic()

    for packet in messaging.drain_sock(car_control_sock, wait_for_one=False):
      if packet.valid and packet.carControl.cruiseControl.cancel:
        session_gesture.record_cancel_request(packet.logMonoTime / 1e9)

    dm_state = DM.get_state_packet(valid=True).driverMonitoringState
    for packet in messaging.drain_sock(car_state_sock, wait_for_one=False):
      event_time = packet.logMonoTime / 1e9
      buttons = [(str(button.type), button.pressed) for button in packet.carState.buttonEvents]
      selfdrive_fresh = (sm.all_checks(['selfdriveState']) and
                         0 <= now - sm.logMonoTime['selfdriveState'] / 1e9 < DRIVER_MONITORING_CONFIG.input_freshness_seconds)
      context = SessionDisableContext(
        now=now,
        event_time=event_time,
        can_valid=packet.valid and packet.carState.canValid,
        selfdrive_valid=selfdrive_fresh,
        selfdrive_enabled=sm['selfdriveState'].enabled,
        dm_lockout=dm_state.lockout,
        dm_alert_three=str(dm_state.alertLevel) == 'three',
        disable_reason=reason,
      )
      if session_gesture.update(context, buttons):
        params.put_bool(SESSION_DISABLED_PARAM, True)
        reason = DriverMonitoringDisableReason.SESSION
        break

    if rk.frame % 40 == 0:
      reason = disable_reason(params)
      demo_mode = params.get_bool("IsDriverViewEnabled")
      DM.always_on = params.get_bool("AlwaysOnDM")

    if reason != DriverMonitoringDisableReason.NONE:
      dat = disabled_state_packet(reason, params.get_bool("IsRhdDetected"), valid=True)
      pm.send('driverMonitoringState', dat)
      rk.keep_time()
      continue

    if not sm.updated['driverStateV2']:
      rk.keep_time()
      continue

    valid = sm.all_checks()
    if demo_mode and sm.valid['driverStateV2']:
      DM.run_step(sm, demo=True)
    elif valid:
      DM.run_step(sm, demo=demo_mode)

    # publish
    dat = DM.get_state_packet(valid=valid)
    set_disable_reason(dat, DriverMonitoringDisableReason.NONE)
    pm.send('driverMonitoringState', dat)

    # save rhd virtual toggle every 5 mins
    if (sm['driverStateV2'].frameId % 6000 == 0 and not demo_mode and
     DM.wheelpos_offsetter.filtered_stat.n > DM.settings._WHEELPOS_FILTER_MIN_COUNT and
     DM.wheel_on_right == (DM.wheelpos_offsetter.filtered_stat.M > DM.settings._WHEELPOS_THRESHOLD)):
      params.put_bool("IsRhdDetected", DM.wheel_on_right)
    rk.keep_time()

def main():
  dmonitoringd_thread()


if __name__ == '__main__':
  main()
