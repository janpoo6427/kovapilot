from types import SimpleNamespace
import unittest

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from opendbc.car.structs import car

from openpilot.kovapilot.bridges.camera_runtime import CAMERA_EVENT_SERVICE, CameraRuntime, camera_cruise_speed, read_camera_profile
from openpilot.kovapilot.navigation.camera_input import MAX_CAMERA_AGE_NS
from openpilot.kovapilot.navigation.camera_profile import STOCK_NAVI_DECEL_MPS2
from openpilot.kovapilot.navigation.vehicle_navi import VEHICLE_SOURCE
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, A_SPEED_CAP_MIN, LongitudinalPlanner


class Inputs(dict):
  healthy = True

  def all_checks(self, services=None):
    return self.healthy


def planner_inputs(**car_state):
  state = {"vEgo": 25.0, "aEgo": 0.0, "vCruise": 120.0, "canValid": True, "standstill": False} | car_state
  return Inputs(
    carState=car.CarState.new_message(**state),
    carControl=car.CarControl.new_message(longActive=True),
    controlsState=log.ControlsState.new_message(longControlState="pid"),
    selfdriveState=log.SelfdriveState.new_message(enabled=True),
    radarState=log.RadarState.new_message(),
    modelV2=log.ModelDataV2.new_message(action={"desiredAcceleration": 1.0}),
    vehicleParameters=SimpleNamespace(roll=0.0),
  )


def camera_event(seq, distance, *, state="present", verified=True, road_match=True, valid=True):
  event = messaging.new_message(CAMERA_EVENT_SERVICE, 1, valid=valid, logMonoTime=seq * 100_000_000)
  event.kovaCameraEvents[0] = {
    "source": "test", "session": "a", "eventId": "one", "sequence": seq,
    "observedMonoTime": event.logMonoTime, "state": state, "roadMatch": road_match,
    "verifiedSinglePoint": verified, "distanceM": distance, "targetMps": 15.0,
  }
  return event.as_reader()


class TestCameraRuntime(unittest.TestCase):
  def test_settings_are_off_on_missing_or_invalid_snapshot(self):
    for state, expected in (
      (None, 0), ({"schemaVersion": 1, "revision": 0, "values": {}}, 0),
      ({"schemaVersion": 1, "revision": 0, "values": {"navigation.speed_camera_profile": 2}}, 2),
      ({"schemaVersion": 1, "revision": 0, "values": {"navigation.speed_camera_profile": True}}, 0),
    ):
      self.assertEqual(read_camera_profile({"KovaParams": state}), expected)

  def test_raw_or_mismatched_inputs_do_not_apply(self):
    runtime = CameraRuntime(1)
    for event in (camera_event(1, 100, verified=False), camera_event(2, 100, road_match=False)):
      runtime.update(event)
      self.assertIsNone(runtime.speed_cap(event.logMonoTime, 25.0, 30.0))

  def test_verified_cap_expires_and_invalid_frame_clears_it(self):
    runtime = CameraRuntime(1)
    active = camera_event(1, 100)
    runtime.update(active)
    self.assertEqual(runtime.speed_cap(active.logMonoTime, 25.0, 30.0), 25.0)
    self.assertIsNone(runtime.speed_cap(active.logMonoTime + MAX_CAMERA_AGE_NS + 1, 25.0, 30.0))
    active = camera_event(30, 60)
    runtime.update(active)
    self.assertIsNotNone(runtime.speed_cap(active.logMonoTime, 25.0, 30.0))
    runtime.update(camera_event(31, 50, valid=False))
    self.assertIsNone(runtime.speed_cap(3_100_000_000, 25.0, 30.0))

  def test_distance_advances_between_fresh_samples_without_confirming_passage(self):
    runtime = CameraRuntime(1)
    runtime.update(camera_event(1, 4))
    start_cap = runtime.speed_cap(100_000_000, 25.0, 30.0)
    next_cap = runtime.speed_cap(200_000_000, 25.0, 30.0)
    at_camera_cap = runtime.speed_cap(300_000_000, 25.0, 30.0)
    self.assertLess(next_cap, start_cap)
    self.assertLess(at_camera_cap, next_cap)
    self.assertFalse(runtime.policy.passed)
    self.assertFalse(runtime.policy.recoveries)
    self.assertIsNone(runtime.speed_cap(100_000_001 + MAX_CAMERA_AGE_NS, 25.0, 30.0))

  def test_live_cruise_limits_advance_approach_start_when_needed(self):
    offline = CameraRuntime(3)
    live = CameraRuntime(3, decel_limit_mps2=0.5, jerk_limit_mps3=0.6, response_time_s=1.5)
    for runtime in (offline, live):
      runtime.update(camera_event(1, 1000))
      self.assertIsNone(runtime.speed_cap(100_000_000, 25.0, 30.0))
      runtime.update(camera_event(2, 420))
    self.assertIsNone(offline.speed_cap(200_000_000, 25.0, 30.0))
    self.assertEqual(live.speed_cap(200_000_000, 25.0, 30.0), 25.0)
    self.assertLessEqual(abs(live.policy.approaches["one"].ramp.jerk_mps3), 0.6)

  def test_override_and_unknown_cruise_disable_camera_eligibility(self):
    for overrides in ({"brakePressed": True}, {"gasPressed": True}, {"vCruise": 255},
                      {"canValid": False}, {"buttonEvents": [{"type": "cancel", "pressed": True}]}):
      self.assertIsNone(camera_cruise_speed(planner_inputs(**overrides), 145, 255, 1 / 3.6))
    inputs = planner_inputs()
    self.assertAlmostEqual(camera_cruise_speed(inputs, 145, 255, 1 / 3.6), 120 / 3.6)
    inputs["controlsState"].forceDecel = True
    self.assertIsNone(camera_cruise_speed(inputs, 145, 255, 1 / 3.6))


