"""Carrot-compatible input from a modified navigation app on the local Wi-Fi.

The app speaks carrot's legacy CarrotMan protocol: the comma advertises itself
with a JSON datagram on UDP 7705 and the app sends JSON datagrams to UDP 7706
(carrot selfdrive/carrot/carrot_man.py and carrot_serv.py). This module only
parses and tracks the speed-camera, section and speed-bump items; it never
applies the app's other commands (time, route, cruise commands).
"""
from dataclasses import dataclass, field
import ipaddress
import json
from math import isfinite


DISCOVERY_PORT = 7705
NAVI_PORT = 7706
MAX_PACKET_BYTES = 4096
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))

# carrot_serv: any packet keeps the app connected for 80 frames at 20 Hz, an
# SDI packet keeps its items for 200 frames.
APP_TIMEOUT_NS = 4_000_000_000
SDI_TIMEOUT_NS = 10_000_000_000

# carrot defaults (common/params_keys.h): AutoNaviSpeedCtrlMode 2 includes
# speed bumps and excludes mobile cameras, AutoNaviSpeedBumpSpeed 35,
# AutoNaviSpeedBumpEndDistance 200 (cm), AutoNaviRearCameraHoldDistance 100.
CAMERA_TYPES = frozenset((0, 1, 2, 3, 8))
REAR_CAMERA_TYPES = frozenset((75, 76))
SECTION_TYPE = 4
SECTION_BLOCK_TYPES = frozenset((2, 3))
BUMP_TYPE = 22
BUMP_SPEED_KPH = 35.0
BUMP_END_DISTANCE_M = 2.0
REAR_CAMERA_HOLD_M = 100.0

MIN_LIMIT_KPH = 10
MAX_LIMIT_KPH = 200
MAX_DISTANCE_M = 5000
MAX_TRACKS = 8
# A reported item that stops being reported within this distance is followed
# by wheel distance until it is passed; a farther one is cleared.
LOST_HOLD_M = 50.0
LOST_HOLD_NS = 30_000_000_000
PASSED_REPORT_NS = 1_000_000_000

SDI_FIELDS = ("nSdiType", "nSdiSpeedLimit", "nSdiDist", "nSdiBlockType", "nSdiBlockDist",
              "nSdiPlusType", "nSdiPlusDist", "roadcate")
SDI_DEFAULTS = {"nSdiType": -1, "nSdiSpeedLimit": 0, "nSdiDist": -1, "nSdiBlockType": -1, "nSdiBlockDist": 0,
                "nSdiPlusType": -1, "nSdiPlusDist": 0, "roadcate": 0}


def lan_peer(address: str, network: ipaddress.IPv4Network | None) -> bool:
  """Accept only a private IPv4 peer on the comma's own Wi-Fi subnet.

  Tailscale (100.64.0.0/10), loopback (tailscaled forwards tailnet traffic to
  127.0.0.1) and public addresses are rejected."""
  if network is None:
    return False
  try:
    peer = ipaddress.IPv4Address(address)
  except ValueError:
    return False
  return peer in network and peer not in (network.network_address, network.broadcast_address) and \
    any(peer in private for private in PRIVATE_NETWORKS)


def _integer(value) -> int:
  # carrot converts with int(); JSON booleans and non-finite numbers are not values here.
  if isinstance(value, bool):
    raise ValueError("boolean")
  if isinstance(value, int):
    return value
  if isinstance(value, float):
    if not isfinite(value):
      raise ValueError("non-finite")
    return int(value)
  if isinstance(value, str) and len(value) <= 12:
    return int(value)
  raise ValueError("not an integer")


@dataclass(frozen=True)
class SdiItem:
  kind: str  # camera, rear_camera, section, bump
  speed_kph: float
  distance_m: float  # section: remaining distance to its end


@dataclass(frozen=True)
class Packet:
  sdi: bool
  items: tuple[SdiItem, ...] = ()


def parse_packet(data: bytes) -> Packet | None:
  """Parse one app datagram. None: not a valid carrot JSON object (dropped)."""
  if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_PACKET_BYTES:
    return None
  try:
    document = json.loads(bytes(data).decode("utf-8"))
  except (UnicodeDecodeError, ValueError, RecursionError):
    return None
  if not isinstance(document, dict):
    return None
  # carrot_serv.update handles SDI only in packets that carry nRoadLimitSpeed.
  if "nRoadLimitSpeed" not in document:
    return Packet(False)
  try:
    _integer(document["nRoadLimitSpeed"])
    values = {key: SDI_DEFAULTS[key] if document.get(key) is None else _integer(document[key]) for key in SDI_FIELDS}
  except (ValueError, OverflowError):
    return None
  return Packet(True, tuple(select_items(values)))


