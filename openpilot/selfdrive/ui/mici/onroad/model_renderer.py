import numpy as np
import raylib_nb as rl
from openpilot.cereal import log
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.kovapilot.display.radar_vehicles import RadarSideVehicles, radar_used
from openpilot.selfdrive.controls.radard import RADAR_TO_CAMERA
from openpilot.selfdrive.locationd.calibrationd import HEIGHT_INIT
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.selfdrive.ui.mici.onroad import blend_colors
from openpilot.selfdrive.ui.mici.onroad.status import status_fade
from openpilot.selfdrive.ui.onroad.model_renderer import ModelRenderer as BaseModelRenderer, CLIP_MARGIN, MAX_DRAW_DISTANCE, STOCK_PATH_GRADIENT
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.polygon import Gradient, draw_polygon

LEAD_BAR_LENGTH = 10.0  # px
LEAD_BAR_WIDTH = 1.8  # m
LANE_EDGES = (-1.8, 1.8)  # m, a 3.6 m lane for a stop bar without lane lines
LANE_LINE_PROB = 0.5

# KOVA: radar vehicles in the side lanes are drawn like lead bars, dimmer so the lead stays the one to follow
SIDE_VEHICLE_OPACITY = 0.6  # of the lead bar opacity

STOP_SPEED = 1.0  # m/s, planned speed that counts as stopping
STOP_APPEAR_DISTANCE = (8.0, 50.0)  # m
STOP_LEAD_MARGIN = 15.0  # m, a lead this close past the stop is what it's stopping for

LANE_LINE_COLORS = {
  UIStatus.DISENGAGED: rl.Color(200, 200, 200, 255),
  UIStatus.OVERRIDE: rl.Color(255, 255, 255, 255),
  UIStatus.ENGAGED: rl.Color(0, 255, 64, 255),
}


def _faded(gradient: Gradient, alpha: float) -> Gradient:
  if alpha >= 1:
    return gradient
  return Gradient(gradient.start, gradient.end, [rl.Color(c.r, c.g, c.b, round(c.a * alpha)) for c in gradient.colors], gradient.stops)


class LeadVehicle:
  def __init__(self):
    self.bar = np.empty((0, 2), dtype=np.float32)
    self.d_filter = FirstOrderFilter(0.0, 0.2, 1 / gui_app.target_fps, initialized=False)
    self.y_filter = FirstOrderFilter(0.0, 0.2, 1 / gui_app.target_fps, initialized=False)
    self.fade_filter = FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps)


class StopPoint:
  """Where the model plans to stop with no lead to stop behind, e.g. at a stop sign or red light"""
  def __init__(self):
    self.bar = np.empty((0, 2), dtype=np.float32)
    self.d = 0.0
    self.planned_d: float | None = None
    self.visible = False
    # the planned stop comes and goes frame to frame, so it needs to persist before showing and before hiding
    self._planning, self._planning_filter = False, FirstOrderFilter(0.0, 0.3, 1 / gui_app.target_fps)
    self._behind_lead, self._behind_lead_filter = False, FirstOrderFilter(0.0, 0.3, 1 / gui_app.target_fps)
    # lateral offsets of the bar's edges from the path, at the lane lines
    self.edges = [FirstOrderFilter(y, 0.2, 1 / gui_app.target_fps, initialized=False) for y in LANE_EDGES]
    self.fade_filter = FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps)

  def update(self, model, model_updated: bool, v_ego: float, lead_distances: list[float]) -> bool:
    # the stop point is fixed on the road, so move it with the car between model frames
    self.d = max(self.d - v_ego / gui_app.target_fps, 0.0)
    if model_updated:
      self.planned_d = self._get_planned_stop(model)
      if self.planned_d is not None:
        self.d = self.d + 0.3 * (self.planned_d - self.d) if self.visible else self.planned_d

    self._planning = hysteresis(self._planning_filter.update(self.planned_d is not None), self._planning, 0.6, 0.2)
    # the lead bar already shows stopping behind a lead
    behind_lead = any(d < self.d + RADAR_TO_CAMERA + STOP_LEAD_MARGIN for d in lead_distances)
    self._behind_lead = hysteresis(self._behind_lead_filter.update(behind_lead), self._behind_lead, 0.6, 0.1)
    # appear out ahead, not right in front of the car while creeping or stopped,
    # and not so far out that a lead to stop behind may not be detected yet
    appear = STOP_APPEAR_DISTANCE[0] < self.d < STOP_APPEAR_DISTANCE[1]
    self.visible = self._planning and not self._behind_lead and (self.visible or appear)
    return self.visible

  @staticmethod
  def _get_planned_stop(model) -> float | None:
    vx, px = np.array(model.velocity.x), np.array(model.position.x)
    stopped = np.flatnonzero(vx < STOP_SPEED)
    if not stopped.size or len(vx) != len(px):
      return None
    i = stopped[0]
    return 0.0 if i == 0 else float(np.interp(STOP_SPEED, vx[[i, i - 1]], px[[i, i - 1]]))


