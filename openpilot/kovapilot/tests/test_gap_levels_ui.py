"""mici longitudinal indicator: three distance bars as before, four on a car whose own cruise gap has four levels."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.selfdrive.ui.mici.onroad import long_indicator
from openpilot.selfdrive.ui.mici.onroad.long_indicator import LongIndicator

# every bar is distance_4 (56x13) at one pitch, three or four of them
THREE_Y = (122, 138, 154)
FOUR_Y = (122, 138, 154, 170)
RECT = SimpleNamespace(x=0, y=0, width=72, height=240)


def fake_texture(name, width, height, keep_aspect_ratio=False):
  return SimpleNamespace(name=name.split("/")[-1].removesuffix(".png"), width=width, height=height)


def draw(*, started, personality, levels=None, param_levels=3, now=100.0):
  sm = {"selfdriveState": SimpleNamespace(personality=SimpleNamespace(raw=personality), personalityLevels=levels or 0)}
  fake_ui = SimpleNamespace(sm=sm, started=started, personality_param=personality, personality_levels_param=param_levels)
  calls = []

  def record(tex, rect, y, alpha):
    calls.append((tex.name, y))

  with (patch.object(long_indicator.gui_app, "texture", side_effect=fake_texture),
        patch.object(long_indicator, "ui_state", fake_ui),
        patch.object(long_indicator.rl, "get_time", return_value=now),
        patch.object(LongIndicator, "_draw_centered", side_effect=record)):
    LongIndicator()._draw_distance_bars(RECT, 1.0, True)
  return calls


def bars(calls):
  return [(name, y) for name, y in calls if not name.endswith("_green")]


@pytest.mark.parametrize("started", [True, False])
def test_three_levels_draw_three_bars(started):
  for personality in (0, 1, 2):
    assert bars(draw(started=started, personality=personality, levels=3)) == [("distance_4", y) for y in THREE_Y]


def test_old_logs_without_levels_draw_three():
  assert [y for _, y in bars(draw(started=True, personality=2, levels=None))] == list(THREE_Y)


@pytest.mark.parametrize("started", [True, False])
def test_four_levels_draw_four_bars(started):
  drawn = bars(draw(started=started, personality=3, levels=4, param_levels=4))
  assert [name for name, _ in drawn] == ["distance_4"] * 4
  ys = [y for _, y in drawn]
  assert ys == list(FOUR_Y)
  # the fourth bar (13 px tall) ends above the 52 px steering wheel centered 36 px from the bottom
  wheel_top = RECT.height - 36 - 52 / 2
  assert ys[-1] + 13 / 2 + 1 <= wheel_top
