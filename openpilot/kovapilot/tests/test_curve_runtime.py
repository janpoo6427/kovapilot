from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from opendbc.car.structs import car

from openpilot.kovapilot.bridges.curve_runtime import CURVE_INPUTS
from openpilot.kovapilot.config import curve_slowdown_settings
from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY, KovaParamsStore, ParamsKovaParamsBackend
from openpilot.kovapilot.tests.test_camera_runtime import planner_inputs
from openpilot.selfdrive.controls import plannerd
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner


def _set_vehicle(inputs, *, parked: bool, enabled: bool) -> None:
  state = inputs["carState"]
  state.gearShifter = "park" if parked else "drive"
  state.vEgo = 0.0 if parked else 25.0
  state.standstill = parked
  control = inputs["carControl"]
  control.enabled = control.longActive = enabled
  inputs["selfdriveState"].enabled = inputs["selfdriveState"].active = enabled
  inputs["controlsState"].longControlState = "pid" if enabled else "off"


def run_plannerd(monkeypatch, *, enabled, native_long=True, interrupt=None, frame_count=5, during=None):
  cp = car.CarParams.new_message(openpilotLongitudinalControl=native_long, longitudinalActuatorDelay=0.5)
  params = {
    "CarParams": cp.to_bytes(),
    "KovaParams": {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: int(enabled)}},
  }
  params_backend = SimpleNamespace(get=lambda key, **kwargs: params.get(key),
                                   put=lambda key, value, **kwargs: params.__setitem__(key, value))
  inputs = planner_inputs()
  times = [i / 2 for i in range(13)]
  model = inputs["modelV2"]
  model.position.t = times
  model.position.x = [25 * t for t in times]
  model.position.y = [0.0] * len(times)
  model.velocity.x = [25.0] * len(times)
  model.orientationRate.z = [0.625] * len(times)

  inputs.frame = 0
  inputs.updated = {"modelV2": True}
  inputs.valid = dict.fromkeys(CURVE_INPUTS, True)
  inputs.alive = dict.fromkeys(CURVE_INPUTS, True)
  inputs.logMonoTime = {}
  now = 1_000_000_000

  class ReplayFinished(Exception):
    pass

  def update():
    nonlocal now
    inputs.frame += 1
    if inputs.frame > frame_count:
      raise ReplayFinished
    now += 100_000_000
    _set_vehicle(inputs, parked=inputs.frame == 1, enabled=inputs.frame > 2)
    inputs.logMonoTime.update(dict.fromkeys(CURVE_INPUTS, now))
    if during is not None:
      during(inputs, params_backend)
    if inputs.frame == frame_count and interrupt is not None:
      interrupt(inputs)

  inputs.update = update
  sent = []

  def send(service, msg):
    if service == "longitudinalPlan":
      sent.append((msg.longitudinalPlan.aTarget, msg.longitudinalPlan.shouldStop, inputs["carState"].vCruise))

  applied = Mock()
  monkeypatch.setattr(plannerd, "Params", lambda: params_backend)
  monkeypatch.setattr(plannerd, "config_realtime_process", Mock())
  monkeypatch.setattr(plannerd, "LaneDepartureWarning", lambda: SimpleNamespace(update=Mock(), left=False, right=False))
  monkeypatch.setattr(plannerd, "publish_applied_curve_slowdown", applied)
  monkeypatch.setattr(plannerd.messaging, "SubMaster", lambda *args, **kwargs: inputs)
  monkeypatch.setattr(plannerd.messaging, "PubMaster", lambda *args, **kwargs: SimpleNamespace(send=send))
  monkeypatch.setattr(plannerd.time, "monotonic_ns", lambda: now)
  with pytest.raises(ReplayFinished):
    plannerd.main()
  return sent, applied


def test_saved_toggle_changes_published_native_long_plan(monkeypatch):
  off, applied = run_plannerd(monkeypatch, enabled=False)
  applied.assert_called_once_with(False, supported=True)
  on, applied = run_plannerd(monkeypatch, enabled=True)
  assert applied.call_args_list == [call(False, supported=True), call(True, supported=True)]
  assert on[-1][0] < off[-1][0]
  assert [p[1:] for p in on] == [p[1:] for p in off]


@pytest.mark.parametrize("native_long,interrupt", [
  (False, None),
  (True, lambda sm: setattr(sm["carState"], "brakePressed", True)),
  (True, lambda sm: setattr(sm["carControl"].cruiseControl, "cancel", True)),
  (True, lambda sm: sm.logMonoTime.update(carState=0)),
  (True, lambda sm: setattr(sm["controlsState"], "forceDecel", True)),
])
def test_blocked_curve_plan_has_no_speed_cap(monkeypatch, native_long, interrupt):
  class RecordingPlanner(LongitudinalPlanner):
    instances = []

    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      self.instances.append(self)

  monkeypatch.setattr(plannerd, "LongitudinalPlanner", RecordingPlanner)
  _, applied = run_plannerd(monkeypatch, enabled=True, native_long=native_long, interrupt=interrupt)
  expected = [call(False, supported=native_long)]
  if native_long:
    expected.append(call(True, supported=True))
  assert applied.call_args_list == expected
  assert RecordingPlanner.instances[-1].a_speed_cap is None


