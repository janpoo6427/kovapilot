#!/usr/bin/env python3
from capnp import KjException
from math import isfinite
import time
from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import Priority, config_realtime_process
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.bridges.camera_runtime import (BUMP_KIND, CAMERA_BATCH_LIMIT, CAMERA_EVENT_SERVICE, CAMERA_KIND,
                                                      ROAD_LIMIT_KIND, ROAD_LIMIT_SOURCE, CameraRuntime, camera_cruise_speed,
                                                      read_profiles)
from openpilot.kovapilot.bridges.curve_runtime import CurveRuntime
from openpilot.kovapilot.bridges.lead_restart_runtime import LeadRestartRuntime
from openpilot.kovapilot.bridges.nav_turn_runtime import NavTurnRuntime
from openpilot.kovapilot.config.curve_slowdown_settings import CurveSlowdownPreference, publish_applied_curve_slowdown
from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY, NAV_TURN_SLOWDOWN_KEY
from openpilot.kovapilot.config.lead_restart_settings import LeadRestartPreference, publish_applied_lead_restart
from openpilot.kovapilot.longitudinal.curve_slowdown import CurveLimits, MIN_MOVING_SPEED_MPS
from openpilot.kovapilot.longitudinal.nav_turn import NAVIGATION_SERVICE
from openpilot.kovapilot.navigation.camera_profile import STOCK_NAVI_DECEL_MPS2
from openpilot.kovapilot.navigation.vehicle_navi import VEHICLE_SOURCE
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.selfdrive.controls.lib.ldw import LaneDepartureWarning
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, CRUISE_ACCEL_RESPONSE_TIME, J_CRUISE_VALS, LongitudinalPlanner
import openpilot.cereal.messaging as messaging


def _report_curve_setting(enabled: bool, supported: bool, key: str = CURVE_SLOWDOWN_KEY) -> None:
  try:
    publish_applied_curve_slowdown(enabled, supported=supported, key=key)
  except OSError:
    cloudlog.exception("failed to publish applied curve slowdown setting")


