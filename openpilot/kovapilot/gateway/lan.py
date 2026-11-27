"""Opt-in TLS listener on the device's private Wi-Fi IPv4 address."""

import asyncio
import hashlib
import hmac
import ipaddress
import os
import re
import signal
import socket
import ssl
from contextlib import suppress
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web


from openpilot.kovapilot.gateway import wifi
from openpilot.kovapilot.gateway.wifi import PRIVATE_NETWORKS, WIFI_INTERFACE
OWNER_FILE = "owner.sha256"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass
class LanAccess:
  token_digest: bytes | None
  address: ipaddress.IPv4Interface | None = None
  # Automatic Wi-Fi only: the phone registered with a code shown on the comma.
  owner_digest: bytes | None = None
  owner_file: Path | None = None

  @property
  def authentication(self) -> str:
    return "localNetwork" if self.token_digest is None else "bearer"

  @property
  def owner_secret(self) -> bytes | None:
    """Digest of the owner's bearer token: the provisioned one, else the paired phone's."""
    return self.token_digest if self.token_digest is not None else self.owner_digest

  def is_owner(self, authorization: list[str]) -> bool:
    secret = self.owner_secret
    if secret is None or len(authorization) != 1:
      return False
    scheme, _, token = authorization[0].partition(" ")
    if scheme.lower() != "bearer" or TOKEN_PATTERN.fullmatch(token) is None:
      return False
    return hmac.compare_digest(hashlib.sha256(token.encode("ascii")).digest(), secret)

  def set_owner(self, token: str) -> None:
    """Replace the paired owner. The token itself is never stored."""
    if self.token_digest is not None or self.owner_file is None or TOKEN_PATTERN.fullmatch(token) is None:
      raise ValueError("owner pairing needs automatic Wi-Fi access and a 64 hex token")
    digest = hashlib.sha256(token.encode("ascii")).digest()
    fd, temporary = tempfile.mkstemp(dir=self.owner_file.parent, prefix=".owner.")
    try:
      with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(digest.hex() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
      os.chmod(temporary, 0o600)
      os.replace(temporary, self.owner_file)
    except BaseException:
      with suppress(FileNotFoundError):
        os.unlink(temporary)
      raise
    self.owner_digest = digest

  def accepts_peer(self, local: str, remote: str) -> bool:
    if self.address is None or local != str(self.address.ip):
      return False
    try:
      peer = ipaddress.IPv4Address(remote)
    except ValueError:
      return False
    return peer in self.address.network and any(peer in network for network in PRIVATE_NETWORKS)

  def authenticates(self, authorization: str) -> bool:
    if self.token_digest is None:
      return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or re.fullmatch(r"[0-9a-f]{64}", token) is None:
      return False
    return hmac.compare_digest(hashlib.sha256(token.encode("ascii")).digest(), self.token_digest)


LAN_ACCESS_KEY = web.AppKey("lan_access", LanAccess)
LAN_SOCKETS_KEY = web.AppKey("lan_sockets", set)

# The comma's Tailscale runs in userspace: tailscaled hands a tailnet
# connection to 100.x:<port> to 127.0.0.1:<port> on the comma. The Gateway
# listens with its Wi-Fi certificate on this loopback port for the owner's
# phone over the VPN.
TAILNET_PORT = 8083
TAILNET_KEY = web.AppKey("tailnet_port", int)
# (local, peer) of a plain loopback connection -> True when tailscaled made
# it (a tailnet peer), which the loopback API must not trust.
LOOPBACK_GUARD_KEY = web.AppKey("loopback_guard", Callable)
VIA_TAILNET = web.RequestKey("kova_via_tailnet", bool)


def _proc_address(ip: str, port: int) -> tuple[str, str]:
  """(IPv4 table, IPv6 table) spelling of ip:port in /proc/net/tcp*."""
  v4 = socket.inet_aton(ip)
  v6 = ipaddress.IPv6Address("::ffff:" + ip).packed
  six = "".join(v6[i:i + 4][::-1].hex() for i in range(0, 16, 4))
  return f"{v4[::-1].hex()}:{port:04x}".upper(), f"{six}:{port:04x}".upper()


def peer_socket_inode(local: tuple, peer: tuple) -> str | None:
  """Inode of the client end of our accepted loopback connection."""
  try:
    ours_v4, ours_v6 = _proc_address(local[0], local[1])
    theirs_v4, theirs_v6 = _proc_address(peer[0], peer[1])
  except (OSError, ValueError, IndexError, TypeError):
    return None
  for table, theirs, ours in (("/proc/net/tcp", theirs_v4, ours_v4), ("/proc/net/tcp6", theirs_v6, ours_v6)):
    try:
      lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
    except OSError:
      continue
    for line in lines:
      fields = line.split()
      if len(fields) > 9 and fields[1] == theirs and fields[2] == ours and fields[9] != "0":
        return fields[9]
  return None


def connection_from_process(pid: int, local: tuple, peer: tuple) -> bool:
  """True when process [pid] holds the other end of a loopback connection.
  Unknown (the socket is not found or the process cannot be read) counts as
  True: the caller refuses the connection."""
  inode = peer_socket_inode(local, peer)
  if inode is None:
    return True
  target = f"socket:[{inode}]"
  fds = Path(f"/proc/{pid}/fd")
  try:
    names = os.listdir(fds)
  except OSError:
    return True
  for name in names:
    with suppress(OSError):
      if os.readlink(fds / name) == target:
        return True
  return False


def load_lan_config(directory: Path, *, require_token: bool = True) -> tuple[LanAccess, ssl.SSLContext] | None:
  if not directory.exists():
    return None
  private_files = ["server.key", "token.sha256"] if require_token else ["server.key"]
  for filename in private_files:
    private = directory / filename
    metadata = private.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
      raise ValueError("LAN credentials must be private files owned by the Gateway user")
  token_digest = None
  if require_token:
    digest = (directory / "token.sha256").read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
      raise ValueError("invalid LAN token digest")
    token_digest = bytes.fromhex(digest)
  context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  context.minimum_version = ssl.TLSVersion.TLSv1_2
  context.load_cert_chain(directory / "server.pem", directory / "server.key", password="")
  if require_token:
    return LanAccess(token_digest), context
  owner_file = directory / OWNER_FILE
  return LanAccess(None, owner_digest=read_owner_digest(owner_file), owner_file=owner_file), context


def read_owner_digest(path: Path) -> bytes | None:
  """A damaged or loosely owned record means no owner; Wi-Fi stays up and the phone pairs again."""
  try:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
      return None
    digest = path.read_text(encoding="ascii").strip()
  except (OSError, UnicodeDecodeError):
    return None
  return bytes.fromhex(digest) if TOKEN_PATTERN.fullmatch(digest) else None


def wifi_address() -> ipaddress.IPv4Interface | None:
  # module globals are read at call time so tests can patch lan.WIFI_INTERFACE / lan.PRIVATE_NETWORKS
  return wifi.wifi_address(WIFI_INTERFACE, PRIVATE_NETWORKS)


def wifi_socket(address: ipaddress.IPv4Interface, port: int) -> socket.socket:
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, WIFI_INTERFACE.encode() + b"\0")
    except PermissionError:
      # Some AGNOS kernels reserve this option for privileged processes.
      # The listener still binds only the discovered interface address.
      pass
    sock.bind((str(address.ip), port))
    return sock
  except (OSError, AttributeError):
    sock.close()
    raise


async def stop_advertisement(task):
  if task is not None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def serve_lan(app, port, context, on_bind_error, *, advertise=None):
  access = app[LAN_ACCESS_KEY]
  sockets = app[LAN_SOCKETS_KEY]
  runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
  stop = asyncio.Event()
  loop = asyncio.get_running_loop()
  for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, stop.set)

  site = None
  advertisement = None
  last_error_address = None
  try:
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    if (tailnet := app.get(TAILNET_KEY)) is not None:
      try:
        await web.TCPSite(runner, "127.0.0.1", tailnet, ssl_context=context).start()
      except OSError:
        on_bind_error()
    while not stop.is_set():
      address = wifi_address()
      if address != access.address:
        access.address = None
        await stop_advertisement(advertisement)
        advertisement = None
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
            if advertise is not None:
              advertisement = asyncio.create_task(advertise())
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
    await stop_advertisement(advertisement)
    await runner.cleanup()
    for sig in (signal.SIGINT, signal.SIGTERM):
      loop.remove_signal_handler(sig)