def hysteresis(x: float, state: bool, on: float, off: float) -> bool:
  return x > on or (state and x > off)


class ModelRenderer(BaseModelRenderer):
  LANE_LINE_WIDTHS = (0.12, 0.16, 0.16, 0.16)
  ROAD_EDGE_WIDTH = 0.16

  def __init__(self):
    super().__init__()
    self._lead_bars = [LeadVehicle(), LeadVehicle()]
    self._stop_point = StopPoint()
    self._side_vehicles = RadarSideVehicles()
    self._side_bars: dict[int, LeadVehicle] = {}
    self._lead_track_ids: set[int] = set()
    self._side_list = []
    self._torque_filter = FirstOrderFilter(0, 0.1, 1 / gui_app.target_fps)
    self._visible = 0.0

  def _draw_offset(self) -> np.ndarray:
    # car space transform maps to rect-origin space
    return np.array([self._rect.x, self._rect.y], dtype=np.float32)

  def _render(self, rect: rl.Rectangle):
    sm = ui_state.sm

    self._torque_filter.update(-ui_state.sm['carOutput'].actuatorsOutput.torque)

    # Check if data is up-to-date
    if not ui_state.received_since_started("extrinsicsCalibration") or not ui_state.received_since_started("modelV2"):
      return

    # Set up clipping region
    self._clip_region = rl.Rectangle(
      rect.x - CLIP_MARGIN, rect.y - CLIP_MARGIN, rect.width + 2 * CLIP_MARGIN, rect.height + 2 * CLIP_MARGIN
    )

    # Update state
    extrinsics_calibration = sm['extrinsicsCalibration']
    self._path_offset_z = extrinsics_calibration.height[0] if extrinsics_calibration.height else HEIGHT_INIT[0]

    if sm.updated['carParams']:
      self._longitudinal_control = sm['carParams'].openpilotLongitudinalControl

    model = sm['modelV2']
    radar_state = sm['radarState'] if sm.valid['radarState'] else None
    lead_one = radar_state.leadOne if radar_state else None
    render_lead_indicator = self._longitudinal_control and radar_state is not None

    # Update model data when needed
    model_updated = sm.updated['modelV2']
    if model_updated or sm.updated['radarState'] or self._transform_dirty:
      if model_updated:
        self._update_raw_points(model)

      path_x_array = self._path.raw_points[:, 0]
      if path_x_array.size == 0:
        return

      self._update_model(lead_one, path_x_array)
      self._transform_dirty = False

    # lane lines and path fade out while disengaged
    self._visible = 1 - status_fade.weight(UIStatus.DISENGAGED)
    if self._visible > 1e-2:
      self._draw_lane_lines()
      self._draw_path()

    if render_lead_indicator:
      self._update_lead_bars(sm)
    else:
      self._lead_bars = [LeadVehicle(), LeadVehicle()]
      self._stop_point = StopPoint()
      self._lead_track_ids = set()

    # KOVA: side-lane radar vehicles, under the lead bars (with or without openpilot longitudinal)
    self._update_side_vehicles(sm)
    self._draw_side_vehicles()
    if render_lead_indicator:
      self._draw_lead_indicator()

  def _update_lead_bars(self, sm):
    plan = sm['longitudinalPlan']
    if plan.longitudinalPlanSource == log.LongitudinalPlan.LongitudinalPlanSource.e2e and len(sm['modelV2'].leadsV3) > 1:
      leads = [(lead.prob > 0.5, lead.x[0], -lead.y[0]) for lead in list(sm['modelV2'].leadsV3)[:2]]
    else:
      radar = sm['radarState']
      leads = [(lead.present, lead.dRel + RADAR_TO_CAMERA, lead.yRel) for lead in (radar.leadOne, radar.leadTwo)]

    # both leads can be the same vehicle
    if leads[0][0] and abs(leads[1][1] - leads[0][1]) < 3.0:
      leads[1] = (False, 0.0, 0.0)
    # KOVA: radar tracks drawn as a lead bar are not drawn again as side vehicles
    self._lead_track_ids = set()
    if plan.longitudinalPlanSource != log.LongitudinalPlan.LongitudinalPlanSource.e2e or len(sm['modelV2'].leadsV3) <= 1:
      radar = sm['radarState']
      self._lead_track_ids = {int(lead.radarTrackId) for lead, (present, _, _) in zip((radar.leadOne, radar.leadTwo), leads, strict=True)
                              if present and lead.radar}

    ss, cs = sm['selfdriveState'], sm['carState']
    # braking disengages without making openpilot unavailable
    available = ss.enabled or ss.engageable or cs.brakePressed
    lane = self._path.raw_points
    opacity = 0.8 - 0.4 * status_fade.weight(UIStatus.DISENGAGED)
    for lead, (present, d_rel, y_rel) in zip(self._lead_bars, leads, strict=True):
      visible = available and present and d_rel < MAX_DRAW_DISTANCE and len(lane) > 0
      # snap to a new vehicle instead of sliding over
      if not visible or abs(y_rel - lead.y_filter.x) > 3.0:
        lead.d_filter.initialized = lead.y_filter.initialized = False
      lead.fade_filter.update(opacity if visible else 0.0)
      if visible:
        lead.bar = self._get_lead_bar(lane, lead.d_filter.update(d_rel), lead.y_filter.update(y_rel))

    # the radar is at the front of the car, which is what stops at the planned stop
    lead_distances = [d_rel for present, d_rel, _ in leads if present]
    planning_stop = self._stop_point.update(sm['modelV2'], sm.updated['modelV2'], cs.vEgo, lead_distances)
    d_stop = self._stop_point.d + RADAR_TO_CAMERA
    visible = available and planning_stop and d_stop < MAX_DRAW_DISTANCE and len(lane) > 0
    # brighter than a lead bar to tell them apart
    self._stop_point.fade_filter.update(opacity + 0.2 if visible else 0.0)
    if visible:
      # a planned stop spans the lane instead of a car
      path_y = np.interp(d_stop, lane[:, 0], lane[:, 1])
      edges = []
      for f, ll, prob, default in zip(self._stop_point.edges, self._lane_lines[1:3], self._lane_line_probs[1:3], LANE_EDGES, strict=True):
        line = ll.raw_points
        y = np.interp(d_stop, line[:, 0], line[:, 1]) - path_y if prob > LANE_LINE_PROB and len(line) else default
        edges.append(f.update(y))
      self._stop_point.bar = self._get_lead_bar(lane, d_stop, 0.0, edges)

  def _update_side_vehicles(self, sm):
    ss, cs = sm['selfdriveState'], sm['carState']
    available = ss.enabled or ss.engageable or cs.brakePressed
    lane = self._path.raw_points
    # the lane test runs on new radar or model data only (20 Hz), the bars move every frame
    if sm.updated['radarTracks'] or sm.updated['modelV2']:
      self._side_list = self._side_vehicles.update(sm, sm['modelV2'], cs.vEgo, radar_used(sm['carParams']), self._lead_track_ids)
    current = {v.track_id: v for v in self._side_list} if available and len(lane) > 0 else {}
    opacity = SIDE_VEHICLE_OPACITY * (0.8 - 0.4 * status_fade.weight(UIStatus.DISENGAGED))
    for track_id, v in current.items():
      bar = self._side_bars.setdefault(track_id, LeadVehicle())
      d_rel = v.d_rel + RADAR_TO_CAMERA
      # snap to a new vehicle instead of sliding over (as the lead bars)
      if abs(v.y_rel - bar.y_filter.x) > 3.0:
        bar.d_filter.initialized = bar.y_filter.initialized = False
      bar.fade_filter.update(opacity)
      bar.bar = self._get_lead_bar(lane, bar.d_filter.update(d_rel), bar.y_filter.update(v.y_rel))
    # lost tracks fade out where they were
    for track_id in [i for i in self._side_bars if i not in current]:
      bar = self._side_bars[track_id]
      if bar.fade_filter.update(0.0) < 1e-2:
        del self._side_bars[track_id]

  def _draw_side_vehicles(self):
    offset = self._draw_offset()
    for bar in self._side_bars.values():
      if len(bar.bar):
        draw_polygon(self._rect, bar.bar + offset, rl.Color(255, 255, 255, int(255 * bar.fade_filter.x)))

  def _get_lead_bar(self, lane, d_rel, y_rel, edges=(-LEAD_BAR_WIDTH / 2, LEAD_BAR_WIDTH / 2)):
    # bar on the road behind the lead, following the lane
    x = np.array([d_rel, d_rel - min(6.0, 0.25 * d_rel)])
    y = np.interp(x, lane[:, 0], lane[:, 1]) - np.interp(d_rel, lane[:, 0], lane[:, 1]) - y_rel
    z = np.interp(x, self._path.raw_points[:, 0], self._path.raw_points[:, 2]) + self._path_offset_z
    corners = np.vstack((np.column_stack((x, y + edges[0], z)), np.column_stack((x, y + edges[1], z))[::-1]))
    pts = self._car_space_transform @ corners.T
    bar = (pts[:2] / pts[2]).T

    far, near = bar[[0, 3]], bar[[1, 2]]
    length = np.linalg.norm(near.mean(axis=0) - far.mean(axis=0))
    bar[[1, 2]] = far + (near - far) * np.clip(length, 3.0, LEAD_BAR_LENGTH) / length
    return bar.astype(np.float32)

  def _get_ll_color(self, prob: float, adjacent: bool, left: bool):
    alpha = np.clip(prob, 0.0, 0.7) * self._visible
    if adjacent:
      _base_color = status_fade.color(LANE_LINE_COLORS)
      color = rl.Color(_base_color.r, _base_color.g, _base_color.b, int(alpha * 255))

      # turn adjacent lls orange if torque is high
      torque = self._torque_filter.x
      high_torque = abs(torque) > 0.6
      if high_torque and (left == (torque > 0)):
        color = blend_colors(
          color,
          rl.Color(255, 115, 0, int(alpha * 255)),  # orange
          np.interp(abs(torque), [0.6, 0.8], [0.0, 1.0])
        )
    else:
      color = rl.Color(255, 255, 255, int(alpha * 255))

    return color

  def _draw_lane_lines(self):
    """Draw lane lines and road edges. Two closest lines should be green (lane line or road edges)."""
    offset = self._draw_offset()

    for i, lane_line in enumerate(self._lane_lines):
      if lane_line.projected_points.size == 0:
        continue

      color = self._get_ll_color(float(self._lane_line_probs[i]), i in (1, 2), i in (0, 1))
      draw_polygon(self._rect, lane_line.projected_points + offset, color)

    for i, road_edge in enumerate(self._road_edges):
      if road_edge.projected_points.size == 0:
        continue

      # if closest lane lines are not confident, make road edges green
      color = self._get_ll_color(float(1.0 - self._road_edge_stds[i]), float(self._lane_line_probs[i + 1]) < 0.25, i == 0)
      draw_polygon(self._rect, road_edge.projected_points + offset, color)

  def _draw_path(self):
    if not self._path.projected_points.size:
      return

    path_pts = self._path.projected_points + self._draw_offset()

    if not self._longitudinal_control:
      draw_polygon(self._rect, path_pts, gradient=_faded(STOCK_PATH_GRADIENT, self._visible))
    elif len(self._path_gradient.colors) > 1:
      draw_polygon(self._rect, path_pts, gradient=_faded(self._path_gradient, self._visible))
    else:
      draw_polygon(self._rect, path_pts, rl.Color(255, 255, 255, round(30 * self._visible)))

  def _draw_lead_indicator(self):
    offset = self._draw_offset()
    for lead in (*self._lead_bars, self._stop_point):
      draw_polygon(self._rect, lead.bar + offset, rl.Color(255, 255, 255, int(255 * lead.fade_filter.x)))
