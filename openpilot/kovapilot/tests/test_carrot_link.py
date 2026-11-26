import ipaddress
import json

import pytest

from openpilot.kovapilot.navigation.camera_input import CameraInput
from openpilot.kovapilot.navigation.carrot_link import (
  APP_TIMEOUT_NS, BUMP_SPEED_KPH, MAX_PACKET_BYTES, REAR_CAMERA_HOLD_M, CarrotApp, EventTracker, advertisement,
  lan_peer, parse_packet,
)

WIFI = ipaddress.IPv4Network("192.168.43.0/24")
PHONE = ("192.168.43.20", 50123)
SEC = 1_000_000_000


def packet(**values) -> bytes:
  # Field names and types as carrot's modified navigation app sends them (carrot_serv.update).
  document = {"carrotIndex": 7, "nRoadLimitSpeed": 80, "nSdiType": -1, "nSdiSpeedLimit": 0, "nSdiSection": -1, "nSdiDist": -1,
              "nSdiBlockType": -1, "nSdiBlockSpeed": 0, "nSdiBlockDist": 0, "nSdiPlusType": -1, "nSdiPlusSpeedLimit": 0,
              "nSdiPlusDist": 0, "nSdiPlusBlockType": -1, "nSdiPlusBlockSpeed": 0, "nSdiPlusBlockDist": 0, "roadcate": 6,
              "nTBTDist": 0, "nTBTTurnType": -1, "szPosRoadName": "", "vpPosPointLat": 37.5, "vpPosPointLon": 127.0,
              "nPosAngle": 90.0, "nPosSpeed": 60.0} | values
  return json.dumps(document).encode()


def app() -> CarrotApp:
  return CarrotApp(EventTracker("carrot_app", "s1"))


def inputs(events, received_ns):
  return [CameraInput(e["source"], e["session"], e["eventId"], e["sequence"], e["observedMonoTime"], received_ns,
                      e["state"], e["roadMatch"] and e["verifiedSinglePoint"], e["distanceM"], e["targetMps"]) for e in events]


def test_fixed_camera_packet_becomes_present_event():
  link = app()
  assert link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=800), PHONE, 10 * SEC, 0.0, WIFI) == "accepted"
  events = link.step(10 * SEC, 0.0)
  assert len(events) == 1
  event = events[0]
  assert (event["state"], event["distanceM"], round(event["targetMps"] * 3.6, 3)) == ("present", 800.0, 60.0)
  assert event["roadMatch"] and event["verifiedSinglePoint"]
  inputs(events, 10 * SEC)  # valid for plannerd's CameraInput


def test_same_camera_keeps_id_and_passes_by_wheel_distance():
  link = app()
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=300), PHONE, 0, 0.0, WIFI)
  first = link.step(0, 0.0)[0]
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=180), PHONE, SEC, 115.0, WIFI)
  second = link.step(SEC, 115.0)[0]
  assert second["eventId"] == first["eventId"] and second["distanceM"] == 180.0 and second["sequence"] > first["sequence"]
  # The app drops the camera 20 m before it: follow wheel distance to it, then report passed.
  link.receive(packet(), PHONE, 2 * SEC, 275.0, WIFI)
  held = link.step(2 * SEC, 275.0)[0]
  assert (held["eventId"], held["state"], held["distanceM"]) == (first["eventId"], "present", 20.0)
  link.receive(packet(), PHONE, 3 * SEC, 296.0, WIFI)
  passed = link.step(3 * SEC, 296.0)[0]
  assert (passed["eventId"], passed["state"]) == (first["eventId"], "passed") and passed["distanceM"] <= 0
  inputs([passed], 3 * SEC)
  # A late report of the same camera does not start a second slowdown.
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=3), PHONE, int(3.1 * SEC), 297.0, WIFI)
  assert all(e["state"] == "passed" for e in link.step(int(3.1 * SEC), 297.0))
  assert link.step(5 * SEC, 330.0) == []


def test_far_camera_that_disappears_is_cleared():
  link = app()
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=700), PHONE, 0, 0.0, WIFI)
  first = link.step(0, 0.0)[0]
  link.receive(packet(), PHONE, SEC, 20.0, WIFI)
  cleared = link.step(SEC, 20.0)
  assert [(e["eventId"], e["state"]) for e in cleared] == [(first["eventId"], "clear")]
  assert link.step(2 * SEC, 40.0) == []


