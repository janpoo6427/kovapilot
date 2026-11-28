"""Navigation speed limit item (camera, road speed limit, speed bump) and curve slowdown item (vision curve,
navigation turn): settings, event routing, road limit and turn runtimes, and the planner caps."""
import asyncio
from copy import deepcopy
import ipaddress
import json
from math import sqrt
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

from opendbc.car.structs import car
import openpilot.cereal.messaging as messaging
from openpilot.kovapilot.bridges.camera_runtime import (BUMP_KIND, CAMERA_EVENT_SERVICE, CAMERA_KIND, ROAD_LIMIT_KIND,
                                                      ROAD_LIMIT_SOURCE, CameraRuntime, camera_event_kind, read_profiles)
from openpilot.kovapilot.bridges.nav_turn_runtime import NavTurnRuntime
from openpilot.kovapilot.config.curve_slowdown_settings import CurveSlowdownPreference, read_curve_slowdown
from openpilot.kovapilot.config.kova_params import (CAMERA_PROFILE_KEY, CURVE_SLOWDOWN_KEY, KOVA_PARAMS_KEY, NAV_TURN_SLOWDOWN_KEY,
                                                  ROAD_LIMIT_PROFILE_KEY, SPEED_BUMP_PROFILE_KEY, KovaParamsStore,
                                                  KovaParamsValidationError, load_bundled_catalog, validate_catalog)
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.longitudinal.curve_slowdown import DECEL_MPS2, JERK_MPS3, LATERAL_ACCEL_MPS2, CurveLimits
from openpilot.kovapilot.longitudinal.nav_turn import TURN_RADIUS_M, TURN_SAFE_S, estimate_turns, turn_points, turn_speed
from openpilot.kovapilot.navigation.camera_profile import RECOVERY_ACCEL_MPS2, STOCK_NAVI_DECEL_MPS2
from openpilot.kovapilot.navigation.carrot_navi_v2 import Snapshot
from openpilot.kovapilot.navigation.navcamd import NavCamera, camera_message
from openpilot.kovapilot.navigation.road_limit import RoadLimitTracker
from openpilot.kovapilot.navigation.vehicle_navi import CAMERA_DISTANCE_TIME_S, HDA_INFO_4A3_ADDR, SIGNAL_TIMEOUT_NS, VEHICLE_SOURCE
from openpilot.kovapilot.tests.test_camera_runtime import planner_inputs
from openpilot.selfdrive.controls.lib.longitudinal_planner import A_CRUISE_MIN, A_SPEED_CAP_MIN, LongitudinalPlanner

SEC = 1_000_000_000
STEP_NS = 100_000_000
WIFI = ipaddress.IPv4Network("192.168.43.0/24")
PHONE = ("192.168.43.20", 40000)


class MemoryBackend:
  def __init__(self, value=None):
    self.value = deepcopy(value)

  def get(self, key):
    return deepcopy(self.value) if key == KOVA_PARAMS_KEY else None

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    self.value = deepcopy(value)


def settings():
  return {setting["key"]: setting for setting in load_bundled_catalog()["settings"]}


# --- settings ---------------------------------------------------------------------------------

def test_road_limit_and_bump_use_the_camera_choices():
  all_settings = settings()
  camera = all_settings[CAMERA_PROFILE_KEY]
  for key in (ROAD_LIMIT_PROFILE_KEY, SPEED_BUMP_PROFILE_KEY):
    setting = all_settings[key]
    assert setting["options"] == camera["options"] == [0, 1, 2, 3]
    assert setting["optionTitles"] == camera["optionTitles"]
    assert setting["optionTitles"]["0"]["ko"] == "사용 안 함"
    assert setting["defaultValue"] == 0
    for field in ("groupId", "type", "access", "risk", "applyPolicy", "savePolicy", "availability"):
      assert setting[field] == camera[field], field
    assert [d["condition"] for d in setting["dependencies"]] == [d["condition"] for d in camera["dependencies"]]


def test_nav_turn_uses_the_curve_slowdown_switch():
  all_settings = settings()
  curve, turn = all_settings[CURVE_SLOWDOWN_KEY], all_settings[NAV_TURN_SLOWDOWN_KEY]
  for field in ("groupId", "type", "control", "options", "optionTitles", "defaultValue", "access", "risk", "applyPolicy",
                "savePolicy", "availability", "dependencies"):
    assert turn[field] == curve[field], field
  assert turn["defaultValue"] == 0


