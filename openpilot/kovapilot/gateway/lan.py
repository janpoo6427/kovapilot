"""Opt-in TLS listener on the device's private Wi-Fi IPv4 address."""

import asyncio
import fcntl
import hashlib
import hmac
import ipaddress
import os
import re
import signal
import socket
import ssl
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web


WIFI_INTERFACE = "wlan0"
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
IFF_UP_RUNNING = 0x1 | 0x40


@dataclass
class LanAccess:
  token_digest: bytes
  address: ipaddress.IPv4Interface | None = None

  def accepts_peer(self, local: str, remote: str) -> bool:
    if self.address is None or local != str(self.address.ip):
      return False
    try:
      peer = ipaddress.IPv4Address(remote)
    except ValueError:
      return False
    return peer in self.address.network and any(peer in network for network in PRIVATE_NETWORKS)

  def authenticates(self, authorization: str) -> bool:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or re.fullmatch(r"[0-9a-f]{64}", token) is None:
      return False
    return hmac.compare_digest(hashlib.sha256(token.encode("ascii")).digest(), self.token_digest)


LAN_ACCESS_KEY = web.AppKey("lan_access", LanAccess)
LAN_SOCKETS_KEY = web.AppKey("lan_sockets", set)


def load_lan_config(directory: Path) -> tuple[LanAccess, ssl.SSLContext] | None:
  if not directory.exists():
    return None
  for private in (directory / "server.key", directory / "token.sha256"):
    metadata = private.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
      raise ValueError("LAN credentials must be private files owned by the Gateway user")
  digest = (directory / "token.sha256").read_text(encoding="ascii").strip()
  if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
    raise ValueError("invalid LAN token digest")
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  context.minimum_version = ssl.TLSVersion.TLSv1_2
  context.load_cert_chain(directory / "server.pem", directory / "server.key", password="")
  return LanAccess(bytes.fromhex(digest)), context


def wifi_address() -> ipaddress.IPv4Interface | None:
  request = struct.pack("256s", WIFI_INTERFACE.encode())
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
      flags = struct.unpack_from("H", fcntl.ioctl(sock, SIOCGIFFLAGS, request), 16)[0]
      if flags & IFF_UP_RUNNING != IFF_UP_RUNNING:
        return None
      address = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFADDR, request)[20:24])
      mask = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFNETMASK, request)[20:24])
      interface = ipaddress.IPv4Interface(f"{address}/{mask}")
  except (OSError, ValueError):
    return None
  return interface if any(interface.ip in network for network in PRIVATE_NETWORKS) else None


def wifi_socket(address: ipaddress.IPv4Interface, port: int) -> socket.socket:
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, WIFI_INTERFACE.encode() + b"\0")
    sock.bind((str(address.ip), port))
    return sock
  except (OSError, AttributeError):
    sock.close()
    raise


async def serve_lan(app, port, context, on_bind_error):
  access = app[LAN_ACCESS_KEY]
  sockets = app[LAN_SOCKETS_KEY]
  runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
  stop = asyncio.Event()
  loop = asyncio.get_running_loop()
  for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, stop.set)

  site = None
  last_error_address = None
  try:
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    while not stop.is_set():
      address = wifi_address()
      if address != access.address:
        access.address = None
        if site is not None:
          await site.stop()
          site = None
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets) if ws.prepared), return_exceptions=True)
        if address is not None:
          try:
            sock = wifi_socket(address, port)
            candidate = web.SockSite(runner, sock, ssl_context=context)
            try:
              await candidate.start()
            except BaseException:
              await candidate.stop()
              sock.close()
              raise
            site = candidate
            access.address = address
            last_error_address = None
          except (OSError, AttributeError):
            if address != last_error_address:
              on_bind_error()
              last_error_address = address
      try:
        await asyncio.wait_for(stop.wait(), timeout=2)
      except TimeoutError:
        pass
  finally:
    access.address = None
    await runner.cleanup()
    for sig in (signal.SIGINT, signal.SIGTERM):
      loop.remove_signal_handler(sig)
