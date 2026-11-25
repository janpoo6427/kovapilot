#!/usr/bin/env python3
from capnp import KjException
from math import isfinite
import time
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.bridges.camera_runtime import (CAMERA_BATCH_LIMIT, CAMERA_EVENT_SERVICE, CameraRuntime,
                                                      camera_cruise_speed, read_camera_profile)
from openpilot.kovapilot.bridges.curve_runtime import CurveRuntime
from openpilot.kovapilot.config.curve_slowdown_settings import CurveSlowdownPreference, publish_applied_curve_slowdown
from openpilot.kovapilot.longitudinal.curve_slowdown import CurveLimits, MIN_MOVING_SPEED_MPS
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.selfdrive.controls.lib.ldw import LaneDepartureWarning
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, CRUISE_ACCEL_RESPONSE_TIME, J_CRUISE_VALS, LongitudinalPlanner
import openpilot.cereal.messaging as messaging


def _report_curve_setting(enabled: bool, supported: bool) -> None:
  try:
    publish_applied_curve_slowdown(enabled, supported=supported)
  except OSError:
    cloudlog.exception("failed to publish applied curve slowdown setting")


def main():
  config_realtime_process(6, Priority.CTRL_LOW)

  cloudlog.info("plannerd is waiting for CarParams")
  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  cloudlog.info("plannerd got CarParams: %s", CP.brand)

  ldw = LaneDepartureWarning()
  longitudinal_planner = LongitudinalPlanner(CP)
  pm = messaging.PubMaster(['longitudinalPlan', 'driverAssistance'])
  sm = messaging.SubMaster(['carControl', 'carState', 'controlsState', 'radarState', 'modelV2', 'selfdriveState', 'vehicleParameters'],
                           poll='modelV2')
  delay = CP.longitudinalActuatorDelay
  longitudinal_supported = CP.openpilotLongitudinalControl and not CP.dashcamOnly
  profile = read_camera_profile(params) if longitudinal_supported else 0
  camera = CameraRuntime(profile, decel_limit_mps2=-A_CRUISE_MIN, jerk_limit_mps3=min(J_CRUISE_VALS),
                         response_time_s=CRUISE_ACCEL_RESPONSE_TIME + delay) if profile and isfinite(delay) and delay >= 0.0 else None
  camera_events = messaging.sub_sock(CAMERA_EVENT_SERVICE) if camera is not None else None
  curve_supported = longitudinal_supported and isfinite(delay) and delay >= 0.0
  curve_preference = CurveSlowdownPreference(params) if curve_supported else None
  curve = None
  _report_curve_setting(False, curve_supported)

  while True:
    sm.update()
    if sm.updated['modelV2']:
      now_ns = time.monotonic_ns()
      if curve_preference is not None and (enabled := curve_preference.poll(sm, now_ns)) is not None:
        curve = (CurveRuntime(CurveLimits(decel_mps2=-A_CRUISE_MIN, jerk_mps3=min(J_CRUISE_VALS),
                                          response_s=CRUISE_ACCEL_RESPONSE_TIME + delay, min_turn_speed_mps=MIN_MOVING_SPEED_MPS))
                 if enabled else None)
        _report_curve_setting(enabled, True)
      if camera is not None:
        for _ in range(CAMERA_BATCH_LIMIT):
          try:
            event = messaging.recv_one_or_none(camera_events)
            if event is not None:
              camera.update(event)
          except KjException:
            camera.invalidate()
            break
          if event is None:
            break
      cruise_speed = (camera_cruise_speed(sm, V_CRUISE_MAX, V_CRUISE_UNSET, CV.KPH_TO_MS)
                      if camera is not None or curve is not None else None)
      camera_cap = camera.speed_cap(sm.logMonoTime['modelV2'], sm['carState'].vEgo, cruise_speed) if camera is not None else None
      curve_cap = None
      curve_eligible = False
      if curve is not None:
        if cruise_speed is not None and curve.eligible(sm, now_ns, cruise_speed):
          curve_eligible = True
          curve_cap = curve.speed_cap(sm, cruise_speed)
        else:
          curve.invalidate()
      longitudinal_planner.update(sm, camera_speed_cap=camera_cap, camera_eligible=camera is not None and cruise_speed is not None,
                                  curve_speed_cap=curve_cap, curve_eligible=curve_eligible)
      longitudinal_planner.publish(sm, pm)

      ldw.update(sm.frame, sm['modelV2'], sm['carState'], sm['carControl'])
      msg = messaging.new_message('driverAssistance')
      msg.valid = sm.all_checks()
      msg.driverAssistance.leftLaneDeparture = ldw.left
      msg.driverAssistance.rightLaneDeparture = ldw.right
      pm.send('driverAssistance', msg)


if __name__ == "__main__":
  main()
