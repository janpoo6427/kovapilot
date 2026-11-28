"""Set speed by the cluster speed (janpoo 11/28 10:39). IONIQ 9 rlog 0000079f segment 20 (2026-11-28): set 86, the wheel
speed held 86 and the cluster showed 89-90 (CRUISE_BUTTONS_ALT CLUSTER_SPEED 3.35 km/h over the wheel speed at 80-89 km/h).
With the cluster speed the car holds the wheel speed at which the cluster shows the set speed."""
import json
from types import SimpleNamespace

import pytest

from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.kovapilot.gateway.telemetry import road_scene
from openpilot.kovapilot.longitudinal.cluster_speed import ClusterSpeedRatio, cluster_speed
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import MIN_PRESS_SAMPLES
from openpilot.selfdrive.car import card
from openpilot.selfdrive.car.cruise import V_CRUISE_INITIAL, V_CRUISE_UNSET, VCruiseHelper
from openpilot.selfdrive.controls.lib.longitudinal_planner import get_cruise_accel

ButtonType = car.CarState.ButtonEvent.Type
PLANNER_DT = 0.05


def ioniq9_cluster(wheel_kph):
  """Cluster number of the IONIQ 9 rlogs: about 3.9 % over the wheel speed at 80-90 km/h, integer."""
  return float(int(wheel_kph * 1.039))


def make_car(monkeypatch, *, pcm=False):
  instance = object.__new__(card.Car)
  instance.CP = car.CarParams(openpilotLongitudinalControl=True, pcmCruise=pcm)
  instance.v_cruise_helper = VCruiseHelper(instance.CP)
  instance.cruise_accelerator_sync = CruiseAcceleratorSyncBridge(instance.v_cruise_helper)
  instance.cruise_sync_settings = (True, False, False)
  instance.is_metric = True
  instance.can_rcv_cum_timeout_counter = 0
  instance.initialized_prev = True
  instance.can_sock = None
  instance.CC_prev = car.CarControl(enabled=False)
  instance.CS_prev = car.CarState()
  instance.initial_cruise_speed = V_CRUISE_INITIAL

  class SubMaster(dict):
    def update(self, timeout):
      pass

    def all_checks(self, services):
      return True

  instance.sm = SubMaster(carControl=car.CarControl(enabled=False), onroadEvents=[])
  instance.RI = SimpleNamespace(update=lambda _: None)
  monkeypatch.setattr(card, "can_capnp_to_list", lambda _: [])
  monkeypatch.setattr(card, "REPLAY", False)
  monkeypatch.setattr(card.messaging, "drain_sock_raw", lambda *args, **kwargs: [b"CAN"])

  def step(wheel_kph, cluster_kph=None, *, enabled=True, pressed=False, buttons=()):
    cluster_kph = ioniq9_cluster(wheel_kph) if cluster_kph is None else cluster_kph
    CC = instance.sm["carControl"]
    CC.enabled = enabled
    CC.longActive = enabled and not pressed
    state = car.CarState(canValid=True, gasPressed=pressed, vEgo=wheel_kph * CV.KPH_TO_MS, vEgoCluster=cluster_kph * CV.KPH_TO_MS,
                         cruiseState={"available": True}, buttonEvents=[{"type": b, "pressed": p} for b, p in buttons])
    instance.CI = SimpleNamespace(update=lambda _: state)
    CS = instance.state_update()[0]
    instance.CC_prev = car.CarControl(enabled=enabled)
    instance.CS_prev = CS
    return CS

  return instance, step


def settled_accel(v_cruise_kph, wheel_kph, steps=200):
  a = 0.0
  for _ in range(steps):
    a = get_cruise_accel(v_cruise_kph * CV.KPH_TO_MS, wheel_kph * CV.KPH_TO_MS, a, a, PLANNER_DT)
  return a


def test_set_86_holds_cluster_86(monkeypatch):
  # set 86: at the wheel speed where the cluster shows 86 the planner neither accelerates nor slows down
  instance, step = make_car(monkeypatch)
  step(82.9, enabled=False)
  step(82.9)
  instance.v_cruise_helper.v_cruise_kph = instance.v_cruise_helper.v_cruise_cluster_kph = 86.0
  for _ in range(1500):
    CS = step(82.9)
  assert ioniq9_cluster(82.9) == 86.0
  assert CS.vCruiseCluster == 86.0
  assert CS.vCruise == pytest.approx(82.9, abs=0.15)
  assert abs(settled_accel(CS.vCruise, 82.9)) < 0.05
  # at the old hold point (wheel 86, cluster 89) the planner now slows down
  for _ in range(1500):
    CS = step(86.0)
  assert CS.vCruiseCluster == 86.0
  assert settled_accel(CS.vCruise, 86.0) < -0.3


def test_without_cluster_speed_unchanged(monkeypatch):
  # a car without a cluster speed (vEgoCluster = vEgo, or 0): the planner set speed is the set speed, exactly
  for cluster in (None, 0.0):
    instance, step = make_car(monkeypatch)
    step(80.0, 80.0 if cluster is None else cluster, enabled=False)
    step(80.0, 80.0 if cluster is None else cluster)
    instance.v_cruise_helper.v_cruise_kph = instance.v_cruise_helper.v_cruise_cluster_kph = 86.0
    for _ in range(300):
      CS = step(80.0, 80.0 if cluster is None else cluster)
    assert CS.vCruise == CS.vCruiseCluster == 86.0


