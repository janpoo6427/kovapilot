"""Driving personality (LongitudinalPersonality, the distance button 1-4) weights: the automatic lane change start
delay and the reaction when following a stopped lead away. Replaces the cruise.lead_restart_response setting."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opendbc.car.structs import car

from openpilot.cereal import log
from openpilot.common.realtime import DT_MDL
from openpilot.kovapilot.config import kova_params
from openpilot.kovapilot.config.kova_params import KOVA_PARAMS_KEY, KovaParamsStore, load_bundled_catalog
from openpilot.kovapilot.config.personality_weights import LANE_CHANGE_START_DELAY_S, LEAD_RESTART_REACTION, personality_value
from openpilot.kovapilot.lateral import auto_lane_change as alc_mod
from openpilot.kovapilot.longitudinal.lead_restart import LeadObservation, LeadRestartResponse
from openpilot.kovapilot.tests.test_auto_lane_change import LCS, Drive
from openpilot.kovapilot.tests.test_camera_runtime import planner_inputs
from openpilot.selfdrive.controls import plannerd

P = log.LongitudinalPersonality
OLD_KEY = "cruise.lead_restart_response"
PERSONALITIES = (P.aggressive, P.standard, P.relaxed, P.moreRelaxed)


def test_weight_tables():
  assert [LANE_CHANGE_START_DELAY_S[p] for p in PERSONALITIES] == [0.7, 1.0, 1.3, 1.5]
  # standard is today default (no extra limit); relaxed and moreRelaxed take the old Normal / Slow reactions
  assert [LEAD_RESTART_REACTION[p] for p in PERSONALITIES] == [None, None, (300_000_000, 0.75), (650_000_000, 0.45)]
  # unknown or missing personality falls back to standard
  for value in (None, 7, -1, True, "relaxed"):
    assert personality_value(LANE_CHANGE_START_DELAY_S, value) == 1.0
  reader = log.SelfdriveState.new_message(personality="relaxed").as_reader()
  assert personality_value(LANE_CHANGE_START_DELAY_S, reader.personality) == 1.3


class PersonalityDrive(Drive):
  def __init__(self, personality=None, seen=True, **kwargs):
    super().__init__(**kwargs)
    self.personality = personality
    self.seen = seen

  def step(self, n=1):
    for _ in range(n):
      from openpilot.kovapilot.tests.test_auto_lane_change import model
      m = model(self.offset)
      sm = {"carState": self.cs, "carControl": self.cc, "driverMonitoringState": self.dm}
      if self.personality is not None:
        sm["selfdriveState"] = SimpleNamespace(personality=self.personality)
        sm = SubMasterLike(sm, self.seen)
      self.alc.update(sm, m, self.dh.lane_change_state, self.lane_change_prob)
      self.dh.update(self.cs, self.cc.latActive, self.lane_change_prob, self.alc)
      self.alc.publish_state(self.dh.lane_change_state)
      self.states.append((self.dh.lane_change_state, self.dh.lane_change_direction, self.alc.state))
    return self.dh.lane_change_state


class SubMasterLike(dict):
  def __init__(self, services, seen):
    super().__init__(services)
    self.seen = dict.fromkeys(services, True) | {"selfdriveState": seen}


def start_time(d, before=0.0):
  d.seconds(before)  # blind spot clear and lane seen long before the turn signal
  first = len(d.states)
  d.blinker("left")
  d.seconds(3.0)
  return next(i - first + 1 for i, s in enumerate(d.states) if i >= first and s[0] == LCS.laneChangeStarting) * DT_MDL


@pytest.mark.parametrize("personality,delay", [(P.aggressive, 0.7), (P.standard, 1.0), (P.relaxed, 1.3), (P.moreRelaxed, 1.5)])
def test_lane_change_start_waits_by_personality(personality, delay):
  assert start_time(PersonalityDrive(personality), before=2.0) == pytest.approx(delay, abs=DT_MDL)


def test_missing_or_unseen_personality_is_standard():
  assert start_time(PersonalityDrive(None), before=2.0) == pytest.approx(1.0, abs=DT_MDL)
  assert start_time(PersonalityDrive(P.aggressive, seen=False), before=2.0) == pytest.approx(1.0, abs=DT_MDL)


def test_safety_waits_do_not_follow_personality():
  assert (alc_mod.BLINDSPOT_CLEAR_TIME, alc_mod.SIDE_LANE_HOLD) == (1.0, 0.2)
  # blind spot occupied for 2 s after the turn signal: every personality waits the same 1 s after it clears
  starts = []
  for personality in PERSONALITIES:
    d = PersonalityDrive(personality)
    d.seconds(2.0)
    d.cs.leftBlindspot = True
    first = len(d.states)
    d.blinker("left")
    d.seconds(2.0)
    d.cs.leftBlindspot = False
    d.seconds(2.0)
    starts.append(next(i - first + 1 for i, s in enumerate(d.states) if i >= first and s[0] == LCS.laneChangeStarting) * DT_MDL)
  assert starts == pytest.approx([3.0] * 4, abs=DT_MDL)
  # aggressive right after the drive starts: the 1 s blind-spot clear time still holds, not 0.7 s
  assert start_time(PersonalityDrive(P.aggressive)) == pytest.approx(1.0, abs=DT_MDL)


def _lead(time_s, distance=6.0, speed=0.0, track_id=3):
  return LeadObservation(track_id, distance, speed, int(time_s * 1e9))


def _step(response, when, lead, *, allowed=True, should_stop=False, target=0.7):
  return response.constrain(lead, allowed=allowed, standing=True, should_stop=should_stop,
                            target_accel=target, previous_accel=0.0, now_ns=int(when * 1e9), dt=0.05)


def test_lost_replaced_or_stale_lead_cannot_release_hold():
  response = LeadRestartResponse((0, 1.15))
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


def run_replay(monkeypatch, personality, *, native=True, intervention=None, change_to=None, change_at=30, stored=None):
  cp = car.CarParams.new_message(openpilotLongitudinalControl=native, pcmCruise=not native, longitudinalActuatorDelay=0.5)
  state = {"CarParams": cp.to_bytes(), "KovaParams": stored}
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
    inputs["selfdriveState"].personality = change_to if change_to is not None and inputs.frame >= change_at else personality
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
  output, restarts = [], []

  def send(service, msg):
    if service == "longitudinalPlan":
      output.append((inputs.frame, msg.longitudinalPlan.aTarget, msg.longitudinalPlan.shouldStop))

  planner_update = plannerd.LongitudinalPlanner.update

  def spy(self, sm, **kwargs):
    runtime = kwargs.get("lead_restart")
    restarts.append(None if runtime is None else (runtime.response.delay_ns, runtime.response.jerk))
    return planner_update(self, sm, **kwargs)

  monkeypatch.setattr(plannerd, "Params", lambda: backend)
  monkeypatch.setattr(plannerd, "config_realtime_process", Mock())
  monkeypatch.setattr(plannerd, "LaneDepartureWarning", lambda: SimpleNamespace(update=Mock(), left=False, right=False))
  monkeypatch.setattr(plannerd, "publish_applied_curve_slowdown", Mock())
  monkeypatch.setattr(plannerd.LongitudinalPlanner, "update", spy)
  monkeypatch.setattr(plannerd.messaging, "SubMaster", lambda *args, **kwargs: inputs)
  monkeypatch.setattr(plannerd.messaging, "PubMaster", lambda *args, **kwargs: SimpleNamespace(send=send))
  monkeypatch.setattr(plannerd.time, "monotonic_ns", lambda: now)
  with pytest.raises(Finished):
    plannerd.main()
  monkeypatch.setattr(plannerd.LongitudinalPlanner, "update", planner_update)
  return output, restarts


def launch(trace):
  return next(frame for frame, accel, stop in trace if frame >= 24 and accel > 0.001 and not stop)


def test_replay_personality_sets_lead_restart_reaction(monkeypatch):
  runs = {p: run_replay(monkeypatch, p) for p in PERSONALITIES}
  # standard and aggressive: no lead restart limit at all (the planner as it is, the old default Off)
  assert set(runs[P.standard][1]) == {None}
  assert set(runs[P.aggressive][1]) == {None}
  assert set(runs[P.relaxed][1]) == {(300_000_000, 0.75)}
  assert set(runs[P.moreRelaxed][1]) == {(650_000_000, 0.45)}
  launches = {p: launch(trace) for p, (trace, _) in runs.items()}
  assert launches[P.aggressive] <= launches[P.standard] < launches[P.relaxed] < launches[P.moreRelaxed]


def test_old_saved_setting_is_ignored(monkeypatch):
  old = {"schemaVersion": 1, "revision": 4, "values": {OLD_KEY: 1}}
  for p in (P.standard, P.moreRelaxed):
    assert run_replay(monkeypatch, p, stored=old) == run_replay(monkeypatch, p)


def test_personality_change_while_stopped_follows_new_level(monkeypatch):
  # moreRelaxed then the distance button to standard before the lead leaves: departs like standard
  trace, restarts = run_replay(monkeypatch, P.moreRelaxed, change_to=P.standard, change_at=20)
  assert restarts[-1] is None
  assert launch(trace) == launch(run_replay(monkeypatch, P.standard)[0])
  # standard to moreRelaxed while stopped: the hold and ramp of moreRelaxed
  trace, restarts = run_replay(monkeypatch, P.standard, change_to=P.moreRelaxed, change_at=10)
  assert restarts[-1] == (650_000_000, 0.45)
  assert launch(trace) == launch(run_replay(monkeypatch, P.moreRelaxed)[0])
  # moreRelaxed held after the lead left, then standard: released at once, not later than staying on moreRelaxed
  trace, _ = run_replay(monkeypatch, P.moreRelaxed, change_to=P.standard, change_at=30)
  assert launch(trace) == 30 < launch(run_replay(monkeypatch, P.moreRelaxed)[0])


def test_stock_longitudinal_ignores_personality_weight(monkeypatch):
  for p in PERSONALITIES:
    assert set(run_replay(monkeypatch, p, native=False)[1]) == {None}


def test_existing_stop_remains_authoritative_in_replay(monkeypatch):
  def model_stop(inputs):
    inputs["modelV2"].action.shouldStop = inputs.frame >= 24

  for p in (P.relaxed, P.moreRelaxed):
    trace, _ = run_replay(monkeypatch, p, intervention=model_stop)
    assert all(stop for frame, _, stop in trace if frame >= 24)


class MemoryBackend:
  def __init__(self, value=None):
    self.value = value
    self.puts = 0

  def get(self, key):
    assert key == KOVA_PARAMS_KEY
    return self.value

  def put(self, key, value, block=False):
    assert key == KOVA_PARAMS_KEY
    self.value = value
    self.puts += 1


def test_lead_restart_setting_is_gone_and_old_value_ignored():
  assert OLD_KEY not in {s["key"] for s in load_bundled_catalog()["settings"]}
  assert not hasattr(kova_params, "LEAD_RESTART_KEY")
  backend = MemoryBackend({"schemaVersion": 1, "revision": 4, "values": {OLD_KEY: 2, "steering.auto_lane_change": 1}})
  store = KovaParamsStore(backend)
  values = store.values()
  assert OLD_KEY not in values and values["steering.auto_lane_change"] == 1
  assert OLD_KEY not in {s["key"] for s in store.document()["settings"]}
  store.initialize_storage()
  assert OLD_KEY not in backend.value["values"]
  assert backend.value["revision"] == 4 and backend.value["values"]["steering.auto_lane_change"] == 1
  puts = backend.puts
  store.initialize_storage()
  assert backend.puts == puts
  store.update("steering.auto_lane_change", 0, 4, is_offroad=True)
  assert backend.value["revision"] == 5 and OLD_KEY not in backend.value["values"]


def test_old_value_dropped_on_next_save_without_initialize():
  backend = MemoryBackend({"schemaVersion": 1, "revision": 0, "values": {OLD_KEY: 3}})
  store = KovaParamsStore(backend)
  store.update("steering.auto_lane_change", 1, 0, is_offroad=True)
  assert OLD_KEY not in backend.value["values"]
  assert backend.value["values"]["steering.auto_lane_change"] == 1
