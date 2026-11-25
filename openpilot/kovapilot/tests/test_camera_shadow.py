from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpilot.kovapilot.bridges.camera_shadow import CameraShadow
from openpilot.kovapilot.config.kova_params import KovaParamsApplyPolicyError, KovaParamsStore
from openpilot.kovapilot.longitudinal.replay_shadow import read_camera_profile
from openpilot.kovapilot.longitudinal.shadow import SpeedCandidate
from openpilot.kovapilot.navigation.camera_input import CameraInput, CameraInputs, MAX_CAMERA_AGE_NS, read_camera_inputs
from openpilot.kovapilot.navigation.camera_profile import (
  CAMERA_PROFILE_KEY, PROFILE_ACCEL_MPS2, PROFILE_JERK_MPS3, speed_ramp,
)


def sample(sequence=1, distance=400, target=15, source="vehicle", *, time_ns=None, **fields):
  timestamp = sequence * 100_000_000 if time_ns is None else time_ns
  defaults = {"session": "a", "event_id": "camera1", "observed_ns": timestamp, "received_ns": timestamp,
              "state": "present", "road_match": True}
  return CameraInput(source=source, sequence=sequence, distance_m=distance, target_mps=target, **(defaults | fields))


def observe(shadow, event, ego=25, cruise=30, curve=None):
  shadow.update(event)
  return shadow.observe(event.received_ns, ego, cruise, curve)


