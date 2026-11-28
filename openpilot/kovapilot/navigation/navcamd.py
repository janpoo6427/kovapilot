#!/usr/bin/env python3
"""Local navigation input: carrot-compatible app on Wi-Fi + stock navigation CAN.

The app (carrot modified TMAP) speaks WebSocket v2 on TCP 7714 (carrot_navi_v2) or the legacy
UDP 7706 JSON (carrot_link); both find the comma from the UDP 7705 advertisement and are
accepted only from the Wi-Fi subnet. When both report, v2 is followed.

Publishes kovaCameraEvents for plannerd when plannerd consumes them this drive (openpilot
longitudinal and a speed camera, speed bump or road speed limit profile), and kovaNavigation
(turn-by-turn, road speed limit, remaining route) on every drive.
"""
from functools import partial
import ipaddress
import secrets
import socket
import time

from opendbc.car.structs import car
import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.bridges.camera_runtime import (CAMERA_BATCH_LIMIT, CAMERA_EVENT_SERVICE, CAMERA_KIND, ROAD_LIMIT_KIND,
                                                      read_profiles)
from openpilot.kovapilot.navigation.carrot_link import (APP_TIMEOUT_NS, DISCOVERY_PORT, NAVI_PORT, MAX_PACKET_BYTES,
                                                       SDI_TIMEOUT_NS, CarrotApp, EventTracker, advertisement, lan_peer)
from openpilot.kovapilot.navigation.carrot_navi_v2 import Snapshot, V2Receiver
from openpilot.kovapilot.navigation.carrot_navi_v2_server import V2Server
from openpilot.kovapilot.navigation.road_limit import RoadLimitTracker
from openpilot.kovapilot.navigation.vehicle_navi import HDA_INFO_4A3_ADDR, NAVI_HU_ADDR, SIGNAL_TIMEOUT_NS, VEHICLE_SOURCE, \
  VehicleCameraTracker, decode_hda_info_4a3, decode_navi_hu


LOOP_S = 0.05
STEP_NS = 100_000_000
BROADCAST_INTERVAL_NS = 1_000_000_000  # carrot_man BROADCAST_INTERVAL
BROADCAST_REMOTE_INTERVAL_NS = 200_000_000  # carrot_man BROADCAST_REMOTE_INTERVAL
NETWORK_REFRESH_NS = 5_000_000_000
MAX_SPEED_GAP_NS = 250_000_000
MAX_DATAGRAMS_PER_LOOP = 32
NAVIGATION_SERVICE = "kovaNavigation"
NAVIGATION_INTERVAL_NS = 200_000_000


def vehicle_navi_signal(CP) -> tuple[int, int] | None:
  """(bus, address) carrot reads the stock navigation camera warning from."""
  if CP.brand != "hyundai":
    return None
  from opendbc.car.hyundai.values import HyundaiFlags
  if CP.flags & HyundaiFlags.CANFD:
    from opendbc.car.hyundai.hyundaicanfd import CanBus
    return CanBus(CP).ECAN, HDA_INFO_4A3_ADDR
  from opendbc.car import CanBusBase
  return CanBusBase(CP, None).offset, NAVI_HU_ADDR


