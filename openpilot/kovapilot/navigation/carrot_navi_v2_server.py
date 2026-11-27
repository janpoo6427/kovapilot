"""TCP 7714 WebSocket v2 server for the carrot modified TMAP app, run in a navcamd thread.

Routes follow carrot_navi_api.md: one control socket and one socket per enabled JSON item.
Image and render routes are not served (their items are disabled in the manifest).
"""
import asyncio
from collections.abc import Callable
import json
import socket
import sys
import threading
import time

from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.navigation.carrot_navi_v2 import MAX_MESSAGE_BYTES, PORT, V2Receiver, protocol_error

CONTROL_PATH = "/api/navi/ws/v2/control/{version}"
JSON_PATH = "/api/navi/ws/v2/json/{session_id}/{name}"
CONTROL_HEARTBEAT_S = 5.0  # carrot_navi
ITEM_HEARTBEAT_S = 10.0
RETRY_S = 10.0


def import_aiohttp():
  """aiohttp comes with the KovaLink Gateway packages on comma (gateway/__main__.py)."""
  from openpilot.common.hardware import PC
  from openpilot.kovapilot.gateway import DEVICE_PACKAGES
  if not PC and DEVICE_PACKAGES.is_dir() and str(DEVICE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(DEVICE_PACKAGES))
  from aiohttp import WSMsgType, web
  return web, WSMsgType


def create_app(receiver: V2Receiver, accept_peer: Callable[[str], bool], clock: Callable[[], int] = time.monotonic_ns):
  web, WSMsgType = import_aiohttp()

  @web.middleware
  async def wifi_only(request, handler):
    # Checked before the WebSocket upgrade: only the Wi-Fi subnet (no loopback, VPN or public peer).
    if not accept_peer(request.remote or ""):
      return web.Response(status=403, text="navigation app must be on the comma Wi-Fi")
    return await handler(request)

  def decode(message) -> dict:
    if message.type != WSMsgType.TEXT:
      raise ValueError("v2 accepts JSON text only")
    try:
      document = json.loads(message.data)
    except (ValueError, RecursionError) as exc:
      raise ValueError("invalid JSON") from exc
    if not isinstance(document, dict):
      raise ValueError("JSON object required")
    return document

  async def send(ws, payload) -> bool:
    if ws.closed:
      return False
    try:
      await ws.send_json(payload)
      return True
    except (ConnectionError, RuntimeError):
      return False

  async def control(request):
    ws = web.WebSocketResponse(heartbeat=CONTROL_HEARTBEAT_S, max_msg_size=MAX_MESSAGE_BYTES, compress=False)
    await ws.prepare(request)
    version = request.match_info["version"]
    try:
      async for message in ws:
        if message.type == WSMsgType.ERROR:
          break
        try:
          payload = decode(message)
          if payload.get("type") == "requirements_query":
            if not await send(ws, receiver.negotiate(ws, payload, version, request.remote or "")):
              break
          else:
            receiver.control(ws, payload)
        except LookupError as exc:
          # A second phone must not take over the session of the connected one (carrot_navi).
          await send(ws, protocol_error("receiver_busy", str(exc)))
          await ws.close(code=1013, message=b"navigation receiver busy")
          break
        except (TypeError, ValueError) as exc:
          if not await send(ws, protocol_error("invalid_control_message", str(exc))):
            break
    finally:
      receiver.disconnect(ws)
    return ws

  async def item(request):
    session_id, name = request.match_info["session_id"], request.match_info["name"]
    ws = web.WebSocketResponse(heartbeat=ITEM_HEARTBEAT_S, max_msg_size=MAX_MESSAGE_BYTES, compress=False)
    await ws.prepare(request)
    try:
      receiver.open_item(ws, session_id, name)
      async for message in ws:
        if message.type == WSMsgType.ERROR:
          break
        receiver.item(ws, session_id, name, decode(message), clock())
    except (TypeError, ValueError) as exc:
      await send(ws, protocol_error("json_stream_error", str(exc), name))
      await ws.close(code=1008, message=b"invalid JSON stream")
    finally:
      receiver.close_item(ws, name)
    return ws

  app = web.Application(client_max_size=MAX_MESSAGE_BYTES, middlewares=[wifi_only])
  app.router.add_get(CONTROL_PATH, control)
  app.router.add_get(JSON_PATH, item)
  return app


def listen_socket(host: str, port: int, interface: str | None) -> socket.socket:
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if interface is not None:
      try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
      except (PermissionError, OSError):
        pass  # the peer filter still accepts only the Wi-Fi subnet
    sock.bind((host, port))
    sock.listen(8)
    sock.setblocking(False)
    return sock
  except OSError:
    sock.close()
    raise


class V2Server:
  """Serves until stop(); restarts after errors and waits for aiohttp to be installed."""

  def __init__(self, receiver: V2Receiver, accept_peer: Callable[[str], bool], *, host: str = "0.0.0.0",
               port: int = PORT, interface: str | None = None):
    self.receiver, self.accept_peer = receiver, accept_peer
    self.host, self.port, self.interface = host, port, interface
    self.ready = threading.Event()
    self.bound_port: int | None = None
    self._loop: asyncio.AbstractEventLoop | None = None
    self._stop: asyncio.Event | None = None
    self._stopped = threading.Event()
    self._thread = threading.Thread(target=self._run, name="navcamd-v2", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def stop(self, timeout: float = 5.0) -> None:
    self._stopped.set()
    loop, stop = self._loop, self._stop
    if loop is not None and stop is not None:
      loop.call_soon_threadsafe(stop.set)
    self._thread.join(timeout)

  def _run(self) -> None:
    logged = False
    while not self._stopped.is_set():
      try:
        web, _ = import_aiohttp()
      except ImportError:
        if not logged:
          cloudlog.warning("navcamd: aiohttp not installed yet, navigation app v2 waits")
          logged = True
        self._stopped.wait(RETRY_S)
        continue
      try:
        asyncio.run(self._serve(web))
      except Exception:
        cloudlog.exception("navcamd: navigation app v2 server failed")
      self.ready.clear()
      self._stopped.wait(RETRY_S)

  async def _serve(self, web) -> None:
    self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
    runner = web.AppRunner(create_app(self.receiver, self.accept_peer), access_log=None, handle_signals=False,
                            shutdown_timeout=1.0)
    await runner.setup()
    try:
      sock = listen_socket(self.host, self.port, self.interface)
      self.bound_port = sock.getsockname()[1]
      await web.SockSite(runner, sock).start()
      self.ready.set()
      if not self._stopped.is_set():
        await self._stop.wait()
    finally:
      await runner.cleanup()
