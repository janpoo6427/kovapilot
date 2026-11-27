import asyncio
import ipaddress

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest

from openpilot.kovapilot.navigation.carrot_link import lan_peer
from openpilot.kovapilot.navigation.carrot_navi_v2 import (
  CATALOG, ENABLED, ITEM_TIMEOUT_NS, PROTOCOL_VERSION, V2Receiver, build_manifest, parse_guide, road_limit_kph,
  sdi_values, stream_handle,
)
from openpilot.kovapilot.navigation.carrot_navi_v2_server import V2Server, create_app
from openpilot.kovapilot.navigation.carrot_link import select_items

SEC = 1_000_000_000
WIFI = ipaddress.IPv4Network("192.168.43.0/24")


def requirements(**overrides):
  streams = [{"kind": kind, "name": name, "schema_version": 1, "nullable": True} for kind, name in CATALOG]
  return {"type": "requirements_query", "protocol_version": 2, "timestamp_ms": 1, "app_version": "11.2.3.3740",
          "catalog_revision": 1, "streams": streams, **overrides}


def envelope(session, name, sequence, value=None, present=True, **overrides):
  return {"type": "item_update", "protocol_version": 2, "session_id": session, "manifest_revision": 1,
          "schema_version": 1, "kind": "json", "name": name, "stream_handle": stream_handle(name),
          "sequence": sequence, "source_timestamp_ms": 1000 + sequence, "sent_at_ms": 1001 + sequence,
          "present": present, "value": value if present else None,
          **({} if present else {"reason": "source_absent"}), **overrides}


def negotiated(owner="phone"):
  receiver = V2Receiver()
  manifest = receiver.negotiate(owner, requirements(), "11.2.3.3740", "192.168.43.20")
  receiver.control(owner, {"type": "manifest_applied", "protocol_version": 2, "session_id": manifest["session_id"],
                           "revision": manifest["revision"]})
  return receiver, manifest["session_id"]


def test_manifest_lists_the_whole_catalog_and_enables_only_the_json_items_used():
  manifest = build_manifest("abcdef0123456789")
  streams = manifest["streams"]
  assert len(streams) == 28 and len({(s["kind"], s["name"]) for s in streams}) == 28
  assert sorted(s["stream_handle"] for s in streams) == list(range(1, 29))
  enabled = {s["name"] for s in streams if s["enabled"]}
  assert enabled == ENABLED == {"guidance_current", "guidance_next", "speed", "route", "navigation_status",
                                "lane_current"}
  assert all(s["kind"] == "json" for s in streams if s["enabled"])  # no PNG, no map render
  assert {s["params"]["theme"] for s in streams if s["name"].startswith("crossroad_")} == {"auto"}
  assert all(s["params"]["delivery_mode"] == "on_change_with_heartbeat" for s in streams if s["kind"] == "json")
  assert manifest["type"] == "subscription_manifest" and manifest["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.parametrize("change", [
  {"catalog_revision": 2}, {"catalog_revision": True}, {"protocol_version": 1}, {"type": "hello"},
  {"streams": requirements()["streams"][:27]},
  {"streams": requirements()["streams"][:27] + requirements()["streams"][:1]},
  {"streams": [{**s, "schema_version": 2} for s in requirements()["streams"]]},
])
def test_invalid_catalog_offer_is_rejected(change):
  with pytest.raises(ValueError):
    V2Receiver().negotiate("phone", requirements(**change), "v", "192.168.43.20")


def test_second_phone_is_busy_and_a_repeated_query_keeps_the_session():
  receiver = V2Receiver()
  first = receiver.negotiate("phone", requirements(), "v", "192.168.43.20")
  assert receiver.negotiate("phone", requirements(), "v", "192.168.43.20")["session_id"] == first["session_id"]
  with pytest.raises(LookupError):
    receiver.negotiate("other", requirements(), "v", "192.168.43.21")
  receiver.disconnect("phone")
  assert receiver.negotiate("other", requirements(), "v", "192.168.43.21")["session_id"] != first["session_id"]