class NavCamera:
  def __init__(self, signal: tuple[int, int] | None, *, is_metric: bool = True, session: str | None = None,
               v2: V2Receiver | None = None, road_limit: bool = False):
    session = session or secrets.token_hex(6)
    # UDP 7706 and WebSocket v2 describe the same app; they share one tracker so a camera
    # keeps its event id when the app switches protocol.
    self.app = CarrotApp(EventTracker("carrot_app", session))
    self.v2 = v2 if v2 is not None else V2Receiver()
    self.v2_live = False
    self.v2_speed_ns: int | None = None  # reception time of the last speed item observed
    self.v2_sdi_ns: int | None = None
    self.snapshot = Snapshot()
    self.guide_anchor: tuple[int, float] | None = None  # (reception ns, odo m) of the current guide point
    self.vehicle = VehicleCameraTracker(VEHICLE_SOURCE, session)
    # Road limit events only while plannerd follows them (navigation.road_speed_limit_profile).
    self.road_limit = RoadLimitTracker(session) if road_limit else None
    self.signal = signal
    self.is_metric = is_metric
    self.warning = None  # (mono ns, Warning)
    self.odo_m = 0.0
    self.last_speed: tuple[int, float] | None = None

  def update_speed(self, now_ns: int, v_ego: float) -> None:
    if not 0.0 <= v_ego < 100.0:
      self.last_speed = None
      return
    if self.last_speed is not None:
      gap_ns = now_ns - self.last_speed[0]
      if gap_ns <= 0:
        return
      if gap_ns <= MAX_SPEED_GAP_NS:
        self.odo_m += (self.last_speed[1] + v_ego) * 0.5 * gap_ns / 1e9
    self.last_speed = now_ns, v_ego

  def handle_can(self, event) -> None:
    if self.signal is None:
      return
    bus, address = self.signal
    for frame in event.can:
      if frame.address == address and frame.src == bus:
        dat = bytes(frame.dat)
        warning = decode_navi_hu(dat) if address == NAVI_HU_ADDR else decode_hda_info_4a3(dat, self.is_metric)
        if warning is not None:
          self.warning = event.logMonoTime, warning

  def receive(self, data: bytes, address, now_ns: int, network) -> str:
    # While the v2 app reports, UDP 7706 cameras are not followed twice.
    return self.app.receive(data, address, now_ns, self.odo_m, network, observe=not self.v2_live)

  def step(self, now_ns: int) -> list[dict]:
    snapshot = self.snapshot = self.v2.snapshot(now_ns)
    received = snapshot.last_received_ns
    self.v2_live = snapshot.connected and received is not None and 0 <= now_ns - received <= APP_TIMEOUT_NS
    if self.v2_live and snapshot.speed_received_ns is not None and snapshot.speed_received_ns != self.v2_speed_ns:
      self.v2_speed_ns, self.v2_sdi_ns = snapshot.speed_received_ns, now_ns
      self.app.tracker.observe(snapshot.speed_items, now_ns, self.odo_m)
    if snapshot.current_received_ns is None:
      self.guide_anchor = None
    elif self.guide_anchor is None or self.guide_anchor[0] != snapshot.current_received_ns:
      self.guide_anchor = snapshot.current_received_ns, self.odo_m

    legacy = self.app.connected(now_ns)
    connected = legacy or self.v2_live
    sdi_times = [t for t in (self.app.last_sdi_ns, self.v2_sdi_ns) if t is not None]
    if not connected or not sdi_times or now_ns - max(sdi_times) > SDI_TIMEOUT_NS:
      self.app.tracker.lose_all()
    if not legacy:
      self.app.remote = None
    events = self.app.tracker.step(now_ns, self.odo_m)

    warning = self.warning[1] if self.warning is not None and 0 <= now_ns - self.warning[0] <= SIGNAL_TIMEOUT_NS else None
    # carrot uses the stock warning only while no navigation app is connected.
    self.vehicle.update(warning, now_ns, self.odo_m, suppressed=connected)
    events += self.vehicle.step(now_ns, self.odo_m)
    # Road limit: the v2 app while it reports, else the stock navigation while no app is connected (carrot).
    # The legacy UDP app carries no limit navcamd reads, so the stock one stays suppressed while it is connected.
    if self.road_limit is not None:
      if self.v2_live:
        road_limit = snapshot.road_limit_kph or None
      elif connected:
        road_limit = None
      else:
        road_limit = warning.limit_kph if warning is not None and warning.limit_kph > 0 else None
      ego_mps = self.last_speed[1] if self.last_speed is not None else 0.0
      self.road_limit.update(road_limit, now_ns, self.odo_m, ego_mps)
      # Appended last: when a batch is full, cameras keep their place.
      events += self.road_limit.step(now_ns, self.odo_m)
    return events[:CAMERA_BATCH_LIMIT]

  def remote(self):
    """Where the 7705 advertisement goes: the app that is sending, else broadcast."""
    if self.app.remote is not None:
      return self.app.remote
    return (self.snapshot.peer, DISCOVERY_PORT) if self.v2_live and self.snapshot.peer else None

  def navigation(self, now_ns: int) -> dict:
    """kovaNavigation from the last step: the turn-by-turn guidance and route of the app."""
    snapshot = self.snapshot
    legacy = self.app.connected(now_ns)
    received = [t for t in (snapshot.last_received_ns, self.app.last_packet_ns) if t is not None]
    state = {"source": "carrotV2" if self.v2_live else ("carrotLegacy" if legacy else "none"),
             "lastReceivedMonoTime": max(received) if received else 0}
    if not self.v2_live:
      return state
    travelled = self.odo_m - self.guide_anchor[1] if self.guide_anchor is not None else 0.0
    current, following = snapshot.current, snapshot.next
    state.update(
      appVersion=snapshot.app_version, guidanceActive=snapshot.guidance_active, offRoute=snapshot.off_route,
      roadLimitKph=snapshot.road_limit_kph, routeValid=snapshot.route_valid,
      remainingDistanceM=snapshot.remaining_distance_m, remainingTimeS=snapshot.remaining_time_s,
      current=_guide(current, max(0.0, current.distance_m - travelled)),
      next=_guide(following, float(following.distance_m)),
    )
    return state


