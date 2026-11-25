from math import inf, nan
from types import SimpleNamespace
import unittest

from openpilot.kovapilot.bridges.longitudinal_shadow import LongitudinalShadow
from openpilot.kovapilot.longitudinal.curve_slowdown import CurveEstimate, CurveLimiter, estimate_curve
from openpilot.kovapilot.longitudinal.shadow import MAX_INPUT_AGE_NS, SpeedCandidate, curve_candidate, select_candidate


TIMES = [i / 2 for i in range(13)]
PATH = [20 * t for t in TIMES]


def event(service, timestamp=10, valid=True, **fields):
  return SimpleNamespace(which=lambda: service, logMonoTime=timestamp, valid=valid, **{service: SimpleNamespace(**fields)})


def model(timestamp=10, valid=True, *, turn=True, big=False, lane_change="off"):
  return event("modelV2", timestamp, valid, position=SimpleNamespace(t=TIMES, x=PATH, y=[0.0] * len(TIMES)),
               velocity=SimpleNamespace(x=[20.0] * len(TIMES)), orientationRate=SimpleNamespace(z=[0.4 if turn else 0.0] * len(TIMES)),
               big=big, meta=SimpleNamespace(laneChangeState=lane_change))


def estimate(curvatures, *, prediction_speed=20.0, accel=0.0, ego=22.0):
  return estimate_curve(TIMES, PATH, [0.0] * len(TIMES), [prediction_speed] * len(TIMES),
                        [prediction_speed * k for k in curvatures], ego_mps=ego, ego_accel_mps2=accel, cruise_mps=27.0)


class TestSpeedCandidate(unittest.TestCase):
  def test_curve_uses_predicted_curvature_and_lookahead(self):
    candidate = curve_candidate(50, [1.0, 3.0, 7.0], [20.0, 20.0, 20.0], [-0.4, 0.1, 1.0])
    self.assertIsNotNone(candidate)
    self.assertEqual(candidate.source, "model_curve")
    self.assertAlmostEqual(candidate.speed_mps, (2.8 / 0.02) ** 0.5)
    self.assertEqual(candidate.observed_at_ns, 50)

  def test_missing_malformed_and_no_measurable_curve(self):
    for data in (([], [], []), ([1.0, 2.0], [20.0], [0.4]), ([1.0], [20.0], [nan]),
                 ([1.0], [inf], [0.4]), ([1.0], [0.0], [0.4]), ([1.0], [20.0], [0.0])):
      with self.subTest(data=data):
        self.assertIsNone(curve_candidate(0, *data))

  def test_select_lowest_fresh_cap_and_drop_bad_candidates(self):
    now = MAX_INPUT_AGE_NS + 5
    best = SpeedCandidate("curve", 12.0, 5)
    candidates = [SpeedCandidate("limit", 13.0, now), SpeedCandidate("expired", 5.0, 4),
                  SpeedCandidate("future", 4.0, now + 1), SpeedCandidate("invalid", nan, now), best]
    self.assertEqual(select_candidate(now, 20.0, candidates), best)
    self.assertIsNone(select_candidate(now, 12.0, candidates))


