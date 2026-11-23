import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class CancelPressSequence:
  required_presses: int = 3
  window_seconds: float = 3.0
  echo_window_seconds: float = 0.15
  press_times: deque[float] = field(default_factory=deque)
  cancel_held: bool = False
  cancel_requests: deque[float] = field(default_factory=lambda: deque(maxlen=64))

  def reset(self) -> None:
    self.press_times.clear()
    self.cancel_held = False

  def record_cancel_request(self, event_time: float) -> None:
    if math.isfinite(event_time):
      self.cancel_requests.append(event_time)

  def _is_cancel_echo(self, event_time: float) -> bool:
    while self.cancel_requests and event_time - self.cancel_requests[0] > self.echo_window_seconds:
      self.cancel_requests.popleft()
    return any(abs(event_time - request_time) <= self.echo_window_seconds for request_time in self.cancel_requests)

  def update(self, event_time: float, buttons: list[tuple[str, bool]]) -> bool:
    if not math.isfinite(event_time):
      self.reset()
      return False

    triggered = False
    for button_type, pressed in buttons:
      if button_type != "cancel":
        self.reset()
        continue

      if not pressed:
        self.cancel_held = False
        continue

      if self.cancel_held:
        continue
      self.cancel_held = True

      if self._is_cancel_echo(event_time):
        self.press_times.clear()
        continue

      while self.press_times and (event_time - self.press_times[0] > self.window_seconds or
                                  event_time < self.press_times[-1]):
        self.press_times.popleft()
      self.press_times.append(event_time)
      if len(self.press_times) == self.required_presses:
        triggered = True
        self.press_times.clear()

    return triggered
