"""Always-on steering (steering.always_on) shows on screen while openpilot is not engaged."""
from types import SimpleNamespace

import pytest

from openpilot.cereal import log
from openpilot.selfdrive.ui.ui_state import UIState, UIStatus

State = log.SelfdriveState.OpenpilotState


class FakeSM:
  def __init__(self):
    self.msgs = {}
    self.updated = {"selfdriveState": False}
    self.frame = 10

  def __getitem__(self, name):
    return self.msgs[name]


@pytest.fixture
def ui():
  # a separate instance: the module singleton ui_state is shared with other tests
  ui = object.__new__(UIState)
  ui._initialize()
  ui.sm = FakeSM()
  ui.started = True
  ui._started_prev = True
  ui._engaged_prev = False
  return ui


def step(ui, *, enabled: bool, lat_active: bool, state=None):
  ui.sm.msgs["carControl"] = SimpleNamespace(latActive=lat_active)
  ui.sm.msgs["selfdriveState"] = SimpleNamespace(state=state if state is not None else (State.enabled if enabled else State.disabled),
                                                 enabled=enabled)
  ui.sm.updated["selfdriveState"] = True
  ui._update_status()
  ui.sm.updated["selfdriveState"] = False


def test_always_on_steering_without_engagement_is_shown(ui):
  step(ui, enabled=False, lat_active=True)
  assert ui.status == UIStatus.DISENGAGED  # cruise is not engaged
  assert ui.lateral_only


@pytest.mark.parametrize("enabled, lat_active", [(False, False), (True, True), (True, False)])
def test_lateral_only_only_when_steering_without_engagement(ui, enabled, lat_active):
  step(ui, enabled=enabled, lat_active=lat_active)
  assert not ui.lateral_only
  assert ui.status == (UIStatus.ENGAGED if enabled else UIStatus.DISENGAGED)


def test_lateral_only_follows_steering_and_resets_offroad(ui):
  step(ui, enabled=False, lat_active=True)
  step(ui, enabled=False, lat_active=False)  # paused: braking, driver steering, standstill
  assert not ui.lateral_only
  step(ui, enabled=False, lat_active=True)
  assert ui.lateral_only
  ui.started = False
  ui._update_status()
  assert not ui.lateral_only
