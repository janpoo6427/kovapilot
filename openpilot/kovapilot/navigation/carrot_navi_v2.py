"""Navigation guidance from the carrot modified TMAP app over WebSocket v2 (TCP 7714).

The app (com.skt.tmap.ku, CarrotNavi 11.2.3.3740) finds the comma from the UDP 7705
advertisement ("ip"), opens a control WebSocket, offers its 28-item catalog and opens
one WebSocket per item the manifest enables (carrot selfdrive/carrot/carrot_navi_api.md,
carrot_navi.py). Only the JSON items used here are enabled; every image and the map
render stay disabled, so the app sends no pictures.

The protocol has no authentication: like UDP 7706, only private peers on the Wi-Fi
subnet of the comma are accepted (carrot_link.lan_peer). This module is the protocol
state; the aiohttp server lives in carrot_navi_v2_server.py.
"""
from dataclasses import dataclass, field
import secrets
import threading

from openpilot.kovapilot.navigation.carrot_link import SDI_DEFAULTS, SdiItem, _integer, select_items


PORT = 7714
PROTOCOL_VERSION = 2
CATALOG_REVISION = 1
SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 256 * 1024
MAX_TEXT = 64

JSON_NAMES = ("vehicle", "guidance_current", "guidance_next", "lane_current", "lane_ahead", "speed", "traffic_signal",
              "crossroad", "route", "navigation_status", "app_status", "camera_state", "composition_state")
IMAGE_NAMES = ("tbt_current_compact", "tbt_current_full", "tbt_next", "traffic_signal", "lane_top", "lane_bottom",
               "safety_primary", "safety_secondary", "safety_section", "crossroad_minimized", "crossroad_expanded",
               "center_tbt_icon", "center_tbt_text", "center_tbt_fee")
RENDER_NAMES = ("map_main",)
CATALOG = tuple([("json", n) for n in JSON_NAMES] + [("image", n) for n in IMAGE_NAMES] +
                [("render", n) for n in RENDER_NAMES])
# Turn-by-turn, road speed limit, cameras and bumps, remaining route, guidance state and
# the road category carrot uses to ignore bumps on motorways.
ENABLED = frozenset(("guidance_current", "guidance_next", "speed", "route", "navigation_status", "lane_current"))
# Heartbeat mode repeats an unchanged value every second, so a camera ahead of a stopped
# car stays fresh as with UDP 7706 (on_change alone goes quiet).
JSON_PARAMS = {"delivery_mode": "on_change_with_heartbeat", "interval_ms": 1000, "stale_timeout_ms": 10000}
IMAGE_PARAMS = {"format": "png", "max_fps": 1, "stale_timeout_ms": 15000}
# carrot_navi._stream_params defaults; the item is disabled.
RENDER_PARAMS = {
  "composition": "map_route_vehicle", "width": 960, "height": 540, "dpi": 360, "fps": 5, "jpeg_quality": 75,
  "codec": "h264", "h264_bitrate_kbps": 3000, "h264_keyframe_interval_sec": 2, "camera_mode": "app_sync",
  "map_theme": "auto", "map_type": "normal", "zoom": 11.0, "tilt": 50.0, "bearing": 0.0,
  "follow_vehicle_bearing": True, "fov": 40.0, "screen_center_y_ratio": 0.8, "follow_vehicle": True,
  "center_latitude": None, "center_longitude": None, "stale_timeout_ms": 5000,
}
# Freshness of one item: the app repeats each enabled item every second.
ITEM_TIMEOUT_NS = 4_000_000_000
MAX_DISTANCE_M = 2_000_000
MAX_TIME_S = 360_000
SECTION_TYPE = 4
SECTION_BLOCK = 2
DEFAULT_ROAD_CATEGORY = 8  # carrot_serv until lane_current reports one