def _item(kind: str, speed_kph: float, distance_m: int) -> SdiItem | None:
  if not MIN_LIMIT_KPH <= speed_kph <= MAX_LIMIT_KPH or not 0 < distance_m <= MAX_DISTANCE_M:
    return None
  return SdiItem(kind, float(speed_kph), float(distance_m))


def select_items(values: dict[str, int]) -> list[SdiItem]:
  """carrot_serv._update_sdi with carrot's default AutoNaviSpeedCtrlMode 2.

  carrot can follow one item; both the primary camera and a secondary speed
  bump are returned here and the planner keeps the lower cap."""
  items = []
  sdi_type, limit = values["nSdiType"], values["nSdiSpeedLimit"]
  if limit > 0 and (sdi_type in CAMERA_TYPES or sdi_type in REAR_CAMERA_TYPES or sdi_type == SECTION_TYPE):
    if values["nSdiBlockType"] in SECTION_BLOCK_TYPES:
      items.append(_item("section", limit, values["nSdiBlockDist"]))
    elif sdi_type == SECTION_TYPE:
      items.append(_item("section", limit, values["nSdiDist"]))
    elif sdi_type in REAR_CAMERA_TYPES:
      items.append(_item("rear_camera", limit, values["nSdiDist"]))
    else:
      items.append(_item("camera", limit, values["nSdiDist"]))
  # roadcate 0/1 are motorways in carrot; bumps are ignored there.
  if values["roadcate"] > 1:
    if values["nSdiPlusType"] == BUMP_TYPE:
      items.append(_item("bump", BUMP_SPEED_KPH, values["nSdiPlusDist"]))
    elif sdi_type == BUMP_TYPE:
      items.append(_item("bump", BUMP_SPEED_KPH, values["nSdiDist"]))
  return [item for item in items if item is not None]


@dataclass
class Track:
  event_id: str
  kind: str
  speed_kph: float
  anchor_odo_m: float
  anchor_distance_m: float
  seen_ns: int
  reported: bool = True
  passed_ns: int | None = None
  cleared: bool = False
  published: bool = False

  def remaining(self, odo_m: float) -> float:
    return self.anchor_distance_m - (odo_m - self.anchor_odo_m)

  def done(self, odo_m: float) -> bool:
    remaining = self.remaining(odo_m)
    if self.kind == "rear_camera":
      return remaining <= -REAR_CAMERA_HOLD_M
    if self.kind == "bump":
      return remaining <= BUMP_END_DISTANCE_M
    return remaining <= 0.0


