import asyncio
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, TCPConnector, web

from openpilot.common.basedir import BASEDIR
from openpilot.kovapilot.config.kova_params import CRUISE_SYNC_KEY, KovaParamsStore
from openpilot.kovapilot.gateway import lan
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.device_restart import DeviceRestart
from openpilot.kovapilot.gateway.owner_pairing import (
  ALERT_TEXT, CODE_SECONDS, MAX_ATTEMPTS, OFFROAD_ALERT, OwnerPairing, PairingBlocked, PairingBusy, PairingExpired,
  show_pairing_code,
)
from openpilot.kovapilot.gateway.runtime import prepare_tls_identity
from openpilot.kovapilot.gateway.tailscale import READ_TOKEN_CONTEXT, Tailscale
from openpilot.kovapilot.tests.test_gateway_app import MemoryBackend


class Clock:
  def __init__(self):
    self.now = 100.0

  def __call__(self):
    return self.now


def _load(directory):
  loaded = lan.load_lan_config(directory, require_token=False)
  assert loaded is not None
  return loaded


def _local_access(tmp_path):
  return _load(prepare_tls_identity(tmp_path / "identity"))


def test_code_is_shown_on_the_device_and_expires(tmp_path):
  access, _ = _local_access(tmp_path)
  shown, clock, offroad = [], Clock(), [False]
  pairing = OwnerPairing(access, lambda: offroad[0], shown.append, clock=clock, new_code=lambda: "123456")
  with pytest.raises(PairingBlocked):
    pairing.start()
  offroad[0] = True
  assert pairing.start() == CODE_SECONDS
  assert shown == ["123456"]
  with pytest.raises(PairingBusy):
    pairing.start()
  clock.now += CODE_SECONDS
  pairing.expire()
  assert shown == ["123456", None]
  with pytest.raises(PairingExpired):
    pairing.confirm("123456")
  assert access.owner_digest is None


def test_wrong_codes_use_up_the_code(tmp_path):
  access, _ = _local_access(tmp_path)
  shown, clock = [], Clock()
  pairing = OwnerPairing(access, lambda: True, shown.append, clock=clock, new_code=lambda: "123456")
  pairing.start()
  for _ in range(MAX_ATTEMPTS - 1):
    with pytest.raises(Exception) as error:
      pairing.confirm("000000")
    assert type(error.value).__name__ == "PairingCodeInvalid"
  with pytest.raises(PairingExpired):
    pairing.confirm("000000")
  assert shown[-1] is None
  with pytest.raises(PairingExpired):
    pairing.confirm("123456")


def test_bearer_gateway_does_not_pair(tmp_path):
  access = lan.LanAccess(b"\0" * 32, owner_file=tmp_path / "owner.sha256")
  pairing = OwnerPairing(access, lambda: True, lambda code: None)
  assert not pairing.available
  with pytest.raises(PairingBlocked):
    pairing.start()
  with pytest.raises(ValueError):
    access.set_owner("ab" * 32)


def test_owner_record_survives_restart_and_damage_means_no_owner(tmp_path):
  access, _ = _local_access(tmp_path)
  token = "cd" * 32
  access.set_owner(token)
  record = tmp_path / "identity" / lan.OWNER_FILE
  assert record.stat().st_mode & 0o077 == 0
  assert token not in record.read_text()
  again, _ = _load(tmp_path / "identity")
  assert again.is_owner([f"Bearer {token}"])
  assert not again.is_owner([f"Bearer {'ab' * 32}"])
  assert not again.is_owner([f"Bearer {token}", f"Bearer {token}"])
  record.chmod(0o644)
  assert _load(tmp_path / "identity")[0].owner_digest is None
  record.chmod(0o600)
  record.write_text("not a digest")
  assert _load(tmp_path / "identity")[0].owner_digest is None


def test_offroad_alert_is_declared_for_both_device_screens():
  alerts = json.loads(Path(BASEDIR, "openpilot/selfdrive/selfdrived/alerts_offroad.json").read_text())
  assert alerts[OFFROAD_ALERT]["text"] == ALERT_TEXT
  assert "%1" in ALERT_TEXT
  keys = Path(BASEDIR, "openpilot/common/params_keys.h").read_text()
  assert f'{{"{OFFROAD_ALERT}", {{CLEAR_ON_MANAGER_START, JSON}}}}' in keys

  class Params:
    def __init__(self):
      self.values = {}

    def put(self, key, value, block=False):
      assert block
      self.values[key] = value

    def remove(self, key):
      self.values.pop(key, None)

  params = Params()
  show_pairing_code(params, "042195")
  assert params.values[OFFROAD_ALERT]["extra"] == "042195"
  assert params.values[OFFROAD_ALERT]["text"].replace("%1", "042195").startswith("KovaLink pairing code: 042195.")
  show_pairing_code(params, None)
  assert OFFROAD_ALERT not in params.values