def test_bundles_draw_each_family_as_one_item():
  catalog = load_bundled_catalog()
  bundles = {bundle["id"]: bundle for bundle in catalog["bundles"]}
  members = {}
  for setting in sorted(catalog["settings"], key=lambda s: s["order"]):
    if "bundleId" in setting:
      members.setdefault(setting["bundleId"], []).append(setting["key"])
      assert setting["groupId"] == bundles[setting["bundleId"]]["groupId"]
  assert members == {
    "navigation.speed_limits": [CAMERA_PROFILE_KEY, ROAD_LIMIT_PROFILE_KEY, SPEED_BUMP_PROFILE_KEY],
    "cruise.curve_slowdowns": [CURVE_SLOWDOWN_KEY, NAV_TURN_SLOWDOWN_KEY],
  }
  assert bundles["navigation.speed_limits"]["title"]["ko"] == "내비 속도 제한"
  assert bundles["cruise.curve_slowdowns"]["title"]["ko"] == "커브 감속"
  document = KovaParamsStore(MemoryBackend()).document()
  assert document["bundles"] == catalog["bundles"]
  assert {s["key"]: s.get("bundleId") for s in document["settings"]}[ROAD_LIMIT_PROFILE_KEY] == "navigation.speed_limits"


@pytest.mark.parametrize("change", ["unknown_bundle", "other_group", "empty_bundle", "duplicate", "not_list", "no_title"])
def test_invalid_bundles_are_rejected(change):
  catalog = load_bundled_catalog()
  if change == "unknown_bundle":
    catalog["settings"][0]["bundleId"] = "missing"
  elif change == "other_group":
    next(s for s in catalog["settings"] if s["key"] == ROAD_LIMIT_PROFILE_KEY)["groupId"] = "cruise"
  elif change == "empty_bundle":
    catalog["bundles"].append({**catalog["bundles"][0], "id": "unused"})
  elif change == "duplicate":
    catalog["bundles"].append(dict(catalog["bundles"][0]))
  elif change == "not_list":
    catalog["bundles"] = {}
  else:
    del catalog["bundles"][0]["title"]
  with pytest.raises(KovaParamsValidationError):
    validate_catalog(catalog)


@pytest.mark.parametrize("saved, bump", [
  (None, 0), ({}, 0), ({CAMERA_PROFILE_KEY: 0}, 0), ({CAMERA_PROFILE_KEY: 2}, 2), ({CAMERA_PROFILE_KEY: 3}, 3),
  ({CAMERA_PROFILE_KEY: 3, SPEED_BUMP_PROFILE_KEY: 0}, 0), ({CAMERA_PROFILE_KEY: 1, SPEED_BUMP_PROFILE_KEY: 2}, 2),
])
def test_speed_bumps_keep_the_saved_camera_profile_until_changed(saved, bump):
  backend = MemoryBackend(None if saved is None else {"schemaVersion": 1, "revision": 3, "values": saved})
  store = KovaParamsStore(backend)
  store.initialize_storage()
  values = backend.value["values"]
  assert values[SPEED_BUMP_PROFILE_KEY] == bump
  assert values[ROAD_LIMIT_PROFILE_KEY] == 0 and values[NAV_TURN_SLOWDOWN_KEY] == 0
  assert values[CAMERA_PROFILE_KEY] == (saved or {}).get(CAMERA_PROFILE_KEY, 0)
  store.initialize_storage()
  assert backend.value["values"] == values


def test_profiles_are_read_per_kind():
  state = {"schemaVersion": 1, "revision": 0,
           "values": {CAMERA_PROFILE_KEY: 2, ROAD_LIMIT_PROFILE_KEY: 3, SPEED_BUMP_PROFILE_KEY: 1}}
  assert read_profiles({"KovaParams": state}) == {CAMERA_KIND: 2, ROAD_LIMIT_KIND: 3, BUMP_KIND: 1}
  assert read_profiles({"KovaParams": None}) == {CAMERA_KIND: 0, ROAD_LIMIT_KIND: 0, BUMP_KIND: 0}
  bad = {"schemaVersion": 1, "revision": 0, "values": {ROAD_LIMIT_PROFILE_KEY: True}}
  assert read_profiles({"KovaParams": bad})[ROAD_LIMIT_KIND] == 0