@dataclass
class EventTracker:
  """Turn repeated distance reports into kovaCameraEvents with stable ids."""
  source: str
  session: str
  tracks: list[Track] = field(default_factory=list)
  # (kind, speed, wheel-distance position, passed time) of passed items: a late
  # report of the same item must not start a second slowdown after it.
  passed: list[tuple[str, float, float, int]] = field(default_factory=list)
  next_id: int = 0
  sequence: int = 0

  def _new(self, item: SdiItem, now_ns: int, odo_m: float) -> Track:
    self.next_id += 1
    return Track(f"{item.kind}-{self.next_id}", item.kind, item.speed_kph, odo_m, item.distance_m, now_ns)

  def observe(self, items, now_ns: int, odo_m: float) -> None:
    active = [track for track in self.tracks if track.passed_ns is None and not track.cleared]
    matched = set()
    for item in items:
      best = None
      for track in active:
        if id(track) in matched or track.kind != item.kind or track.speed_kph != item.speed_kph:
          continue
        error = abs(track.remaining(odo_m) - item.distance_m)
        if item.kind == "section" or error <= 30.0 + 0.1 * item.distance_m:
          if best is None or error < best[0]:
            best = error, track
      if best is None:
        position = odo_m + item.distance_m
        if any(kind == item.kind and speed == item.speed_kph and abs(position - passed_at) <= 30.0 + 0.1 * item.distance_m
               for kind, speed, passed_at, _ in self.passed):
          continue
        track = self._new(item, now_ns, odo_m)
        self.tracks.append(track)
      else:
        track = best[1]
        track.anchor_odo_m, track.anchor_distance_m = odo_m, item.distance_m
      track.seen_ns, track.reported = now_ns, True
      matched.add(id(track))
    for track in active:
      if id(track) not in matched:
        track.reported = False
    if sum(not track.cleared for track in self.tracks) > MAX_TRACKS:
      # Keep the reported, then the nearest; a published overflow track is cleared, never silently dropped.
      keep = sorted((track for track in self.tracks if not track.cleared),
                    key=lambda track: (not track.reported, track.remaining(odo_m)))[:MAX_TRACKS]
      kept = {id(track) for track in keep}
      for track in self.tracks:
        if id(track) not in kept:
          track.cleared = True
      self.tracks = [track for track in self.tracks if id(track) in kept or track.published]

  def lose_all(self) -> None:
    for track in self.tracks:
      track.reported = False

  def step(self, now_ns: int, odo_m: float) -> list[dict]:
    events = []
    alive = []
    for track in self.tracks:
      remaining = track.remaining(odo_m)
      if track.cleared:
        events.append(self._event(track, now_ns, "clear", 0.0))
        continue
      if track.passed_ns is None:
        if track.done(odo_m) or (not track.reported and track.kind == "section"):
          track.passed_ns = now_ns
          if track.kind != "section":
            self.passed.append((track.kind, track.speed_kph, track.anchor_odo_m + track.anchor_distance_m, now_ns))
        elif not track.reported and (remaining > LOST_HOLD_M or now_ns - track.seen_ns > LOST_HOLD_NS):
          events.append(self._event(track, now_ns, "clear", 0.0))
          continue
      if track.passed_ns is not None:
        if now_ns - track.passed_ns > PASSED_REPORT_NS:
          continue
        events.append(self._event(track, now_ns, "passed", min(0.0, remaining) if track.kind != "section" else 0.0))
      else:
        distance = 0.0 if track.kind == "section" else max(0.0, remaining)
        events.append(self._event(track, now_ns, "present", distance))
      alive.append(track)
    self.tracks = alive
    self.passed = [row for row in self.passed if now_ns - row[3] <= LOST_HOLD_NS][-MAX_TRACKS:]
    return events

  def _event(self, track: Track, now_ns: int, state: str, distance_m: float) -> dict:
    self.sequence += 1
    track.published = True
    return {"source": self.source, "session": self.session, "eventId": track.event_id, "sequence": self.sequence,
            "observedMonoTime": now_ns, "state": state, "roadMatch": True, "verifiedSinglePoint": True,
            "distanceM": float(distance_m), "targetMps": float(track.speed_kph / 3.6) if state != "clear" else 0.0}

  def nearest_distance(self, odo_m: float) -> int:
    distances = [track.remaining(odo_m) for track in self.tracks if track.passed_ns is None and not track.cleared]
    return int(max(0.0, min(distances))) if distances else 0


@dataclass
class CarrotApp:
  """State of the app connection: freshness, SDI freshness and the tracker."""
  tracker: EventTracker
  last_packet_ns: int | None = None
  last_sdi_ns: int | None = None
  remote: tuple[str, int] | None = None

  def receive(self, data: bytes, address: tuple[str, int], now_ns: int, odo_m: float,
              network: ipaddress.IPv4Network | None, observe: bool = True) -> str:
    """observe=False: the WebSocket v2 app reports the cameras, this packet only keeps the app connected."""
    if not lan_peer(address[0], network):
      return "rejected_address"
    packet = parse_packet(data)
    if packet is None:
      return "rejected_packet"
    self.last_packet_ns, self.remote = now_ns, address
    if packet.sdi and observe:
      self.last_sdi_ns = now_ns
      self.tracker.observe(packet.items, now_ns, odo_m)
    return "accepted"

  def connected(self, now_ns: int) -> bool:
    return self.last_packet_ns is not None and 0 <= now_ns - self.last_packet_ns <= APP_TIMEOUT_NS

  def step(self, now_ns: int, odo_m: float) -> list[dict]:
    if not self.connected(now_ns) or self.last_sdi_ns is None or now_ns - self.last_sdi_ns > SDI_TIMEOUT_NS:
      self.tracker.lose_all()
    if not self.connected(now_ns):
      self.remote = None
    return self.tracker.step(now_ns, odo_m)


def advertisement(*, version: str, onroad: bool, ip: str, v_ego_kph: int, v_cruise_kph: float, cruise_speed_kph: float,
                  active: bool, sdi_dist: int) -> bytes:
  """carrot_man.make_send_message keys the app reads to find and follow the comma."""
  return json.dumps({
    "Carrot2": version, "IsOnroad": onroad, "CarrotRouteActive": False, "ip": ip, "port": NAVI_PORT, "navi_debug": 0,
    "log_carrot": "", "v_cruise_kph": v_cruise_kph, "carcruiseSpeed": cruise_speed_kph, "v_ego_kph": v_ego_kph,
    "tbt_dist": 0, "sdi_dist": sdi_dist, "active": active, "xState": 0, "trafficState": 0,
  }).encode("utf-8")