def test_tailscale_accepts_the_paired_owner(tmp_path):
  access, _ = _local_access(tmp_path)
  tailscale = Tailscale(tmp_path / "tailscale", access, lambda: True, lambda: None, dict)

  class Request:
    def __init__(self, auth):
      self.auth = auth

    @property
    def headers(self):
      auth = self.auth
      return type("H", (), {"getall": lambda _, key, default: [auth] if auth else default})()

  token = "ef" * 32
  assert not tailscale._has_owner_token(Request(f"Bearer {token}"))
  access.set_owner(token)
  assert tailscale._has_owner_token(Request(f"Bearer {token}"))
  assert not tailscale._has_owner_token(Request(None))
  # KovaLink derives the VPN read token as HMAC(sha256(owner token), context).
  phone = hmac.new(hashlib.sha256(token.encode()).digest(), READ_TOKEN_CONTEXT, hashlib.sha256).hexdigest()
  assert hmac.digest(access.owner_secret, READ_TOKEN_CONTEXT, "sha256").hex() == phone


def test_paired_phone_gets_owner_actions_over_wifi(tmp_path, monkeypatch):
  access, context = _local_access(tmp_path)
  access.address = ipaddress.IPv4Interface("127.0.0.1/32")
  monkeypatch.setattr(lan, "PRIVATE_NETWORKS", (access.address.network,))
  shown = []
  offroad = [True]
  pairing = OwnerPairing(access, lambda: offroad[0], shown.append, new_code=lambda: "654321")
  reboots = []
  restart = DeviceRestart(lambda: offroad[0], lambda: reboots.append(True))

  async def check():
    store = KovaParamsStore(MemoryBackend())
    app = create_app(store, lambda: offroad[0], lan_access=access, restart=restart, owner_pairing=pairing)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
      await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
      base = f"https://127.0.0.1:{runner.addresses[0][1]}"
      async with ClientSession(base, connector=TCPConnector(ssl=False)) as client:
        async def health(headers=None):
          async with client.get("/v1/health", headers=headers) as response:
            return response.status, await response.json()

        status, body = await health()
        assert status == 200
        assert body["authentication"] == "localNetwork"
        assert (body["owner"], body["ownerRegistered"], body["ownerPairing"]) == (False, False, True)
        async with client.post("/v1/device/restart") as response:
          assert response.status == 403

        offroad[0] = False
        async with client.post("/v1/owner/pair") as response:
          assert response.status == 409
        offroad[0] = True
        async with client.post("/v1/owner/pair", headers={"Origin": "https://example.test"}) as response:
          assert response.status == 403
        async with client.post("/v1/owner/pair") as response:
          assert response.status == 200
          assert await response.json() == {"expiresIn": CODE_SECONDS}
          assert response.headers["Cache-Control"] == "no-store"
        assert shown == ["654321"]
        for bad in ({}, {"code": 654321}, {"code": "65432"}, {"code": "654321", "extra": 1}):
          async with client.post("/v1/owner/confirm", json=bad) as response:
            assert response.status == 400
        async with client.post("/v1/owner/confirm", json={"code": "111111"}) as response:
          assert response.status == 403
          assert (await response.json())["error"]["remainingAttempts"] == MAX_ATTEMPTS - 1
        async with client.post("/v1/owner/confirm", json={"code": "654321"}) as response:
          assert response.status == 200
          token = (await response.json())["token"]
        assert shown[-1] is None
        owner = {"Authorization": f"Bearer {token}"}

        status, body = await health(owner)
        assert (body["owner"], body["ownerRegistered"]) == (True, True)
        assert body["authentication"] == "localNetwork"
        status, body = await health()
        assert (status, body["owner"], body["ownerRegistered"]) == (200, False, True)
        status, _ = await health({"Authorization": f"Bearer {'00' * 32}"})
        assert status == 401

        async with client.get("/v1/kova-params", headers=owner) as response:
          settings = {s["key"]: s for s in (await response.json())["settings"]}
          assert settings[CRUISE_SYNC_KEY]["access"] == "readWrite"
        async with client.post("/v1/device/restart") as response:
          assert response.status == 403
        assert not reboots
        async with client.post("/v1/device/restart", headers=owner) as response:
          assert response.status == 202
        assert reboots == [True]

        # Pairing again on the device replaces the owner; the old phone is refused.
        await asyncio.sleep(3.1)
        pairing.new_code = lambda: "222222"
        async with client.post("/v1/owner/pair") as response:
          assert response.status == 200
        async with client.post("/v1/owner/confirm", json={"code": "222222"}) as response:
          new_token = (await response.json())["token"]
        assert new_token != token
        status, _ = await health(owner)
        assert status == 401
        status, body = await health({"Authorization": f"Bearer {new_token}"})
        assert body["owner"] is True

    finally:
      await runner.cleanup()

  asyncio.run(check())


def test_loopback_has_no_pairing():
  async def check():
    app = create_app(KovaParamsStore(MemoryBackend()), lambda: True)
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
      async with client.post("/v1/owner/pair") as response:
        assert response.status == 404
      async with client.get("/v1/health") as response:
        body = await response.json()
        assert "owner" not in body

  asyncio.run(check())