def test_nav_turn_switch_is_read_on_its_own_key():
  state = {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: 0, NAV_TURN_SLOWDOWN_KEY: 1}}
  assert read_curve_slowdown({"KovaParams": state}) is False
  assert read_curve_slowdown({"KovaParams": state}, NAV_TURN_SLOWDOWN_KEY) is True
  assert CurveSlowdownPreference({}, NAV_TURN_SLOWDOWN_KEY).key == NAV_TURN_SLOWDOWN_KEY
  assert CurveSlowdownPreference({}).key == CURVE_SLOWDOWN_KEY


# --- events -----------------------------------------------------------------------------------

def batch(*rows, mono=SEC):
  message = messaging.new_message(CAMERA_EVENT_SERVICE, len(rows), valid=True, logMonoTime=mono)
  for index, (source, event_id, distance, target) in enumerate(rows):
    message.kovaCameraEvents[index] = {
      "source": source, "session": "s", "eventId": event_id, "sequence": 1, "observedMonoTime": mono, "state": "present",
      "roadMatch": True, "verifiedSinglePoint": True, "distanceM": distance, "targetMps": target}
  return message.as_reader()


def test_each_kind_follows_its_own_runtime():
  assert camera_event_kind("carrot_app", "camera-1") == CAMERA_KIND
  assert camera_event_kind("carrot_app", "section-2") == CAMERA_KIND
  assert camera_event_kind(VEHICLE_SOURCE, "car-1") == CAMERA_KIND
  assert camera_event_kind("carrot_app", "bump-3") == BUMP_KIND
  assert camera_event_kind(ROAD_LIMIT_SOURCE, "road-1") == ROAD_LIMIT_KIND
  event = batch(("carrot_app", "camera-1", 300.0, 16.7), ("carrot_app", "bump-2", 200.0, 9.72),
                (VEHICLE_SOURCE, "car-1", 360.0, 16.7), (ROAD_LIMIT_SOURCE, "road-1", 300.0, 13.9))
  seen = {}
  for kind in (CAMERA_KIND, BUMP_KIND, ROAD_LIMIT_KIND, None):
    runtime = CameraRuntime(2, kind=kind)
    runtime.update(event)
    seen[kind] = sorted(event_id for _, event_id in runtime.policy.inputs.events)
  assert seen == {CAMERA_KIND: ["camera-1", "car-1"], BUMP_KIND: ["bump-2"], ROAD_LIMIT_KIND: ["road-1"],
                  None: ["bump-2", "camera-1", "car-1", "road-1"]}


def test_road_limit_tracker_lower_higher_unknown():
  tracker = RoadLimitTracker("s")
  tracker.update(80.0, 0, 0.0)
  first = tracker.step(0, 0.0)
  assert [(e["eventId"], e["state"]) for e in first] == [("road-1", "present")]
  assert first[0]["source"] == ROAD_LIMIT_SOURCE
  assert first[0]["distanceM"] == pytest.approx(80 / 3.6 * CAMERA_DISTANCE_TIME_S)
  assert first[0]["targetMps"] == pytest.approx(80 / 3.6)
  tracker.update(80.0, SEC, 500.0)
  assert tracker.step(SEC, 500.0)[0]["distanceM"] == 0.0  # held at the virtual endpoint
  tracker.update(50.0, 2 * SEC, 520.0)  # lower: old one cleared, new approach
  assert [(e["eventId"], e["state"]) for e in tracker.step(2 * SEC, 520.0)] == [("road-1", "clear"), ("road-2", "present")]
  tracker.update(70.0, 3 * SEC, 540.0)  # higher: old one passed (profile recovery)
  assert [(e["eventId"], e["state"]) for e in tracker.step(3 * SEC, 540.0)] == [("road-2", "passed"), ("road-3", "present")]
  tracker.update(None, 4 * SEC, 560.0)  # unknown or stale
  assert [(e["eventId"], e["state"]) for e in tracker.step(4 * SEC, 560.0)] == [("road-2", "passed"), ("road-3", "passed")]
  assert tracker.step(6 * SEC, 600.0) == []
  for invalid in (0, 5, 255, None):
    tracker.update(invalid, 7 * SEC, 600.0)
    assert tracker.step(7 * SEC, 600.0) == []


