"""Announce the authenticated Gateway through the device's Avahi daemon."""

import asyncio
import secrets
import socket

from jeepney import DBusAddress, new_method_call
from jeepney.io.asyncio import open_dbus_router
from jeepney.io.common import RouterClosed
from jeepney.wrappers import DBusErrorResponse, unwrap_msg


SERVICE_TYPE = "_kovalink._tcp"
AVAHI_BUS = "org.freedesktop.Avahi"
AVAHI_IPV4 = 0
AVAHI_GROUP_ESTABLISHED = 2


async def _call(router, address, method, signature=None, body=()):
  reply = await router.send_and_get_reply(new_method_call(address, method, signature, body))
  return unwrap_msg(reply)


async def register(router, interface_index: int, port: int, name: str) -> DBusAddress:
  server = DBusAddress("/", AVAHI_BUS, f"{AVAHI_BUS}.Server")
  path, = await _call(router, server, "EntryGroupNew")
  group = DBusAddress(path, AVAHI_BUS, f"{AVAHI_BUS}.EntryGroup")
  await _call(router, group, "AddService", "iiussssqaay",
              (interface_index, AVAHI_IPV4, 0, name, SERVICE_TYPE, "local", "", port, []))
  await _call(router, group, "Commit")
  return group


async def advertise_gateway(interface: str, port: int, on_error) -> None:
  had_error = False
  while True:
    try:
      # The service name is temporary and carries no device identifier.
      name = f"KovaLink Gateway {secrets.token_hex(4)}"
      async with open_dbus_router(bus="SYSTEM") as router:
        async with asyncio.timeout(3):
          group = await register(router, socket.if_nametoindex(interface), port, name)
        while True:
          await asyncio.sleep(10)
          async with asyncio.timeout(3):
            state, = await _call(router, group, "GetState")
          if state != AVAHI_GROUP_ESTABLISHED:
            raise RuntimeError("KovaLink discovery service not established")
          had_error = False
    except (DBusErrorResponse, OSError, RouterClosed, RuntimeError, TimeoutError):
      if not had_error:
        on_error()
        had_error = True
      await asyncio.sleep(10)
