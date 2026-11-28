"""Allowlisted road-scene snapshots from cereal."""

import math
import time

from openpilot.kovapilot.config.auto_engage_settings import applied_auto_engage
from openpilot.kovapilot.config.cruise_sync_settings import applied_cruise_sync
from openpilot.kovapilot.config.curve_slowdown_settings import applied_curve_slowdown, applied_nav_turn_slowdown
from openpilot.kovapilot.config.parked_setting import fresh_message, parked_and_disengaged, parked_for_settings as parked_for_settings
from openpilot.kovapilot.gateway.navigation_status import (NAVIGATION_FRESH_NS, AutoLaneChangeSetting,
                                                          auto_lane_change_fields, navigation_fields)
from openpilot.kovapilot.gateway.radar_objects import RadarObjects


LANE_MIN_PROB = 0.2  # below this the app hides even an already shown ego lane line


def _points(line, limit=33):
  """Display points in the app frame: x forward, y left (the model's y points right), cm."""
  try:
    xs, ys = line.x, line.y
    points = []
    for index in range(min(len(xs), len(ys), limit)):
      x, y = xs[index], ys[index]
      if math.isfinite(x) and math.isfinite(y):
        points.append({"x": round(x, 2), "y": round(-y, 2)})
    return points
  except Exception:
    return []


def _model_fields(model):
  """Planned path, lane lines (index, probability) and road edges (std); display only, never raises."""
  fields = {}
  try:
    fields["modelFrameId"] = int(model.frameId)
    fields["plannedPath"] = _points(model.position)
    probs = list(model.laneLineProbs)
    lanes = []
    for index, line in enumerate(list(model.laneLines)[:4]):
      prob = probs[index] if index < len(probs) else 0.
      if math.isfinite(prob) and prob >= LANE_MIN_PROB:
        points = _points(line)
        if len(points) >= 2:
          lanes.append({"index": index, "probability": round(min(max(prob, 0.), 1.), 3), "points": points})
    fields["laneLines"] = lanes
    stds = list(model.roadEdgeStds)
    edges, edge_stds = [], []
    for index, edge in enumerate(list(model.roadEdges)[:2]):
      points = _points(edge)
      std = stds[index] if index < len(stds) else float("inf")
      if len(points) >= 2 and math.isfinite(std):
        edges.append(points)
        edge_stds.append(round(std, 3))
    if len(edges) == 2:
      fields["roadEdges"] = edges
      fields["roadEdgeStds"] = edge_stds
  except Exception:
    return {key: value for key, value in fields.items() if key == "plannedPath"}
  return fields


def road_scene(sm, now_ns, auto_lane_change_setting: int | None = None, radar_objects=None):
  if not fresh_message(sm, now_ns, "carState"):
    return None
  car = sm["carState"]
  if not math.isfinite(car.vEgo):
    return None
  gear = str(car.gearShifter)
  scene = {
    "schemaVersion": 2, "monotonicTimeNanos": now_ns, "speedMps": car.vEgo,
    "gear": gear if gear in ("park", "reverse", "neutral", "drive") else "unknown",
    "leftBlinker": car.leftBlinker, "rightBlinker": car.rightBlinker, "brakeActive": car.brakePressed,
  }
  if math.isfinite(car.steeringAngleDeg):
    scene["steeringAngleDegrees"] = car.steeringAngleDeg
  # the set speed the driver set and the cluster shows (vCruise is the planner set speed in wheel speed units)
  if math.isfinite(car.vCruiseCluster) and 0 < car.vCruiseCluster < 250:
    scene["setSpeedMps"] = car.vCruiseCluster / 3.6
  if fresh_message(sm, now_ns, "selfdriveState"):
    scene["engaged"] = sm["selfdriveState"].enabled
  if fresh_message(sm, now_ns, "modelV2"):
    scene.update(_model_fields(sm["modelV2"]))
  if _fresh(sm, now_ns, "kovaNavigation", NAVIGATION_FRESH_NS):
    scene.update(navigation_fields(sm["kovaNavigation"], now_ns))
  scene.update(auto_lane_change_fields(sm["modelV2"] if fresh_message(sm, now_ns, "modelV2") else None, car,
                                       auto_lane_change_setting))
  # radar vehicles for the road view; left out without current, error free radar tracks (radar_objects.py)
  objects = radar_objects(sm, now_ns) if radar_objects is not None else None
  if objects is not None:
    scene["objects"] = objects
  return scene


def _fresh(sm, now_ns, service, window_ns) -> bool:
  try:
    return bool(sm.valid[service] and sm.alive[service] and 0 <= now_ns - sm.logMonoTime[service] <= window_ns)
  except KeyError:
    return False


class CerealTelemetry:
  def __init__(self):
    from openpilot.cereal.messaging import SubMaster

    self.sm = SubMaster(["carState", "carControl", "selfdriveState", "modelV2", "managerState", "kovaNavigation",
                         "radarTracks", "radarState"])
    self.auto_lane_change_setting = AutoLaneChangeSetting()
    self.radar_objects = RadarObjects()

  def __call__(self):
    self.sm.update(0)
    return road_scene(self.sm, time.monotonic_ns(), self.auto_lane_change_setting(), self.radar_objects)

  def is_parked(self) -> bool:
    self.sm.update(0)
    return parked_for_settings(self.sm, time.monotonic_ns())

  def is_parked_and_disengaged(self) -> bool:
    self.sm.update(0)
    return parked_and_disengaged(self.sm, time.monotonic_ns())

  def applied_cruise_sync(self) -> int | None:
    return applied_cruise_sync(self._running_pid("card"))

  def applied_curve_slowdown(self) -> int | str | None:
    return applied_curve_slowdown(self._running_pid("plannerd"))

  def applied_nav_turn_slowdown(self) -> int | str | None:
    return applied_nav_turn_slowdown(self._running_pid("plannerd"))

  def applied_auto_engage(self) -> dict | None:
    return applied_auto_engage(self._running_pid("selfdrived"), time.monotonic_ns())

  def _running_pid(self, name: str) -> int | None:
    self.sm.update(0)
    service = "managerState"
    age = time.monotonic_ns() - self.sm.logMonoTime[service]
    if not self.sm.valid[service] or not 0 <= age <= 2_000_000_000:
      return None
    for process in self.sm[service].processes:
      if process.name == name and process.running:
        return process.pid
    return None