def hda_info(limit, map_source, link_class=0):
  dat = bytearray(8)
  dat[0] = link_class & 0x7
  dat[1] = limit
  dat[3] = (map_source & 0x7) << 3
  return bytes(dat)


def can_event(dat, mono_ns, address=HDA_INFO_4A3_ADDR):
  event = messaging.new_message("can", 1, logMonoTime=mono_ns)
  event.can[0] = {"address": address, "dat": dat, "src": 0}
  return event.as_reader()


def road_events(events):
  return [(e["state"], round(e["targetMps"] * 3.6)) for e in events if e["source"] == ROAD_LIMIT_SOURCE]


def test_stock_road_limit_from_4a3_any_map_source():
  nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", road_limit=True)
  nav.handle_can(can_event(hda_info(80, 1), SEC))
  assert road_events(nav.step(SEC)) == [("present", 80)]
  nav.handle_can(can_event(hda_info(80, 2), 2 * SEC))  # camera warning frame: same limit
  assert road_events(nav.step(2 * SEC)) == [("present", 80)]
  nav.handle_can(can_event(hda_info(0, 1), 3 * SEC))  # 0: unknown
  assert road_events(nav.step(3 * SEC)) == [("passed", 80)]
  nav.handle_can(can_event(hda_info(60, 1), 5 * SEC))
  assert road_events(nav.step(5 * SEC)) == [("present", 60)]
  assert road_events(nav.step(5 * SEC + SIGNAL_TIMEOUT_NS + 1)) == [("passed", 60)]  # stale


def test_road_limit_events_only_when_the_profile_is_on():
  nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s")
  nav.handle_can(can_event(hda_info(80, 1), SEC))
  assert nav.step(SEC) == []  # a road limit frame alone: no events, as before


class FakeV2:
  def __init__(self, limit):
    self.limit = limit

  def snapshot(self, now_ns):
    return Snapshot(connected=True, last_received_ns=now_ns, road_limit_kph=self.limit)


def test_app_road_limit_wins_and_legacy_app_suppresses_the_stock_one():
  nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", road_limit=True, v2=FakeV2(50))
  nav.handle_can(can_event(hda_info(80, 1), SEC))
  assert road_events(nav.step(SEC)) == [("present", 50)]
  nav.v2.limit = 0  # the app does not know the limit: no limit, the stock one stays suppressed
  assert road_events(nav.step(SEC + STEP_NS)) == [("passed", 50)]
  legacy = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", road_limit=True)
  legacy.handle_can(can_event(hda_info(80, 1), SEC))
  assert legacy.receive(json.dumps({"nRoadLimitSpeed": 80}).encode(), PHONE, SEC, WIFI) == "accepted"
  assert road_events(legacy.step(SEC)) == []


# --- closed loop: road limit and speed bump -----------------------------------------------------

def follow(cap, v, dt, cruise):
  """Ego speed that follows the cap down exactly and climbs at up to 2 m/s^2 toward it or the cruise speed."""
  target = cruise if cap is None else min(cap, cruise)
  return min(v + 2.0 * dt, target) if target > v else target


def speed_runtime(kind, profile):
  estimated = {ROAD_LIMIT_SOURCE: STOCK_NAVI_DECEL_MPS2} if kind == ROAD_LIMIT_KIND else None
  return CameraRuntime(profile, decel_limit_mps2=-A_CRUISE_MIN, jerk_limit_mps3=0.6, response_time_s=1.5,
                       estimated_distance_sources=estimated, kind=kind)


def drive_road_limit(profile, limits, cruise_kph=100.0, v0_kph=80.0):
  """limits: (seconds, 0x4A3 limit kph or None for no frames); rows per 100 ms: (t, v, cap, odo)."""
  nav = NavCamera((0, HDA_INFO_4A3_ADDR), session="s", road_limit=True)
  runtime = speed_runtime(ROAD_LIMIT_KIND, profile)
  v, cruise, rows = v0_kph / 3.6, cruise_kph / 3.6, []
  for step in range(int(limits[-1][0] * 10) + 1):
    now = SEC + step * STEP_NS
    limit = [value for at, value in limits if at <= step / 10][-1]
    if limit is not None:
      nav.handle_can(can_event(hda_info(limit, 1), now))
    nav.update_speed(now, v)
    message = camera_message(nav.step(now))
    message.logMonoTime = now
    runtime.update(message.as_reader())
    cap = runtime.speed_cap(now, v, cruise)
    rows.append((step / 10, v, cap, nav.odo_m))
    v = follow(cap, v, 0.1, cruise)
  return rows


