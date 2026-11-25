from types import SimpleNamespace
import unittest

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log
from opendbc.car.structs import car

from openpilot.kovapilot.bridges.camera_runtime import CAMERA_EVENT_SERVICE, CameraRuntime, camera_cruise_speed, read_camera_profile
from openpilot.kovapilot.navigation.camera_input import MAX_CAMERA_AGE_NS
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner


class Inputs(dict):
  healthy = True

  def all_checks(self, services):
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
    camera_accel = enabled.a_camera
    enabled.update(inputs, camera_eligible=True)
    self.assertGreater(enabled.a_camera, camera_accel)
    self.assertLess(enabled.a_camera - camera_accel, 0.1)
    enabled.update(inputs)
    self.assertIsNone(enabled.a_camera)

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
        self.assertIsNone(enabled.a_camera)
