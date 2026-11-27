"""Pedal resume: re-engage on the driver's brake/accelerator release (janpoo 11/27 19:19 option 2).

Start conditions follow carrot's automatic cruise start for pedal releases; panda allows controls on
the same releases (opendbc ALT_EXP_PEDAL_RESUME)."""
from types import SimpleNamespace

import pytest
from opendbc.car.hyundai.values import HyundaiFlags
from opendbc.car.structs import car
from opendbc.safety import ALTERNATIVE_EXPERIENCE

from openpilot.cereal import log
from openpilot.kovapilot.config import auto_engage_settings
from openpilot.kovapilot.config.kova_params import AUTO_ENGAGE_KEY
from openpilot.kovapilot.engagement.pedal_resume import DRIVE_WAIT_NS, PedalResume, PedalResumeInputs
from openpilot.kovapilot.tests.test_auto_engage import Inputs, SelfdriveStep
from openpilot.kovapilot.vehicle.pedal_resume import apply_pedal_resume, pedal_resume_enabled

ButtonType = car.CarState.ButtonEvent.Type
FRAME_NS = 10_000_000


class Driver:
  def __init__(self, **kwargs):
    self.policy = PedalResume()
    self.now = 0
    self.values = {"enabled": False, "driving": True, "cruise_available": True, "brake_pressed": False, "gas_pressed": False,
                       "v_ego": 15.0, "v_ego_cluster": 15.0, "a_ego": 0.0, "steering_angle_deg": 0.0, "blinker": False, "hold": False,
                       "cancel_pressed": False, "set_resume_released": False, "lead_distance": 0.0,
                       "disengage_on_accelerator": False, "authority": True, "entry_ok": True}
    self.values.update(kwargs)
    self.step()
    self.now += DRIVE_WAIT_NS

  def step(self, **kwargs):
    self.now += FRAME_NS
    values = {**self.values, **kwargs}
    return self.policy.update(PedalResumeInputs(now_ns=self.now, **values))

  def hold(self, frames, **kwargs):
    return [self.step(**kwargs) for _ in range(frames)]

  def brake_tap(self, **kwargs):
    assert not any(self.hold(50, brake_pressed=True, **kwargs))
    return self.step(**kwargs)

  def gas_release(self, frames=100, **kwargs):
    assert not any(self.hold(frames, gas_pressed=True, **kwargs))
    return self.step(**kwargs)


def test_brake_release_while_moving_resumes():
  assert Driver().brake_tap()


def test_brake_release_at_standstill_needs_a_close_lead_and_straight_wheel():
  stopped = {"v_ego": 0.0, "v_ego_cluster": 0.0}
  assert not Driver(**stopped).brake_tap()
  assert Driver(**stopped, lead_distance=12.0).brake_tap()
  assert not Driver(**stopped, lead_distance=25.0).brake_tap()
  assert not Driver(**stopped, lead_distance=12.0, steering_angle_deg=25.0).brake_tap()


def test_accelerator_release_resumes_from_30_kph():
  assert Driver(v_ego=30 / 3.6, v_ego_cluster=30 / 3.6).gas_release()
  assert not Driver(v_ego=20 / 3.6, v_ego_cluster=20 / 3.6).gas_release()
  # a lead too close for the speed blocks it
  assert not Driver(lead_distance=8.0).gas_release()


def test_accelerator_tap_resumes_at_any_speed():
  assert Driver(v_ego=0.0, v_ego_cluster=0.0).gas_release(frames=20)
  assert not Driver(v_ego=0.0, v_ego_cluster=0.0).gas_release(frames=50)
  assert not Driver(v_ego=0.0, v_ego_cluster=0.0, disengage_on_accelerator=True).gas_release(frames=20)


@pytest.mark.parametrize("block", [
  {"blinker": True}, {"hold": True}, {"steering_angle_deg": 70.0}, {"steering_angle_deg": -75.0},
  {"cruise_available": False}, {"enabled": True},
])
def test_blocks(block):
  assert not Driver(**block).brake_tap()
  assert not Driver(**block, v_ego=40 / 3.6, v_ego_cluster=40 / 3.6).gas_release()


def test_not_in_drive_and_20_seconds_after_entering_drive():
  driver = Driver(driving=False)
  assert not driver.brake_tap()
  driver.values["driving"] = True
  driver.step()
  assert not driver.brake_tap()
  driver.hold(int(DRIVE_WAIT_NS / FRAME_NS))
  assert driver.brake_tap()


def test_set_resume_release_ends_the_drive_wait():
  driver = Driver(driving=False)
  driver.values["driving"] = True
  driver.step()
  driver.step(set_resume_released=True)
  assert driver.brake_tap()


def test_cancel_blocks_until_set_or_resume():
  driver = Driver()
  assert not driver.step(cancel_pressed=True)
  assert not driver.brake_tap()
  assert not driver.gas_release(frames=20)
  assert not driver.step(set_resume_released=True)
  assert driver.brake_tap()


def test_waits_for_panda_authority_briefly():
  driver = Driver(authority=False)
  assert not driver.brake_tap()
  assert driver.step(authority=True)

  driver = Driver(authority=False)
  assert not driver.brake_tap()
  driver.hold(50)
  assert not driver.step(authority=True)


def test_entry_checks_and_brake_cancel_the_pending_start():
  driver = Driver(entry_ok=False)
  assert not driver.brake_tap()
  assert not any(driver.hold(60))

  driver = Driver(authority=False)
  assert not driver.brake_tap()
  assert not driver.step(brake_pressed=True)
  assert not driver.step(brake_pressed=True, authority=True)


