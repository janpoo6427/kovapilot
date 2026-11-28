"""Automatic engagement: no sound when the brake ends cruise (janpoo 11/28 11:09).

With the automatic engagement on (pedal resume engages again at the brake release), a brake press that
disengages openpilot played the disengage sound and 0.2 s later the always-on steering engage sound
(79f logs). Both sounds are left out at that moment; the alerts are still shown. Other disengagements,
the safety alerts and the automatic engagement off keep their sounds.
"""
from types import SimpleNamespace

from opendbc.car.structs import car
from opendbc.safety import ALTERNATIVE_EXPERIENCE

from openpilot.cereal import log
from openpilot.kovapilot.bridges.always_lateral_runtime import AlwaysLateralRuntime
from openpilot.kovapilot.config import auto_engage_settings
from openpilot.kovapilot.config.kova_params import AUTO_ENGAGE_KEY
from openpilot.kovapilot.engagement.pedal_resume import DRIVE_WAIT_NS
from openpilot.kovapilot.tests.test_auto_engage import Inputs, SelfdriveStep
from openpilot.kovapilot.tests.test_pedal_resume import _cp
from openpilot.selfdrive.selfdrived.alertmanager import AlertManager
from openpilot.selfdrive.selfdrived.events import Events, AudibleAlert

EventName = log.OnroadEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type
STEP_FRAMES = 10  # SelfdriveStep advances 100 ms


class Drive:
  def __init__(self, monkeypatch, tmp_path, *, auto_engage=True, always_on=True):
    monkeypatch.setattr(auto_engage_settings, "status_path", lambda: tmp_path / "auto-engage.json")
    # card sets the pedal resume flag only when the automatic engagement is on
    alt = (ALTERNATIVE_EXPERIENCE.PEDAL_RESUME if auto_engage else 0) | (ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL if always_on else 0)
    cp = _cp(alternativeExperience=alt)
    params = SimpleNamespace(get=lambda key, **kwargs: 40 if key == "InitialCruiseSpeedKph" else {
      "schemaVersion": 1, "revision": 0, "values": {AUTO_ENGAGE_KEY: int(auto_engage)},
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
    sm.updated = {"pandaStates": False}
    sm.all_checks = lambda services=None: True
    sm.frame = 0
    self.sm = sm
    self.rt = SelfdriveStep(monkeypatch, cp, params, sm)
    sd = self.sd = self.rt.sd
    del sd.update_alerts  # the real alerts: Events -> AlertManager, as published in selfdriveState
    sd.AM = AlertManager()
    sd.personality = log.LongitudinalPersonality.standard
    sd.is_metric = True
    sd.disengage_on_accelerator = False
    sd.always_lateral = AlwaysLateralRuntime(cp) if always_on else None
    self.cs = self.rt.cs
    self.cs.gearShifter = "drive"
    self.cs.standstill = False
    self.cs.vEgo = self.cs.vEgoCluster = 13.0
    for _ in range(int(DRIVE_WAIT_NS / 100_000_000) + 1):
      self.step()
    # engaged by the driver, then the engage alert runs out
    self.cs.buttonEvents = [car.CarState.ButtonEvent(type=ButtonType.mainCruise, pressed=True)]
    self.step()
    self.cs.buttonEvents = []
    assert self.step(EventName.buttonEnable)
    for _ in range(3):
      assert self.step()
    assert self.sound() == AudibleAlert.none

  def step(self, *event_names):
    self.sm.frame += STEP_FRAMES
    self.rt.now_ns += 100_000_000
    self.sd.events = Events()
    for name in event_names:
      self.sd.events.add(name)
    self.sm.logMonoTime.update(dict.fromkeys(self.sm, self.rt.now_ns))
    self.sd.step()
    return self.sd.enabled

  def sound(self):
    return self.sd.AM.current_alert.audible_alert

  def hold(self, *event_names, steps=5):
    """Steps with the events of the held pedal; returns the sounds and alert texts published meanwhile."""
    sounds, texts = [], []
    for _ in range(steps):
      assert not self.step(*event_names)
      sounds.append(self.sound())
      texts.append(self.sd.AM.current_alert.alert_text_1)
    return sounds, texts

  def brake(self, *extra_events, steps=5):
    self.cs.brakePressed = True
    assert not self.step(EventName.pedalPressed, *extra_events)
    first = self.sound()
    sounds, texts = self.hold(EventName.pedalPressed, *extra_events, steps=steps)
    return [first, *sounds], texts


def test_brake_with_auto_engage_plays_no_sound_but_shows_the_alerts(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path)
  sounds, texts = d.brake()
  assert sounds == [AudibleAlert.none] * len(sounds)
  # the always-on steering takeover alert is still shown, only its sound is left out
  assert "Always-on Steering Active" in texts
  assert d.sd.always_lateral.engaged


def test_brake_release_resume_keeps_its_engage_sound(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path)
  d.brake()
  d.cs.brakePressed = False
  assert d.step()
  assert d.sound() == AudibleAlert.engage


def test_brake_without_always_on_steering_plays_no_sound(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path, always_on=False)
  sounds, _ = d.brake()
  assert sounds == [AudibleAlert.none] * len(sounds)


def test_brake_with_auto_engage_off_keeps_both_sounds(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path, auto_engage=False)
  sounds, texts = d.brake()
  assert sounds[0] == AudibleAlert.disengage
  assert AudibleAlert.engage in sounds
  assert "Always-on Steering Active" in texts


def test_cancel_button_with_auto_engage_keeps_the_disengage_sound(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path)
  d.cs.buttonEvents = [car.CarState.ButtonEvent(type=ButtonType.cancel, pressed=True)]
  assert not d.step(EventName.buttonCancel)
  assert d.sound() == AudibleAlert.disengage


def test_brake_together_with_a_fault_keeps_the_sounds(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path)
  sounds, texts = d.brake(EventName.steerTempUnavailable, steps=2)
  assert sounds[0] != AudibleAlert.none
  # always-on steering ends on the fault and warns as before
  assert AudibleAlert.warningSoft in sounds or sounds[0] == AudibleAlert.disengage
  assert not d.sd.always_lateral.engaged


def test_regen_braking_keeps_the_disengage_sound(monkeypatch, tmp_path):
  # only the brake pedal is quiet; the regen paddle ends cruise without a pedal resume at its release
  d = Drive(monkeypatch, tmp_path)
  d.cs.regenBraking = True
  assert not d.step(EventName.pedalPressed)
  assert d.sound() == AudibleAlert.disengage


def test_accelerator_disengage_with_auto_engage_keeps_the_disengage_sound(monkeypatch, tmp_path):
  d = Drive(monkeypatch, tmp_path)
  d.sd.disengage_on_accelerator = True
  d.cs.gasPressed = True
  assert not d.step(EventName.pedalPressed)
  assert d.sound() == AudibleAlert.disengage