def _guide(guide, distance_m: float) -> dict:
  if not guide.valid:
    return {"valid": False, "turnType": -1}
  return {"valid": True, "turnType": guide.turn_type, "distanceM": distance_m, "timeS": float(guide.time_s),
          "mainText": guide.main_text, "roadName": guide.road_name, "nearDirection": guide.near_direction,
          "farDirection": guide.far_direction}


def camera_message(events: list[dict]):
  message = messaging.new_message(CAMERA_EVENT_SERVICE, len(events), valid=True)
  for index, event in enumerate(events):
    message.kovaCameraEvents[index] = event
  return message


def navigation_message(state: dict):
  message = messaging.new_message(NAVIGATION_SERVICE, valid=True)
  message.kovaNavigation = state
  return message


def _udp_socket(address: str, port: int, *, broadcast: bool = False) -> socket.socket:
  from openpilot.kovapilot.gateway.wifi import WIFI_INTERFACE
  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  try:
    if broadcast:
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, WIFI_INTERFACE.encode() + b"\0")
    except (PermissionError, OSError):
      pass  # the peer filter still accepts only the Wi-Fi subnet
    sock.bind((address, port))
    sock.setblocking(False)
    return sock
  except OSError:
    sock.close()
    raise


class Network:
  """UDP 7706 receive and carrot-style advertisement on the Wi-Fi network only."""

  def __init__(self):
    self.interface: ipaddress.IPv4Interface | None = None
    self.receiver: socket.socket | None = None
    self.sender: socket.socket | None = None
    self.refreshed_ns = None
    self.advertised_ns = 0

  def close(self) -> None:
    for sock in (self.receiver, self.sender):
      if sock is not None:
        sock.close()
    self.receiver = self.sender = None

  def refresh(self, now_ns: int) -> None:
    if self.refreshed_ns is not None and now_ns - self.refreshed_ns < NETWORK_REFRESH_NS:
      return
    from openpilot.kovapilot.gateway.wifi import wifi_address
    self.refreshed_ns = now_ns
    interface = wifi_address()
    if interface != self.interface:
      self.close()
      self.interface = interface
    if self.interface is None or self.receiver is not None:
      return
    try:
      # carrot binds 0.0.0.0; the app may unicast or broadcast. lan_peer() accepts only Wi-Fi subnet peers.
      self.receiver = _udp_socket("0.0.0.0", NAVI_PORT)
      self.sender = _udp_socket(str(self.interface.ip), 0, broadcast=True)
    except OSError:
      cloudlog.exception("navcamd: cannot open UDP sockets")
      self.close()

  def receive(self) -> list[tuple[bytes, tuple[str, int]]]:
    datagrams = []
    if self.receiver is None:
      return datagrams
    for _ in range(MAX_DATAGRAMS_PER_LOOP):
      try:
        datagrams.append(self.receiver.recvfrom(MAX_PACKET_BYTES + 1))
      except BlockingIOError:
        break
      except OSError:
        self.close()
        break
    return datagrams

  def advertise(self, now_ns: int, remote, payload) -> None:
    if self.sender is None or self.interface is None:
      return
    interval = BROADCAST_REMOTE_INTERVAL_NS if remote is not None else BROADCAST_INTERVAL_NS
    if now_ns - self.advertised_ns < interval:
      return
    self.advertised_ns = now_ns
    target = remote[0] if remote is not None else str(self.interface.network.broadcast_address)
    try:
      self.sender.sendto(payload(str(self.interface.ip)), (target, DISCOVERY_PORT))
    except OSError:
      pass