def test_accelerator_held_while_braking_hard_blocks_for_5_seconds():
  driver = Driver()
  assert not any(driver.hold(60, gas_pressed=True, a_ego=-1.0))
  assert not driver.step()
  assert not driver.brake_tap()
  driver.hold(500)
  assert driver.brake_tap()


def _cp(**kwargs):
  values = {"brand": "hyundai", "flags": int(HyundaiFlags.CANFD), "openpilotLongitudinalControl": True,
                "safetyConfigs": [{"safetyModel": "hyundaiCanfd", "safetyParam": 1}]}
  values.update(kwargs)
  return car.CarParams.new_message(**values)


def test_panda_flag_only_for_hyundai_canfd_openpilot_longitudinal():
  for cp, expected in (
    (_cp(), True),
    (_cp(openpilotLongitudinalControl=False), False),
    (_cp(pcmCruise=True), False),
    (_cp(flags=0, safetyConfigs=[{"safetyModel": "hyundai"}]), False),
    (_cp(brand="toyota"), False),
    (_cp(passive=True), False),
  ):
    builder = cp.as_builder() if hasattr(cp, "as_builder") else cp
    assert apply_pedal_resume(builder, True) == expected
    assert pedal_resume_enabled(builder) == expected
    assert not apply_pedal_resume(builder, False)
    assert not pedal_resume_enabled(builder)


def _canfd_runtime(monkeypatch, tmp_path, *, pedal=True):
  monkeypatch.setattr(auto_engage_settings, "status_path", lambda: tmp_path / "auto-engage.json")
  alt = ALTERNATIVE_EXPERIENCE.PEDAL_RESUME if pedal else 0
  cp = _cp(alternativeExperience=alt)
  params = SimpleNamespace(get=lambda key, **kwargs: 40 if key == "InitialCruiseSpeedKph" else {
    "schemaVersion": 1, "revision": 0, "values": {AUTO_ENGAGE_KEY: 1},
  })
  sm = Inputs(
    deviceState=log.DeviceState.new_message(started=True, startedMonoTime=1_000_000_000),
    pandaStates=[log.PandaState.new_message(safetyModel="hyundaiCanfd", safetyParam=1, controlsAllowed=True,
                                            alternativeExperience=alt)],
    driverMonitoringState=log.DriverMonitoringState.new_message(),
    longitudinalPlan=log.LongitudinalPlan.new_message(),
    carControl=car.CarControl.new_message(),
    modelV2=log.ModelDataV2.new_message(),
    radarState=log.RadarState.new_message(),
  )
  sm.valid = dict.fromkeys(sm, True)
  sm.alive = dict.fromkeys(sm, True)
  sm.logMonoTime = {}
  runtime = SelfdriveStep(monkeypatch, cp, params, sm)
  runtime.sd.disengage_on_accelerator = False
  runtime.cs.gearShifter = "drive"
  runtime.cs.standstill = False
  runtime.cs.vEgo = runtime.cs.vEgoCluster = 20.0
  for _ in range(int(DRIVE_WAIT_NS / 100_000_000) + 1):
    assert not runtime.step()
  return runtime


def _main_and_brake(runtime):
  # the main button no longer blocks the start; brake press disengages
  runtime.cs.buttonEvents = [car.CarState.ButtonEvent(type=ButtonType.mainCruise, pressed=True)]
  assert not runtime.step()
  runtime.cs.buttonEvents = []
  assert runtime.step(log.OnroadEvent.EventName.buttonEnable)
  runtime.cs.brakePressed = True
  assert not runtime.step(log.OnroadEvent.EventName.pedalPressed)
  runtime.cs.brakePressed = False


def test_runtime_brake_release_reengages_after_main_button(monkeypatch, tmp_path):
  runtime = _canfd_runtime(monkeypatch, tmp_path)
  _main_and_brake(runtime)
  assert runtime.step()
  # cancel blocks the next pedal start until SET/RES
  runtime.cs.buttonEvents = [car.CarState.ButtonEvent(type=ButtonType.cancel, pressed=True)]
  assert not runtime.step(log.OnroadEvent.EventName.buttonCancel)
  runtime.cs.buttonEvents = []
  runtime.cs.brakePressed = True
  assert not runtime.step()
  runtime.cs.brakePressed = False
  for _ in range(10):
    assert not runtime.step()
  assert auto_engage_settings.read_auto_engage_status()["longitudinal"] == "manualRequired"


def test_runtime_without_panda_flag_keeps_manual_reengagement(monkeypatch, tmp_path):
  runtime = _canfd_runtime(monkeypatch, tmp_path, pedal=False)
  _main_and_brake(runtime)
  for _ in range(10):
    assert not runtime.step()
  assert auto_engage_settings.read_auto_engage_status()["longitudinal"] == "manualRequired"


def test_runtime_waiting_status_while_pedal_resume_is_armed(monkeypatch, tmp_path):
  runtime = _canfd_runtime(monkeypatch, tmp_path)
  _main_and_brake(runtime)
  runtime.cs.vEgo = runtime.cs.vEgoCluster = 0.0
  runtime.cs.standstill = True
  runtime.cs.brakePressed = True
  assert not runtime.step()
  runtime.cs.brakePressed = False
  for _ in range(10):
    assert not runtime.step()
  assert auto_engage_settings.read_auto_engage_status()["longitudinal"] == "waiting"
