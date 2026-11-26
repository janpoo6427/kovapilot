from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from opendbc.car.structs import car

from openpilot.kovapilot.config.kova_params import LEAD_RESTART_KEY
from openpilot.kovapilot.longitudinal.lead_restart import LeadObservation, LeadRestartResponse
from openpilot.kovapilot.tests.test_camera_runtime import planner_inputs
from openpilot.selfdrive.controls import plannerd


def _lead(time_s, distance=6.0, speed=0.0, track_id=3):
  return LeadObservation(track_id, distance, speed, int(time_s * 1e9))


def _step(response, when, lead, *, allowed=True, should_stop=False, target=0.7):
  return response.constrain(lead, allowed=allowed, standing=True, should_stop=should_stop,
                            target_accel=target, previous_accel=0.0, now_ns=int(when * 1e9), dt=0.05)


def test_lost_replaced_or_stale_lead_cannot_release_hold():
  response = LeadRestartResponse(3)
  _step(response, 1.0, _lead(1.0))
  assert _step(response, 1.55, _lead(1.55)) == (0.0, True)
  moving = _lead(2.0, distance=7, speed=1.0)
  assert _step(response, 2.0, moving) == (0.0, True)
  assert _step(response, 2.3, moving) == (0.0, True)
  assert _step(response, 2.31, None) == (0.0, True)
  assert _step(response, 2.6, _lead(2.6, distance=8, speed=1.0, track_id=4)) == (0.0, True)
  assert _step(response, 3.0, _lead(3.0, distance=9, speed=1.0, track_id=4)) == (0.0, True)
  assert _step(response, 3.05, None, should_stop=True, target=-0.4) == (-0.4, True)
  assert _step(response, 3.1, None, allowed=False, should_stop=True, target=-0.3) == (-0.3, True)
  assert not response.armed


def run_replay(monkeypatch, profile, *, native=True, intervention=None, change_while_driving=False):
  cp = car.CarParams.new_message(openpilotLongitudinalControl=native, pcmCruise=not native, longitudinalActuatorDelay=0.5)
  values = {LEAD_RESTART_KEY: profile}
  state = {"CarParams": cp.to_bytes(), "KovaParams": {"schemaVersion": 1, "revision": 0, "values": values}}
  backend = SimpleNamespace(get=lambda key, **kwargs: state.get(key))
  inputs = planner_inputs(vEgo=0.0, vCruise=30.0, standstill=True, gearShifter="park")
  services = tuple(inputs)
  inputs.frame = 0
  inputs.updated = {"modelV2": True}
  inputs.valid = dict.fromkeys(services, True)
  inputs.alive = dict.fromkeys(services, True)
  inputs.logMonoTime = {}
  now = 1_000_000_000

  class Finished(Exception):
    pass

  def update():
    nonlocal now
    inputs.frame += 1
    if inputs.frame > 58:
      raise Finished
    now += 50_000_000
    inputs.logMonoTime.update(dict.fromkeys(services, now))
    driving = inputs.frame > 2
    if driving and change_while_driving:
      values[LEAD_RESTART_KEY] = 3
    inputs["carState"].gearShifter = "drive" if driving else "park"
    inputs["carControl"].enabled = inputs["carControl"].longActive = driving
    inputs["selfdriveState"].enabled = inputs["selfdriveState"].active = driving
    inputs["controlsState"].longControlState = "pid" if driving else "off"
    lead = inputs["radarState"].leadOne
    lead.present = lead.radar = True
    lead.radarTrackId = 3
    lead.modelProb = 0.9
    lead.dRel = 7.0 if inputs.frame < 24 else 7.0 + (inputs.frame - 24) * 0.14
    lead.vLead = lead.vLeadK = 0.0 if inputs.frame < 24 else 2.8
    if intervention is not None:
      intervention(inputs)

  inputs.update = update
  output = []

  def send(service, msg):
    if service == "longitudinalPlan":
      output.append((inputs.frame, msg.longitudinalPlan.aTarget, msg.longitudinalPlan.shouldStop))

  applied = Mock()
  monkeypatch.setattr(plannerd, "Params", lambda: backend)
  monkeypatch.setattr(plannerd, "config_realtime_process", Mock())
  monkeypatch.setattr(plannerd, "LaneDepartureWarning", lambda: SimpleNamespace(update=Mock(), left=False, right=False))
  monkeypatch.setattr(plannerd, "publish_applied_curve_slowdown", Mock())
  monkeypatch.setattr(plannerd, "publish_applied_lead_restart", applied)
  monkeypatch.setattr(plannerd.messaging, "SubMaster", lambda *args, **kwargs: inputs)
  monkeypatch.setattr(plannerd.messaging, "PubMaster", lambda *args, **kwargs: SimpleNamespace(send=send))
  monkeypatch.setattr(plannerd.time, "monotonic_ns", lambda: now)
  with pytest.raises(Finished):
    plannerd.main()
  return output, applied.call_args_list


def test_replay_profiles_change_launch_order_and_stock_ignores_preference(monkeypatch):
  profiles = [run_replay(monkeypatch, profile) for profile in (0, 1, 2, 3)]
  assert profiles[0][1] == [call(0)]
  for level, (_, applied) in enumerate(profiles[1:], 1):
    assert applied == [call(0), call(level)]
  launches = [next(frame for frame, accel, stop in trace if frame >= 24 and accel > 0.001 and not stop)
              for trace, _ in profiles]
  assert launches[0] <= launches[3] < launches[2] < launches[1]
  assert run_replay(monkeypatch, 0, change_while_driving=True) == profiles[0]
  assert run_replay(monkeypatch, 0, native=False) == run_replay(monkeypatch, 3, native=False)


def test_existing_stop_remains_authoritative_in_replay(monkeypatch):
  def model_stop(inputs):
    inputs["modelV2"].action.shouldStop = inputs.frame >= 24

  trace, _ = run_replay(monkeypatch, 3, intervention=model_stop)
  assert all(stop for frame, _, stop in trace if frame >= 24)
