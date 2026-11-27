"""First start engages at the accelerator press (janpoo 11/27 22:28), not at the release.

c02502bb panda allows controls only at the press and the heartbeat check ends it within about 3 s,
so a start that waited for the release could miss the authority."""
from openpilot.cereal import log
from openpilot.kovapilot.tests.test_auto_engage import _runtime
from openpilot.selfdrive.selfdrived.state import State

GAS = log.OnroadEvent.EventName.gasPressedOverride


def _drive(runtime, *, speed=15.0, steps=11):
  assert not runtime.step()
  runtime.cs.gearShifter = "drive"
  runtime.cs.standstill = False
  runtime.cs.vEgo = speed
  for _ in range(steps):
    assert not runtime.step()


def test_engages_at_the_press_into_the_accelerator_override(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  _drive(runtime)
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)
  assert runtime.sd.state_machine.state == State.overriding
  for _ in range(40):
    assert runtime.step(GAS)
    assert runtime.sd.state_machine.state == State.overriding
  runtime.cs.gasPressed = False
  assert runtime.step()
  assert runtime.sd.state_machine.state == State.enabled


def test_press_waits_briefly_for_panda_authority(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  panda = runtime.sd.sm["pandaStates"][0]
  panda.controlsAllowed = False
  _drive(runtime)
  runtime.cs.gasPressed = True
  assert not runtime.step(GAS)
  assert not runtime.step(GAS)
  panda.controlsAllowed = True
  assert runtime.step(GAS)


def test_authority_arriving_after_the_wait_does_not_start(monkeypatch, tmp_path):
  # KOVA 11/27 23:12: the wait is now 1.5 s while the pedal stays pressed (LAUNCH_WINDOW_NS, was 0.5 s)
  runtime = _runtime(monkeypatch, tmp_path)
  panda = runtime.sd.sm["pandaStates"][0]
  panda.controlsAllowed = False
  _drive(runtime)
  runtime.cs.gasPressed = True
  for _ in range(16):
    assert not runtime.step(GAS)
  panda.controlsAllowed = True
  for _ in range(20):
    assert not runtime.step(GAS)


def test_release_no_longer_starts(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  panda = runtime.sd.sm["pandaStates"][0]
  panda.controlsAllowed = False
  _drive(runtime)
  runtime.cs.gasPressed = True
  for _ in range(30):
    assert not runtime.step(GAS)
  runtime.cs.gasPressed = False
  panda.controlsAllowed = True
  for _ in range(30):
    assert not runtime.step()


def test_entry_conditions_must_be_stable_before_the_press(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  _drive(runtime, steps=4)
  runtime.cs.gasPressed = True
  for _ in range(10):
    assert not runtime.step(GAS)
  runtime.cs.gasPressed = False
  for _ in range(11):
    assert not runtime.step()
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)


def test_press_below_the_initial_cruise_speed_starts(monkeypatch, tmp_path):
  # KOVA 11/27 23:12: no 40 km/h floor any more (was: 8 m/s never started, also not after speeding up)
  runtime = _runtime(monkeypatch, tmp_path)
  _drive(runtime, speed=8.0)
  runtime.cs.gasPressed = True
  assert runtime.step(GAS)
