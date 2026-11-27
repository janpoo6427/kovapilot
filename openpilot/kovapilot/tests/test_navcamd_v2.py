import ipaddress
import json

import openpilot.cereal.messaging as messaging
from openpilot.kovapilot.navigation.carrot_navi_v2 import stream_handle
from openpilot.kovapilot.navigation.navcamd import NavCamera, navigation_message
from openpilot.kovapilot.navigation.vehicle_navi import NaviWarning
from openpilot.kovapilot.tests.test_carrot_navi_v2 import envelope, requirements

SEC = 1_000_000_000
WIFI = ipaddress.IPv4Network("192.168.43.0/24")
PHONE = ("192.168.43.20", 40000)


class App:
  """A connected v2 app: one socket object per enabled item."""

  def __init__(self, nav: NavCamera):
    self.receiver = nav.v2
    manifest = self.receiver.negotiate("control", requirements(), "11.2.3.3740", PHONE[0])
    self.session = manifest["session_id"]
    self.receiver.control("control", {"type": "manifest_applied", "protocol_version": 2, "session_id": self.session,
                                      "revision": 1})
    self.sockets, self.sequence = {}, {}
    for stream in manifest["streams"]:
      if stream["enabled"]:
        self.sockets[stream["name"]] = object()
        self.receiver.open_item(self.sockets[stream["name"]], self.session, stream["name"])

  def send(self, name, value, now_ns, present=True):
    self.sequence[name] = self.sequence.get(name, 0) + 1
    self.receiver.item(self.sockets[name], self.session, name,
                       envelope(self.session, name, self.sequence[name], value, present), now_ns)


def legacy(**values):
  return json.dumps({"nRoadLimitSpeed": 60, "roadcate": 6, **values}).encode()


def present(events, source="carrot_app"):
  return [(e["state"], round(e["distanceM"]), round(e["targetMps"] * 3.6)) for e in events if e["source"] == source]


def test_v2_cameras_become_the_same_camera_events_as_udp_7706():
  nav = NavCamera(None, session="s")
  app = App(nav)
  app.send("speed", {"road_limit_kph": 60, "sdi": {"type": 1, "distance_m": 400, "speed_limit_kph": 60}}, 0)
  first = nav.step(0)
  assert present(first) == [("present", 400, 60)]
  nav.update_speed(0, 20.0)
  nav.update_speed(SEC // 5, 20.0)
  assert present(nav.step(SEC // 5)) == [("present", 396, 60)]
  app.send("speed", {"road_limit_kph": 60, "sdi": {"type": 1, "distance_m": 380, "speed_limit_kph": 60}}, SEC)
  events = nav.step(SEC)
  assert present(events) == [("present", 380, 60)] and events[0]["eventId"] == first[0]["eventId"]
  app.send("speed", None, 2 * SEC, present=False)  # gone 380 m ahead: cleared, not followed
  assert [e["state"] for e in nav.step(2 * SEC)] == ["clear"]


def test_v2_wins_over_udp_7706_and_7706_takes_over_when_v2_stops():
  nav = NavCamera(None, session="s")
  app = App(nav)
  app.send("speed", {"sdi": {"type": 1, "distance_m": 400, "speed_limit_kph": 60}}, 0)
  nav.step(0)
  assert nav.receive(legacy(nSdiType=1, nSdiSpeedLimit=80, nSdiDist=900), PHONE, SEC // 10, WIFI) == "accepted"
  assert present(nav.step(SEC // 10)) == [("present", 400, 60)]  # the 7706 camera is not followed twice
  assert nav.navigation(SEC // 10)["source"] == "carrotV2"
  # v2 goes quiet: after the app timeout, 7706 is followed again
  later = 5 * SEC
  nav.step(later)
  assert not nav.v2_live
  nav.receive(legacy(nSdiType=1, nSdiSpeedLimit=80, nSdiDist=900), PHONE, later, WIFI)
  assert ("present", 900, 80) in present(nav.step(later + SEC // 10))
  assert nav.navigation(later + SEC // 10)["source"] == "carrotLegacy"


def test_stock_navigation_warning_waits_while_the_v2_app_reports():
  nav = NavCamera((1, 0x544), session="s")
  App(nav).send("route", {"remain_distance_m": 1000}, 0)
  nav.warning = 0, NaviWarning(60.0, True)
  assert present(nav.step(0), "vehicle_navi") == []
  nav.v2.disconnect("control")
  nav.warning = 5 * SEC, NaviWarning(60.0, True)
  assert [e["state"] for e in nav.step(5 * SEC) if e["source"] == "vehicle_navi"] == ["present"]


def test_navigation_state_turn_by_turn_and_route():
  nav = NavCamera(None, session="s")
  assert nav.navigation(0) == {"source": "none", "lastReceivedMonoTime": 0}
  app = App(nav)
  app.send("guidance_current", {"distance_m": 300, "time_sec": 20, "turn_type": 101, "main_text": "오른쪽 방향",
                                "road_name": "강변북로", "near_direction": "구리"}, SEC)
  app.send("guidance_next", {"distance_m": 900, "turn_type": 12, "road_name": "올림픽대로"}, SEC)
  app.send("speed", {"road_limit_kph": 80}, SEC)
  app.send("route", {"remain_distance_m": 12_300, "remain_time_sec": 840}, SEC)
  app.send("navigation_status", {"mode": "guiding", "guidance_active": True, "off_route": False}, SEC)
  nav.update_speed(SEC, 25.0)
  nav.step(SEC)
  nav.update_speed(SEC + SEC // 5, 25.0)
  nav.step(SEC + SEC // 5)
  state = nav.navigation(SEC + SEC // 5)
  assert state["source"] == "carrotV2" and state["appVersion"] == "11.2.3.3740" and state["lastReceivedMonoTime"] == SEC
  assert (state["guidanceActive"], state["offRoute"], state["roadLimitKph"]) == (True, False, 80)
  assert (state["routeValid"], state["remainingDistanceM"], state["remainingTimeS"]) == (True, 12_300, 840)
  assert state["current"]["turnType"] == 101 and state["current"]["distanceM"] == 295.0  # 5 m driven since the report
  assert (state["current"]["mainText"], state["current"]["roadName"], state["current"]["nearDirection"]) == (
    "오른쪽 방향", "강변북로", "구리")
  assert (state["next"]["turnType"], state["next"]["distanceM"], state["next"]["roadName"]) == (12, 900.0, "올림픽대로")
  message = navigation_message(state)
  reader = messaging.log_from_bytes(message.to_bytes())
  assert reader.which() == "kovaNavigation"
  nav_msg = reader.kovaNavigation
  assert (str(nav_msg.source), nav_msg.current.turnType, nav_msg.current.mainText, nav_msg.roadLimitKph) == (
    "carrotV2", 101, "오른쪽 방향", 80)
  # guidance goes away: a tombstone clears it
  app.send("guidance_current", None, 2 * SEC, present=False)
  nav.step(2 * SEC)
  assert nav.navigation(2 * SEC)["current"] == {"valid": False, "turnType": -1}
  assert stream_handle("guidance_current") == 2
