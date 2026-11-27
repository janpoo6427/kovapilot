"""card: cruise engaged on an accelerator press publishes a set speed that starts at the speed of that
moment and climbs with the held pedal (planner vCruise and the cluster/HUD vCruiseCluster)."""
from types import SimpleNamespace

from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.selfdrive.car import card
from openpilot.selfdrive.car.cruise import VCruiseHelper


def test_card_set_speed_follows_the_press_that_engaged(monkeypatch):
  instance = object.__new__(card.Car)
  instance.CP = car.CarParams(openpilotLongitudinalControl=True)
  instance.v_cruise_helper = VCruiseHelper(instance.CP)
  instance.cruise_accelerator_sync = CruiseAcceleratorSyncBridge(instance.v_cruise_helper)
  instance.cruise_sync_settings = (True, False, False)
  instance.is_metric = True
  instance.initial_cruise_speed = 40
  instance.can_rcv_cum_timeout_counter = 0
  instance.initialized_prev = True
  instance.can_sock = None
  instance.CC_prev = car.CarControl(enabled=False)
  instance.CS_prev = car.CarState()

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

  def step(speed_kph, pressed, enabled):
    instance.sm["carControl"] = car.CarControl(enabled=enabled, longActive=enabled and not pressed)
    state = car.CarState(canValid=True, gasPressed=pressed, vEgo=speed_kph * CV.KPH_TO_MS,
                         vEgoCluster=speed_kph * CV.KPH_TO_MS, cruiseState={"available": True})
    instance.CI = SimpleNamespace(update=lambda _: state)
    out = instance.state_update()[0]
    instance.CC_prev = instance.sm["carControl"]
    instance.CS_prev = out
    return out.vCruise, out.vCruiseCluster

  step(52.0, False, False)
  step(52.3, True, False)
  assert step(52.6, True, True) == (52.0, 52.0)
  speed = 52.6
  for _ in range(400):
    speed += 0.05
    step(speed, True, True)
  assert step(speed, False, True) == (round(speed), round(speed))
  for _ in range(30):
    assert step(speed - 5, False, True) == (round(speed), round(speed))
