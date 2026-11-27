import math


MIN_PRESS_SAMPLES = 15  # 150 ms at card's 100 Hz
MIN_SPEED_GAIN_KPH = 1.0


class CruiseAcceleratorSync:
  """Raise the cruise set speed to the speed reached with the accelerator pedal.

  The set speed follows while the pedal is held (once the press is longer than MIN_PRESS_SAMPLES), so the
  driver sees it climb, and the release applies the same rule one last time. It only ever goes up: easing off
  while still pressing, or after releasing, keeps the highest step reached.

  A press normally counts only when it starts after a released pedal with control active. When cruise engages
  on the accelerator press itself, allow_held_press() lets that press count from the engagement on."""

  def __init__(self):
    self._saw_released_pedal = False
    self._press_samples = 0
    self._initial_speed_kph = 0.0
    self._initial_setpoint = None
    self._reported_step = None

  def allow_held_press(self) -> None:
    """Cruise engaged on this accelerator press: follow it like a press started after a release."""
    if not self._press_samples:
      self._saw_released_pedal = True

  def follow_setpoint(self, set_speed_kph: float, step_kph: float) -> None:
    """The caller applied a target while the pedal is held: that new set speed is ours, not a driver change."""
    if self._press_samples:
      self._initial_setpoint = (set_speed_kph, step_kph)

  def _target(self, speed_kph: float, release_speed_kph: float, set_speed_kph: float, step_kph: float,
              max_speed_kph: float) -> float | None:
    if speed_kph - self._initial_speed_kph < MIN_SPEED_GAIN_KPH or release_speed_kph <= set_speed_kph:
      return None
    current_step = math.floor(set_speed_kph / step_kph + 0.5)
    if self._reported_step is not None:
      current_step = max(current_step, self._reported_step)
    target_step = min(math.floor(release_speed_kph / step_kph + 0.5), math.floor(max_speed_kph / step_kph))
    if target_step <= current_step:
      return None
    self._reported_step = target_step
    return target_step * step_kph

  def update(self, *, eligible: bool, can_arm: bool, pressed: bool, speed_kph: float, release_speed_kph: float,
             set_speed_kph: float, step_kph: float, max_speed_kph: float) -> float | None:
    if not eligible or not (0 < speed_kph < math.inf and 0 < release_speed_kph < math.inf and 0 < set_speed_kph <= max_speed_kph):
      self._saw_released_pedal = False
      self._press_samples = 0
      self._reported_step = None
      return None

    setpoint = (set_speed_kph, step_kph)
    if self._press_samples and setpoint != self._initial_setpoint:
      self._saw_released_pedal = False
      self._press_samples = 0
      self._reported_step = None

    if pressed:
      if self._saw_released_pedal:
        if self._press_samples == 0:
          self._initial_speed_kph = speed_kph
          self._initial_setpoint = setpoint
          self._reported_step = None
        self._press_samples += 1
        # held longer than a release would need: follow the speed now instead of waiting for the release
        if self._press_samples > MIN_PRESS_SAMPLES:
          return self._target(speed_kph, release_speed_kph, set_speed_kph, step_kph, max_speed_kph)
      return None

    held_long_enough = self._press_samples >= MIN_PRESS_SAMPLES
    self._saw_released_pedal = can_arm
    self._press_samples = 0
    target = self._target(speed_kph, release_speed_kph, set_speed_kph, step_kph, max_speed_kph) if held_long_enough else None
    self._reported_step = None
    return target
