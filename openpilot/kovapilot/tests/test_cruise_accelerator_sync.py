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