@pytest.mark.parametrize("profile", [1, 2, 3])
def test_road_limit_lowers_to_the_limit_and_holds(profile):
  rows = drive_road_limit(profile, [(0, 80), (30, 80)], v0_kph=100.0)
  caps = [cap for _, _, cap, _ in rows if cap is not None]
  assert caps and min(caps) == pytest.approx(80 / 3.6, abs=1e-6)
  # No step: the cap asks for no more than the stock navigation deceleration.
  assert all(b - a >= -STOCK_NAVI_DECEL_MPS2 * 0.1 - 1e-6 for a, b in zip(caps, caps[1:], strict=False))
  assert rows[-1][1] == pytest.approx(80 / 3.6, abs=1e-6)


def test_road_limit_profiles_differ_only_in_recovery():
  recovery, lowering = {}, {}
  for profile in (1, 2, 3):
    rows = drive_road_limit(profile, [(0, 60), (25, 90), (60, 90)], cruise_kph=100.0, v0_kph=60.0)
    recovery[profile] = next(t for t, v, _, _ in rows if t > 25 and v >= 89 / 3.6) - 25
    lowering[profile] = [cap for _, _, cap, _ in drive_road_limit(profile, [(0, 60), (25, 60)], v0_kph=90.0)]
  assert recovery[1] > recovery[2] > recovery[3]
  for profile in (1, 2, 3):
    assert recovery[profile] >= (29 / 3.6) / RECOVERY_ACCEL_MPS2[profile] - 0.2
  # Lowering follows the stock navigation method whatever the profile.
  assert lowering[1] == lowering[2] == lowering[3]


@pytest.mark.parametrize("profile", [1, 2, 3])
def test_unknown_or_stale_road_limit_releases_with_the_profile(profile):
  rows = drive_road_limit(profile, [(0, 60), (20, None), (60, None)], cruise_kph=90.0, v0_kph=60.0)
  before = [cap for t, _, cap, _ in rows if 15 <= t < 20]
  assert before and all(cap == pytest.approx(60 / 3.6) for cap in before)
  after = [cap for t, _, cap, _ in rows if t >= 21.5]
  assert after[-1] is None  # released once back at the cruise speed
  climbing = [cap for cap in after if cap is not None]
  assert climbing and all(b >= a for a, b in zip(climbing, climbing[1:], strict=False))
  assert rows[-1][1] == pytest.approx(90 / 3.6)


def test_no_road_limit_profile_no_cap():
  assert all(cap is None for _, _, cap, _ in drive_road_limit(0, [(0, 50), (10, 50)], v0_kph=80.0))


def bump_packet(distance, roadcate=6):
  return json.dumps({"nRoadLimitSpeed": 50, "nSdiType": -1, "nSdiPlusType": 22, "nSdiPlusDist": int(distance),
                     "roadcate": roadcate}).encode()


def drive_bump(kind, profile, *, bump_m=250.0, v0_kph=60.0, cruise_kph=60.0, roadcate=6, seconds=60.0):
  nav = NavCamera(None, session="s")
  runtime = speed_runtime(kind, profile)
  v, cruise, rows = v0_kph / 3.6, cruise_kph / 3.6, []
  for step in range(int(seconds * 10)):
    now = SEC + step * STEP_NS
    remaining = bump_m - nav.odo_m
    if step % 10 == 0 and remaining >= 1.0:
      nav.receive(bump_packet(remaining, roadcate), PHONE, now, WIFI)
    nav.update_speed(now, v)
    message = camera_message(nav.step(now))
    message.logMonoTime = now
    runtime.update(message.as_reader())
    cap = runtime.speed_cap(now, v, cruise)
    rows.append((step / 10, v, cap, bump_m - nav.odo_m))
    v = follow(cap, v, 0.1, cruise)
  return rows


