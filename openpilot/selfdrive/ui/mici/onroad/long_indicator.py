import raylib_nb as rl
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.kovapilot.config.longitudinal_personality import clamp_personality
from openpilot.selfdrive.ui.mici.onroad import DIMMED_ICON_ALPHA
from openpilot.selfdrive.ui.mici.onroad.status import status_fade
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets import Widget

HIGHLIGHT_TIME = 2.5  # seconds
# every bar is the largest distance bar (56x13), stacked at one pitch from the stock top bar below the lead car:
# three, or four on a car whose own cruise gap has four levels (kept above the steering wheel)
BAR_SIZE = (56, 13)
BAR_GREEN_SIZE = (84, 41)
BAR_TOP_Y = 122
BAR_PITCH = 16
BAR_Y = {levels: tuple(BAR_TOP_Y + BAR_PITCH * i for i in range(levels)) for levels in (3, 4)}


class LongIndicator(Widget):
  def __init__(self):
    super().__init__()
    self._txt_lead_car = (self._texture('car', 35, 27), self._texture('car_green', 50, 42))
    # one bar picture for every level, never a smaller one stretched
    self._txt_distance = (self._texture('distance_4', *BAR_SIZE), self._texture('distance_4_green', *BAR_GREEN_SIZE))
    self._alpha_filter = FirstOrderFilter(0.0, 0.05, 1 / gui_app.target_fps)
    # crossfade between states, a white and a green filter per icon
    self._lead_car_filters = (FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps), FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps))
    self._distance_filters = [(FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps), FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps))
                              for _ in range(4)]
    self._personality: int | None = None
    self._personality_changed_time = -HIGHLIGHT_TIME
    self._should_draw = False

  @staticmethod
  def _texture(name: str, width: int, height: int) -> rl.Texture:
    return gui_app.texture(f'icons_mici/longitudinal/{name}.png', width, height, keep_aspect_ratio=False)

  def set_should_draw(self, should_draw: bool):
    self._should_draw = should_draw

  def _render(self, rect: rl.Rectangle) -> None:
    # offroad shows the disengaged state
    if not ui_state.has_longitudinal_control or (ui_state.started and not ui_state.received_since_started('selfdriveState')):
      self._personality = None
      self._personality_changed_time = -HIGHLIGHT_TIME
      self._alpha_filter.x = 0.0
      return

    # grayed out while disengaged, with the lit bars at the dimmed icon alpha
    visible = self._should_draw
    dim = 1 - (1 - DIMMED_ICON_ALPHA / 0.9) * status_fade.weight(UIStatus.DISENGAGED)
    alpha = self._alpha_filter.update(visible) * dim
    self._draw_lead_car(rect, alpha)
    self._draw_distance_bars(rect, alpha, visible)

  def _draw_lead_car(self, rect: rl.Rectangle, alpha: float) -> None:
    sm = ui_state.sm
    plan = sm['longitudinalPlan']
    # green whenever a lead is tracked while engaged, whatever the plan source
    has_lead = bool(sm.alive['longitudinalPlan'] and plan.hasLead)
    white_f, green_f = self._lead_car_filters
    white_alpha = white_f.update(0.0 if has_lead else 0.35)
    green_alpha = green_f.update(float(has_lead))
    # static and filled in while disengaged, like the lit distance bars
    disengaged = status_fade.weight(UIStatus.DISENGAGED)
    white_alpha, green_alpha = white_alpha + (0.9 - white_alpha) * disengaged, green_alpha * (1 - disengaged)

    white, green = self._txt_lead_car
    self._draw_centered(white, rect, 100, white_alpha * alpha)
    self._draw_centered(green, rect, 100, green_alpha * alpha)

  def _draw_distance_bars(self, rect: rl.Rectangle, alpha: float, visible: bool) -> None:
    sm = ui_state.sm
    now = rl.get_time()
    if ui_state.started:
      personality = sm['selfdriveState'].personality.raw
      levels = 4 if sm['selfdriveState'].personalityLevels == 4 else 3
    else:
      personality, levels = ui_state.personality_param, ui_state.personality_levels_param
    personality = clamp_personality(personality, levels)
    if self._personality is not None and personality != self._personality:
      self._personality_changed_time = now
    self._personality = personality
    # hold the highlight until the bars show
    if not visible and now - self._personality_changed_time < HIGHLIGHT_TIME:
      self._personality_changed_time = now
    highlight = now - self._personality_changed_time < HIGHLIGHT_TIME

    count = personality + 1
    white, green = self._txt_distance
    bars = zip(BAR_Y[levels], self._distance_filters[:levels], strict=True)
    for i, (y, (active_f, green_f)) in enumerate(bars):
      active = active_f.update(float(i < count))
      green_alpha = green_f.update(float(highlight and i == count - 1))
      self._draw_centered(white, rect, y, (0.35 * (1 - active) + 0.9 * (active - green_alpha)) * alpha)
      self._draw_centered(green, rect, y, green_alpha * alpha)

  @staticmethod
  def _draw_centered(texture: rl.Texture, rect: rl.Rectangle, y: float, alpha: float) -> None:
    pos = rl.Vector2(rect.x + (rect.width - texture.width) / 2, rect.y + y - texture.height / 2)
    rl.draw_texture_ex(texture, pos, 0.0, 1.0, rl.Color(255, 255, 255, round(255 * alpha)))
