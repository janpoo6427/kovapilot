"""Distance setting pickers (tici toggles, mici driving): three choices as before, a fourth (moreRelaxed) only on a car
whose own cruise gap has four levels."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.selfdrive.ui import ui_state as ui_state_module
from openpilot.selfdrive.ui.layouts.settings import toggles
from openpilot.selfdrive.ui.mici.layouts.settings import driving
from openpilot.selfdrive.ui.mici.widgets.button import BigMultiParamToggle
from openpilot.system.ui.widgets import list_view


class FakeParams:
  def __init__(self, personality):
    self.personality = personality

  def get(self, key, return_default=False):
    assert key == "LongitudinalPersonality"
    return self.personality


def ui(started=False, live_levels=0, param_levels=3):
  state = object.__new__(ui_state_module.UIState)
  state.started = started
  state.sm = {"selfdriveState": SimpleNamespace(personalityLevels=live_levels)}
  state.personality_levels_param = param_levels
  return state


@pytest.mark.parametrize(("started", "live", "param", "expected"), [
  (False, 0, 3, 3), (False, 4, 3, 3), (False, 0, 4, 4),   # offroad: what is remembered for this car
  (True, 3, 3, 3), (True, 0, 3, 3), (True, 4, 3, 4),      # onroad: the car reported level 4 this drive
  (True, 0, 4, 4),                                         # onroad before the first selfdriveState
])
def test_levels_offered(started, live, param, expected):
  assert ui(started, live, param).personality_levels == expected


def tici_row(personality):
  with patch.object(list_view.gui_app, "font", return_value=None):
    action = list_view.MultipleButtonAction(toggles.PERSONALITY_BUTTONS[:3], toggles.PERSONALITY_BUTTON_WIDTH[3], min(personality, 2))
  layout = SimpleNamespace(_long_personality_setting=SimpleNamespace(action_item=action), _params=FakeParams(personality))
  return layout, action


def test_tici_three_level_car_keeps_the_three_buttons():
  layout, action = tici_row(2)
  with patch.object(toggles, "ui_state", SimpleNamespace(personality_levels=3)):
    toggles.TogglesLayout._update_personality_levels(layout)
  assert [label() for label in action.buttons] == ["Aggressive", "Standard", "Relaxed"]
  assert (action.button_width, action.rect.width, action.selected_button) == (255, 805, 2)


def test_tici_four_level_car_gets_a_fourth_button_and_back():
  layout, action = tici_row(3)
  assert action.selected_button == 2
  with patch.object(toggles, "ui_state", SimpleNamespace(personality_levels=4)):
    toggles.TogglesLayout._update_personality_levels(layout)
  assert [label() for label in action.buttons] == ["Aggressive", "Standard", "Relaxed", "Relaxed+"]
  assert (action.button_width, action.rect.width, action.selected_button) == (225, 960, 3)
  with patch.object(toggles, "ui_state", SimpleNamespace(personality_levels=3)):
    toggles.TogglesLayout._update_personality_levels(layout)
  assert len(action.buttons) == 3
  assert (action.rect.width, action.selected_button) == (805, 2)


def mici_toggle(personality):
  toggle = object.__new__(BigMultiParamToggle)
  toggle._options = driving.PERSONALITY_OPTIONS[:3]
  toggle._param = "LongitudinalPersonality"
  toggle._params = FakeParams(personality)
  toggle.value = toggle._options[min(personality, 2)]
  return toggle


@pytest.fixture
def plain_set_value():
  with patch.object(BigMultiParamToggle, "set_value", lambda self, value: setattr(self, "value", value)):
    yield


def test_mici_three_level_car_keeps_three_options(plain_set_value):
  toggle = mici_toggle(1)
  with patch.object(driving, "ui_state", SimpleNamespace(personality_levels=3)):
    driving.DrivingLayoutMici._update_personality_levels(SimpleNamespace(_personality_toggle=toggle))
  assert toggle._options == ["aggressive", "standard", "relaxed"]
  assert toggle.value == "standard"


def test_mici_four_level_car_gets_a_fourth_option_and_back(plain_set_value):
  toggle = mici_toggle(3)
  assert toggle.value == "relaxed"
  layout = SimpleNamespace(_personality_toggle=toggle)
  with patch.object(driving, "ui_state", SimpleNamespace(personality_levels=4)):
    driving.DrivingLayoutMici._update_personality_levels(layout)
  assert toggle._options == ["aggressive", "standard", "relaxed", "more relaxed"]
  assert toggle.value == "more relaxed"
  with patch.object(driving, "ui_state", SimpleNamespace(personality_levels=3)):
    driving.DrivingLayoutMici._update_personality_levels(layout)
  assert toggle._options == ["aggressive", "standard", "relaxed"]
  assert toggle.value == "relaxed"
