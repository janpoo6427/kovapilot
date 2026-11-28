"""KOVA: cluster NAV icon input (hudControl.navigationActive): navigation info from the car navigation or a navigation app.

KOVA: 계기판 NAV 아이콘 입력 — 차량 순정 내비(0x4A3/0x544 도로 정보) 또는 외부 내비 앱(navcamd)에서 정보를 받는 중인지
(janpoo 2026-11-28 10:52·10:53)."""
import openpilot.cereal.messaging as messaging
from openpilot.kovapilot.hud.navigation import NAVIGATION_FRESH_NS, navigation_active
from openpilot.kovapilot.navigation.navcamd import NavCamera, navigation_message
from openpilot.kovapilot.navigation.vehicle_navi import HDA_INFO_4A3_ADDR, NAVI_HU_ADDR, SIGNAL_TIMEOUT_NS
from openpilot.kovapilot.tests.test_navcamd import can_event, hda_info, navi_hu

SEC = 1_000_000_000


def reader(state):
  return messaging.log_from_bytes(navigation_message(state).to_bytes()).kovaNavigation


def test_app_source_is_navigation_info():
  for source in ("carrotV2", "carrotLegacy"):
    assert navigation_active(reader({"source": source}), SEC, SEC + NAVIGATION_FRESH_NS)
  assert not navigation_active(reader({"source": "none"}), SEC, SEC)


def test_vehicle_navigation_is_navigation_info():
  assert navigation_active(reader({"source": "none", "vehicleNaviValid": True}), SEC, SEC)
  assert navigation_active(reader({"source": "carrotV2", "vehicleNaviValid": True}), SEC, SEC)


def test_stale_or_missing_message_is_no_navigation_info():
  message = reader({"source": "carrotV2", "vehicleNaviValid": True})
  assert not navigation_active(message, SEC, SEC + NAVIGATION_FRESH_NS + 1)  # navcamd stopped
  assert not navigation_active(message, 0, SEC)  # never received
  assert not navigation_active(message, 2 * SEC, SEC)  # newer than now: not trusted
  assert not navigation_active(None, SEC, SEC)


def test_navcamd_reports_vehicle_navigation_without_camera_profiles():
  # no camera or road limit profile: the stock signal is read for the NAV icon only, no camera events
  nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", vehicle_cameras=False)
  assert nav.navigation(0) == {"source": "none", "lastReceivedMonoTime": 0}
  nav.handle_can(can_event(HDA_INFO_4A3_ADDR, hda_info(60, 2, 5), 0, SEC))  # camera warning, 60 km/h
  assert nav.step(SEC) == []
  state = nav.navigation(SEC)
  assert state["vehicleNaviValid"] is True and state["source"] == "none"
  assert reader(state).vehicleNaviValid
  assert nav.step(SEC + SIGNAL_TIMEOUT_NS + 1) == []
  assert "vehicleNaviValid" not in nav.navigation(SEC + SIGNAL_TIMEOUT_NS + 1)
  assert not reader(nav.navigation(SEC + SIGNAL_TIMEOUT_NS + 1)).vehicleNaviValid


def test_vehicle_navigation_road_info():
  # IONIQ 9 rlogs 2026-11-27: 0x4A3 every 200 ms; LinkClass 0 with no speed limit (no road info) vs a speed limit
  for dat, valid in ((hda_info(0, 1, 0), False), (hda_info(255, 1, 7), False), (hda_info(80, 1, 1), True),
                     (hda_info(0, 2, 0), True), (hda_info(70, 2, 5), True)):
    nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", vehicle_cameras=False)
    nav.handle_can(can_event(HDA_INFO_4A3_ADDR, dat, 0, SEC))
    nav.step(SEC)
    assert nav.vehicle_valid is valid, dat.hex()
  # CAN Navi_HU: the camera warning only
  for dat, valid in ((navi_hu(50, True), True), (navi_hu(50, False), False)):
    nav = NavCamera((0, NAVI_HU_ADDR), session="s", vehicle_cameras=False)
    nav.handle_can(can_event(NAVI_HU_ADDR, dat, 0, SEC))
    nav.step(SEC)
    assert nav.vehicle_valid is valid, dat.hex()


def test_vehicle_cameras_still_follow_profiles():
  # with a camera profile the stock warning still becomes camera events (as before) and is navigation info
  nav = NavCamera((0, NAVI_HU_ADDR), session="s")
  nav.handle_can(can_event(NAVI_HU_ADDR, navi_hu(50, True), 0, SEC))
  assert [e["state"] for e in nav.step(SEC)] == ["present"]
  assert nav.navigation(SEC)["vehicleNaviValid"] is True
