import ipaddress
import json

import pytest

import openpilot.cereal.messaging as messaging
from opendbc.car.structs import car
from openpilot.kovapilot.bridges.camera_runtime import CAMERA_EVENT_SERVICE, CameraRuntime
from openpilot.kovapilot.navigation.navcamd import NavCamera, camera_message, vehicle_navi_signal
from openpilot.kovapilot.navigation.vehicle_navi import (
  CAMERA_DISTANCE_TIME_S, HDA_INFO_4A3_ADDR, NAVI_HU_ADDR, SIGNAL_TIMEOUT_NS, VehicleCameraTracker, NaviWarning,
  decode_hda_info_4a3, decode_navi_hu,
)

SEC = 1_000_000_000
WIFI = ipaddress.IPv4Network("192.168.43.0/24")
PHONE = ("192.168.43.20", 40000)


def navi_hu(limit, camera):
  dat = bytearray(8)
  dat[0] = limit
  dat[3] = 0x40 if camera else 0
  return bytes(dat)


def hda_info(limit, map_source, link_class=0):
  dat = bytearray(8)
  dat[0] = link_class & 0x7
  dat[1] = limit
  dat[3] = (map_source & 0x7) << 3
  return bytes(dat)


def can_event(address, dat, bus, mono_ns):
  event = messaging.new_message("can", 1, logMonoTime=mono_ns)
  event.can[0] = {"address": address, "dat": dat, "src": bus}
  return event.as_reader()


def test_decode_stock_navigation_signals():
  assert decode_navi_hu(navi_hu(60, True)) == NaviWarning(60.0, True)
  assert decode_navi_hu(navi_hu(255, True)).limit_kph == 0.0
  assert decode_navi_hu(navi_hu(60, False)) == NaviWarning(0.0, False)
  assert decode_navi_hu(b"\x00" * 7) is None
  assert decode_hda_info_4a3(hda_info(80, 2), True) == NaviWarning(80.0, True, False)
  assert decode_hda_info_4a3(hda_info(80, 1), True).camera is False
  assert decode_hda_info_4a3(hda_info(30, 2, link_class=1), True).controlled_access is True
  assert decode_hda_info_4a3(hda_info(50, 2), False).limit_kph == pytest.approx(80.4672)
  assert decode_hda_info_4a3(b"\x00" * 64, True) is None


def test_vehicle_warning_uses_carrot_virtual_distance_and_holds():
  tracker = VehicleCameraTracker("vehicle_navi", "s")
  tracker.update(NaviWarning(60.0, True), 0, 0.0, suppressed=False)
  event = tracker.step(0, 0.0)[0]
  assert (event["state"], event["distanceM"]) == ("present", 60 * CAMERA_DISTANCE_TIME_S)
  tracker.update(NaviWarning(60.0, True), 20 * SEC, 400.0, suppressed=False)
  held = tracker.step(20 * SEC, 400.0)[0]
  assert (held["eventId"], held["state"], held["distanceM"]) == (event["eventId"], "present", 0.0)
  tracker.update(NaviWarning(0.0, False), 21 * SEC, 420.0, suppressed=False)
  passed = tracker.step(21 * SEC, 420.0)[0]
  assert (passed["eventId"], passed["state"], passed["distanceM"]) == (event["eventId"], "passed", 0.0)
  assert tracker.step(23 * SEC, 460.0) == []


def test_vehicle_warning_cleared_when_app_connected_or_signal_lost():
  tracker = VehicleCameraTracker("vehicle_navi", "s")
  tracker.update(NaviWarning(60.0, True), 0, 0.0, suppressed=False)
  tracker.step(0, 0.0)
  tracker.update(NaviWarning(60.0, True), SEC, 10.0, suppressed=True)
  assert [e["state"] for e in tracker.step(SEC, 10.0)] == ["clear"]
  tracker.update(NaviWarning(60.0, True), 2 * SEC, 20.0, suppressed=False)
  tracker.step(2 * SEC, 20.0)
  tracker.update(None, 3 * SEC, 30.0, suppressed=False)
  assert [e["state"] for e in tracker.step(3 * SEC, 30.0)] == ["clear"]