def build_manifest(session_id: str, revision: int = 1) -> dict:
  streams = []
  for handle, (kind, name) in enumerate(CATALOG, start=1):
    if kind == "json":
      params = dict(JSON_PARAMS)
    elif kind == "image":
      params = dict(IMAGE_PARAMS)
      if name.startswith("crossroad_"):
        params["theme"] = "auto"  # both crossroad items must resolve to the same theme
    else:
      params = dict(RENDER_PARAMS)
    streams.append({"kind": kind, "name": name, "schema_version": SCHEMA_VERSION, "stream_handle": handle,
                    "enabled": kind == "json" and name in ENABLED, "params": params})
  return {"type": "subscription_manifest", "protocol_version": PROTOCOL_VERSION, "session_id": session_id,
          "revision": revision, "metrics_enabled": False, "streams": streams}


def stream_handle(name: str) -> int:
  return CATALOG.index(("json", name)) + 1


def protocol_error(code: str, message: str, name: str | None = None) -> dict:
  error = {"type": "protocol_error", "protocol_version": PROTOCOL_VERSION, "code": code, "recoverable": True,
           "message": str(message)[:256]}
  if name is not None:
    error.update(kind="json", name=name)
  return error


def _count(value, maximum: int) -> int | None:
  """A nonnegative count (m, s); None when absent or not a usable number."""
  if value is None:
    return None
  try:
    number = _integer(value)
  except (ValueError, OverflowError):
    return None
  return number if 0 <= number <= maximum else None


def _code(value) -> int | None:
  """An enum-like integer (TMAP turn or SDI type): 0 is a value, negative means absent."""
  if value is None:
    return None
  try:
    number = _integer(value)
  except (ValueError, OverflowError):
    return None
  return number if 0 <= number <= 100_000 else None


def _text(value) -> str:
  return value[:MAX_TEXT] if isinstance(value, str) else ""


def _dict(value) -> dict:
  return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Guide:
  turn_type: int = -1  # TMAP turn code (carrot nTBTTurnType); -1 none
  distance_m: int = 0
  time_s: int = 0
  main_text: str = ""
  road_name: str = ""
  near_direction: str = ""
  far_direction: str = ""

  @property
  def valid(self) -> bool:
    return self.turn_type >= 0


def parse_guide(value) -> Guide:
  value = _dict(value)
  turn_type = _code(value.get("turn_type"))
  distance = _count(value.get("distance_m"), MAX_DISTANCE_M)
  if turn_type is None or distance is None:
    return Guide()
  return Guide(turn_type, distance, _count(value.get("time_sec"), MAX_TIME_S) or 0, _text(value.get("main_text")),
               _text(value.get("road_name")), _text(value.get("near_direction")), _text(value.get("far_direction")))


def road_limit_kph(speed) -> int:
  """speed.road_limit_kph is already validated by the app: 10..200 in steps of 10; 0 unknown."""
  limit = _count(_dict(speed).get("road_limit_kph"), 200)
  return limit if limit is not None and limit >= 10 and limit % 10 == 0 else 0


def sdi_values(speed, road_category: int, off_route: bool) -> dict[str, int]:
  """carrot_serv._apply_carrot_navi_speed: a v2 speed value as UDP 7706 SDI fields."""
  values = dict(SDI_DEFAULTS)
  values["roadcate"] = road_category
  speed = _dict(speed)
  if off_route:
    return values
  section = _dict(speed.get("section"))
  section_limit = _count(section.get("speed_limit_kph"), 200) or 0
  section_distance = _count(section.get("remaining_distance_m"), MAX_DISTANCE_M) or 0
  section_active = (section.get("active") is True and section.get("suspended") is not True and
                    section.get("off_route") is not True and section_limit > 0)
  if section_active:
    values.update(nSdiType=SECTION_TYPE, nSdiSpeedLimit=section_limit, nSdiDist=section_distance,
                  nSdiBlockType=SECTION_BLOCK, nSdiBlockDist=section_distance)
  else:
    sdi = _dict(speed.get("sdi"))
    sdi_type = _code(sdi.get("type"))
    if sdi_type is not None:
      block_type = _code(sdi.get("block_type"))
      values.update(nSdiType=sdi_type, nSdiSpeedLimit=_count(sdi.get("speed_limit_kph"), 200) or 0,
                    nSdiDist=_count(sdi.get("distance_m"), MAX_DISTANCE_M) or -1,
                    nSdiBlockType=-1 if block_type is None else block_type,
                    nSdiBlockDist=_count(sdi.get("block_distance_m"), MAX_DISTANCE_M) or 0)
  secondary = _dict(speed.get("sdi_secondary"))
  secondary_type = _code(secondary.get("type"))
  if secondary_type is not None:
    values.update(nSdiPlusType=secondary_type, nSdiPlusDist=_count(secondary.get("distance_m"), MAX_DISTANCE_M) or 0)
  return values


