import asyncio
import hashlib
import ipaddress
import socket
import ssl
import subprocess
from contextlib import suppress

import pytest
from aiohttp import ClientConnectorCertificateError, ClientError, ClientSession, ClientTimeout

from openpilot.kovapilot.config.kova_params import KovaParamsStore
from openpilot.kovapilot.gateway import lan
from openpilot.kovapilot.gateway.app import create_app


def test_accepts_only_current_private_wifi_subnet():
  access = lan.LanAccess(b"", ipaddress.IPv4Interface("192.168.43.20/24"))
  assert access.accepts_peer("192.168.43.20", "192.168.43.1")
  assert not access.accepts_peer("192.168.43.19", "192.168.43.1")
  assert not access.accepts_peer("192.168.43.20", "192.168.44.1")
  assert not access.accepts_peer("192.168.43.20", "2606:4700::1")


def test_lan_https_and_websocket_require_credentials(tmp_path, monkeypatch):
  token = "ab" * 32
  digest = tmp_path / "token.sha256"
  key = tmp_path / "server.key"
  cert = tmp_path / "server.pem"
  subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                  "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.2,IP:127.0.0.3",
                  "-keyout", key, "-out", cert], check=True, capture_output=True)
  digest.write_text(hashlib.sha256(token.encode()).hexdigest(), encoding="ascii")
  digest.chmod(0o644)
  with pytest.raises(ValueError, match="private files"):
    lan.load_lan_config(tmp_path)
  digest.chmod(0o600)
  access, server_ssl = lan.load_lan_config(tmp_path)
  client_ssl = ssl.create_default_context(cafile=cert)
  # Exercise the two actual listeners on loopback without a Wi-Fi adapter in CI.
  monkeypatch.setattr(lan, "PRIVATE_NETWORKS", (ipaddress.IPv4Network("127.0.0.0/8"),))
  monkeypatch.setattr(lan, "WIFI_INTERFACE", "lo")
  address = ipaddress.IPv4Interface("127.0.0.2/8")
  monkeypatch.setattr(lan, "wifi_address", lambda: address)
  with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]

  async def run():
    nonlocal address
    app = create_app(KovaParamsStore(None), lambda: True, lan_access=access,
                     telemetry=lambda: {"schemaVersion": 1, "speedMps": 7})
    task = asyncio.create_task(lan.serve_lan(app, port, server_ssl, lambda: pytest.fail("LAN bind failed")))

    async def wait_until_bound():
      async with asyncio.timeout(5):
        while access.address != address:
          if task.done():
            task.result()
          await asyncio.sleep(0.05)

    try:
      await wait_until_bound()
      origin = f"https://127.0.0.2:{port}"
      headers = {"Authorization": f"Bearer {token}"}
      async with ClientSession(timeout=ClientTimeout(total=3)) as client:
        async with client.get(f"http://127.0.0.1:{port}/v1/health") as response:
          assert (await response.json())["bindScope"] == "loopback"
        with pytest.raises(ClientConnectorCertificateError):
          await client.get(f"{origin}/v1/health")
        async with client.get(f"{origin}/v1/health", ssl=client_ssl) as response:
          assert response.status == 401
        async with client.get(f"{origin}/v1/health", ssl=client_ssl, headers={"Authorization": "Bearer " + "00" * 32}) as response:
          assert response.status == 401
        async with client.get(f"{origin}/v1/health", ssl=client_ssl, headers=headers) as response:
          assert (await response.json())["bindScope"] == "wifi"
        async with client.ws_connect(f"{origin}/v1/telemetry", ssl=client_ssl, headers=headers) as ws:
          assert (await ws.receive_json(timeout=2))["speedMps"] == 7
          address = ipaddress.IPv4Interface("127.0.0.3/8")
          await wait_until_bound()
          async with client.get(f"https://127.0.0.3:{port}/v1/health", ssl=client_ssl, headers=headers) as response:
            assert response.status == 200
          async with asyncio.timeout(2):
            async for _ in ws:
              pass
      async with ClientSession(timeout=ClientTimeout(total=2)) as new_client:
        with pytest.raises(ClientError):
          await new_client.get(f"{origin}/v1/health", ssl=client_ssl, headers=headers)
    finally:
      task.cancel()
      with suppress(asyncio.CancelledError):
        await task

  asyncio.run(run())