def test_unset_and_pcm_pass_through(monkeypatch):
  _, step = make_car(monkeypatch)
  CS = step(82.9, enabled=False)
  assert CS.vCruise == CS.vCruiseCluster == V_CRUISE_UNSET
  helper = VCruiseHelper(car.CarParams(pcmCruise=True))
  helper.v_cruise_kph = 80.0
  helper.update_v_cruise(car.CarState(vEgo=20.0, vEgoCluster=21.0), True, True)
  assert helper.planner_v_cruise_kph == helper.v_cruise_kph


def test_set_button_engages_at_the_cluster_speed(monkeypatch):
  # SET at wheel 96.3 / cluster 100: set 100, like the cluster shows (was the wheel speed 96)
  _, step = make_car(monkeypatch)
  for _ in range(1500):
    step(96.3, 100.0, enabled=False)
  step(96.3, 100.0, enabled=False, buttons=((ButtonType.setCruise, True),))
  CS = step(96.3, 100.0, buttons=((ButtonType.setCruise, False),))
  assert CS.vCruiseCluster == 100.0
  assert CS.vCruise == pytest.approx(96.3, abs=0.1)


def test_first_start_on_the_pedal_at_the_cluster_speed(monkeypatch):
  # engaged on an accelerator press without buttons: the set speed is the cluster speed of that moment
  _, step = make_car(monkeypatch)
  for _ in range(1500):
    step(57.5, 60.0, enabled=False)
  step(57.5, 60.0, enabled=False, pressed=True)
  CS = step(57.6, 60.0, pressed=True)
  assert CS.vCruiseCluster == 60.0


def test_gas_set_clips_to_the_cluster_speed():
  helper = VCruiseHelper(car.CarParams(pcmCruise=False, openpilotLongitudinalControl=True))
  helper.v_cruise_kph = 70.0
  helper.button_change_states[ButtonType.decelCruise] = {"standstill": False, "enabled": True}
  CS = car.CarState(vEgo=86.0 * CV.KPH_TO_MS, vEgoCluster=89.0 * CV.KPH_TO_MS, gasPressed=True, cruiseState={"available": True},
                    buttonEvents=[{"type": ButtonType.decelCruise, "pressed": False}])
  helper.update_v_cruise(CS, True, True)
  assert helper.v_cruise_kph == 89.0


def test_accelerator_sync_releases_at_the_cluster_speed(monkeypatch):
  # set 80, pedal held up to wheel 89.6 / cluster 93 and released: set 93
  instance, step = make_car(monkeypatch)
  step(76.9, 80.0, enabled=False)
  step(76.9, 80.0)
  instance.v_cruise_helper.v_cruise_kph = instance.v_cruise_helper.v_cruise_cluster_kph = 80.0
  for _ in range(300):
    step(76.9, 80.0)
  wheel = 77.0
  for _ in range(MIN_PRESS_SAMPLES + 50):
    wheel = min(wheel + 0.1, 89.6)
    step(wheel, float(int(wheel * 1.039)), pressed=True)
  CS = step(89.6, 93.0)
  assert CS.vCruiseCluster == 93.0


def test_ratio_smooths_integer_steps_and_holds_outliers():
  ratio = ClusterSpeedRatio()
  for i in range(1000):
    ratio.update(86.0 * CV.KPH_TO_MS, (89.0 if i % 2 else 90.0) * CV.KPH_TO_MS)
  assert ratio.ratio == pytest.approx(86.0 / 89.5, abs=0.002)
  held = ratio.ratio
  ratio.update(86.0 * CV.KPH_TO_MS, 150.0 * CV.KPH_TO_MS)  # not a cluster speed
  ratio.update(2.0, 2.5)  # too slow for an integer cluster speed
  assert ratio.ratio == held
  assert ratio.planner_kph(V_CRUISE_UNSET) == V_CRUISE_UNSET and ratio.planner_kph(-1) == -1
  assert cluster_speed(car.CarState(vEgo=10.0)) == 10.0


def test_app_shows_the_set_speed_not_the_planner_speed():
  class Sample(dict):
    valid = {"carState": True, "selfdriveState": False, "modelV2": False, "kovaNavigation": False}
    alive = valid
    logMonoTime = {"carState": 1_000_000_000, "selfdriveState": 1, "modelV2": 1, "kovaNavigation": 1}

  car_state = SimpleNamespace(vEgo=23.0, steeringAngleDeg=0., vCruise=82.9, vCruiseCluster=86.0, gearShifter="drive",
                              leftBlinker=False, rightBlinker=False, brakePressed=False)
  scene = road_scene(Sample(carState=car_state), 1_000_000_001)
  assert scene["setSpeedMps"] == pytest.approx(86.0 / 3.6)


def test_navcamd_advertises_the_set_speed():
  from openpilot.kovapilot.navigation import navcamd
  state = car.CarState(vEgo=23.0, vEgoCluster=86.0 / 3.6, vCruise=82.9, vCruiseCluster=86.0)
  sm = {"carState": state, "selfdriveState": SimpleNamespace(active=True)}
  nav = SimpleNamespace(app=SimpleNamespace(tracker=SimpleNamespace(nearest_distance=lambda _: 0)), odo_m=0.0)
  document = json.loads(navcamd._advertisement("0.10", sm, nav, "192.168.43.5"))
  assert document["v_cruise_kph"] == 86.0 and document["v_ego_kph"] == 86