def test_section_holds_limit_until_section_ends():
  link = app()
  link.receive(packet(nSdiType=4, nSdiSpeedLimit=100, nSdiDist=4000, nSdiBlockType=2, nSdiBlockSpeed=100, nSdiBlockDist=3500),
               PHONE, 0, 0.0, WIFI)
  event = link.step(0, 0.0)[0]
  assert (event["state"], event["distanceM"], round(event["targetMps"] * 3.6)) == ("present", 0.0, 100)
  link.receive(packet(nSdiType=4, nSdiSpeedLimit=100, nSdiDist=3000, nSdiBlockType=3, nSdiBlockSpeed=100, nSdiBlockDist=2600),
               PHONE, 30 * SEC, 900.0, WIFI)
  same = link.step(30 * SEC, 900.0)[0]
  assert same["eventId"] == event["eventId"] and same["state"] == "present"
  link.receive(packet(), PHONE, 31 * SEC, 930.0, WIFI)
  assert link.step(31 * SEC, 930.0)[0]["state"] == "passed"


def test_speed_bump_like_carrot_defaults():
  link = app()
  link.receive(packet(nSdiPlusType=22, nSdiPlusDist=120, roadcate=6), PHONE, 0, 0.0, WIFI)
  event = link.step(0, 0.0)[0]
  assert event["eventId"].startswith("bump") and event["targetMps"] == pytest.approx(BUMP_SPEED_KPH / 3.6)
  # carrot ends the bump slowdown 2 m before it (AutoNaviSpeedBumpEndDistance 200)
  link.receive(packet(nSdiPlusType=22, nSdiPlusDist=1, roadcate=6), PHONE, SEC, 119.0, WIFI)
  assert link.step(SEC, 119.0)[0]["state"] == "passed"
  # roadcate 0/1 (motorway) bumps are ignored, as in carrot
  other = app()
  other.receive(packet(nSdiType=22, nSdiDist=120, roadcate=1), PHONE, 0, 0.0, WIFI)
  assert other.step(0, 0.0) == []


def test_camera_and_secondary_bump_are_both_reported():
  link = app()
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=50, nSdiDist=400, nSdiPlusType=22, nSdiPlusDist=90), PHONE, 0, 0.0, WIFI)
  assert sorted(e["eventId"].split("-")[0] for e in link.step(0, 0.0)) == ["bump", "camera"]


def test_mobile_and_other_sdi_types_are_ignored():
  for sdi_type in (7, 5, 9, 13, 20):
    link = app()
    link.receive(packet(nSdiType=sdi_type, nSdiSpeedLimit=60, nSdiDist=300), PHONE, 0, 0.0, WIFI)
    assert link.step(0, 0.0) == []


def test_rear_camera_holds_after_passing():
  link = app()
  link.receive(packet(nSdiType=75, nSdiSpeedLimit=60, nSdiDist=40), PHONE, 0, 0.0, WIFI)
  link.step(0, 0.0)
  link.receive(packet(), PHONE, SEC, 60.0, WIFI)  # 20 m past the camera
  event = link.step(SEC, 60.0)[0]
  assert (event["state"], event["distanceM"]) == ("present", 0.0)
  link.receive(packet(), PHONE, 2 * SEC, 40.0 + REAR_CAMERA_HOLD_M + 1, WIFI)
  assert link.step(2 * SEC, 40.0 + REAR_CAMERA_HOLD_M + 1)[0]["state"] == "passed"


def test_app_timeout_drops_far_and_finishes_near_items():
  link = app()
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=30, nSdiPlusType=22, nSdiPlusDist=600), PHONE, 0, 0.0, WIFI)
  assert len(link.step(0, 0.0)) == 2
  assert link.connected(APP_TIMEOUT_NS) and not link.connected(APP_TIMEOUT_NS + 1)
  states = {e["eventId"].split("-")[0]: e["state"] for e in link.step(APP_TIMEOUT_NS + 1, 10.0)}
  assert states == {"camera": "present", "bump": "clear"}
  assert link.step(APP_TIMEOUT_NS + 2 * SEC, 31.0)[0]["state"] == "passed"
  assert link.remote is None