class TestLongitudinalShadow(unittest.TestCase):
  def setUp(self):
    self.shadow = LongitudinalShadow(145, 255, 1 / 3.6)
    self.enable()
    self.feed_car_state()
    self.feed_car_control()
    self.shadow.update(event("controlsState", forceDecel=False))
    self.shadow.update(event("selfdriveState", enabled=True))

  def enable(self, supported=True, dashcam=False):
    self.shadow.update(event("carParams", openpilotLongitudinalControl=supported, dashcamOnly=dashcam))

  def feed_car_state(self, **overrides):
    state = {"vCruise": 100.0, "vEgo": 22.0, "aEgo": 0.0, "standstill": False, "canValid": True,
             "gasPressed": False, "brakePressed": False, "buttonEvents": []} | overrides
    self.shadow.update(event("carState", **state))

  def feed_car_control(self, active=True, override=False):
    self.shadow.update(event("carControl", longActive=active, cruiseControl=SimpleNamespace(override=override, cancel=False)))

  def test_observe_and_explain_candidate_without_writing_inputs(self):
    state = self.shadow.inputs["carState"].carState
    obs = self.shadow.update(model())
    self.assertTrue(obs.selected)
    self.assertAlmostEqual(obs.cruise_speed_mps, 100 / 3.6)
    self.assertEqual(obs.reason, "curve_below_cruise")
    self.assertIsNotNone(obs.baseline_candidate)
    self.assertEqual(state.vCruise, 100.0)
    self.feed_car_state(vCruise=20.0)
    obs = self.shadow.update(model(11))
    self.assertFalse(obs.selected)
    self.assertEqual(obs.reason, "no_curve_candidate")
    self.assertIsNone(obs.candidate)

  def test_invalid_or_stale_inputs_never_reuse_last_candidate(self):
    self.assertTrue(self.shadow.update(model()).selected)
    obs = self.shadow.update(model(11 + MAX_INPUT_AGE_NS))
    self.assertEqual(obs.reason, "invalid_or_stale_carState")
    self.assertIsNone(obs.candidate)
    self.assertEqual(self.shadow.update(model(12 + MAX_INPUT_AGE_NS, valid=False)).reason, "invalid_or_stale_carState")

  def test_respects_pedals_override_and_disengagement(self):
    for pedal in ("brakePressed", "gasPressed"):
      self.feed_car_state(**{pedal: True})
      self.assertEqual(self.shadow.update(model()).reason, "driver_override")
    self.feed_car_state(buttonEvents=[SimpleNamespace(pressed=True, type="cancel")])
    self.assertEqual(self.shadow.update(model()).reason, "driver_override")
    self.feed_car_state()
    self.feed_car_control(override=True)
    self.assertEqual(self.shadow.update(model()).reason, "driver_override")
    self.feed_car_control(active=False)
    self.assertEqual(self.shadow.update(model()).reason, "longitudinal_inactive")
    self.feed_car_control()
    self.assertTrue(self.shadow.update(model(11)).selected)

  def test_capability_change_clears_inputs_and_rejects_dashcam(self):
    self.enable(dashcam=True)
    self.assertEqual(self.shadow.update(model()).reason, "longitudinal_unavailable")
    self.enable()
    self.assertEqual(self.shadow.update(model()).reason, "invalid_or_stale_carState")

  def test_forced_decel_invalid_can_and_unset_cruise(self):
    self.feed_car_state(vCruise=255)
    self.assertEqual(self.shadow.update(model()).reason, "cruise_unavailable")
    self.feed_car_state(canValid=False)
    self.assertEqual(self.shadow.update(model()).reason, "invalid_can")
    self.feed_car_state()
    self.shadow.update(event("controlsState", forceDecel=True))
    self.assertEqual(self.shadow.update(model()).reason, "forced_deceleration")

  def test_invalid_geometry_hold_and_interframe_cancel(self):
    self.assertTrue(self.shadow.update(model(10)).selected)
    missing = self.shadow.update(model(11, valid=False))
    self.assertTrue(missing.selected)
    self.assertEqual(missing.reason, "invalid_model")
    self.feed_car_state(brakePressed=True)
    self.feed_car_state()
    self.assertIsNone(self.shadow.update(model(12, valid=False)).candidate)

  def test_model_switch_lane_change_and_duplicate(self):
    self.assertTrue(self.shadow.update(model(10)).selected)
    self.assertEqual(self.shadow.update(model(10)).reason, "repeated_model")
    turning = self.shadow.update(model(11, lane_change="laneChangeStarting"))
    self.assertEqual(turning.reason, "lane_change_geometry")
    self.assertTrue(turning.selected)
    switched = self.shadow.update(model(12, turn=False, big=True))
    self.assertTrue(switched.big_model)
    self.assertIsNone(switched.candidate)

  def test_geometry_only_measures_passive_recordings_without_selecting_a_cap(self):
    shadow = LongitudinalShadow(145, 255, 1 / 3.6, geometry_only=True)
    shadow.update(self.shadow.inputs["carState"])
    obs = shadow.update(model())
    self.assertEqual(obs.reason, "geometry_only")
    self.assertIsNotNone(obs.approach)
    self.assertIsNotNone(obs.baseline_candidate)
    self.assertFalse(obs.selected)
    self.assertIsNone(obs.candidate)


