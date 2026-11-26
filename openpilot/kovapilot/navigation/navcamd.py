#!/usr/bin/env python3
"""Local navigation camera input: carrot-compatible app on Wi-Fi + stock navigation CAN.

Publishes kovaCameraEvents for plannerd. Runs only when plannerd consumes the
events for this drive (openpilot longitudinal and a speed camera profile);
otherwise it opens no network socket.
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
from openpilot.kovapilot.bridges.camera_runtime import CAMERA_BATCH_LIMIT, CAMERA_EVENT_SERVICE, read_camera_profile
from openpilot.kovapilot.navigation.carrot_link import DISCOVERY_PORT, NAVI_PORT, MAX_PACKET_BYTES, CarrotApp, EventTracker, \
  advertisement
from openpilot.kovapilot.navigation.vehicle_navi import HDA_INFO_4A3_ADDR, NAVI_HU_ADDR, SIGNAL_TIMEOUT_NS, VEHICLE_SOURCE, \
  VehicleCameraTracker, decode_hda_info_4a3, decode_navi_hu


LOOP_S = 0.05
STEP_NS = 100_000_000
BROADCAST_INTERVAL_NS = 1_000_000_000  # carrot_man BROADCAST_INTERVAL
BROADCAST_REMOTE_INTERVAL_NS = 200_000_000  # carrot_man BROADCAST_REMOTE_INTERVAL
NETWORK_REFRESH_NS = 5_000_000_000
MAX_SPEED_GAP_NS = 250_000_000
MAX_DATAGRAMS_PER_LOOP = 32


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
  def __init__(self, signal: tuple[int, int] | None, *, is_metric: bool = True, session: str | None = None):
    session = session or secrets.token_hex(6)
    self.app = CarrotApp(EventTracker("carrot_app", session))
    self.vehicle = VehicleCameraTracker(VEHICLE_SOURCE, session)
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
    return self.app.receive(data, address, now_ns, self.odo_m, network)

  def step(self, now_ns: int) -> list[dict]:
    warning = self.warning[1] if self.warning is not None and 0 <= now_ns - self.warning[0] <= SIGNAL_TIMEOUT_NS else None
    events = self.app.step(now_ns, self.odo_m)
    # carrot uses the stock warning only while no navigation app is connected.
    self.vehicle.update(warning, now_ns, self.odo_m, suppressed=self.app.connected(now_ns))
    events += self.vehicle.step(now_ns, self.odo_m)
    return events[:CAMERA_BATCH_LIMIT]


def camera_message(events: list[dict]):
  message = messaging.new_message(CAMERA_EVENT_SERVICE, len(events), valid=True)
  for index, event in enumerate(events):
    message.kovaCameraEvents[index] = event
  return message


def _udp_socket(address: str, port: int, *, broadcast: bool = False) -> socket.socket:
  from openpilot.kovapilot.gateway.lan import WIFI_INTERFACE
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
    from openpilot.kovapilot.gateway.lan import wifi_address
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
  params = Params()
  CP = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  profile = read_camera_profile(params) if CP.openpilotLongitudinalControl and not CP.dashcamOnly else 0
  if not profile:
    # plannerd does not read camera events this drive.
    while True:
      time.sleep(60)

  nav = NavCamera(vehicle_navi_signal(CP), is_metric=params.get_bool("IsMetric"))
  version = params.get("Version") or ""
  version = version.decode("utf-8", "replace") if isinstance(version, bytes) else str(version)
  sm = messaging.SubMaster(["carState", "selfdriveState"])
  can_sock = messaging.sub_sock("can") if nav.signal is not None else None
  pm = messaging.PubMaster([CAMERA_EVENT_SERVICE])
  network = Network()
  next_step_ns = 0
  cloudlog.info("navcamd: profile %d, vehicle signal %s", profile, nav.signal)

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
      if events:
        pm.send(CAMERA_EVENT_SERVICE, camera_message(events))
    network.advertise(now_ns, nav.app.remote, partial(_advertisement, version, sm, nav))
    time.sleep(LOOP_S)


if __name__ == "__main__":
  main()
