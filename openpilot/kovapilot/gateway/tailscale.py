"""Manage an opt-in, userspace Tailscale node and its read-only Gateway."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from openpilot.kovapilot.gateway.lan import LAN_ACCESS_KEY, TAILNET_PORT, LanAccess, connection_from_process
from openpilot.kovapilot.vpn_switch import NETWORK_CUT, RUNTIME_DIR, VPN_OFF


PORT = 8084
# A failed automatic install is retried after this, doubling up to the cap.
INSTALL_RETRY = 30.0
INSTALL_RETRY_MAX = 900.0
READ_TOKEN_CONTEXT = b"kovalink-tailscale-read-v1"
SERVE_TARGET = f"http://127.0.0.1:{PORT}"
LOGIN_URL = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]{1,128}")
TAILNET_IPS = ipaddress.IPv4Network("100.64.0.0/10")
TAILNET_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net")


class Tailscale:
  # [offroad] says when connecting and disconnecting are allowed: the
  # Gateway passes "offroad, or in Park and disengaged".
  def __init__(self, directory: Path, access: LanAccess, offroad: Callable[[], bool], telemetry: Callable,
               device_info: Callable[[], dict], installer: Callable[[], None] | None = None,
               runtime_dir: Path = RUNTIME_DIR):
    self.directory = directory
    # Boot connects by itself; a VPN turned off by the owner or by the user
    # cutting the comma's network stays off until KovaLink turns it on.
    self.network_cut = runtime_dir / NETWORK_CUT
    self.vpn_off = runtime_dir / VPN_OFF
    self.auto_up = False
    # Installs the binaries into [directory] when they are missing (blocking;
    # runs in a thread). None keeps the manual installation only.
    self.installer = installer
    self.install_retry = INSTALL_RETRY
    self.next_install = 0.0
    self.access = access
    self.offroad = offroad
    self.telemetry = telemetry
    self.device_info = device_info
    self.socket = directory / "tailscaled.sock"
    self.daemon: asyncio.subprocess.Process | None = None
    self.login: asyncio.Task | None = None
    self.lock = asyncio.Lock()
    self.current: dict = {"state": "notInstalled"}

  def installed(self) -> bool:
    try:
      root = self.directory.lstat()
      if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid() or root.st_mode & 0o077:
        return False
      for name in ("tailscaled", "tailscale"):
        binary = (self.directory / name).lstat()
        if not stat.S_ISREG(binary.st_mode) or binary.st_uid != root.st_uid or binary.st_mode & 0o022 or not binary.st_mode & 0o100:
          return False
    except OSError:
      return False
    return True

  async def _command(self, *args: str, deadline: float = 5) -> str | None:
    try:
      proc = await asyncio.create_subprocess_exec(
        str(self.directory / "tailscale"), f"--socket={self.socket}", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
      )
    except OSError:
      return None
    try:
      async with asyncio.timeout(deadline):
        stdout, _ = await proc.communicate()
      return stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None
    except TimeoutError:
      return None
    finally:
      if proc.returncode is None:
        with suppress(ProcessLookupError):
          proc.kill()
        await proc.wait()

  @staticmethod
  def _tailnet_ip(status: dict) -> str | None:
    for value in (status.get("Self") or {}).get("TailscaleIPs") or []:
      try:
        address = ipaddress.ip_address(value)
      except (TypeError, ValueError):
        continue
      if address.version == 4 and address in TAILNET_IPS:
        return str(address)
    return None

  async def _refresh(self) -> None:
    raw = await self._command("status", "--json")
    try:
      status = json.loads(raw) if raw is not None else {}
    except ValueError:
      status = {}
    state = status.get("BackendState")
    if state == "Running":
      # The phone connects to the comma's VPN IP; no MagicDNS or HTTPS.
      host = self._tailnet_ip(status)
      self.current = {"state": "connected", "host": host, "port": TAILNET_PORT} if host else {"state": "starting"}
    elif state == "NeedsMachineAuth":
      self.current = {"state": "approvalRequired"}
    elif state in ("NeedsLogin", "Stopped"):
      url = status.get("AuthURL") or ""
      if self.login is not None and not self.login.done():
        self.current = {"state": "starting" if self.auto_up and not LOGIN_URL.fullmatch(url) else "authorizing"}
        if LOGIN_URL.fullmatch(url):
          self.current["loginUrl"] = url
      elif state == "Stopped" and not self.vpn_off.exists():
        # Logged in and off, not on purpose (boot): connect by itself.
        self.auto_up = True
        self.login = asyncio.create_task(self._authorize())
        self.current = {"state": "starting"}
      else:
        # Stopped: logged in, VPN off on purpose (`tailscale down`).
        self.current = {"state": "stopped" if state == "Stopped" else "needsLogin"}
    else:
      self.current = {"state": "starting" if self.login is None or self.login.done() else "authorizing"}

  async def _install(self) -> None:
    """One automatic install attempt when it is due. The lock is not held:
    the download can take a while and status requests must still answer."""
    loop = asyncio.get_running_loop()
    if self.installer is None or loop.time() < self.next_install:
      return
    self.current = {"state": "installing"}
    try:
      await asyncio.to_thread(self.installer)
    except Exception:
      self.current = {"state": "installFailed"}
      self.next_install = loop.time() + self.install_retry
      self.install_retry = min(self.install_retry * 2, INSTALL_RETRY_MAX)
    else:
      self.install_retry = INSTALL_RETRY
      self.next_install = 0.0

  async def _run(self) -> None:
    while True:
      if not self.installed():
        await self._install()
      async with self.lock:
        if not self.installed():
          if self.installer is None or self.current["state"] not in ("installing", "installFailed"):
            self.current = {"state": "notInstalled"}
        else:
          try:
            if self.daemon is None or self.daemon.returncode is not None:
              self.current = {"state": "starting"}
              self.daemon = await asyncio.create_subprocess_exec(
                str(self.directory / "tailscaled"), "--tun=userspace-networking",
                f"--state={self.directory / 'tailscaled.state'}", f"--socket={self.socket}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "TS_NO_LOGS_NO_SUPPORT": "true"},
              )
            await self._network_cut_by_user()
            await self._refresh()
          except OSError:
            self.current = {"state": "daemonUnavailable"}
      await asyncio.sleep(3)

  async def _authorize(self) -> None:
    await self._command("up", "--accept-dns=false", "--accept-routes=false", "--timeout=180s", deadline=185)

  def _set_off(self, off: bool) -> None:
    with suppress(OSError):
      if off:
        self.vpn_off.write_text("1\n", encoding="ascii")
      else:
        self.vpn_off.unlink(missing_ok=True)

  async def _stop(self) -> None:
    """VPN off on purpose; stays off until KovaLink connects again."""
    await self._cancel_login()
    self._set_off(True)
    if await self._command("down") is None:
      raise web.HTTPServiceUnavailable(text="Could not turn the VPN off.")
    self.current = {"state": "stopped"}

  async def _network_cut_by_user(self) -> None:
    """The comma screen reported that the user cut the network."""
    if not self.network_cut.exists():
      return
    with suppress(OSError):
      self.network_cut.unlink()
    if self.current["state"] in ("connected", "starting", "authorizing", "approvalRequired"):
      with suppress(web.HTTPServiceUnavailable):
        await self._stop()

  def _has_owner_token(self, request: web.Request) -> bool:
    return self.access.is_owner(request.headers.getall("Authorization", []))

  async def manage(self, request: web.Request) -> web.Response:
    if not request.secure or request.app.get(LAN_ACCESS_KEY) is not self.access or not self._has_owner_token(request):
      raise web.HTTPForbidden()
    if request.method == "POST":
      async with self.lock:
        if not self.offroad():
          raise web.HTTPConflict(text="VPN registration requires Park or offroad mode.")
        if self.daemon is None or self.daemon.returncode is not None or not self.installed():
          raise web.HTTPServiceUnavailable(text="Tailscale is unavailable.")
        if request.match_info["action"] == "connect":
          self._set_off(False)
          if self.current["state"] in ("needsLogin", "starting", "stopped") and (self.login is None or self.login.done()):
            self.auto_up = False
            self.login = asyncio.create_task(self._authorize())
            self.current = {"state": "authorizing"}
        elif request.match_info["action"] == "stop":
          await self._stop()
        else:
          self._set_off(False)
          await self._cancel_login()
          if await self._command("logout") is None:
            raise web.HTTPServiceUnavailable(text="Could not disconnect Tailscale.")
          self.current = {"state": "needsLogin"}
    return web.json_response(self.current, headers={"Cache-Control": "no-store"})

  async def _snapshot(self, request: web.Request) -> web.Response:
    auth = request.headers.getall("Authorization", [])
    if (request.remote != "127.0.0.1" or self.current["state"] != "connected" or "Origin" in request.headers
        or (owner := self.access.owner_secret) is None or len(auth) != 1):
      raise web.HTTPForbidden()
    read_token = hmac.digest(owner, READ_TOKEN_CONTEXT, "sha256").hex()
    if not auth[0].isascii() or not hmac.compare_digest(auth[0], f"Bearer {read_token}"):
      raise web.HTTPForbidden()
    info = await asyncio.to_thread(self.device_info)
    return web.json_response({**info, "telemetry": self.telemetry()}, headers={"Cache-Control": "no-store"})

  async def _cancel_login(self) -> None:
    if self.login is not None:
      self.login.cancel()
      await asyncio.gather(self.login, return_exceptions=True)
      self.login = None

  async def lifecycle(self, app: web.Application):
    read_only = web.Application()
    read_only.router.add_get("/v1/vpn/device", self._snapshot, allow_head=False)
    runner = web.AppRunner(read_only, access_log=None, shutdown_timeout=2)
    await runner.setup()
    try:
      await web.TCPSite(runner, "127.0.0.1", PORT).start()
    except OSError:
      self.current = {"state": "gatewayUnavailable"}
      await runner.cleanup()
      yield
      return
    task = asyncio.create_task(self._run())
    try:
      yield
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)
      await self._cancel_login()
      if self.daemon is not None and self.daemon.returncode is None:
        with suppress(ProcessLookupError):
          self.daemon.terminate()
        try:
          await asyncio.wait_for(self.daemon.wait(), 3)
        except TimeoutError:
          with suppress(ProcessLookupError):
            self.daemon.kill()
          await self.daemon.wait()
      await runner.cleanup()

  def from_daemon(self, local: tuple, peer: tuple) -> bool:
    """True when the running tailscaled made this loopback connection (a
    tailnet peer forwarded to 127.0.0.1)."""
    daemon = self.daemon
    if daemon is None or daemon.returncode is not None:
      return False
    return connection_from_process(daemon.pid, local, peer)

  def attach(self, app: web.Application) -> None:
    app.cleanup_ctx.append(self.lifecycle)
    app.router.add_get("/v1/vpn", self.manage, allow_head=False)
    app.router.add_post(r"/v1/vpn/{action:connect|stop|disconnect}", self.manage)