@pytest.mark.parametrize("profile", [1, 2, 3])
def test_speed_bump_slows_to_35_before_it_and_resumes_after(profile):
  rows = drive_bump(BUMP_KIND, profile)
  assert next(v for _, v, _, remaining in rows if remaining <= 2.0) <= 35 / 3.6 + 1e-6
  assert next(remaining for _, v, _, remaining in rows if v <= 35 / 3.6 + 1e-6) >= 19.0  # carrot margin max(20 m, 2 s)
  assert rows[-1][1] == pytest.approx(60 / 3.6, abs=0.05)


def test_speed_bump_profiles_change_where_and_how_fast():
  start, back = {}, {}
  for profile in (1, 2, 3):
    rows = drive_bump(BUMP_KIND, profile, bump_m=600.0, seconds=90.0)
    start[profile] = next(remaining for _, _, cap, remaining in rows if cap is not None and cap < 60 / 3.6 - 0.01)
    passed = next(t for t, _, _, remaining in rows if remaining <= 2.0)
    back[profile] = next(t for t, v, _, _ in rows if t > passed and v >= 59 / 3.6) - passed
  assert start[1] > start[2] > start[3]
  assert back[1] > back[2] > back[3]


def test_speed_bump_only_with_its_own_profile_and_not_on_motorways():
  assert all(cap is None for _, _, cap, _ in drive_bump(CAMERA_KIND, 3))  # the camera profile alone: bumps not followed
  assert all(cap is None for _, _, cap, _ in drive_bump(BUMP_KIND, 0))
  assert all(cap is None for _, _, cap, _ in drive_bump(BUMP_KIND, 2, roadcate=1))


# --- navigation turns -----------------------------------------------------------------------------

def guide(turn_type=-1, distance=0.0):
  return SimpleNamespace(valid=turn_type >= 0, turnType=turn_type, distanceM=distance)


def navigation(current=None, following=None, *, source="carrotV2", active=True, off_route=False):
  return SimpleNamespace(source=source, guidanceActive=active, offRoute=off_route, current=current or guide(),
                         next=following or guide())


def test_turn_speeds_from_turn_type():
  assert turn_speed(12) == turn_speed(13) == pytest.approx(sqrt(LATERAL_ACCEL_MPS2 * 15.0))
  assert turn_speed(12) * 3.6 == pytest.approx(19.7, abs=0.1)
  assert turn_speed(16) == turn_speed(19) == pytest.approx(sqrt(LATERAL_ACCEL_MPS2 * 10.0))
  assert turn_speed(14) == pytest.approx(sqrt(LATERAL_ACCEL_MPS2 * 6.0))
  assert turn_speed(131) == turn_speed(142) == turn_speed(12)
  assert turn_speed(14, 4.0) == 4.0
  for not_turn in (-1, 0, 5, 6, 7, 43, 101, 102, 201, 51):
    assert turn_speed(not_turn) is None
  assert set(TURN_RADIUS_M) == {12, 13, 14, 16, 19, *range(131, 143)}


def test_turn_points_current_and_next():
  assert turn_points(navigation(guide(13, 300.0), guide(12, 80.0))) == [(300.0, turn_speed(13)), (380.0, turn_speed(12))]
  assert turn_points(navigation(guide(102, 300.0), guide(12, 80.0))) == [(380.0, turn_speed(12))]
  assert turn_points(navigation(guide(13, 6000.0))) == []
  assert turn_points(navigation(guide(13, 300.0), off_route=True)) == []
  assert turn_points(navigation(guide(13, 300.0), active=False)) == []
  assert turn_points(navigation(guide(13, 300.0), source="none")) is None
  assert turn_points(navigation(guide(13, float("nan")))) is None
  assert turn_points(None) is None


def limits():
  return CurveLimits(decel_mps2=DECEL_MPS2, jerk_mps3=JERK_MPS3, response_s=1.5, min_turn_speed_mps=3.0)


def test_turn_estimate_reaches_turn_speed_before_the_point():
  speed = turn_speed(13)
  near = estimate_turns([(speed * TURN_SAFE_S, speed)], ego_mps=14.0, ego_accel_mps2=0.0, cruise_mps=14.0, limits=limits())
  assert near.complete and near.approach.approach_speed_mps == pytest.approx(speed, abs=0.01)
  far = estimate_turns([(2000.0, speed)], ego_mps=14.0, ego_accel_mps2=0.0, cruise_mps=14.0, limits=limits())
  assert far.approach.approach_speed_mps == pytest.approx(14.0)
  assert estimate_turns([], ego_mps=14.0, ego_accel_mps2=0.0, cruise_mps=14.0, limits=limits()).approach is None
  assert estimate_turns(None, ego_mps=14.0, ego_accel_mps2=0.0, cruise_mps=14.0, limits=limits()).complete is False
  slow = estimate_turns([(50.0, speed)], ego_mps=5.0, ego_accel_mps2=0.0, cruise_mps=5.0, limits=limits())
  assert slow.approach is None  # the cruise speed is already below the turn speed


