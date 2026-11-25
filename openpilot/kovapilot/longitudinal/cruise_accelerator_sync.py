import math


MIN_PRESS_SAMPLES = 15  # 150 ms at card's 100 Hz
MIN_SPEED_GAIN_KPH = 1.0


class CruiseAcceleratorSync:
  def __init__(self):
    self._saw_released_pedal = False
    self._press_samples = 0
    self._initial_speed_kph = 0.0
    self._initial_setpoint = None

  def update(self, *, eligible: bool, can_arm: bool, pressed: bool, speed_kph: float, release_speed_kph: float,
             set_speed_kph: float, step_kph: float, max_speed_kph: float) -> float | None:
    if not eligible or not (0 < speed_kph < math.inf and 0 < release_speed_kph < math.inf and 0 < set_speed_kph <= max_speed_kph):
      self._saw_released_pedal = False
      self._press_samples = 0
      return None

    setpoint = (set_speed_kph, step_kph)
    if self._press_samples and setpoint != self._initial_setpoint:
      self._saw_released_pedal = False
      self._press_samples = 0

    if pressed:
      if self._saw_released_pedal:
        if self._press_samples == 0:
          self._initial_speed_kph = speed_kph
          self._initial_setpoint = setpoint
        self._press_samples += 1
      return None

    held_long_enough = self._press_samples >= MIN_PRESS_SAMPLES
    self._saw_released_pedal = can_arm
    self._press_samples = 0
    if not held_long_enough or speed_kph - self._initial_speed_kph < MIN_SPEED_GAIN_KPH or release_speed_kph <= set_speed_kph:
      return None

    current_step = math.floor(set_speed_kph / step_kph + 0.5)
    target_step = min(math.floor(release_speed_kph / step_kph + 0.5), math.floor(max_speed_kph / step_kph))
    return target_step * step_kph if target_step > current_step else None
