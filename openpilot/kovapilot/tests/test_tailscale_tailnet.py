"""Tailscale over the VPN IP: the owner's phone reaches the full Gateway on
100.x:8083 (tailscaled forwards it to 127.0.0.1:8083), the plain loopback
API refuses what tailscaled forwards, and the VPN follows boot / the user."""

import asyncio
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import make_mocked_request

from openpilot.kovapilot import vpn_switch
from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway import lan
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.lan import LAN_ACCESS_KEY, LOOPBACK_GUARD_KEY, TAILNET_KEY, LanAccess
from openpilot.kovapilot.gateway.tailscale import Tailscale


TOKEN = "c" * 64
IP = "100.71.162.38"


class MemoryBackend:
  value = None

  def get(self, key):
    return self.value

  def put(self, key, value, block=False):
    self.value = value


def running(*ips):
  return json.dumps({"BackendState": "Running", "Self": {"DNSName": "comma.tail1234.ts.net.", "TailscaleIPs": list(ips)}})


@pytest.fixture
def service(tmp_path):
  access = LanAccess(hashlib.sha256(TOKEN.encode()).digest())
  (tmp_path / "run").mkdir()
  return Tailscale(tmp_path / "ts", access, lambda: True, lambda: {"speedMps": 0}, lambda: {"version": "test"},
                   runtime_dir=tmp_path / "run")


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def test_running_reports_the_vpn_ip_without_dns_or_serve(service, monkeypatch):
  async def check():
    command = AsyncMock(return_value=running("fd7a:115c:a1e0::1", IP))
    monkeypatch.setattr(service, "_command", command)
    await service._refresh()
    assert service.current == {"state": "connected", "host": IP, "port": lan.TAILNET_PORT}
    command.assert_awaited_once_with("status", "--json")
    command.return_value = running("192.168.0.5")
    await service._refresh()
    assert service.current == {"state": "starting"}, "only a tailnet IPv4 is offered"

  asyncio.run(check())


def test_boot_connects_by_itself_but_an_off_vpn_stays_off(service, monkeypatch):
  async def check():
    authorize = AsyncMock()
    monkeypatch.setattr(service, "_authorize", authorize)
    monkeypatch.setattr(service, "_command", AsyncMock(return_value=json.dumps({"BackendState": "Stopped"})))
    await service._refresh()
    await asyncio.sleep(0)
    assert authorize.await_count == 1, "logged in and off after boot: connect again"
    assert service.current == {"state": "starting"}
    service.vpn_off.write_text("1\n")
    service.login = None
    await service._refresh()
    assert service.current == {"state": "stopped"}
    assert authorize.await_count == 1
    # Not logged in at all: nothing happens by itself.
    service.vpn_off.unlink()
    monkeypatch.setattr(service, "_command", AsyncMock(return_value=json.dumps({"BackendState": "NeedsLogin"})))
    await service._refresh()
    assert service.current == {"state": "needsLogin"}
    assert authorize.await_count == 1

  asyncio.run(check())


def owner_post(service, action):
  app = web.Application()
  app[LAN_ACCESS_KEY] = service.access
  return make_mocked_request("POST", f"/v1/vpn/{action}", match_info={"action": action}, sslcontext=object(), app=app,
                             headers={"Authorization": f"Bearer {TOKEN}"})


def test_owner_turns_the_vpn_off_and_on(service, monkeypatch):
  async def check():
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "_authorize", AsyncMock())
    service.daemon = SimpleNamespace(returncode=None)
    command = AsyncMock(return_value="")
    monkeypatch.setattr(service, "_command", command)
    service.current = {"state": "connected", "host": IP, "port": lan.TAILNET_PORT}
    assert json.loads((await service.manage(owner_post(service, "stop"))).text) == {"state": "stopped"}
    command.assert_awaited_once_with("down")
    assert service.vpn_off.exists()
    assert json.loads((await service.manage(owner_post(service, "connect"))).text) == {"state": "authorizing"}
    assert not service.vpn_off.exists()
    await service._cancel_login()

  asyncio.run(check())


def test_user_cutting_the_network_turns_the_vpn_off(service, monkeypatch, tmp_path):
  async def check():
    command = AsyncMock(return_value="")
    monkeypatch.setattr(service, "_command", command)
    service.current = {"state": "needsLogin"}
    vpn_switch.user_cut_network(tmp_path / "run")
    await service._network_cut_by_user()
    assert not service.network_cut.exists(), "the request is consumed"
    command.assert_not_awaited()
    assert not service.vpn_off.exists()

    service.current = {"state": "connected", "host": IP, "port": lan.TAILNET_PORT}
    vpn_switch.user_cut_network(tmp_path / "run")
    await service._network_cut_by_user()
    command.assert_awaited_once_with("down")
    assert service.vpn_off.exists()
    assert service.current == {"state": "stopped"}
    # The network comes back: the VPN stays off (Stopped + off marker).
    command.return_value = json.dumps({"BackendState": "Stopped"})
    await service._refresh()
    assert service.current == {"state": "stopped"}

  asyncio.run(check())