def test_vehicle_warning_30_on_motorway_ignored():
  tracker = VehicleCameraTracker("vehicle_navi", "s")
  tracker.update(NaviWarning(30.0, True, controlled_access=True), 0, 0.0, suppressed=False)
  assert tracker.step(0, 0.0) == []
  tracker.update(NaviWarning(250.0, True), SEC, 0.0, suppressed=False)
  assert tracker.step(SEC, 0.0) == []


def cp(fingerprint, flags=0, brand="hyundai", configs=1):
  return car.CarParams.new_message(brand=brand, carFingerprint=fingerprint, flags=flags,
                                   safetyConfigs=[car.CarParams.SafetyConfig.new_message() for _ in range(configs)])


def test_vehicle_signal_bus_per_platform():
  from opendbc.car.hyundai.values import HyundaiFlags
  assert vehicle_navi_signal(cp("KIA_SELTOS")) == (0, NAVI_HU_ADDR)
  assert vehicle_navi_signal(cp("HYUNDAI_IONIQ_9", flags=HyundaiFlags.CANFD.value)) == (0, HDA_INFO_4A3_ADDR)
  assert vehicle_navi_signal(cp("X", flags=HyundaiFlags.CANFD.value | HyundaiFlags.CANFD_LKA_STEER_MSG.value)) == (1, HDA_INFO_4A3_ADDR)
  assert vehicle_navi_signal(cp("TOYOTA_RAV4", brand="toyota")) is None


def test_can_frames_reach_vehicle_tracker_with_timeout():
  nav = NavCamera((0, NAVI_HU_ADDR), session="s")
  nav.handle_can(can_event(NAVI_HU_ADDR, navi_hu(50, True), 1, SEC))  # other bus: ignored
  assert nav.step(SEC) == []
  nav.handle_can(can_event(NAVI_HU_ADDR, navi_hu(50, True), 0, SEC))
  assert [e["state"] for e in nav.step(SEC)] == ["present"]
  assert [e["state"] for e in nav.step(SEC + SIGNAL_TIMEOUT_NS + 1)] == ["clear"]


def test_app_has_priority_over_vehicle_warning():
  nav = NavCamera((0, NAVI_HU_ADDR), session="s")
  nav.handle_can(can_event(NAVI_HU_ADDR, navi_hu(50, True), 0, SEC))
  nav.step(SEC)
  data = json.dumps({"nRoadLimitSpeed": 60, "nSdiType": 1, "nSdiSpeedLimit": 50, "nSdiDist": 250, "roadcate": 6}).encode()
  assert nav.receive(data, PHONE, SEC, WIFI) == "accepted"
  events = nav.step(SEC)
  assert {(e["source"], e["state"]) for e in events} == {("carrot_app", "present"), ("vehicle_navi", "clear")}


def test_odometer_integrates_vehicle_speed():
  nav = NavCamera(None, session="s")
  for index in range(11):
    nav.update_speed(index * 100_000_000, 20.0)
  assert nav.odo_m == pytest.approx(20.0)
  nav.update_speed(2 * SEC, 20.0)  # gap: no distance invented
  assert nav.odo_m == pytest.approx(20.0)


class Inputs(dict):
  def all_checks(self, services=None):
    return True


def test_published_message_drives_camera_runtime():
  nav = NavCamera(None, session="s")
  data = json.dumps({"nRoadLimitSpeed": 80, "nSdiType": 1, "nSdiSpeedLimit": 60, "nSdiDist": 150, "roadcate": 6}).encode()
  nav.receive(data, PHONE, 10 * SEC, WIFI)
  message = camera_message(nav.step(10 * SEC))
  message.logMonoTime = 10 * SEC
  runtime = CameraRuntime(3)
  runtime.update(message.as_reader())
  assert not runtime.saturated
  first = runtime.speed_cap(10 * SEC, 25.0, 30.0)
  for step in range(1, 21):
    cap = runtime.speed_cap(10 * SEC + step * 50_000_000, 25.0, 30.0)
  assert first is not None and cap is not None and cap < first <= 25.0
  assert message.which() == CAMERA_EVENT_SERVICE
