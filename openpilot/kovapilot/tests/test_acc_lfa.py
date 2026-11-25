from dataclasses import replace

from openpilot.kovapilot.engagement.acc_lfa import CruiseState, EngagementInputs, EngagementState, next_engagement_state


READY = EngagementInputs(
  cruise=CruiseState.OFF,
  standstill=False,
  below_normal_steering_speed=False,
  lateral_permitted=True,
  longitudinal_permitted=True,
)


def test_cruise_and_lfa_selections_are_independent_after_cruise_on():
  state = EngagementState()
  steps = (
    (READY, False, False, False, False),
    (replace(READY, cruise=CruiseState.STANDBY), True, True, True, False),
    (replace(READY, cruise=CruiseState.ACTIVE), True, True, True, True),
    (replace(READY, cruise=CruiseState.ACTIVE, lfa_toggle_requested=True), True, False, False, True),
    (replace(READY, cruise=CruiseState.STANDBY), True, False, False, False),
    (replace(READY, cruise=CruiseState.ACTIVE), True, False, False, True),
    (READY, False, False, False, False),
    (replace(READY, cruise=CruiseState.ACTIVE), True, True, True, True),
    (READY, False, True, True, False),
    (replace(READY, lfa_toggle_requested=True), False, False, False, False),
  )
  for frame, acc_selected, lfa_selected, lat_requested, long_requested in steps:
    state = next_engagement_state(state, frame)
    assert state.cruise.selected == acc_selected
    assert state.lfa_selected == lfa_selected
    assert state.lateral_requested == lat_requested
    assert state.longitudinal_requested == long_requested


def test_always_steering_keeps_selected_lfa_during_braking_and_low_speed():
  active = replace(READY, cruise=CruiseState.ACTIVE)
  engaged = next_engagement_state(EngagementState(cruise=CruiseState.OFF), active)
  for frame in (replace(active, braking=True), replace(active, below_normal_steering_speed=True)):
    normal = next_engagement_state(engaged, frame)
    assert normal.lfa_selected and not normal.lateral_requested
    assert next_engagement_state(normal, active).lateral_requested
    always = next_engagement_state(engaged, replace(frame, always_steering=True))
    assert always.lfa_selected and always.lateral_requested
    assert always.longitudinal_requested == (not frame.braking)
    off = next_engagement_state(always, replace(frame, always_steering=True, lfa_toggle_requested=True))
    assert not off.lfa_selected and not off.lateral_requested


def test_always_steering_preserves_standstill_and_hard_safety_limits():
  active = replace(READY, cruise=CruiseState.ACTIVE, always_steering=True)
  engaged = next_engagement_state(EngagementState(cruise=CruiseState.OFF), active)
  stopped = next_engagement_state(engaged, replace(active, standstill=True))
  assert stopped.lfa_selected and not stopped.lateral_requested
  for frame in (replace(active, safety_disable=True), replace(active, lateral_permitted=False)):
    blocked = next_engagement_state(engaged, frame)
    assert not blocked.lfa_selected and not blocked.lateral_requested
    assert not next_engagement_state(blocked, active).lfa_selected
  assert not next_engagement_state(engaged, replace(active, safety_disable=True)).longitudinal_requested
  assert not next_engagement_state(engaged, replace(active, longitudinal_permitted=False)).longitudinal_requested


def test_unobserved_cruise_edges_do_not_arm_lfa():
  active = replace(READY, cruise=CruiseState.ACTIVE)
  first = next_engagement_state(EngagementState(), active)
  assert not first.lfa_selected and not first.longitudinal_requested
  unknown = next_engagement_state(first, replace(READY, cruise=CruiseState.UNKNOWN))
  assert not unknown.longitudinal_requested
  recovered = next_engagement_state(unknown, active)
  assert not recovered.lfa_selected and not recovered.longitudinal_requested
  assert not next_engagement_state(EngagementState(), replace(active, lateral_permitted=False)).lateral_requested


def test_driver_override_and_simultaneous_toggle():
  initial = next_engagement_state(EngagementState(), replace(READY, lfa_toggle_requested=True))
  active = replace(READY, cruise=CruiseState.ACTIVE, always_steering=True)
  assert not next_engagement_state(initial, replace(active, lfa_toggle_requested=True)).lfa_selected
  override = next_engagement_state(initial, replace(active, driver_steering=True))
  assert override.lfa_selected and not override.lateral_requested
  assert not next_engagement_state(override, active).lateral_requested
  assert next_engagement_state(override, replace(active, driver_release_confirmed=True)).lateral_requested