@dataclass(frozen=True)
class Record:
  present: bool
  value: object
  sequence: int
  received_ns: int


@dataclass(frozen=True)
class Snapshot:
  """What the app reported, read by navcamd once per step."""
  connected: bool = False  # control socket open with an applied manifest
  app_version: str = ""
  peer: str | None = None
  last_received_ns: int | None = None
  speed_sequence: int | None = None  # changes with every speed item (cameras, bumps)
  speed_items: tuple[SdiItem, ...] = ()
  speed_received_ns: int | None = None
  road_limit_kph: int = 0
  current: Guide = Guide()
  current_received_ns: int | None = None
  next: Guide = Guide()
  guidance_active: bool = False
  off_route: bool = False
  route_valid: bool = False
  remaining_distance_m: int = 0
  remaining_time_s: int = 0


def _nonnegative(envelope: dict, key: str) -> int:
  value = envelope.get(key)
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise ValueError(f"invalid {key}")
  return value


@dataclass
class V2Receiver:
  """Protocol state shared by the server thread and navcamd (thread-safe)."""
  lock: threading.Lock = field(default_factory=threading.Lock)
  owner: object = None  # the control socket that negotiated the session
  session_id: str | None = None
  revision: int = 0
  applied: bool = False
  app_version: str = ""
  peer: str | None = None
  records: dict[str, Record] = field(default_factory=dict)
  # Latest item socket per name: a reconnected item socket replaces the old one.
  sockets: dict[str, object] = field(default_factory=dict)
  road_category: int = DEFAULT_ROAD_CATEGORY
  last_received_ns: int | None = None
  last_error: str = ""

  def negotiate(self, owner, requirements: dict, app_version: str, peer: str) -> dict:
    """The manifest; ValueError for an invalid offer, LookupError while another phone owns the session."""
    if requirements.get("type") != "requirements_query" or requirements.get("protocol_version") != PROTOCOL_VERSION:
      raise ValueError("invalid v2 requirements query")
    revision = requirements.get("catalog_revision")
    if isinstance(revision, bool) or revision != CATALOG_REVISION:
      raise ValueError("unsupported v2 catalog revision")
    offered = requirements.get("streams")
    if not isinstance(offered, list) or len(offered) != len(CATALOG):
      raise ValueError("app v2 catalog does not contain exactly 28 items")
    keys = []
    for stream in offered:
      if not isinstance(stream, dict) or stream.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid v2 catalog entry")
      keys.append((stream.get("kind"), stream.get("name")))
    if len(set(keys)) != len(keys) or set(keys) != set(CATALOG):
      raise ValueError("app v2 catalog does not match receiver catalog")
    with self.lock:
      if self.owner is not None and self.owner is not owner:
        raise LookupError("another navigation control connection is active")
      if self.owner is owner and self.session_id is not None:
        # The app repeats requirements_query every second until a manifest is applied.
        return build_manifest(self.session_id, self.revision)
      self.owner, self.session_id, self.revision, self.applied = owner, secrets.token_hex(8), 1, False
      self.app_version, self.peer = _text(app_version), peer
      self.records.clear()
      self.sockets.clear()
      self.road_category = DEFAULT_ROAD_CATEGORY
      return build_manifest(self.session_id, self.revision)

  def control(self, owner, payload: dict) -> None:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
      raise ValueError("unsupported v2 control protocol")
    with self.lock:
      if payload.get("type") == "protocol_error":
        # Recorded, never answered: no error loop between the two ends.
        self.last_error = _text(payload.get("message"))
        return
      if owner is not self.owner:
        raise ValueError("negotiate navigation control before sending events")
      kind = payload.get("type")
      if kind == "manifest_applied":
        if payload.get("session_id") != self.session_id or payload.get("revision") != self.revision:
          raise ValueError("manifest_applied for another session or revision")
        self.applied = True
      elif kind == "manifest_rejected":
        self.applied = False
        self.last_error = _text(payload.get("error"))

  def disconnect(self, owner) -> None:
    with self.lock:
      if owner is self.owner:
        self.owner, self.session_id, self.applied, self.peer = None, None, False, None
        self.records.clear()
        self.sockets.clear()

  def open_item(self, socket, session_id: str, name: str) -> None:
    with self.lock:
      if self.owner is None or session_id != self.session_id or name not in ENABLED:
        raise ValueError("v2 stream is not enabled")
      self.sockets[name] = socket
      self.records.pop(name, None)  # a replaced item worker may restart its sequence

  def close_item(self, socket, name: str) -> None:
    with self.lock:
      if self.sockets.get(name) is socket:
        del self.sockets[name]

  def item(self, socket, session_id: str, name: str, envelope, now_ns: int) -> None:
    """Record one item_update; ValueError closes the item socket (carrot ws_json)."""
    if (not isinstance(envelope, dict) or envelope.get("type") != "item_update" or
        envelope.get("protocol_version") != PROTOCOL_VERSION or envelope.get("session_id") != session_id or
        envelope.get("kind") != "json" or envelope.get("name") != name):
      raise ValueError("v2 JSON envelope/path mismatch")
    sequence = _nonnegative(envelope, "sequence")
    _nonnegative(envelope, "source_timestamp_ms")
    present = envelope.get("present")
    if not isinstance(present, bool) or "value" not in envelope:
      raise ValueError("v2 JSON item needs present and value")
    value = envelope["value"]
    if present and not isinstance(value, dict):
      raise ValueError(f"v2 JSON {name} value must be an object")
    if not present and value is not None:
      raise ValueError("absent JSON item must contain value=null")
    with self.lock:
      if self.sockets.get(name) is not socket or session_id != self.session_id:
        raise ValueError("stale v2 session or replaced item socket")
      if envelope.get("manifest_revision") != self.revision:
        raise ValueError("stale v2 manifest revision")
      if envelope.get("stream_handle") != stream_handle(name):
        raise ValueError("v2 stream handle mismatch")
      previous = self.records.get(name)
      if previous is not None and sequence <= previous.sequence:
        raise ValueError("stale v2 sequence")
      self.records[name] = Record(present, value if present else None, sequence, now_ns)
      self.last_received_ns = now_ns
      if name == "lane_current" and present:
        category = _code(value.get("road_category"))
        if category is not None and category <= 12:
          self.road_category = category

  def snapshot(self, now_ns: int) -> Snapshot:
    with self.lock:
      def fresh(name):
        record = self.records.get(name)
        if record is None or not 0 <= now_ns - record.received_ns <= ITEM_TIMEOUT_NS:
          return None
        return record

      if self.owner is None or not self.applied:
        return Snapshot(last_received_ns=self.last_received_ns)
      status = fresh("navigation_status")
      status_value = _dict(status.value) if status is not None and status.present else {}
      off_route = status_value.get("off_route") is True
      speed = fresh("speed")
      speed_value = speed.value if speed is not None and speed.present else None
      current, following, route = fresh("guidance_current"), fresh("guidance_next"), fresh("route")
      route_value = _dict(route.value) if route is not None and route.present else {}
      remaining = _count(route_value.get("remain_distance_m"), MAX_DISTANCE_M)
      return Snapshot(
        connected=True, app_version=self.app_version, peer=self.peer, last_received_ns=self.last_received_ns,
        speed_sequence=speed.sequence if speed is not None else None,
        speed_items=tuple(select_items(sdi_values(speed_value, self.road_category, off_route))) if speed else (),
        speed_received_ns=speed.received_ns if speed is not None else None,
        road_limit_kph=road_limit_kph(speed_value),
        current=parse_guide(current.value) if current is not None and current.present else Guide(),
        current_received_ns=current.received_ns if current is not None else None,
        next=parse_guide(following.value) if following is not None and following.present else Guide(),
        guidance_active=status_value.get("guidance_active") is True, off_route=off_route,
        route_valid=remaining is not None, remaining_distance_m=remaining or 0,
        remaining_time_s=_count(route_value.get("remain_time_sec"), MAX_TIME_S) or 0,
      )
