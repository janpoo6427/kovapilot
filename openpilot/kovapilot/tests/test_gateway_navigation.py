from types import SimpleNamespace

import openpilot.cereal.messaging as messaging
from openpilot.kovapilot.gateway.navigation_status import (
  AutoLaneChangeSetting, auto_lane_change_fields, maneuver, navigation_fields,
)
from openpilot.kovapilot.gateway.telemetry import road_scene
from openpilot.kovapilot.navigation.navcamd import NavCamera, navigation_message
from openpilot.kovapilot.tests.test_navcamd_v2 import App

SEC = 1_000_000_000


def navigation_reader(state):
  return messaging.log_from_bytes(navigation_message(state).to_bytes()).kovaNavigation


def guided_state():
  nav = NavCamera(None, session="s")
  app = App(nav)
  app.send("guidance_current", {"distance_m": 300, "turn_type": 12, "main_text": "좌회전", "road_name": "테헤란로"}, SEC)
  app.send("guidance_next", {"distance_m": 900, "turn_type": 101, "road_name": "강변북로"}, SEC)
  app.send("speed", {"road_limit_kph": 50}, SEC)
  app.send("route", {"remain_distance_m": 12_300, "remain_time_sec": 840}, SEC)
  app.send("navigation_status", {"guidance_active": True, "off_route": False}, SEC)
  nav.step(SEC)
  return nav.navigation(SEC)


def test_turn_by_turn_reaches_the_kovalink_tbt_fields():
  fields = navigation_fields(navigation_reader(guided_state()), SEC + 250_000_000)
  assert fields["navigation"] == {"source": "carrotV2", "appVersion": "11.2.3.3740", "lastReceivedAgeMs": 250,
                                  "guidanceActive": True, "offRoute": False}
  assert fields["speedLimitKph"] == 50.0
  assert fields["tbt"] == {"maneuver": "turnLeft", "turnType": 12, "distanceMeters": 300.0, "roadName": "테헤란로",
                           "mainText": "좌회전", "nearDirection": None, "nextManeuver": "slightRight", "nextTurnType": 101,
                           "nextDistanceMeters": 1200.0, "nextRoadName": "강변북로", "remainingDistanceMeters": 12300.0,
                           "remainingSeconds": 840}


def test_without_the_v2_app_only_the_connection_is_reported():
  nav = NavCamera(None, session="s")
  assert navigation_fields(navigation_reader(nav.navigation(SEC)), SEC) == {
    "navigation": {"source": "none", "appVersion": None, "lastReceivedAgeMs": None}}
  legacy = navigation_reader({"source": "carrotLegacy", "lastReceivedMonoTime": SEC})
  assert navigation_fields(legacy, 2 * SEC) == {
    "navigation": {"source": "carrotLegacy", "appVersion": None, "lastReceivedAgeMs": 1000}}
  assert navigation_fields(None, SEC) == {}  # display only: never raises


def test_tmap_turn_codes():
  assert [maneuver(t) for t in (12, 16, 13, 19, 7, 102, 6, 101, 14, 201, 51, 131, 153, -1, 999)] == [
    "turnLeft", "turnLeft", "turnRight", "turnRight", "slightLeft", "slightLeft", "slightRight", "slightRight", "uTurn",
    "arrive", "straight", "unknown", "unknown", "unknown", "unknown"]


def test_auto_lane_change_state_in_this_build_and_with_the_automatic_lane_change_fields():
  model = messaging.new_message("modelV2")
  model.modelV2.meta.laneChangeState = "preLaneChange"
  model.modelV2.meta.laneChangeDirection = "left"
  car = messaging.new_message("carState")
  car.carState.leftBlindspot = True
  state = auto_lane_change_fields(model.modelV2, car.carState, 1)["autoLaneChange"]
  assert state["setting"] == "on" and state["laneChange"] == "preLaneChange" and state["direction"] == "left"
  assert state["blindSpotLeft"] is True and state["blindSpotRight"] is False
  assert state["auto"] == "none" and state["blindSpotFresh"] is False
  model.modelV2.meta.kovaAutoLaneChange = "pending"
  car.carState.blindspotFresh = True
  state = auto_lane_change_fields(model.modelV2, car.carState, 1)["autoLaneChange"]
  assert (state["auto"], state["blindSpotFresh"]) == ("pending", True)
  # fields a build does not have are "unknown" / None, never invented
  state = auto_lane_change_fields(SimpleNamespace(meta=SimpleNamespace(laneChangeState="off", laneChangeDirection="none")),
                                  SimpleNamespace(leftBlindspot=False, rightBlindspot=False), 1)["autoLaneChange"]
  assert (state["auto"], state["blindSpotFresh"], state["laneChange"]) == ("unknown", None, "off")
  meta = SimpleNamespace(laneChangeState="laneChangeStarting", laneChangeDirection="right", kovaAutoLaneChange="returning")
  car_state = SimpleNamespace(leftBlindspot=False, rightBlindspot=True, blindspotFresh=True)
  state = auto_lane_change_fields(SimpleNamespace(meta=meta), car_state, 0)["autoLaneChange"]
  assert state == {"setting": "off", "laneChange": "laneChangeStarting", "direction": "right", "auto": "returning",
                   "blindSpotFresh": True, "blindSpotLeft": False, "blindSpotRight": True}
  assert auto_lane_change_fields(None, None, None)["autoLaneChange"] == {
    "setting": "unknown", "laneChange": "unknown", "direction": "none", "auto": "unknown"}
  assert auto_lane_change_fields(SimpleNamespace(), None, 1)["autoLaneChange"]["laneChange"] == "unknown"


def test_setting_is_read_once_a_second_and_unknown_on_errors():
  now, reads = [0], []

  def read():
    reads.append(now[0])
    if now[0] >= 3 * SEC:
      raise RuntimeError("params")
    return 1

  setting = AutoLaneChangeSetting(read, clock=lambda: now[0])
  assert setting() == 1 and setting() == 1 and len(reads) == 1
  now[0] = SEC
  assert setting() == 1 and len(reads) == 2
  now[0] = 3 * SEC
  assert setting() is None


def test_scene_carries_navigation_only_while_navcamd_publishes():
  class Sample(dict):
    valid = {"carState": True, "selfdriveState": True, "modelV2": True, "kovaNavigation": True}
    alive = valid
    logMonoTime = {"carState": SEC, "selfdriveState": 1, "modelV2": 1, "kovaNavigation": SEC}

  car = SimpleNamespace(vEgo=10., steeringAngleDeg=4., vCruise=72., gearShifter="drive", leftBlinker=False,
                        rightBlinker=False, brakePressed=False, leftBlindspot=False, rightBlindspot=False)
  sm = Sample(carState=car, kovaNavigation=navigation_reader(guided_state()))
  scene = road_scene(sm, SEC + 1, 0)
  assert scene["tbt"]["maneuver"] == "turnLeft" and scene["speedLimitKph"] == 50.0
  assert scene["autoLaneChange"]["setting"] == "off" and scene["autoLaneChange"]["laneChange"] == "unknown"
  sm.logMonoTime = {**sm.logMonoTime, "kovaNavigation": 0}  # 1 s old
  later = road_scene(sm, SEC + 1, None)
  assert "tbt" not in later and "navigation" not in later and later["autoLaneChange"]["setting"] == "unknown"