class TestCameraPlanner(unittest.TestCase):
  def test_camera_cap_only_adds_a_more_restrictive_acceleration(self):
    cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
    baseline, enabled = (LongitudinalPlanner(cp, init_v=25.0) for _ in range(2))
    inputs = planner_inputs()
    for _ in range(3):
      baseline.update(inputs)
      enabled.update(inputs, camera_eligible=True)
      self.assertEqual(baseline.output_a_target, enabled.output_a_target)
    baseline.update(inputs)
    enabled.update(inputs, camera_speed_cap=15.0, camera_eligible=True)
    self.assertLess(enabled.output_a_target, baseline.output_a_target)
    self.assertEqual(enabled.output_should_stop, baseline.output_should_stop)
    camera_accel = enabled.a_speed_cap
    enabled.update(inputs, camera_eligible=True)
    self.assertGreater(enabled.a_speed_cap, camera_accel)
    self.assertLess(enabled.a_speed_cap - camera_accel, 0.1)
    enabled.update(inputs)
    self.assertIsNone(enabled.a_speed_cap)

  def test_camera_cannot_override_force_decel_or_stock_acc(self):
    for openpilot_long, force_decel in ((False, False), (True, True)):
      with self.subTest(openpilot_long=openpilot_long, force_decel=force_decel):
        cp = car.CarParams.new_message(openpilotLongitudinalControl=openpilot_long, longitudinalActuatorDelay=0.5)
        baseline, enabled = (LongitudinalPlanner(cp, init_v=25.0) for _ in range(2))
        inputs = planner_inputs()
        inputs["controlsState"].forceDecel = force_decel
        baseline.update(inputs)
        enabled.update(inputs, camera_speed_cap=15.0, camera_eligible=True)
        self.assertEqual(baseline.output_a_target, enabled.output_a_target)
        self.assertIsNone(enabled.a_speed_cap)


def sourced_event(seq, distance, source, *, target=16.7):
  event = messaging.new_message(CAMERA_EVENT_SERVICE, 1, valid=True, logMonoTime=seq * 100_000_000)
  event.kovaCameraEvents[0] = {
    "source": source, "session": "a", "eventId": "one", "sequence": seq,
    "observedMonoTime": event.logMonoTime, "state": "present", "roadMatch": True,
    "verifiedSinglePoint": True, "distanceM": distance, "targetMps": target,
  }
  return event.as_reader()


