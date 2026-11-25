from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opendbc.car.structs import car

from openpilot.kovapilot.bridges.curve_runtime import CURVE_INPUTS
from openpilot.kovapilot.config import curve_slowdown_settings
from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY
from openpilot.kovapilot.tests.test_camera_runtime import planner_inputs
from openpilot.selfdrive.controls import plannerd
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner


def run_plannerd(monkeypatch, *, enabled, native_long=True, interrupt=None):
  cp = car.CarParams.new_message(openpilotLongitudinalControl=native_long, longitudinalActuatorDelay=0.5)
  params = {
    "CarParams": cp.to_bytes(),
    "KovaParams": {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: enabled}},
  }
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
  inputs.logMonoTime = {}
  now = 1_000_000_000

  class ReplayFinished(Exception):
    pass

  def update():
    nonlocal now
    inputs.frame += 1
    if inputs.frame > 5:
      raise ReplayFinished
    now += 50_000_000
    inputs["controlsState"].longControlState = "off" if inputs.frame == 1 else "pid"
    inputs.logMonoTime.update(dict.fromkeys(CURVE_INPUTS, now))
    if inputs.frame == 5 and interrupt is not None:
      interrupt(inputs)

  inputs.update = update
  sent = []

  def send(service, msg):
    if service == "longitudinalPlan":
      sent.append((msg.longitudinalPlan.aTarget, msg.longitudinalPlan.shouldStop, inputs["carState"].vCruise))

  applied = Mock()
  monkeypatch.setattr(plannerd, "Params", lambda: SimpleNamespace(get=lambda key, **kwargs: params.get(key)))
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
  applied.assert_called_once_with(True, supported=True)
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
  applied.assert_called_once_with(native_long, supported=native_long)
  assert RecordingPlanner.instances[-1].a_speed_cap is None


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
  assert curve_slowdown_settings.applied_curve_slowdown(1234) is True
  assert curve_slowdown_settings.applied_curve_slowdown(1235) is None
  curve_slowdown_settings.publish_applied_curve_slowdown(False, supported=False)
  assert curve_slowdown_settings.applied_curve_slowdown(1234) == "unsupported"


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), (1, False), ("true", False), (None, False)])
def test_curve_preference_fails_closed(value, expected):
  params = {"KovaParams": {"schemaVersion": 1, "revision": 0, "values": {CURVE_SLOWDOWN_KEY: value}}}
  assert curve_slowdown_settings.read_curve_slowdown(params) is expected
