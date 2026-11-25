from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path
import re


MAX_CAMERA_AGE_NS = 2_000_000_000
ALIAS = re.compile(r"[a-zA-Z0-9_-]{1,48}\Z")


@dataclass(frozen=True)
class CameraInput:
  source: str
  session: str
  event_id: str
  sequence: int
  observed_ns: int
  received_ns: int
  state: str
  road_match: bool
  distance_m: float | None = None
  target_mps: float | None = None

  def __post_init__(self):
    if any(not isinstance(alias, str) or not ALIAS.fullmatch(alias) for alias in (self.source, self.session, self.event_id)):
      raise ValueError("source, session and event_id must be fixture aliases")
    if any(type(value) is not int or value < 0 for value in (self.sequence, self.observed_ns, self.received_ns)):
      raise ValueError("sequence and timestamps must be non-negative integers")
    if self.observed_ns > self.received_ns or self.state not in ("present", "clear", "passed") or type(self.road_match) is not bool:
      raise ValueError("invalid time, state or road_match")
    if self.state != "clear":
      if type(self.distance_m) not in (int, float) or not isfinite(self.distance_m) or abs(self.distance_m) > 5000:
        raise ValueError("distance_m must be a finite camera distance")
      if self.state == "present":
        if self.distance_m < 0 or type(self.target_mps) not in (int, float) or not isfinite(self.target_mps) or not 0 < self.target_mps <= 60:
          raise ValueError("present cameras require positive target_mps and a non-negative distance")
      elif self.distance_m > 0:
        raise ValueError("passed requires a confirmed non-positive distance")


def read_camera_inputs(path: Path) -> list[CameraInput]:
  events = []
  with path.open(encoding="utf-8-sig") as stream:
    for line_number, line in enumerate(stream, 1):
      if not line.strip():
        continue
      try:
        events.append(CameraInput(**json.loads(line)))
      except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"navigation input line {line_number}: {exc}") from exc
  return sorted(events, key=lambda event: (event.received_ns, event.source, event.sequence))


class CameraInputs:
  def __init__(self):
    self.sessions: dict[str, str] = {}
    self.retired_sessions: set[tuple[str, str]] = set()
    self.events: dict[tuple[str, str], CameraInput] = {}

  def put(self, event: CameraInput) -> str | None:
    if (event.source, event.session) in self.retired_sessions:
      return "retired_session"
    previous_session = self.sessions.get(event.source)
    if previous_session is not None and previous_session != event.session:
      self.retired_sessions.add((event.source, previous_session))
      self.events = {key: row for key, row in self.events.items() if key[0] != event.source}
    self.sessions[event.source] = event.session
    key = event.source, event.event_id
    previous = self.events.get(key)
    if previous is not None and (event.sequence <= previous.sequence or event.observed_ns <= previous.observed_ns):
      return "duplicate_or_older"
    self.events[key] = event
    return None

  def current(self, now_ns: int) -> tuple[dict[str, list[CameraInput]], list[dict[str, str]]]:
    current: dict[str, list[CameraInput]] = {}
    excluded = []
    for event in self.events.values():
      reason = None
      if event.state == "clear":
        reason = "cleared"
      elif not event.road_match:
        reason = "road_mismatch"
      elif not 0 <= now_ns - event.observed_ns <= MAX_CAMERA_AGE_NS:
        reason = "expired"
      if reason is None:
        current.setdefault(event.event_id, []).append(event)
      else:
        excluded.append({"source": event.source, "reason": reason})
    return current, excluded