def test_snapshot_needs_an_applied_manifest_and_fresh_items():
  receiver = V2Receiver()
  manifest = receiver.negotiate("phone", requirements(), "v", "192.168.43.20")
  session = manifest["session_id"]
  receiver.open_item("ws", session, "route")
  receiver.item("ws", session, "route", envelope(session, "route", 1, {"remain_distance_m": 1200, "remain_time_sec": 90}),
                SEC)
  assert not receiver.snapshot(SEC).connected
  receiver.control("phone", {"type": "manifest_applied", "protocol_version": 2, "session_id": session, "revision": 1})
  snapshot = receiver.snapshot(SEC)
  assert (snapshot.connected, snapshot.route_valid, snapshot.remaining_distance_m, snapshot.remaining_time_s) == (
    True, True, 1200, 90)
  assert not receiver.snapshot(SEC + ITEM_TIMEOUT_NS + 1).route_valid
  with pytest.raises(ValueError):
    receiver.control("other", {"type": "manifest_applied", "protocol_version": 2, "session_id": session, "revision": 1})
  receiver.control("other", {"type": "protocol_error", "protocol_version": 2, "message": "x"})  # recorded, no reply


def test_item_frames_out_of_order_or_for_another_identity_are_rejected():
  receiver, session = negotiated()
  receiver.open_item("ws", session, "speed")
  receiver.item("ws", session, "speed", envelope(session, "speed", 5, {"road_limit_kph": 60}), SEC)
  for bad in (envelope(session, "speed", 5, {}), envelope(session, "speed", 4, {}),
              envelope(session, "speed", 6, {}, manifest_revision=2), envelope(session, "speed", 6, {}, stream_handle=1),
              envelope(session, "route", 6, {}), envelope("x" * 16, "speed", 6, {}), envelope(session, "speed", -1, {}),
              envelope(session, "speed", True, {}), envelope(session, "speed", 6, [1, 2]),
              {**envelope(session, "speed", 6, {}), "present": "yes"},
              {**envelope(session, "speed", 6, None, present=False), "value": {}},
              {k: v for k, v in envelope(session, "speed", 6, {}).items() if k != "value"}, [1], None):
    with pytest.raises(ValueError):
      receiver.item("ws", session, "speed", bad, 2 * SEC)
  assert receiver.snapshot(2 * SEC).road_limit_kph == 60
  # a replaced item socket wins; the old one cannot write any more, the new one may restart at 1
  receiver.open_item("ws2", session, "speed")
  with pytest.raises(ValueError):
    receiver.item("ws", session, "speed", envelope(session, "speed", 9, {"road_limit_kph": 80}), 3 * SEC)
  receiver.item("ws2", session, "speed", envelope(session, "speed", 1, {"road_limit_kph": 80}), 3 * SEC)
  assert receiver.snapshot(3 * SEC).road_limit_kph == 80
  receiver.item("ws2", session, "speed", envelope(session, "speed", 2, None, present=False), 4 * SEC)
  assert receiver.snapshot(4 * SEC).road_limit_kph == 0
  with pytest.raises(ValueError):
    receiver.open_item("ws3", session, "vehicle")  # disabled in the manifest


def test_turn_by_turn_guide_points():
  guide = parse_guide({"distance_m": 420.7, "time_sec": 31, "turn_type": 12, "road_name": "서울대로",
                       "main_text": "서울대 입구 교차로에서 좌회전", "near_direction": "신림", "far_direction": "x" * 200})
  assert (guide.valid, guide.turn_type, guide.distance_m, guide.time_s, guide.road_name) == (True, 12, 420, 31, "서울대로")
  assert guide.main_text == "서울대 입구 교차로에서 좌회전" and len(guide.far_direction) == 64
  assert parse_guide({"distance_m": 0, "turn_type": 0}).valid  # 0 is a TMAP value, not missing
  for bad in ({"distance_m": 100, "turn_type": -1}, {"distance_m": 100}, {"turn_type": 12},
              {"distance_m": 100, "turn_type": True}, {"distance_m": -5, "turn_type": 12}, None, [],
              {"distance_m": float("nan"), "turn_type": 12}, {"distance_m": "100", "turn_type": "12x"}):
    assert not parse_guide(bad).valid


def test_road_speed_limit_is_used_only_as_the_app_validates_it():
  assert [road_limit_kph({"road_limit_kph": v}) for v in (30, 100, 200, 35, 0, 210, None, True, -10)] == [
    30, 100, 200, 0, 0, 0, 0, 0, 0]
  assert road_limit_kph(None) == 0


