#!/usr/bin/env python3
from capnp import KjException
from math import isfinite
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.bridges.camera_runtime import (CAMERA_BATCH_LIMIT, CAMERA_EVENT_SERVICE, CameraRuntime,
                                                      camera_cruise_speed, read_camera_profile)
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.selfdrive.controls.lib.ldw import LaneDepartureWarning
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, CRUISE_ACCEL_RESPONSE_TIME, J_CRUISE_VALS, LongitudinalPlanner
import openpilot.cereal.messaging as messaging


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
  profile = read_camera_profile(params) if CP.openpilotLongitudinalControl and not CP.dashcamOnly else 0
  delay = CP.longitudinalActuatorDelay
  camera = CameraRuntime(profile, decel_limit_mps2=-A_CRUISE_MIN, jerk_limit_mps3=min(J_CRUISE_VALS),
                         response_time_s=CRUISE_ACCEL_RESPONSE_TIME + delay) if profile and isfinite(delay) and delay >= 0.0 else None
  camera_events = messaging.sub_sock(CAMERA_EVENT_SERVICE) if camera is not None else None

  while True:
    sm.update()
    if sm.updated['modelV2']:
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
        cruise_speed = camera_cruise_speed(sm, V_CRUISE_MAX, V_CRUISE_UNSET, CV.KPH_TO_MS)
        speed_cap = camera.speed_cap(sm.logMonoTime['modelV2'], sm['carState'].vEgo, cruise_speed)
        longitudinal_planner.update(sm, camera_speed_cap=speed_cap, camera_eligible=cruise_speed is not None)
      else:
        longitudinal_planner.update(sm)
      longitudinal_planner.publish(sm, pm)

      ldw.update(sm.frame, sm['modelV2'], sm['carState'], sm['carControl'])
      msg = messaging.new_message('driverAssistance')
      msg.valid = sm.all_checks()
      msg.driverAssistance.leftLaneDeparture = ldw.left
      msg.driverAssistance.rightLaneDeparture = ldw.right
      pm.send('driverAssistance', msg)


if __name__ == "__main__":
  main()