class TestCameraShadow(unittest.TestCase):
  def test_arrival_targets_and_hold(self):
    minimum_m = speed_ramp(25, 15, PROFILE_ACCEL_MPS2).distance_m
    for profile, finish in ((1, 300), (2, 200), (3, 30)):
      with self.subTest(profile=profile):
        shadow = CameraShadow(profile)
        first = observe(shadow, sample())["cameras"][0]
        self.assertEqual(first["target_distance_m"], finish)
        if profile > 1:
          self.assertEqual(first["phase"], "waiting")
        start = 400 if profile == 1 else finish + minimum_m + 0.5
        at_start = observe(shadow, sample(2, start))["cameras"][0]
        self.assertEqual(at_start["phase"], "approach")
        self.assertAlmostEqual(at_start["start_distance_m"], start)
        # At the sample rate used here the preferred start can be crossed between observations.
        actual_finish = at_start["planned_arrival_m"]
        self.assertLessEqual(actual_finish, finish + 1e-6)
        at_target = observe(shadow, sample(3, max(0, actual_finish - 0.01)), ego=15)["cameras"][0]
        self.assertEqual(at_target["phase"], "holding")
        self.assertEqual(at_target["speed_cap_mps"], 15)

  def test_late_input_and_no_early_resume(self):
    shadow = CameraShadow(3)
    result = observe(shadow, sample(distance=35), ego=35)["cameras"][0]
    self.assertFalse(result["target_feasible"])
    self.assertEqual(result["start_distance_m"], 35)
    result = observe(shadow, sample(2, 0), ego=30)["cameras"][0]
    self.assertEqual(result["phase"], "approach")
    expired = shadow.observe(200_000_000 + MAX_CAMERA_AGE_NS + 1, 30, 35, None)
    self.assertIsNone(expired["selected_candidate"])
    self.assertEqual(expired["excluded"][0]["reason"], "expired")

  def test_recovery_rates_and_other_caps(self):
    restored_caps = []
    for profile in (1, 2, 3):
      shadow = CameraShadow(profile)
      observe(shadow, sample(), ego=15)
      observe(shadow, sample(2, 20), ego=15)
      passed = observe(shadow, sample(3, 0, state="passed"), ego=15)
      self.assertEqual([row["phase"] for row in passed["cameras"]], ["passed", "recovery"])
      now = 5_300_000_000
      curve = SpeedCandidate("model_curve", 14, now)
      recovered = shadow.observe(now, 15, 30, curve)
      self.assertEqual(recovered["selected_candidate"]["source"], "model_curve")
      restored_caps.append(recovered["cameras"][0]["speed_cap_mps"])
      lower_cruise = shadow.observe(now + 1, 15, 13, None)
      self.assertIsNone(lower_cruise["selected_candidate"])
    self.assertLess(restored_caps[0], restored_caps[1])
    self.assertLess(restored_caps[1], restored_caps[2])

  def test_multiple_sources_duplicate_conflict_clear_and_scope(self):
    shadow = CameraShadow(2)
    observe(shadow, sample())
    report = observe(shadow, sample(2, 380, source="external_a"))
    self.assertEqual(report["cameras"][0]["initial_distance_m"], 400)
    self.assertIn({"source": "vehicle", "reason": "duplicate_camera"}, report["excluded"])
    report = observe(shadow, sample(3, 378, 10, "external_b"))
    self.assertFalse(report["cameras"])
    self.assertTrue(all(row["reason"] == "conflicting_camera" for row in report["excluded"]))
    report = observe(shadow, sample(4, source="external_b", state="clear"))
    self.assertEqual(report["cameras"][0]["source"], "external_a")
    self.assertEqual(report["cameras"][0]["target_distance_m"], 200)
    report = observe(shadow, sample(5, 40, source="external_b", road_match=False))
    self.assertIn({"source": "external_b", "reason": "road_mismatch"}, report["excluded"])

  def test_first_accepted_distance_survives_updates_before_model_frame(self):
    shadow = CameraShadow(2)
    shadow.update(sample())
    report = observe(shadow, sample(2, 398))
    self.assertEqual(report["cameras"][0]["target_distance_m"], 200)

  def test_profile_off_and_unavailable_control(self):
    self.assertEqual(observe(CameraShadow(0), sample())["reason"], "profile_off")
    shadow = CameraShadow(1)
    report = observe(shadow, sample(), cruise=None)
    self.assertFalse(report["eligible"])
    self.assertTrue(report["cameras"])
    self.assertIsNone(report["selected_candidate"])
    report = observe(shadow, sample(2, 0, state="passed"), cruise=30)
    self.assertEqual([row["phase"] for row in report["cameras"]], ["passed"])

  def test_ramp_respects_acceleration_and_jerk(self):
    ramp = speed_ramp(25, 15, PROFILE_ACCEL_MPS2, 202)
    dt = ramp.duration_s / 300
    samples = [ramp.sample(index * dt)[1] for index in range(301)]
    accels = [(b - a) / dt for a, b in zip(samples[:-1], samples[1:], strict=True)]
    jerks = [(b - a) / dt for a, b in zip(accels[:-1], accels[1:], strict=True)]
    self.assertAlmostEqual(ramp.distance_m, 202)
    self.assertAlmostEqual(ramp.sample(ramp.duration_s)[1], 15)
    self.assertLessEqual(max(map(abs, accels)), PROFILE_ACCEL_MPS2 + 1e-7)
    self.assertLessEqual(max(map(abs, jerks)), PROFILE_JERK_MPS3 + 1e-7)

  def test_input_order_retransmission_and_retired_session(self):
    feed = CameraInputs()
    original = sample()
    self.assertIsNone(feed.put(original))
    duplicate = replace(original, sequence=2, received_ns=300_000_000)
    self.assertEqual(feed.put(duplicate), "duplicate_or_older")
    self.assertFalse(feed.current(original.observed_ns + MAX_CAMERA_AGE_NS + 1)[0])
    self.assertIsNone(feed.put(sample(2, session="b")))
    self.assertEqual(feed.put(sample(3, session="a")), "retired_session")

  def test_fixture_validation_and_kova_params_options(self):
    self.assertEqual(read_camera_profile(None), 0)
    with TemporaryDirectory() as directory:
      path = Path(directory) / "input.jsonl"
      path.write_text(json.dumps(vars(sample(2))) + "\n" + json.dumps(vars(sample(1))), encoding="utf-8")
      self.assertEqual([item.sequence for item in read_camera_inputs(path)], [1, 2])
      with self.assertRaises(ValueError):
        replace(sample(), target_mps=float("nan"))
      for option in (0, 1, 2, 3, 4, True, 1.0, "1"):
        path.write_text(json.dumps({"schemaVersion": 1, "revision": 0, "values": {CAMERA_PROFILE_KEY: option}}), encoding="utf-8")
        if type(option) is int and option in range(4):
          self.assertEqual(read_camera_profile(path), option)
        else:
          with self.assertRaises(ValueError):
            read_camera_profile(path)

  def test_catalog_uses_offroad_revisioned_write(self):
    class Backend(dict):
      def put(self, key, value, block=False):
        self[key] = value

    backend = Backend()
    store = KovaParamsStore(backend)
    with self.assertRaises(KovaParamsApplyPolicyError):
      store.update(CAMERA_PROFILE_KEY, 2, 0, is_offroad=False)
    self.assertFalse(backend)
    document = store.update(CAMERA_PROFILE_KEY, 2, 0, is_offroad=True)
    setting = next(item for item in document["settings"] if item["key"] == CAMERA_PROFILE_KEY)
    self.assertEqual(setting["optionLabels"]["2"]["ko"], "균형")
    self.assertEqual(setting["value"], 2)
    self.assertEqual(document["revision"], 1)