def kinds(speed, road_category=8, off_route=False):
  return [(item.kind, item.speed_kph, item.distance_m) for item in select_items(sdi_values(speed, road_category, off_route))]


def test_speed_value_maps_to_the_same_camera_items_as_udp_7706():
  assert kinds({"road_limit_kph": 60, "sdi": {"type": 1, "distance_m": 420, "speed_limit_kph": 60}}) == [
    ("camera", 60.0, 420.0)]
  assert kinds({"sdi": {"type": 0, "distance_m": 300, "speed_limit_kph": 50}}) == [("camera", 50.0, 300.0)]
  assert kinds({"sdi": {"type": 75, "distance_m": 80, "speed_limit_kph": 50}}) == [("rear_camera", 50.0, 80.0)]
  assert kinds({"section": {"active": True, "speed_limit_kph": 80, "remaining_distance_m": 2500}}) == [
    ("section", 80.0, 2500.0)]
  assert kinds({"section": {"active": True, "suspended": True, "speed_limit_kph": 80, "remaining_distance_m": 2500}}) == []
  # speed bump: TMAP type 22, ignored on motorways (road category 0/1)
  assert kinds({"sdi": {"type": 22, "distance_m": 93}}) == [("bump", 35.0, 93.0)]
  assert kinds({"sdi": {"type": 22, "distance_m": 93}}, road_category=0) == []
  assert kinds({"sdi": {"type": 1, "distance_m": 400, "speed_limit_kph": 60}, "sdi_secondary": {"type": 22, "distance_m": 50}}) == [
    ("camera", 60.0, 400.0), ("bump", 35.0, 50.0)]
  assert kinds({"sdi": {"type": 1, "distance_m": 420, "speed_limit_kph": 60}}, off_route=True) == []
  assert kinds({"sdi": {"type": 1, "distance_m": 420}}) == []  # no limit, nothing to slow down for
  assert kinds({"sdi": {"type": True, "distance_m": 420, "speed_limit_kph": 60}}) == []


def test_road_category_from_lane_guidance_gates_bumps():
  receiver, session = negotiated()
  ws = {name: object() for name in ("lane_current", "speed", "navigation_status")}
  for name, socket in ws.items():
    receiver.open_item(socket, session, name)
  bump = {"sdi": {"type": 22, "distance_m": 93}}
  receiver.item(ws["speed"], session, "speed", envelope(session, "speed", 1, bump), SEC)
  assert [i.kind for i in receiver.snapshot(SEC).speed_items] == ["bump"]
  receiver.item(ws["lane_current"], session, "lane_current", envelope(session, "lane_current", 1, {"road_category": 0}), SEC)
  assert receiver.snapshot(SEC).speed_items == ()
  receiver.item(ws["lane_current"], session, "lane_current", envelope(session, "lane_current", 2, {"road_category": 6}), SEC)
  receiver.item(ws["navigation_status"], session, "navigation_status",
                envelope(session, "navigation_status", 1, {"mode": "off_route", "off_route": True}), SEC)
  assert receiver.snapshot(SEC).speed_items == () and receiver.snapshot(SEC).off_route


async def _ws_flow(client, receiver):
  control = await client.ws_connect("/api/navi/ws/v2/control/11.2.3.3740")
  await control.send_json(requirements())
  manifest = await control.receive_json(timeout=2)
  session = manifest["session_id"]
  enabled = [s["name"] for s in manifest["streams"] if s["enabled"]]
  await control.send_json({"type": "manifest_applied", "protocol_version": 2, "timestamp_ms": 2, "session_id": session,
                           "revision": manifest["revision"], "effective_config": manifest["streams"]})
  await control.send_json({"type": "resource_status", "protocol_version": 2, "timestamp_ms": 3})
  items = {name: await client.ws_connect(f"/api/navi/ws/v2/json/{session}/{name}") for name in enabled}
  await items["guidance_current"].send_json(envelope(session, "guidance_current", 1,
                                                     {"distance_m": 300, "turn_type": 12, "main_text": "좌회전"}))
  await items["speed"].send_json(envelope(session, "speed", 1, {"road_limit_kph": 50,
                                                                "sdi": {"type": 1, "distance_m": 250, "speed_limit_kph": 50}}))
  for _ in range(100):
    snapshot = receiver.snapshot(0)
    if snapshot.connected and snapshot.current.valid and snapshot.speed_items:
      break
    await asyncio.sleep(0.02)
  return control, items, session