@pytest.mark.parametrize("data", [
  b"", b"not json", b"[1, 2]", b"\xff\xfe", b"{" * 5000,
  b"x" * (MAX_PACKET_BYTES + 1),
  packet(nSdiType=1, nSdiSpeedLimit=True, nSdiDist=300),
  packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist="far"),
  packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=[300]),
  b'{"nRoadLimitSpeed": 80, "nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": NaN}',
  b'{"nRoadLimitSpeed": 80, "nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": Infinity}',
])
def test_bad_packets_are_dropped(data):
  link = app()
  assert link.receive(data, PHONE, 0, 0.0, WIFI) == "rejected_packet"
  assert link.step(0, 0.0) == [] and not link.connected(0)


@pytest.mark.parametrize("values", [
  {"nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": 6000},
  {"nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": 0},
  {"nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": -5},
  {"nSdiType": 1, "nSdiSpeedLimit": 5, "nSdiDist": 300},
  {"nSdiType": 1, "nSdiSpeedLimit": 255, "nSdiDist": 300},
  {"nSdiType": 1, "nSdiSpeedLimit": 0, "nSdiDist": 300},
])
def test_out_of_range_items_are_ignored(values):
  link = app()
  assert link.receive(packet(**values), PHONE, 0, 0.0, WIFI) == "accepted"
  assert link.step(0, 0.0) == []


def test_packets_without_sdi_keep_connection_only():
  link = app()
  assert parse_packet(json.dumps({"latitude": 37.5, "longitude": 127.0, "accuracy": 5}).encode()).sdi is False
  link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=300), PHONE, 0, 0.0, WIFI)
  link.receive(json.dumps({"latitude": 37.5}).encode(), PHONE, SEC, 25.0, WIFI)
  assert link.step(SEC, 25.0)[0]["distanceM"] == 275.0


@pytest.mark.parametrize("address, network, accepted", [
  ("192.168.43.20", WIFI, True),
  ("10.1.2.3", ipaddress.IPv4Network("10.0.0.0/8"), True),
  ("172.20.0.9", ipaddress.IPv4Network("172.20.0.0/16"), True),
  ("192.168.44.20", WIFI, False),          # private but another subnet
  ("100.71.162.38", WIFI, False),          # Tailscale
  ("100.64.0.1", ipaddress.IPv4Network("100.64.0.0/10"), False),
  ("8.8.8.8", ipaddress.IPv4Network("8.8.8.0/24"), False),  # public
  ("127.0.0.1", WIFI, False),              # tailscaled forwards tailnet traffic to loopback
  ("192.168.43.255", WIFI, False),
  ("192.168.43.20", None, False),          # no Wi-Fi address
  ("::1", WIFI, False),
  ("not-an-ip", WIFI, False),
])
def test_only_wifi_subnet_private_peers(address, network, accepted):
  assert lan_peer(address, network) is accepted
  link = app()
  result = link.receive(packet(nSdiType=1, nSdiSpeedLimit=60, nSdiDist=300), (address, 1), 0, 0.0, network)
  assert (result == "accepted") is accepted
  assert bool(link.step(0, 0.0)) is accepted


def test_track_count_is_bounded():
  link = app()
  for index in range(20):
    link.receive(packet(nSdiType=1, nSdiSpeedLimit=60 + index, nSdiDist=100 + 200 * index), PHONE, index, 0.0, WIFI)
  events = link.step(SEC, 0.0)
  assert len(events) <= 16
  assert sum(e["state"] == "present" for e in events) <= 8


def test_advertisement_has_carrot_discovery_fields():
  document = json.loads(advertisement(version="0.10", onroad=True, ip="192.168.43.5", v_ego_kph=50, v_cruise_kph=80.0,
                                      cruise_speed_kph=80.0, active=True, sdi_dist=120))
  assert document["ip"] == "192.168.43.5" and document["port"] == 7706 and document["Carrot2"] == "0.10"
  assert {"IsOnroad", "CarrotRouteActive", "v_ego_kph", "sdi_dist", "active"} <= document.keys()
