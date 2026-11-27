"""KOVA navigation turn signal (navigation.auto_blinker).

When the carrot-compatible navigation app (navcamd, UDP 7706) announces a fork or an exit, request the turn signal
on that side; the lit turn signal then goes through the automatic lane change conditions in DesireHelper exactly as
a driver's turn signal (kovapilot/lateral/auto_lane_change.py). Guidance fields and turn types follow carrot
carrot_serv._update_tbt (nTBTTurnType, nTBTDist, nRoadLimitSpeed); the start distance follows carrot
update_auto_turn (start_fork_dist).

No vehicle can turn the turn signal on from openpilot yet, so nav_blinker_output_supported() is false everywhere,
the gateway reports the setting as unsupported and nothing sends a turn-signal frame:
  * IONIQ 9 (CAN FD, camera-SCC harness): the lamp follows SPAS2 (0x16A) BLINKER_CONTROL, and the parking ECU sends
    SPAS1/SPAS2 every 20 ms on the same bus (bus 0, 2026-11-27 drive). A second sender with the same id collides
    with it (carrot logged error frames in 78 and bus-off in 33 of 111 attempts on an IONIQ 5 PE). Avoiding that
    needs the parking ECU silenced over UDS (not a turn-signal message) or panda firmware that sends right behind the
    stock frame; kovapilot has neither.
  * Seltos (CAN): no turn-signal command is known on the buses the harness reaches (carrot has none either).
The stock navigation's CAN frames that navcamd reads carry camera warnings only, no turn-by-turn guidance.
"""
from dataclasses import dataclass
import json

import numpy as np

from openpilot.kovapilot.navigation.carrot_link import MAX_PACKET_BYTES, _integer

NAV_BLINKER_KEY = "navigation.auto_blinker"
LEFT, RIGHT = "left", "right"
# carrot _update_tbt: off ramps (slight left/right) and forks, xTurnInfo 3 and 4
TURN_TYPES = {
  **dict.fromkeys((102, 105, 112, 115, 7, 44, 17, 75, 76, 118), LEFT),
  **dict.fromkeys((101, 104, 111, 114, 6, 43, 73, 74, 123, 124, 117), RIGHT),
}
GUIDANCE_TIMEOUT_NS = 2_000_000_000
MAX_GUIDANCE_DISTANCE_M = 5000


def nav_blinker_output_supported(CP) -> bool:
  """Vehicles where openpilot can turn the turn signal on without disturbing another ECU: none yet (module doc)."""
  return False


def start_distance(road_limit_kph: float) -> float:
  """carrot update_auto_turn start_fork_dist: 160 m at 30 km/h, 200 m at 50 km/h, 350 m at 100 km/h."""
  return float(np.interp(road_limit_kph, [30, 50, 100], [160, 200, 350]))


@dataclass(frozen=True)
class Guidance:
  side: str  # LEFT or RIGHT
  distance_m: float
  road_limit_kph: float


def parse_guidance(data: bytes) -> Guidance | None:
  """Fork/exit guidance from one app datagram; None for anything else (no guidance, other turn types, bad values)."""
  if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_PACKET_BYTES:
    return None
  try:
    document = json.loads(bytes(data).decode("utf-8"))
  except (UnicodeDecodeError, ValueError, RecursionError):
    return None
  if not isinstance(document, dict) or "nRoadLimitSpeed" not in document:
    return None
  try:
    turn_type = _integer(document.get("nTBTTurnType", -1))
    distance = _integer(document.get("nTBTDist", 0))
    limit = _integer(document["nRoadLimitSpeed"])
  except (ValueError, OverflowError):
    return None
  side = TURN_TYPES.get(turn_type)
  if side is None or not 0 < distance <= MAX_GUIDANCE_DISTANCE_M:
    return None
  return Guidance(side, float(distance), float(max(limit, 0)))


class NavBlinker:
  """Turn-signal request from the latest guidance. The driver cancels it for that guidance by steering or signalling
  the other way (carrot atc_paused)."""

  def __init__(self):
    self.guidance: Guidance | None = None
    self.received_ns = 0
    self.canceled_side: str | None = None

  def receive(self, guidance: Guidance | None, now_ns: int) -> None:
    if guidance is None:
      return
    if self.guidance is None or guidance.side != self.guidance.side:
      self.canceled_side = None
    self.guidance, self.received_ns = guidance, now_ns

  def request(self, now_ns: int, CS) -> str | None:
    guidance = self.guidance
    if guidance is None or not 0 <= now_ns - self.received_ns <= GUIDANCE_TIMEOUT_NS:
      self.guidance, self.canceled_side = None, None
      return None
    if guidance.distance_m > start_distance(guidance.road_limit_kph):
      return None
    other_blinker = CS.rightBlinker if guidance.side == LEFT else CS.leftBlinker
    other_steer = CS.steeringPressed and (CS.steeringTorque < 0 if guidance.side == LEFT else CS.steeringTorque > 0)
    if other_blinker or other_steer:
      self.canceled_side = guidance.side
    return None if self.canceled_side == guidance.side else guidance.side