def test_network_screen_signal_never_raises(tmp_path):
  vpn_switch.user_cut_network(tmp_path / "missing" / "dir")
  vpn_switch.user_cut_network(tmp_path)
  assert (tmp_path / vpn_switch.NETWORK_CUT).exists()


def test_wifi_manager_reports_only_leaving_the_current_network(monkeypatch):
  wifi_manager = pytest.importorskip("openpilot.system.ui.lib.wifi_manager")
  calls = []
  monkeypatch.setattr(wifi_manager, "user_cut_network", lambda: calls.append(1))
  monkeypatch.setattr(wifi_manager.WifiManager, "connected_ssid", property(lambda self: "Home"))
  manager = object.__new__(wifi_manager.WifiManager)
  manager._exit = True
  manager._requests = SimpleNamespace(put=lambda worker: None)
  manager._connections = {}
  manager._forgotten = []
  manager._enqueue_callbacks = lambda *args: None
  manager._user_cut_network("Home")
  assert calls == []
  manager._user_cut_network("Office")
  assert calls == [1]
  manager.forget_connection("Office")
  assert calls == [1], "forgetting another saved network keeps this one"
  manager.forget_connection("Home", block=True)
  assert calls == [1], "connect_to_network clearing its target is not the user"
  manager.forget_connection("Home")
  assert calls == [1, 1]
  manager.set_tethering_active(False)
  assert calls == [1, 1]
  manager.set_tethering_active(True)
  assert calls == [1, 1, 1]


def test_loopback_connection_owner_is_found_by_process(tmp_path):
  with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    child = subprocess.Popen([sys.executable, "-c",
                              f"import socket,time; s=socket.create_connection(('127.0.0.1',{port})); time.sleep(20)"])
    try:
      conn, peer = server.accept()
      with conn:
        local = conn.getsockname()
        assert lan.connection_from_process(child.pid, local, peer)
        assert not lan.connection_from_process(os.getpid(), local, peer)
        assert lan.connection_from_process(child.pid, local, ("127.0.0.1", 1)), "unknown counts as refused"
    finally:
      child.kill()
      child.wait()


def test_full_gateway_over_the_vpn_needs_the_owner_and_loopback_refuses_tailscaled(tmp_path, monkeypatch):
  key = tmp_path / "server.key"
  cert = tmp_path / "server.pem"
  subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                  "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                  "-keyout", key, "-out", cert], check=True, capture_output=True)
  key.chmod(0o600)
  server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
  server_ssl.load_cert_chain(cert, key)
  client_ssl = ssl.create_default_context(cafile=cert)
  monkeypatch.setattr(lan, "wifi_address", lambda: None)
  port, tailnet = free_port(), free_port()
  access = LanAccess(None, owner_digest=hashlib.sha256(TOKEN.encode()).digest())
  from_tailscaled = {"value": False}

  async def run():
    app = create_app(KovaParamsStore(MemoryBackend()), lambda: True, lan_access=access)
    app[TAILNET_KEY] = tailnet
    app[LOOPBACK_GUARD_KEY] = lambda local, peer: from_tailscaled["value"]
    task = asyncio.create_task(lan.serve_lan(app, port, server_ssl, lambda: pytest.fail("bind failed")))
    try:
      origin = f"https://127.0.0.1:{tailnet}"
      owner = {"Authorization": f"Bearer {TOKEN}"}
      async with ClientSession(timeout=ClientTimeout(total=3)) as client:
        for _ in range(100):
          with suppress(OSError):
            async with client.get(f"{origin}/v1/health", ssl=client_ssl) as response:
              break
          await asyncio.sleep(0.05)
        async with client.get(f"{origin}/v1/health", ssl=client_ssl) as response:
          assert response.status == 401
          assert response.headers["WWW-Authenticate"] == "Bearer"
        async with client.get(f"{origin}/v1/health", ssl=client_ssl, headers={"Authorization": "Bearer " + "d" * 64}) as response:
          assert response.status == 401
        async with client.get(f"{origin}/v1/health", ssl=client_ssl, headers=owner) as response:
          body = await response.json()
          assert response.status == 200
          assert body["bindScope"] == "tailnet" and body["authentication"] == "bearer"
          assert body["owner"] is True and body["ownerPairing"] is False
        async with client.get(f"{origin}/v1/kova-params", ssl=client_ssl, headers=owner) as response:
          assert response.status == 200
        async with client.get(f"{origin}/v1/health", ssl=client_ssl, headers=owner | {"Origin": "https://x.example"}) as response:
          assert response.status == 403
        async with client.post(f"{origin}/v1/owner/pair", ssl=client_ssl, headers=owner) as response:
          assert response.status != 200, "no owner pairing over the VPN"
        async with client.get(f"http://127.0.0.1:{port}/v1/health") as response:
          assert (await response.json())["bindScope"] == "loopback"
        from_tailscaled["value"] = True
        async with client.get(f"http://127.0.0.1:{port}/v1/health") as response:
          assert response.status == 403
    finally:
      task.cancel()
      with suppress(asyncio.CancelledError):
        await task

  asyncio.run(run())
