import asyncio
import ipaddress
from contextlib import suppress

from jeepney import HeaderFields, new_method_return

from openpilot.kovapilot.gateway import discovery, lan


def test_avahi_registers_ipv4_on_the_gateway_interface():
  class Router:
    def __init__(self):
      self.calls = []

    async def send_and_get_reply(self, message):
      member = message.header.fields[HeaderFields.member]
      self.calls.append((member, message.body))
      if member == "EntryGroupNew":
        return new_method_return(message, "o", ("/Client1/EntryGroup1",))
      return new_method_return(message)

  router = Router()
  group = asyncio.run(discovery.register(router, 7, 8082, "KovaLink Gateway test"))
  assert group.object_path == "/Client1/EntryGroup1"
  assert router.calls == [
    ("EntryGroupNew", ()),
    ("AddService", (7, 0, 0, "KovaLink Gateway test", "_kovalink._tcp", "local", "", 8082, [])),
    ("Commit", ()),
  ]


def test_gateway_withdraws_advertisement_when_listener_loses_address(monkeypatch):
  class Runner:
    def __init__(self, *args, **kwargs):
      pass

    async def setup(self):
      pass

    async def cleanup(self):
      pass

  class Site:
    def __init__(self, *args, **kwargs):
      pass

    async def start(self):
      pass

    async def stop(self):
      pass

  access = lan.LanAccess(b"")
  address = ipaddress.IPv4Interface("192.168.43.20/24")
  monkeypatch.setattr(lan, "wifi_address", lambda: address)
  monkeypatch.setattr(lan, "wifi_socket", lambda *_: object())
  monkeypatch.setattr(lan.web, "AppRunner", Runner)
  monkeypatch.setattr(lan.web, "SockSite", Site)
  monkeypatch.setattr(lan.web, "TCPSite", Site)

  async def run():
    started = asyncio.Event()
    withdrawn = asyncio.Event()

    async def advertise():
      assert access.address == address
      started.set()
      try:
        await asyncio.Future()
      finally:
        withdrawn.set()

    app = {lan.LAN_ACCESS_KEY: access, lan.LAN_SOCKETS_KEY: set()}
    task = asyncio.create_task(lan.serve_lan(app, 8082, None, lambda: None, advertise=advertise))
    try:
      await asyncio.wait_for(started.wait(), timeout=3)
      monkeypatch.setattr(lan, "wifi_address", lambda: None)
      await asyncio.wait_for(withdrawn.wait(), timeout=3)
      assert access.address is None
    finally:
      task.cancel()
      with suppress(asyncio.CancelledError):
        await task

  asyncio.run(run())
