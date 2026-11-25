"""Allowlisted road-scene snapshots from cereal."""

import math
import time

from openpilot.kovapilot.config.cruise_sync_settings import applied_cruise_sync
from openpilot.kovapilot.config.curve_slowdown_settings import applied_curve_slowdown
from openpilot.kovapilot.config.parked_setting import fresh_message, parked_for_settings as parked_for_settings


def _points(line):
  xs, ys, zs = line.x, line.y, line.z
  points = []
  for index in range(min(len(xs), len(ys), len(zs), 33)):
    x, y, z = xs[index], ys[index], zs[index]
    if all(math.isfinite(value) for value in (x, y, z)):
      points.append({"x": x, "y": y, "z": z})
  return points


def road_scene(sm, now_ns):
  if not fresh_message(sm, now_ns, "carState"):
    return None
  car = sm["carState"]
  if not math.isfinite(car.vEgo):
    return None
  gear = str(car.gearShifter)
  scene = {
    "schemaVersion": 1, "monotonicTimeNanos": now_ns, "speedMps": car.vEgo,
    "gear": gear if gear in ("park", "reverse", "neutral", "drive") else "unknown",
    "leftBlinker": car.leftBlinker, "rightBlinker": car.rightBlinker, "brakeActive": car.brakePressed,
  }
  if math.isfinite(car.steeringAngleDeg):
    scene["steeringAngleDegrees"] = car.steeringAngleDeg
  if math.isfinite(car.vCruise) and 0 < car.vCruise < 250:
    scene["setSpeedMps"] = car.vCruise / 3.6
  if fresh_message(sm, now_ns, "selfdriveState"):
    scene["engaged"] = sm["selfdriveState"].enabled
  if fresh_message(sm, now_ns, "modelV2"):
    model = sm["modelV2"]
    scene["plannedPath"] = _points(model.position)
    for key, index in (("leftLane", 1), ("rightLane", 2)):
      if index < min(len(model.laneLines), len(model.laneLineProbs)) and model.laneLineProbs[index] >= 0.5:
        scene[key] = _points(model.laneLines[index])
  return scene


class CerealTelemetry:
  def __init__(self):
    from openpilot.cereal.messaging import SubMaster

    self.sm = SubMaster(["carState", "carControl", "selfdriveState", "modelV2", "managerState"])

  def __call__(self):
    self.sm.update(0)
    return road_scene(self.sm, time.monotonic_ns())

  def is_parked(self) -> bool:
    self.sm.update(0)
    return parked_for_settings(self.sm, time.monotonic_ns())

  def is_parked_and_disengaged(self) -> bool:
    self.sm.update(0)
    now = time.monotonic_ns()
    if not parked_for_settings(self.sm, now) or not fresh_message(self.sm, now, "carControl"):
      return False
    control = self.sm["carControl"]
    return not (control.enabled or control.latActive or control.longActive or self.sm["selfdriveState"].active)

  def applied_cruise_sync(self) -> int | None:
    return applied_cruise_sync(self._running_pid("card"))

  def applied_curve_slowdown(self) -> int | str | None:
    return applied_curve_slowdown(self._running_pid("plannerd"))

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