class TestCurveApproach(unittest.TestCase):
  def test_distance_and_current_acceleration_change_approach_not_turn_speed(self):
    near_curve = [0.0] * 2 + [0.02] * 11
    far_curve = [0.0] * 7 + [0.02] * 6
    near, far, accelerating, slower_prediction = (
      estimate(near_curve).approach, estimate(far_curve).approach,
      estimate(near_curve, accel=1.0).approach, estimate(near_curve, prediction_speed=15.0).approach,
    )
    self.assertLess(near.approach_speed_mps, far.approach_speed_mps)
    self.assertLess(accelerating.approach_speed_mps, near.approach_speed_mps)
    self.assertFalse(near.target_feasible)
    self.assertAlmostEqual(near.turn_speed_mps, far.turn_speed_mps)
    self.assertAlmostEqual(near.approach_speed_mps, slower_prediction.approach_speed_mps)
    self.assertAlmostEqual(near.turn_speed_mps, estimate([-k for k in near_curve]).approach.turn_speed_mps)

  def test_reject_spikes_breaks_and_low_speed(self):
    self.assertEqual(estimate([0.0] * 13), CurveEstimate(None, True))
    self.assertEqual(estimate([0.0] * 5 + [0.25] + [0.0] * 7), CurveEstimate(None, True))
    self.assertEqual(estimate([0.02] * 13, ego=0.5), CurveEstimate(None, False))
    self.assertEqual(estimate([0.02, nan, 0.02] + [0.0] * 10), CurveEstimate(None, False))
    partial = estimate([0.02] * 9 + [nan] + [0.0] * 3)
    self.assertIsNotNone(partial.approach)
    self.assertFalse(partial.complete)
    self.assertIsNotNone(estimate([0.03] * 6 + [-0.03] * 7).approach)

  def test_exit_needs_new_confirmed_frames_and_releases_gradually(self):
    limiter = CurveLimiter()
    curve = estimate([0.02] * 13)
    clear = estimate([0.0] * 13)
    cap = limiter.update(0, curve, 27.0)
    for t in (50_000_000, 50_000_000, 100_000_000, 150_000_000):
      self.assertEqual(limiter.update(t, clear, 27.0), cap)
    self.assertLess(cap, limiter.update(200_000_000, clear, 27.0))
    self.assertEqual(limiter.update(250_000_000, curve, 27.0), cap)

  def test_dropout_holds_then_recovers_with_monotonic_time(self):
    limiter = CurveLimiter()
    cap = limiter.update(0, estimate([0.02] * 13), 27.0)
    missing = CurveEstimate(None, False)
    for ms in (100, 200, 300, 350, 350, 325):
      self.assertEqual(limiter.update(ms * 10**6, missing, 27.0), cap)
    self.assertLess(cap, limiter.update(400_000_000, missing, 27.0))
    self.assertIsNone(limiter.update(450_000_000, missing, 5.0))

  def test_partial_preview_can_tighten_but_cannot_confirm_exit(self):
    limiter = CurveLimiter()
    current = estimate([0.015] * 13)
    cap = limiter.update(0, current, 27.0)
    partial_clear = estimate([0.0] * 9 + [nan] + [0.0] * 3)
    for ms in (50, 100, 150, 200):
      self.assertEqual(limiter.update(ms * 10**6, partial_clear, 27.0), cap)
    partial_tight = estimate([0.04] * 9 + [nan] + [0.0] * 3)
    self.assertLess(limiter.update(250_000_000, partial_tight, 27.0), cap)