def _advertisement(version: str, sm, nav: NavCamera, ip: str) -> bytes:
  state = sm["carState"]
  return advertisement(version=version, onroad=True, ip=ip, v_ego_kph=int(state.vEgoCluster * 3.6 + 0.5),
                       v_cruise_kph=float(state.vCruise), cruise_speed_kph=float(state.cruiseState.speed * 3.6),
                       active=bool(sm["selfdriveState"].active), sdi_dist=nav.app.tracker.nearest_distance(nav.odo_m))


def main():
  from openpilot.kovapilot.gateway.wifi import WIFI_INTERFACE

  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  # plannerd reads camera events only with openpilot longitudinal and a speed camera, speed bump or
  # road speed limit profile; the guidance (kovaNavigation) is published on every drive.
  profiles = read_profiles(params) if CP.openpilotLongitudinalControl and not CP.dashcamOnly else {}
  profile = max(profiles.values(), default=0)

  # The stock navigation CAN gives camera warnings and the road limit, no speed bumps.
  vehicle_signal = vehicle_navi_signal(CP) if profiles.get(CAMERA_KIND) or profiles.get(ROAD_LIMIT_KIND) else None
  nav = NavCamera(vehicle_signal, is_metric=params.get_bool("IsMetric"), road_limit=bool(profiles.get(ROAD_LIMIT_KIND)))
  version = params.get("Version") or ""
  version = version.decode("utf-8", "replace") if isinstance(version, bytes) else str(version)
  sm = messaging.SubMaster(["carState", "selfdriveState"])
  can_sock = messaging.sub_sock("can") if nav.signal is not None else None
  pm = messaging.PubMaster([CAMERA_EVENT_SERVICE, NAVIGATION_SERVICE])
  network = Network()

  def accept_peer(address: str) -> bool:
    interface = network.interface
    return lan_peer(address, interface.network if interface is not None else None)

  server = V2Server(nav.v2, accept_peer, interface=WIFI_INTERFACE)
  server.start()
  next_step_ns = next_navigation_ns = 0
  cloudlog.info("navcamd: profiles %s, vehicle signal %s", profiles, nav.signal)

  while True:
    sm.update(0)
    if sm.updated["carState"]:
      nav.update_speed(sm.logMonoTime["carState"], sm["carState"].vEgo)
    if can_sock is not None:
      for event in messaging.drain_sock(can_sock):
        nav.handle_can(event)
    now_ns = time.monotonic_ns()
    network.refresh(now_ns)
    for data, address in network.receive():
      nav.receive(data, address, now_ns, network.interface.network if network.interface is not None else None)
    if now_ns >= next_step_ns:
      next_step_ns = now_ns + STEP_NS
      events = nav.step(now_ns)
      if events and profile:
        pm.send(CAMERA_EVENT_SERVICE, camera_message(events))
      if now_ns >= next_navigation_ns:
        next_navigation_ns = now_ns + NAVIGATION_INTERVAL_NS
        pm.send(NAVIGATION_SERVICE, navigation_message(nav.navigation(now_ns)))
    network.advertise(now_ns, nav.remote(), partial(_advertisement, version, sm, nav))
    time.sleep(LOOP_S)


if __name__ == "__main__":
  main()