@pytest.mark.parametrize("initial", [False, True])
def test_saved_change_waits_for_park_without_restarting_plannerd(monkeypatch, initial):
  def parked_later(inputs, params):
    if inputs.frame == 8:
      _set_vehicle(inputs, parked=True, enabled=False)
    elif inputs.frame == 9:
      _set_vehicle(inputs, parked=False, enabled=False)

  def save_onroad(inputs, params):
    if inputs.frame == 3:
      KovaParamsStore(ParamsKovaParamsBackend(params)).update(CURVE_SLOWDOWN_KEY, int(not initial), 0, is_offroad=False)
    parked_later(inputs, params)

  baseline, _ = run_plannerd(monkeypatch, enabled=initial, frame_count=12, during=parked_later)
  changed, applied = run_plannerd(monkeypatch, enabled=initial, frame_count=12, during=save_onroad)
  assert changed[:7] == baseline[:7]
  assert (changed[-1][0] > baseline[-1][0]) if initial else (changed[-1][0] < baseline[-1][0])
  expected = [call(False, supported=True)]
  if initial:
    expected.append(call(True, supported=True))
  assert applied.call_args_list == expected + [call(not initial, supported=True)]


def test_parked_preference_requires_fresh_disengaged_control():
  now = 1_000_000_000
  inputs = planner_inputs()
  _set_vehicle(inputs, parked=True, enabled=False)
  inputs.valid = dict.fromkeys(CURVE_INPUTS, True)
  inputs.alive = dict.fromkeys(CURVE_INPUTS, True)
  inputs.logMonoTime = dict.fromkeys(CURVE_INPUTS, now)
  params = {"KovaParams": {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: 1}}}
  preference = curve_slowdown_settings.CurveSlowdownPreference(params)

  inputs.logMonoTime["carControl"] = 0
  assert preference.poll(inputs, now) is None
  inputs.logMonoTime["carControl"] = now
  inputs["carControl"].latActive = True
  assert preference.poll(inputs, now) is None
  inputs["carControl"].latActive = False
  inputs["selfdriveState"].active = True
  assert preference.poll(inputs, now) is None
  inputs["selfdriveState"].active = False
  assert preference.poll(inputs, now) is True


def test_curve_camera_arbitration_and_clearing_ineligible_source():
  cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
  planner = LongitudinalPlanner(cp, init_v=25)
  inputs = planner_inputs()
  planner.update(inputs, camera_speed_cap=18.0, camera_eligible=True, curve_speed_cap=15.0, curve_eligible=True)
  assert planner.speed_cap_source == "curve"
  planner.update(inputs, camera_speed_cap=18.0, camera_eligible=True, curve_speed_cap=20.0, curve_eligible=True)
  assert planner.speed_cap_source == "camera"
  planner.update(inputs, camera_speed_cap=18.0, camera_eligible=False, curve_eligible=True)
  assert planner.speed_cap_source is None
  assert planner.a_speed_cap is None


def test_curve_cap_preserves_stronger_braking_and_stop_decision():
  cp = car.CarParams.new_message(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5)
  inputs = planner_inputs()
  inputs["modelV2"].action.desiredAcceleration = -1.5
  inputs["modelV2"].action.shouldStop = True
  baseline, curve = (LongitudinalPlanner(cp, init_v=25) for _ in range(2))
  baseline.update(inputs)
  curve.update(inputs, curve_speed_cap=15.0, curve_eligible=True)
  assert curve.output_a_target == baseline.output_a_target
  assert curve.output_should_stop and baseline.output_should_stop


def test_curve_application_snapshot_rejects_an_old_planner(tmp_path, monkeypatch):
  monkeypatch.setattr(curve_slowdown_settings, "_applied_path", lambda: tmp_path / "curve.json")
  monkeypatch.setattr(curve_slowdown_settings.os, "getpid", lambda: 1234)
  curve_slowdown_settings.publish_applied_curve_slowdown(True, supported=True)
  value = curve_slowdown_settings.applied_curve_slowdown(1234)
  assert type(value) is int and value == 1
  assert curve_slowdown_settings.applied_curve_slowdown(1235) is None
  curve_slowdown_settings.publish_applied_curve_slowdown(False, supported=False)
  assert curve_slowdown_settings.applied_curve_slowdown(1234) == "unsupported"


@pytest.mark.parametrize("value,expected", [(False, False), (True, False), (0, False), (1, True), (2, False), ("true", False), (None, False)])
def test_curve_preference_fails_closed(value, expected):
  params = {"KovaParams": {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: value}}}
  assert curve_slowdown_settings.read_curve_slowdown(params) is expected
