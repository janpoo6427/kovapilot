"""Pedal resume on the accelerator (janpoo 11/28, 79f segments 72 and 73).

- no 30 km/h floor: the presses at 14.5, 9.8 and 15.6 km/h (4376, 4383 and 4387 s) did not start
- the release is checked too: a press that could not start (turn signal, lead too close) starts at
  the release when the conditions hold then
- unchanged: no start with a turn signal on (4341 to 4370 s, five presses), with a lead too close
  for the speed while moving, or when the accelerator disengages
"""
import pytest

from openpilot.kovapilot.tests.test_pedal_resume import Driver, _canfd_runtime, _disengaged_by_brake_without_start, _gas_override_events

KPH = 1 / 3.6


@pytest.mark.parametrize("kph", [14.5, 9.8, 15.6, 5.0, 0.0])
def test_accelerator_press_starts_below_30_kph(kph):
  assert Driver(v_ego=kph * KPH, v_ego_cluster=kph * KPH).gas_press()


def test_presses_with_the_turn_signal_on_never_start():
  # 79f-72: five presses at 40, 51, 22, 24 and 21 km/h with the signal on all the time
  driver = Driver()
  for kph in (40, 51, 22, 24, 21):
    v = {"v_ego": kph * KPH, "v_ego_cluster": kph * KPH, "blinker": True}
    assert not driver.gas_press(**v)
    assert not any(driver.hold(80, gas_pressed=True, **v))
    assert not driver.step(**v)
    assert not any(driver.hold(100, **v))


def test_release_starts_after_the_turn_signal_went_off():
  driver = Driver(v_ego=40 * KPH, v_ego_cluster=40 * KPH)
  assert not driver.gas_press(blinker=True)
  assert not any(driver.hold(100, gas_pressed=True, blinker=True))
  assert not any(driver.hold(50, gas_pressed=True))
  assert driver.step()


def test_release_starts_when_the_lead_is_no_longer_too_close():
  driver = Driver(v_ego=15.0, v_ego_cluster=15.0)
  assert not driver.gas_press(lead_distance=8.0)
  assert not any(driver.hold(100, gas_pressed=True, lead_distance=8.0))
  assert driver.step(lead_distance=30.0)


@pytest.mark.parametrize("at_release", [
  {"blinker": True}, {"lead_distance": 8.0}, {"disengage_on_accelerator": True}, {"hold": True},
  {"steering_angle_deg": 75.0}, {"cruise_available": False}, {"enabled": True}, {"brake_pressed": True},
])
def test_release_keeps_the_press_conditions(at_release):
  driver = Driver(v_ego=15.0, v_ego_cluster=15.0)
  assert not driver.gas_press(blinker=True)
  assert not any(driver.hold(100, gas_pressed=True, blinker=True))
  assert not driver.step(**at_release)
  assert not any(driver.hold(60, **at_release))


def test_release_waits_for_panda_authority_briefly():
  driver = Driver(authority=False)
  assert not driver.gas_press(blinker=True)
  assert not any(driver.hold(100, gas_pressed=True, blinker=True))
  assert not driver.step()
  assert driver.step(authority=True)


def test_runtime_release_engages(monkeypatch, tmp_path):
  runtime = _canfd_runtime(monkeypatch, tmp_path)
  _gas_override_events(runtime)
  _disengaged_by_brake_without_start(runtime)
  runtime.cs.vEgo = runtime.cs.vEgoCluster = 15 * KPH
  runtime.cs.leftBlinker = True
  runtime.cs.gasPressed = True
  for _ in range(10):
    assert not runtime.step()
  runtime.cs.leftBlinker = False
  for _ in range(5):
    assert not runtime.step()
  runtime.cs.gasPressed = False
  assert runtime.step()