def _report_lead_restart(profile: int) -> None:
  try:
    publish_applied_lead_restart(profile)
  except OSError:
    cloudlog.exception("failed to publish applied lead restart response")


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
  profiles = read_profiles(params) if longitudinal_supported else {}

  def speed_runtime(kind: str, estimated_distance_sources: dict[str, float] | None) -> CameraRuntime | None:
    # Cameras, speed bumps and road limits share kovaCameraEvents; each kind follows its own profile.
    profile = profiles.get(kind, 0)
    return CameraRuntime(profile, decel_limit_mps2=-A_CRUISE_MIN, jerk_limit_mps3=min(J_CRUISE_VALS),
                         response_time_s=CRUISE_ACCEL_RESPONSE_TIME + delay, estimated_distance_sources=estimated_distance_sources,
                         kind=kind) if profile and isfinite(delay) and delay >= 0.0 else None

  camera = speed_runtime(CAMERA_KIND, {VEHICLE_SOURCE: STOCK_NAVI_DECEL_MPS2})
  road_limit = speed_runtime(ROAD_LIMIT_KIND, {ROAD_LIMIT_SOURCE: STOCK_NAVI_DECEL_MPS2})
  bump = speed_runtime(BUMP_KIND, None)
  speed_runtimes = [runtime for runtime in (camera, road_limit, bump) if runtime is not None]
  camera_events = messaging.sub_sock(CAMERA_EVENT_SERVICE) if speed_runtimes else None
  curve_supported = longitudinal_supported and isfinite(delay) and delay >= 0.0
  curve_preference = CurveSlowdownPreference(params) if curve_supported else None
  curve = None
  _report_curve_setting(False, curve_supported)
  nav_turn_preference = CurveSlowdownPreference(params, NAV_TURN_SLOWDOWN_KEY) if curve_supported else None
  nav_turn = None
  navigation_sock = messaging.sub_sock(NAVIGATION_SERVICE, conflate=True) if curve_supported else None
  _report_curve_setting(False, curve_supported, NAV_TURN_SLOWDOWN_KEY)
  curve_limits = CurveLimits(decel_mps2=-A_CRUISE_MIN, jerk_mps3=min(J_CRUISE_VALS), response_s=CRUISE_ACCEL_RESPONSE_TIME + delay,
                             min_turn_speed_mps=MIN_MOVING_SPEED_MPS)
  restart_supported = longitudinal_supported and not CP.pcmCruise and not CP.notCar
  restart_preference = LeadRestartPreference(params) if restart_supported else None
  lead_restart = None
  if restart_supported:
    _report_lead_restart(0)

  while True:
    sm.update()
    if sm.updated['modelV2']:
      now_ns = time.monotonic_ns()
      if restart_preference is not None and (restart_profile := restart_preference.poll(sm, now_ns)) is not None:
        lead_restart = LeadRestartRuntime(restart_profile) if restart_profile else None
        _report_lead_restart(restart_profile)
      if curve_preference is not None and (enabled := curve_preference.poll(sm, now_ns)) is not None:
        curve = CurveRuntime(curve_limits) if enabled else None
        _report_curve_setting(enabled, True)
      if nav_turn_preference is not None and (enabled := nav_turn_preference.poll(sm, now_ns)) is not None:
        nav_turn = NavTurnRuntime(curve_limits) if enabled else None
        _report_curve_setting(enabled, True, NAV_TURN_SLOWDOWN_KEY)
      if navigation_sock is not None:
        try:
          navigation = messaging.recv_one_or_none(navigation_sock)
          if navigation is not None and nav_turn is not None:
            nav_turn.receive(navigation.kovaNavigation, navigation.logMonoTime)
        except KjException:
          if nav_turn is not None:
            nav_turn.invalidate()
      if speed_runtimes:
        for _ in range(CAMERA_BATCH_LIMIT):
          try:
            event = messaging.recv_one_or_none(camera_events)
            if event is not None:
              for runtime in speed_runtimes:
                runtime.update(event)
          except KjException:
            for runtime in speed_runtimes:
              runtime.invalidate()
            break
          if event is None:
            break
      cruise_speed = (camera_cruise_speed(sm, V_CRUISE_MAX, V_CRUISE_UNSET, CV.KPH_TO_MS)
                      if speed_runtimes or curve is not None or nav_turn is not None else None)
      camera_cap = camera.speed_cap(sm.logMonoTime['modelV2'], sm['carState'].vEgo, cruise_speed) if camera is not None else None
      road_limit_cap = (road_limit.speed_cap(sm.logMonoTime['modelV2'], sm['carState'].vEgo, cruise_speed)
                        if road_limit is not None else None)
      bump_cap = bump.speed_cap(sm.logMonoTime['modelV2'], sm['carState'].vEgo, cruise_speed) if bump is not None else None
      nav_turn_cap = None
      nav_turn_eligible = False
      if nav_turn is not None:
        if cruise_speed is not None and nav_turn.eligible(sm, now_ns, cruise_speed):
          nav_turn_eligible = True
          nav_turn_cap = nav_turn.speed_cap(now_ns, sm['carState'].vEgo, sm['carState'].aEgo, cruise_speed)
        else:
          nav_turn.invalidate()
      curve_cap = None
      curve_eligible = False
      if curve is not None:
        if cruise_speed is not None and curve.eligible(sm, now_ns, cruise_speed):
          curve_eligible = True
          curve_cap = curve.speed_cap(sm, cruise_speed)
        else:
          curve.invalidate()
      longitudinal_planner.update(sm, camera_speed_cap=camera_cap, camera_eligible=camera is not None and cruise_speed is not None,
                                  curve_speed_cap=curve_cap, curve_eligible=curve_eligible, lead_restart=lead_restart, now_ns=now_ns,
                                  camera_decel_mps2=camera.cap_decel_mps2 if camera is not None else None,
                                  road_limit_speed_cap=road_limit_cap,
                                  road_limit_eligible=road_limit is not None and cruise_speed is not None,
                                  road_limit_decel_mps2=road_limit.cap_decel_mps2 if road_limit is not None else None,
                                  bump_speed_cap=bump_cap, bump_eligible=bump is not None and cruise_speed is not None,
                                  nav_turn_speed_cap=nav_turn_cap, nav_turn_eligible=nav_turn_eligible)
      longitudinal_planner.publish(sm, pm)

      ldw.update(sm.frame, sm['modelV2'], sm['carState'], sm['carControl'])
      msg = messaging.new_message('driverAssistance')
      msg.valid = sm.all_checks()
      msg.driverAssistance.leftLaneDeparture = ldw.left
      msg.driverAssistance.rightLaneDeparture = ldw.right
      pm.send('driverAssistance', msg)


if __name__ == "__main__":
  main()