def drive_turn(turn_type=13, *, start_m=400.0, cruise_kph=60.0, stale_after_s=None, seconds=80.0):
  runtime = NavTurnRuntime(limits())
  v, cruise, odo, rows = cruise_kph / 3.6, cruise_kph / 3.6, 0.0, []
  for step in range(int(seconds * 20)):
    now = SEC + step * 50_000_000
    remaining = start_m - odo
    if (stale_after_s is None or step / 20 < stale_after_s) and step % 4 == 0:
      # After the turn the next guide point is straight on, 900 m away.
      runtime.receive(navigation(guide(turn_type, remaining) if remaining > 0 else guide(5, 900.0)), now)
    cap = runtime.speed_cap(now, v, 0.0, cruise)
    rows.append((step / 20, v, cap, remaining))
    target = cruise if cap is None else min(cap, cruise)
    v = min(v + 0.5 * 0.05, target) if target > v else target
    odo += v * 0.05
  return rows


@pytest.mark.parametrize("turn_type", [12, 13, 16, 19, 14, 131])
def test_turn_slows_before_the_turn_and_resumes_after(turn_type):
  speed = turn_speed(turn_type)
  rows = drive_turn(turn_type)
  assert next(v for _, v, _, remaining in rows if remaining <= 0.0) <= speed + 0.05
  assert next(remaining for _, v, _, remaining in rows if v <= speed + 0.05) >= speed * TURN_SAFE_S - 2.0
  caps = [cap for _, _, cap, _ in rows if cap is not None]
  # The vision curve deceleration (1.2 m/s^2) and jerk shape the cap: no step.
  assert all(b - a >= -DECEL_MPS2 * 0.05 - 0.02 for a, b in zip(caps, caps[1:], strict=False))
  assert rows[-1][1] == pytest.approx(60 / 3.6, abs=0.05)


def test_turn_releases_when_guidance_goes_stale():
  rows = drive_turn(13, start_m=120.0, stale_after_s=4.0, seconds=30.0)
  assert min(v for _, v, _, _ in rows) < 60 / 3.6 - 1.0
  assert rows[-1][2] is None and rows[-1][1] == pytest.approx(60 / 3.6, abs=0.05)


def test_no_turn_no_cap():
  assert all(cap is None for _, _, cap, _ in drive_turn(5))  # straight on


# --- planner ---------------------------------------------------------------------------------------

def planner():
  cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
  return LongitudinalPlanner(cp, init_v=25.0)


CAP_NAMES = ("camera", "curve", "road_limit", "bump", "nav_turn")


def cap_args(caps):
  args = {}
  for name, cap in caps.items():
    args[name + "_speed_cap"] = cap
    args[name + "_eligible"] = True
  return args


def test_lowest_of_all_caps_is_followed():
  for lowest in CAP_NAMES:
    caps = {name: 12.0 if name == lowest else 18.0 + index for index, name in enumerate(CAP_NAMES)}
    p, inputs = planner(), planner_inputs(vCruise=100.0)
    for _ in range(5):
      p.update(inputs, **cap_args(caps))
    assert p.speed_cap_source == lowest


def test_new_caps_restrict_only_when_eligible():
  for name in ("road_limit", "bump", "nav_turn"):
    baseline, enabled, ineligible = planner(), planner(), planner()
    inputs = planner_inputs(vCruise=100.0)
    for _ in range(3):
      baseline.update(inputs)
      enabled.update(inputs, **cap_args({name: 15.0}))
      ineligible.update(inputs, **{name + "_speed_cap": 15.0})
    assert enabled.output_a_target < baseline.output_a_target
    assert enabled.speed_cap_source == name
    assert ineligible.output_a_target == baseline.output_a_target
    assert ineligible.a_speed_cap is None


