from math import inf, nan
from types import SimpleNamespace
import unittest

from openpilot.kovapilot.bridges.longitudinal_shadow import LongitudinalShadow
from openpilot.kovapilot.longitudinal.shadow import MAX_INPUT_AGE_NS, SpeedCandidate, curve_candidate, select_candidate


def event(service, timestamp=10, valid=True, **fields):
  return SimpleNamespace(which=lambda: service, logMonoTime=timestamp, valid=valid, **{service: SimpleNamespace(**fields)})


def model(timestamp=10, valid=True):
  return event("modelV2", timestamp, valid, position=SimpleNamespace(t=[1.0]), velocity=SimpleNamespace(x=[20.0]),
               orientationRate=SimpleNamespace(z=[0.4]))


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

  def enable(self, supported=True, dashcam=False):
    self.shadow.update(event("carParams", openpilotLongitudinalControl=supported, dashcamOnly=dashcam))

  def feed_car_state(self, **overrides):
    state = {"vCruise": 100.0, "canValid": True, "gasPressed": False, "brakePressed": False, "buttonEvents": []} | overrides
    self.shadow.update(event("carState", **state))

  def feed_car_control(self, active=True, override=False):
    self.shadow.update(event("carControl", longActive=active, cruiseControl=SimpleNamespace(override=override)))

  def test_observe_and_explain_candidate_without_writing_inputs(self):
    state = self.shadow.inputs["carState"].carState
    obs = self.shadow.update(model())
    self.assertTrue(obs.selected)
    self.assertAlmostEqual(obs.cruise_speed_mps, 100 / 3.6)
    self.assertEqual(obs.reason, "curve_below_cruise")
    self.assertEqual(state.vCruise, 100.0)
    self.feed_car_state(vCruise=20.0)
    obs = self.shadow.update(model())
    self.assertFalse(obs.selected)
    self.assertEqual(obs.reason, "cruise_is_lower_or_equal")
    self.assertIsNotNone(obs.candidate)

  def test_invalid_or_stale_inputs_never_reuse_last_candidate(self):
    self.assertTrue(self.shadow.update(model()).selected)
    obs = self.shadow.update(model(11 + MAX_INPUT_AGE_NS))
    self.assertEqual(obs.reason, "invalid_or_stale_carState")
    self.assertIsNone(obs.candidate)
    self.assertEqual(self.shadow.update(model(valid=False)).reason, "invalid_model")

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
    self.assertTrue(self.shadow.update(model()).selected)

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
