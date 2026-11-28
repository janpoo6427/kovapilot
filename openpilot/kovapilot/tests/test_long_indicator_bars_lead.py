"""mici 종방향 표시: 간격 막대는 3칸·4칸 모두 가장 큰 막대 그림 하나로 같은 간격, 앞차 아이콘은 종방향 켜짐+앞차면 계획 출처 상관없이 초록.

mici longitudinal indicator: every distance bar (three or four levels) is the largest bar picture at one pitch,
and the lead car is green whenever engaged with a lead, whatever the plan source.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.cereal import log
from openpilot.selfdrive.ui.mici.onroad import long_indicator
from openpilot.selfdrive.ui.mici.onroad.long_indicator import LongIndicator
from openpilot.selfdrive.ui.ui_state import UIStatus

SOURCES = list(log.LongitudinalPlan.LongitudinalPlanSource.schema.enumerants)
RECT = SimpleNamespace(x=0, y=0, width=72, height=240)
BAR_W, BAR_H = 56, 13  # distance_4
CAR_Y, CAR_H = 100, 27
WHEEL_TOP = RECT.height - 36 - 52 / 2  # 52 px steering wheel centered 36 px from the bottom
FRAMES = 300  # lets the crossfade filters settle


def fake_texture(name, width, height, keep_aspect_ratio=False):
  return SimpleNamespace(name=name.split("/")[-1].removesuffix(".png"), width=width, height=height)


class FakeSM(dict):
  pass


def make_ui(levels=3, personality=0, has_lead=False, source="cruise", alive=True):
  sm = FakeSM(selfdriveState=SimpleNamespace(personality=SimpleNamespace(raw=personality), personalityLevels=levels),
              longitudinalPlan=SimpleNamespace(hasLead=has_lead, longitudinalPlanSource=source))
  sm.alive = {"longitudinalPlan": alive}
  return SimpleNamespace(sm=sm, started=True, personality_param=personality, personality_levels_param=levels)


class FakeFade:
  def __init__(self, status):
    self.status = status

  def weight(self, status):
    return 1.0 if status == self.status else 0.0


def frames(ui, draw, status=UIStatus.ENGAGED, count=1, before=None):
  """Run count frames of draw(indicator); returns the draw calls of the last frame."""
  calls = []

  def record(tex, rect, y, alpha):
    calls.append(SimpleNamespace(name=tex.name, w=tex.width, h=tex.height, y=y, alpha=alpha))

  with (patch.object(long_indicator.gui_app, "texture", side_effect=fake_texture),
        patch.object(long_indicator, "ui_state", ui),
        patch.object(long_indicator, "status_fade", FakeFade(status)),
        patch.object(long_indicator.rl, "get_time", return_value=100.0),
        patch.object(LongIndicator, "_draw_centered", side_effect=record)):
    ind = LongIndicator()
    if before is not None:
      before(ind)
    for _ in range(count):
      calls.clear()
      draw(ind)
  return calls


def bar_calls(levels, personality, **kw):
  return frames(make_ui(levels, personality), lambda ind: ind._draw_distance_bars(RECT, 1.0, True), **kw)


def lead_alphas(status=UIStatus.ENGAGED, **ui_kw):
  calls = frames(make_ui(**ui_kw), lambda ind: ind._draw_lead_car(RECT, 1.0), status=status, count=FRAMES)
  return {c.name: c.alpha for c in calls}


@pytest.mark.parametrize("levels", [3, 4])
def test_every_bar_is_the_largest_picture(levels):
  for personality in range(levels):
    calls = bar_calls(levels, personality)
    white = [c for c in calls if not c.name.endswith("_green")]
    green = [c for c in calls if c.name.endswith("_green")]
    assert [(c.name, c.w, c.h) for c in white] == [("distance_4", BAR_W, BAR_H)] * levels
    assert [(c.name, c.w, c.h) for c in green] == [("distance_4_green", 84, 41)] * levels


@pytest.mark.parametrize("levels", [3, 4])
def test_bars_stack_at_one_pitch_between_car_and_wheel(levels):
  ys = [c.y for c in bar_calls(levels, levels - 1) if not c.name.endswith("_green")]
  pitches = {b - a for a, b in zip(ys, ys[1:], strict=False)}
  assert len(pitches) == 1
  assert pitches.pop() >= BAR_H + 2  # same gap, bars never touch
  assert ys[0] - BAR_H / 2 >= CAR_Y + CAR_H / 2  # below the lead car
  assert ys[-1] + BAR_H / 2 + 1 <= WHEEL_TOP  # above the steering wheel


def test_three_and_four_levels_share_the_pitch_and_top():
  three = [c.y for c in bar_calls(3, 2) if not c.name.endswith("_green")]
  four = [c.y for c in bar_calls(4, 3) if not c.name.endswith("_green")]
  assert three == four[:3]


@pytest.mark.parametrize("levels", [3, 4])
def test_highlight_uses_the_largest_green_picture(levels):
  ui = make_ui(levels, 0)

  def change(ind):
    ind._draw_distance_bars(RECT, 1.0, True)
    ui.sm["selfdriveState"].personality.raw = levels - 1

  calls = frames(ui, lambda ind: ind._draw_distance_bars(RECT, 1.0, True), count=FRAMES, before=change)
  lit = [c for c in calls if c.name.endswith("_green") and c.alpha > 0.5]
  assert [(c.name, c.w, c.h) for c in lit] == [("distance_4_green", 84, 41)]
  assert lit[0].y == max(c.y for c in calls)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("status", [UIStatus.ENGAGED, UIStatus.OVERRIDE])
def test_lead_car_green_whenever_engaged_with_a_lead(source, status):
  a = lead_alphas(status=status, has_lead=True, source=source)
  assert a["car_green"] == pytest.approx(1.0, abs=1e-3)
  assert a["car"] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize("source", SOURCES)
def test_lead_car_white_while_disengaged(source):
  a = lead_alphas(status=UIStatus.DISENGAGED, has_lead=True, source=source)
  assert a["car_green"] == pytest.approx(0.0, abs=1e-6)
  assert a["car"] == pytest.approx(0.9, abs=1e-3)


@pytest.mark.parametrize("kw", [{"has_lead": False}, {"has_lead": True, "alive": False}])
def test_no_lead_is_dim_white(kw):
  a = lead_alphas(**kw)
  assert a["car_green"] == pytest.approx(0.0, abs=1e-3)
  assert a["car"] == pytest.approx(0.35, abs=1e-3)
