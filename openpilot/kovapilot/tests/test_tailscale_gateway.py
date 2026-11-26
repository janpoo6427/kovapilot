import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from openpilot.kovapilot.gateway.lan import LAN_ACCESS_KEY, LanAccess
from openpilot.kovapilot.gateway.tailscale import READ_TOKEN_CONTEXT, Tailscale


TOKEN = "a" * 64
HOST = "comma.test-tailnet.ts.net"


@pytest.fixture
def service(tmp_path):
  access = LanAccess(hashlib.sha256(TOKEN.encode()).digest())
  return Tailscale(tmp_path, access, lambda: True, lambda: {"speedMps": 0}, lambda: {"version": "test"})


def test_routes_and_browser_login_follow_confirmed_daemon_state(service, monkeypatch):
  async def check():
    login_task = asyncio.create_task(asyncio.sleep(60))
    service.login = login_task
    try:
      command = AsyncMock(return_value=json.dumps({"BackendState": "NeedsLogin", "AuthURL": "https://example.org/a/foo"}))
      monkeypatch.setattr(service, "_command", command)
      await service._refresh()
      assert service.current == {"state": "authorizing"}
      url = "https://login.tailscale.com/a/testLink"
      command.return_value = json.dumps({"BackendState": "NeedsLogin", "AuthURL": url})
      await service._refresh()
      assert service.current == {"state": "authorizing", "loginUrl": url}
      command.return_value = json.dumps({"BackendState": "NeedsMachineAuth"})
      await service._refresh()
      assert service.current == {"state": "approvalRequired"}
      # 2026-11-27 janpoo: no MagicDNS/HTTPS serve; the phone uses the VPN IP.
      command.return_value = json.dumps({"BackendState": "Running",
                                         "Self": {"DNSName": HOST + ".", "TailscaleIPs": ["100.101.102.103"]}})
      await service._refresh()
      assert service.current == {"state": "connected", "host": "100.101.102.103", "port": 8083}
      command.assert_awaited_with("status", "--json")
    finally:
      await service._cancel_login()

  asyncio.run(check())


def test_vpn_snapshot_needs_token_and_serves_only_the_registered_read_route(service):
  async def check():
    app = web.Application()
    app.router.add_get("/v1/vpn/device", service._snapshot, allow_head=False)
    service.current = {"state": "connected", "host": HOST}
    async with TestClient(TestServer(app)) as client:
      route = "/v1/vpn/device"
      assert (await client.get(route)).status == 403
      assert (await client.get(route, headers={"Authorization": f"Bearer {TOKEN}"})).status == 403
      readonly = hmac.digest(service.access.token_digest, READ_TOKEN_CONTEXT, "sha256").hex()
      assert readonly == "a5027aa222ec5f1acebedab5b86bb4df75fe5114c97bc743361beee5d644de3c"
      headers = {"Authorization": f"Bearer {readonly}"}
      response = await client.get(route, headers=headers)
      assert response.status == 200
      assert await response.json() == {"version": "test", "telemetry": {"speedMps": 0}}
      assert (await client.get(route, headers=headers | {"Origin": "https://elsewhere.example"})).status == 403
      assert (await client.post(route, headers=headers)).status == 405
      assert (await client.get("/v1/git/status", headers=headers)).status == 404
      service.current = {"state": "needsLogin"}
      assert (await client.get(route, headers=headers)).status == 403

  asyncio.run(check())


def test_vpn_management_requires_owner_lan_and_offroad(service, monkeypatch):
  async def check():
    app = web.Application()
    app[LAN_ACCESS_KEY] = service.access
    with pytest.raises(web.HTTPForbidden):
      await service.manage(make_mocked_request("GET", "/v1/vpn", app=app))

    with pytest.raises(web.HTTPForbidden):
      await service.manage(make_mocked_request("GET", "/v1/vpn", sslcontext=object(), app=app))
    request = make_mocked_request("POST", "/v1/vpn/connect", match_info={"action": "connect"}, sslcontext=object(), app=app,
                                  headers={"Authorization": f"Bearer {TOKEN}"})
    app[LAN_ACCESS_KEY] = LanAccess(None)
    with pytest.raises(web.HTTPForbidden):
      await service.manage(request)
    app[LAN_ACCESS_KEY] = service.access
    monkeypatch.setattr(service, "offroad", lambda: False)
    with pytest.raises(web.HTTPConflict):
      await service.manage(request)
    monkeypatch.setattr(service, "offroad", lambda: True)
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "_authorize", AsyncMock())
    service.daemon = SimpleNamespace(returncode=None)
    service.current = {"state": "needsLogin"}
    try:
      assert json.loads((await service.manage(request)).text) == {"state": "authorizing"}
      command = AsyncMock(return_value="")
      monkeypatch.setattr(service, "_command", command)
      disconnect = make_mocked_request("POST", "/v1/vpn/disconnect", match_info={"action": "disconnect"},
                                       sslcontext=object(), app=app, headers={"Authorization": f"Bearer {TOKEN}"})
      assert json.loads((await service.manage(disconnect)).text) == {"state": "needsLogin"}
      command.assert_awaited_once_with("logout")
    finally:
      await service._cancel_login()

  asyncio.run(check())
