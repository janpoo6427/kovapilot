import pytest

from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import CruiseAcceleratorSync, MIN_PRESS_SAMPLES


@pytest.fixture
def update():
  policy = CruiseAcceleratorSync()

  def sample(speed_kph=80.0, pressed=False, *, eligible=True, can_arm=True, set_speed_kph=80.0, step_kph=1.0):
    return policy.update(eligible=eligible, can_arm=can_arm, pressed=pressed, speed_kph=speed_kph,
                         release_speed_kph=speed_kph, set_speed_kph=set_speed_kph, step_kph=step_kph, max_speed_kph=145.0)

  return sample


def press(update, samples=MIN_PRESS_SAMPLES, **kwargs):
  assert update(80.0, True, **kwargs) is None
  for _ in range(samples - 1):
    assert update(95.0, True, **kwargs) is None


def test_only_release_speed_is_used_once(update):
  assert update() is None
  press(update, can_arm=False)
  assert update(93.0, can_arm=False) == 93.0
  assert update(94.0) is None
  press(update, set_speed_kph=93.0)
  assert update(95.0, set_speed_kph=93.0) == 95.0


@pytest.mark.parametrize('release_speed', [77.0, 80.0, 80.8, float('nan'), float('inf')])
def test_rejects_non_increasing_or_invalid_release(update, release_speed):
  update()
  press(update)
  assert update(release_speed) is None


def test_no_reengagement_after_invalid_held_sample(update):
  update()
  press(update)
  update(90.0, True, eligible=False)
  press(update)
  assert update(93.0) is None


def test_must_see_released_pedal_while_cruise_active(update):
  update(can_arm=False)
  press(update)
  assert update(93.0) is None


def test_ignores_short_tap_and_changed_setpoint(update):
  update()
  press(update, samples=MIN_PRESS_SAMPLES - 1)
  assert update(93.0) is None
  press(update)
  assert update(93.0, set_speed_kph=85.0) is None


def test_metric_upper_limit(update):
  update()
  press(update)
  assert update(147.0) == 145.0


def test_imperial_rounding_and_upper_limit(update):
  step = 1.609344
  update(step_kph=step)
  press(update, step_kph=step)
  assert update(93.0, step_kph=step) == pytest.approx(58 * step)
  press(update, step_kph=step)
  assert update(150.0, step_kph=step) == pytest.approx(90 * step)


def test_imperial_rounding_does_not_advance_an_equal_step(update):
  step = 1.609344
  update(78.0, set_speed_kph=80.0, step_kph=step)
  for _ in range(MIN_PRESS_SAMPLES):
    assert update(78.0, True, set_speed_kph=80.0, step_kph=step) is None
  assert update(80.5, set_speed_kph=80.0, step_kph=step) is None


def held(update, speeds, **kwargs):
  return [update(speed, True, **kwargs) for speed in speeds]


def test_set_speed_follows_while_the_pedal_is_held(update):
  update()
  press(update, set_speed_kph=80.0)
  # one sample past the press length a release needs, the reached speed is used without waiting for the release
  assert held(update, [81.0, 81.4, 82.0, 85.0, 84.0, 85.0, 90.0], set_speed_kph=80.0) == [
    81.0, None, 82.0, 85.0, None, None, 90.0]
  # releasing at a lower speed keeps the highest step reached, and does not report it again
  assert update(88.0, set_speed_kph=80.0) is None


def test_follow_waits_as_long_as_a_release_would(update):
  update()
  assert update(80.0, True) is None
  for _ in range(MIN_PRESS_SAMPLES - 1):
    assert update(95.0, True) is None
  assert update(95.0, True) == 95.0


def test_follow_needs_a_speed_gain_and_a_speed_above_the_set_speed(update):
  update(85.0, set_speed_kph=90.0)
  assert held(update, [85.0] * MIN_PRESS_SAMPLES + [85.8, 89.0, 90.0, 90.4], set_speed_kph=90.0) == [None] * (MIN_PRESS_SAMPLES + 4)
  assert update(91.0, True, set_speed_kph=90.0) == 91.0


def test_follow_stops_when_the_state_becomes_invalid_while_held(update):
  update()
  press(update)
  assert update(90.0, True) == 90.0
  assert update(92.0, True, eligible=False) is None
  # without seeing the pedal released again nothing more is followed or applied on release
  assert held(update, [93.0] * (MIN_PRESS_SAMPLES + 2)) == [None] * (MIN_PRESS_SAMPLES + 2)
  assert update(93.0) is None


def test_follow_goes_on_after_the_caller_applies_its_target_and_stops_on_other_changes():
  policy = CruiseAcceleratorSync()

  def sample(speed_kph, pressed, set_speed_kph):
    return policy.update(eligible=True, can_arm=True, pressed=pressed, speed_kph=speed_kph, release_speed_kph=speed_kph,
                         set_speed_kph=set_speed_kph, step_kph=1.0, max_speed_kph=145.0)

  sample(80.0, False, 80.0)
  for _ in range(MIN_PRESS_SAMPLES):
    assert sample(80.0, True, 80.0) is None
  assert sample(85.0, True, 80.0) == 85.0
  policy.follow_setpoint(85.0, 1.0)
  assert sample(87.0, True, 85.0) == 87.0
  policy.follow_setpoint(87.0, 1.0)
  # a set speed the policy did not produce, for example a button, ends the press
  assert sample(90.0, True, 70.0) is None
  assert sample(91.0, True, 70.0) is None
  assert sample(92.0, False, 70.0) is None


def test_follow_metric_upper_limit(update):
  update()
  press(update)
  assert held(update, [146.0, 150.0]) == [145.0, None]
  assert update(150.0) is None


def test_follow_imperial_steps(update):
  step = 1.609344
  update(step_kph=step)
  press(update, step_kph=step)
  out = held(update, [81.0, 81.5, 82.0, 150.0], step_kph=step)
  assert out[0] is None  # 81 km/h is still step 50, the same as the 80 km/h set speed
  assert out[1] == pytest.approx(51 * step)
  assert out[2] is None
  assert out[3] == pytest.approx(90 * step)
