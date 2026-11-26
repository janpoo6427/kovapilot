"""Manage an opt-in, userspace Tailscale node and its read-only Gateway."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from openpilot.kovapilot.gateway.lan import LAN_ACCESS_KEY, LanAccess


PORT = 8084
READ_TOKEN_CONTEXT = b"kovalink-tailscale-read-v1"
SERVE_TARGET = f"http://127.0.0.1:{PORT}"
LOGIN_URL = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]{1,128}")
TAILNET_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net")


class Tailscale:
  def __init__(self, directory: Path, access: LanAccess, offroad: Callable[[], bool], telemetry: Callable,
               device_info: Callable[[], dict]):
    self.directory = directory
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

  async def _refresh(self) -> None:
    raw = await self._command("status", "--json")
    try:
      status = json.loads(raw) if raw is not None else {}
    except ValueError:
      status = {}
    state = status.get("BackendState")
    if state == "Running":
      host = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
      if TAILNET_HOST.fullmatch(host) and self.current.get("host") == host and self.current["state"] == "connected":
        return
      self.current = {"state": "gatewayUnavailable"}
      if TAILNET_HOST.fullmatch(host) and await self._command("serve", "--bg", "--https=443", SERVE_TARGET) is not None:
        self.current = {"state": "connected", "host": host}
    elif state == "NeedsMachineAuth":
      self.current = {"state": "approvalRequired"}
    elif state in ("NeedsLogin", "Stopped"):
      url = status.get("AuthURL") or ""
      if self.login is not None and not self.login.done():
        self.current = {"state": "authorizing"}
        if LOGIN_URL.fullmatch(url):
          self.current["loginUrl"] = url
      else:
        self.current = {"state": "needsLogin"}
    else:
      self.current = {"state": "starting" if self.login is None or self.login.done() else "authorizing"}

  async def _run(self) -> None:
    while True:
      async with self.lock:
        if not self.installed():
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
            await self._refresh()
          except OSError:
            self.current = {"state": "daemonUnavailable"}
      await asyncio.sleep(3)

  async def _authorize(self) -> None:
    await self._command("up", "--accept-dns=false", "--accept-routes=false", "--timeout=180s", deadline=185)

  def _has_owner_token(self, request: web.Request) -> bool:
    return self.access.is_owner(request.headers.getall("Authorization", []))

  async def manage(self, request: web.Request) -> web.Response:
    if not request.secure or request.app.get(LAN_ACCESS_KEY) is not self.access or not self._has_owner_token(request):
      raise web.HTTPForbidden()
    if request.method == "POST":
      async with self.lock:
        if not self.offroad():
          raise web.HTTPConflict(text="VPN registration requires offroad mode.")
        if self.daemon is None or self.daemon.returncode is not None or not self.installed():
          raise web.HTTPServiceUnavailable(text="Tailscale is unavailable.")
        if request.match_info["action"] == "connect":
          if self.current["state"] in ("needsLogin", "starting") and (self.login is None or self.login.done()):
            self.login = asyncio.create_task(self._authorize())
            self.current = {"state": "authorizing"}
        else:
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

  def attach(self, app: web.Application) -> None:
    app.cleanup_ctx.append(self.lifecycle)
    app.router.add_get("/v1/vpn", self.manage, allow_head=False)
    app.router.add_post(r"/v1/vpn/{action:connect|disconnect}", self.manage)