def fixed_clock():
  return 0


def test_websocket_flow_negotiation_items_and_bad_frames():
  async def check():
    receiver = V2Receiver()
    app = create_app(receiver, lambda address: address == "127.0.0.1", clock=fixed_clock)
    async with TestClient(TestServer(app)) as client:
      control, items, session = await _ws_flow(client, receiver)
      snapshot = receiver.snapshot(0)
      assert snapshot.connected and snapshot.app_version == "11.2.3.3740"
      assert (snapshot.current.turn_type, snapshot.current.distance_m, snapshot.current.main_text) == (12, 300, "좌회전")
      assert [(i.kind, i.distance_m) for i in snapshot.speed_items] == [("camera", 250.0)] and snapshot.road_limit_kph == 50
      # a cut or garbled frame closes only that item socket; the app reopens it and may restart at 1
      await items["speed"].send_str("{\"type\": \"item_upd")
      error = await items["speed"].receive_json(timeout=2)
      assert error["code"] == "json_stream_error"
      assert (await items["speed"].receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
      assert items["speed"].close_code == 1008
      reopened = await client.ws_connect(f"/api/navi/ws/v2/json/{session}/speed")
      await reopened.send_json(envelope(session, "speed", 1, {"road_limit_kph": 80}))
      for _ in range(100):
        if receiver.snapshot(0).road_limit_kph == 80:
          break
        await asyncio.sleep(0.02)
      assert receiver.snapshot(0).road_limit_kph == 80
      # out of order on the reopened socket
      await reopened.send_json(envelope(session, "speed", 1, {"road_limit_kph": 30}))
      assert (await reopened.receive_json(timeout=2))["code"] == "json_stream_error"
      assert receiver.snapshot(0).road_limit_kph == 80
      # binary on control: an error reply, the session stays
      await control.send_bytes(b"CNV2")
      assert (await control.receive_json(timeout=2))["code"] == "invalid_control_message"
      assert receiver.snapshot(0).connected
      # an image socket is not served
      with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect(f"/api/navi/ws/v2/image/{session}/tbt_next")
      # second phone
      other = await client.ws_connect("/api/navi/ws/v2/control/x")
      await other.send_json(requirements())
      assert (await other.receive_json(timeout=2))["code"] == "receiver_busy"
      assert (await other.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE and other.close_code == 1013
      assert receiver.snapshot(0).connected
      # disconnect, then the app reconnects with a new session
      await control.close()
      for _ in range(100):
        if not receiver.snapshot(0).connected:
          break
        await asyncio.sleep(0.02)
      assert not receiver.snapshot(0).connected
      control, items, new_session = await _ws_flow(client, receiver)
      assert new_session != session and receiver.snapshot(0).connected
  asyncio.run(check())


def test_only_wifi_subnet_peers_reach_the_websocket():
  async def check():
    app = create_app(V2Receiver(), lambda address: lan_peer(address, WIFI))
    async with TestClient(TestServer(app)) as client:  # 127.0.0.1: what tailscaled forwards
      with pytest.raises(aiohttp.WSServerHandshakeError) as error:
        await client.ws_connect("/api/navi/ws/v2/control/11.2.3.3740")
      assert error.value.status == 403
  asyncio.run(check())
  for address, accepted in (("192.168.43.20", True), ("100.101.102.103", False), ("127.0.0.1", False),
                            ("8.8.8.8", False), ("192.168.44.2", False)):
    assert lan_peer(address, WIFI) is accepted


def test_server_thread_on_a_real_socket():
  receiver = V2Receiver()
  server = V2Server(receiver, lambda address: address == "127.0.0.1", host="127.0.0.1", port=0)
  server.start()
  try:
    assert server.ready.wait(10)

    async def check():
      async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"http://127.0.0.1:{server.bound_port}/api/navi/ws/v2/control/v")
        await ws.send_json(requirements())
        manifest = await ws.receive_json(timeout=2)
        assert len(manifest["streams"]) == 28
        await ws.close()
    asyncio.run(check())
  finally:
    server.stop()
  assert not server._thread.is_alive()