class TestEstimatedDistanceCamera(unittest.TestCase):
  def live(self, profile):
    return CameraRuntime(profile, decel_limit_mps2=0.5, jerk_limit_mps3=0.6, response_time_s=1.2,
                         estimated_distance_sources={VEHICLE_SOURCE: STOCK_NAVI_DECEL_MPS2})

  def test_stock_warning_starts_on_receipt_and_reports_its_deceleration(self):
    for profile in (1, 2, 3):
      with self.subTest(profile=profile):
        runtime = self.live(profile)
        runtime.update(sourced_event(1, 360.0, VEHICLE_SOURCE))
        self.assertIsNotNone(runtime.speed_cap(100_000_000, 27.8, 30.0))
        self.assertEqual(runtime.cap_decel_mps2, STOCK_NAVI_DECEL_MPS2)
        plan = runtime.policy.approaches["one"]
        self.assertTrue(plan.estimated_distance)
        self.assertEqual(plan.start_distance_m, 360.0)
        # Reaches the target by the margin before the estimated point, the same for every profile.
        report = runtime.policy.observe(100_000_000, 27.8, 30.0, None)
        self.assertAlmostEqual(report["cameras"][0]["target_distance_m"], max(20.0, 16.7 * 2.0), places=4)
        self.assertTrue(report["cameras"][0]["target_feasible"])

  def test_stock_warning_uses_only_the_deceleration_the_distance_needs(self):
    near, far = self.live(2), self.live(2)
    near.update(sourced_event(1, 360.0, VEHICLE_SOURCE))
    far.update(sourced_event(1, 900.0, VEHICLE_SOURCE))
    near.speed_cap(100_000_000, 27.8, 30.0)
    far.speed_cap(100_000_000, 27.8, 30.0)
    near_ramp, far_ramp = near.policy.approaches["one"].ramp, far.policy.approaches["one"].ramp
    self.assertLess(abs(far_ramp.jerk_mps3) * far_ramp.ramp_s, abs(near_ramp.jerk_mps3) * near_ramp.ramp_s)
    self.assertLess(abs(far_ramp.jerk_mps3) * far_ramp.ramp_s, 0.5)
    self.assertLessEqual(abs(near_ramp.jerk_mps3) * near_ramp.ramp_s, STOCK_NAVI_DECEL_MPS2 + 1e-9)

  def test_app_cameras_keep_the_profile_and_cruise_deceleration(self):
    plain = CameraRuntime(3, decel_limit_mps2=0.5, jerk_limit_mps3=0.6, response_time_s=1.2)
    live = self.live(3)
    for runtime in (plain, live):
      runtime.update(sourced_event(1, 900.0, "carrot_app"))
      self.assertIsNone(runtime.speed_cap(100_000_000, 27.8, 30.0))
      self.assertIsNone(runtime.cap_decel_mps2)
      self.assertFalse(runtime.policy.approaches["one"].estimated_distance)
      runtime.update(sourced_event(2, 400.0, "carrot_app"))
    caps = [runtime.speed_cap(200_000_000, 27.8, 30.0) for runtime in (plain, live)]
    self.assertIsNotNone(caps[0])
    self.assertEqual(caps[0], caps[1])
    self.assertEqual(live.cap_decel_mps2, 0.5)
    self.assertEqual(plain.policy.approaches["one"].ramp, live.policy.approaches["one"].ramp)

  def test_invalidate_keeps_the_estimated_distance_sources(self):
    runtime = self.live(2)
    runtime.update(sourced_event(1, 360.0, VEHICLE_SOURCE))
    runtime.speed_cap(100_000_000, 27.8, 30.0)
    runtime.invalidate()
    self.assertIsNone(runtime.cap_decel_mps2)
    self.assertEqual(runtime.policy.estimated_distance_sources, {VEHICLE_SOURCE: STOCK_NAVI_DECEL_MPS2})


class TestCameraPlannerDeceleration(unittest.TestCase):
  def run_cap(self, **kw):
    cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
    planner = LongitudinalPlanner(cp, init_v=25.0)
    inputs = planner_inputs(vCruise=100.0)
    lowest = 0.0
    for _ in range(200):
      planner.update(inputs, camera_speed_cap=10.0, camera_eligible=True, **kw)
      lowest = min(lowest, planner.a_speed_cap)
    return lowest

  def test_cruise_floor_holds_without_a_planned_deceleration(self):
    self.assertAlmostEqual(self.run_cap(), A_CRUISE_MIN)
    for decel in (0.5, 0.3, float("nan"), float("inf"), -1.0):
      self.assertAlmostEqual(self.run_cap(camera_decel_mps2=decel), A_CRUISE_MIN)

  def test_planned_camera_deceleration_is_followed_up_to_the_hard_floor(self):
    self.assertAlmostEqual(self.run_cap(camera_decel_mps2=0.9), -0.9)
    self.assertAlmostEqual(self.run_cap(camera_decel_mps2=5.0), A_SPEED_CAP_MIN)

  def test_curve_cap_ignores_the_camera_deceleration(self):
    cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
    planner = LongitudinalPlanner(cp, init_v=25.0)
    inputs = planner_inputs(vCruise=100.0)
    lowest = 0.0
    for _ in range(200):
      planner.update(inputs, camera_speed_cap=20.0, camera_eligible=True, curve_speed_cap=10.0, curve_eligible=True,
                     camera_decel_mps2=1.2)
      lowest = min(lowest, planner.a_speed_cap)
    self.assertEqual(planner.speed_cap_source, "curve")
    self.assertAlmostEqual(lowest, A_CRUISE_MIN)