def lowest_cap_accel(**kw):
  p, inputs, low = planner(), planner_inputs(vCruise=100.0), 0.0
  for _ in range(200):
    p.update(inputs, **kw)
    low = min(low, p.a_speed_cap)
  return low


def test_road_limit_follows_its_planned_deceleration_bump_and_turn_keep_the_cruise_floor():
  road = cap_args({"road_limit": 10.0})
  assert lowest_cap_accel(**road, road_limit_decel_mps2=0.9) == pytest.approx(-0.9)
  assert lowest_cap_accel(**road, road_limit_decel_mps2=5.0) == pytest.approx(A_SPEED_CAP_MIN)
  assert lowest_cap_accel(**road) == pytest.approx(A_CRUISE_MIN)
  assert lowest_cap_accel(**cap_args({"bump": 10.0}), road_limit_decel_mps2=0.9) == pytest.approx(A_CRUISE_MIN)
  assert lowest_cap_accel(**cap_args({"nav_turn": 10.0}), camera_decel_mps2=0.9) == pytest.approx(A_CRUISE_MIN)


def test_new_settings_off_plan_exactly_as_before():
  old, new = planner(), planner()
  sequence = [{}, {"camera_speed_cap": 18.0, "camera_eligible": True},
              {"camera_speed_cap": 15.0, "camera_eligible": True, "camera_decel_mps2": 1.2},
              {"curve_speed_cap": 14.0, "curve_eligible": True, "camera_eligible": True}, {}]
  off = {"road_limit_speed_cap": None, "road_limit_eligible": False, "road_limit_decel_mps2": None,
         "bump_speed_cap": None, "bump_eligible": False, "nav_turn_speed_cap": None, "nav_turn_eligible": False}
  inputs = planner_inputs(vCruise=100.0)
  for kw in sequence * 20:
    old.update(inputs, **kw)
    new.update(inputs, **kw, **off)
    expected = (old.output_a_target, old.output_should_stop, old.speed_cap_source, old.a_speed_cap)
    assert (new.output_a_target, new.output_should_stop, new.speed_cap_source, new.a_speed_cap) == expected


# --- gateway -------------------------------------------------------------------------------------

UPDATE_PATH = "/v1/kova-params/update"


async def gateway_nav_turn_flow():
  store = KovaParamsStore(MemoryBackend())
  applied = {"curve": 0, "turn": None}
  conditions = {"openpilotLongitudinalApplied": "met"}
  app = create_app(store, lambda: True, applied_curve_provider=lambda: applied["curve"],
                   applied_nav_turn_provider=lambda: applied["turn"], dependency_conditions=lambda: conditions)
  client = TestClient(TestServer(app))
  await client.start_server()
  try:
    async def get():
      response = await client.get("/v1/kova-params")
      return {s["key"]: s for s in (await response.json())["settings"]}

    turn = (await get())[NAV_TURN_SLOWDOWN_KEY]
    assert (turn["applyStatus"], turn["applyState"]) == ("available", "awaitingStart")
    response = await client.post(UPDATE_PATH, json={"key": NAV_TURN_SLOWDOWN_KEY, "value": 1, "expectedRevision": 0})
    assert response.status == 200
    applied["turn"] = 0
    turn = (await get())[NAV_TURN_SLOWDOWN_KEY]
    assert (turn["value"], turn["appliedValue"], turn["applyState"]) == (1, 0, "pendingParked")
    applied["turn"] = 1
    assert (await get())[NAV_TURN_SLOWDOWN_KEY]["applyState"] == "applied"
    assert (await get())[CURVE_SLOWDOWN_KEY]["applyState"] == "applied"  # the curve keeps its own provider
    conditions["openpilotLongitudinalApplied"] = "unsupported"
    turn = (await get())[NAV_TURN_SLOWDOWN_KEY]
    assert (turn["applyStatus"], turn["access"]) == ("unsupported", "readOnly")
    response = await client.post(UPDATE_PATH, json={"key": NAV_TURN_SLOWDOWN_KEY, "value": 0, "expectedRevision": 1})
    assert response.status == 403
    assert store.values()[NAV_TURN_SLOWDOWN_KEY] == 1
  finally:
    await client.close()


def test_gateway_reports_nav_turn_like_curve_slowdown():
  asyncio.run(gateway_nav_turn_flow())
