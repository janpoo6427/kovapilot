from dataclasses import replace
from unittest import TestCase

from openpilot.kovapilot.engagement.always_lateral import LateralInputs, LateralState, next_lateral_state


class TestAlwaysLateral(TestCase):
  ready = LateralInputs(mode_enabled=True, lateral_permitted=True)

  def test_explicit_engagement_required(self):
    self.assertIs(next_lateral_state(LateralState.DISABLED, LateralInputs(engage_requested=True)), LateralState.DISABLED)
    self.assertIs(next_lateral_state(LateralState.DISABLED, self.ready), LateralState.DISABLED)
    engaged = next_lateral_state(LateralState.DISABLED, replace(self.ready, engage_requested=True))
    self.assertIs(engaged, LateralState.ACTIVE)

  def test_yields_until_driver_release_confirmed(self):
    state = next_lateral_state(LateralState.DISABLED, replace(self.ready, engage_requested=True, driver_steering=True))
    self.assertIs(state, LateralState.DRIVER_OVERRIDE)
    self.assertIs(next_lateral_state(LateralState.ACTIVE, replace(self.ready, driver_steering=True)), state)
    self.assertIs(next_lateral_state(state, self.ready), state)
    self.assertIs(next_lateral_state(state, replace(self.ready, driver_steering=True, driver_release_confirmed=True)), state)
    self.assertIs(next_lateral_state(state, replace(self.ready, driver_release_confirmed=True)), LateralState.ACTIVE)

  def test_longitudinal_disengagement_with_lateral_permission(self):
    # A brake-triggered cruise disengagement supplies no new lateral engagement
    # request. When independent permission survives, lateral assistance can stay
    # active or resume after driver override.
    self.assertIs(next_lateral_state(LateralState.ACTIVE, self.ready), LateralState.ACTIVE)
    released = replace(self.ready, driver_release_confirmed=True)
    self.assertIs(next_lateral_state(LateralState.DRIVER_OVERRIDE, released), LateralState.ACTIVE)

  def test_hard_disable_takes_precedence_and_requires_reengagement(self):
    released = replace(self.ready, driver_release_confirmed=True, engage_requested=True)
    for blocked in (
      replace(released, mode_enabled=False),
      replace(released, cancel_requested=True),
      replace(released, lateral_permitted=False),
      replace(released, safety_disable=True),
    ):
      for prior in LateralState:
        with self.subTest(blocked=blocked, prior=prior):
          state = next_lateral_state(prior, blocked)
          self.assertIs(state, LateralState.DISABLED)
          self.assertIs(next_lateral_state(state, self.ready), LateralState.DISABLED)
