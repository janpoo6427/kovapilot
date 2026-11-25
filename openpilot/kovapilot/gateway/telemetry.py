"""Allowlisted road-scene snapshots from cereal."""

import math
import time

from openpilot.kovapilot.config.cruise_sync_settings import applied_cruise_sync


def _points(line):
  xs, ys, zs = line.x, line.y, line.z
  points = []
  for index in range(min(len(xs), len(ys), len(zs), 33)):
    x, y, z = xs[index], ys[index], zs[index]
    if all(math.isfinite(value) for value in (x, y, z)):
      points.append({"x": x, "y": y, "z": z})
  return points


def _fresh(sm, now_ns, service):
  return sm.valid[service] and sm.alive[service] and 0 <= now_ns - sm.logMonoTime[service] <= 500_000_000


def parked_for_settings(sm, now_ns) -> bool:
  if not (_fresh(sm, now_ns, "carState") and _fresh(sm, now_ns, "selfdriveState")):
    return False
  car = sm["carState"]
  return bool(car.canValid and car.standstill and str(car.gearShifter) == "park" and abs(car.vEgo) < 0.1
              and not sm["selfdriveState"].enabled)


def road_scene(sm, now_ns):
  if not _fresh(sm, now_ns, "carState"):
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
  if _fresh(sm, now_ns, "selfdriveState"):
    scene["engaged"] = sm["selfdriveState"].enabled
  if _fresh(sm, now_ns, "modelV2"):
    model = sm["modelV2"]
    scene["plannedPath"] = _points(model.position)
    for key, index in (("leftLane", 1), ("rightLane", 2)):
      if index < min(len(model.laneLines), len(model.laneLineProbs)) and model.laneLineProbs[index] >= 0.5:
        scene[key] = _points(model.laneLines[index])
  return scene


class CerealTelemetry:
  def __init__(self):
    from openpilot.cereal.messaging import SubMaster

    self.sm = SubMaster(["carState", "selfdriveState", "modelV2", "managerState"])

  def __call__(self):
    self.sm.update(0)
    return road_scene(self.sm, time.monotonic_ns())

  def is_parked(self) -> bool:
    self.sm.update(0)
    return parked_for_settings(self.sm, time.monotonic_ns())

  def applied_cruise_sync(self) -> bool | None:
    self.sm.update(0)
    service = "managerState"
    age = time.monotonic_ns() - self.sm.logMonoTime[service]
    if not self.sm.valid[service] or not 0 <= age <= 2_000_000_000:
      return None
    for process in self.sm[service].processes:
      if process.name == "card" and process.running:
        return applied_cruise_sync(process.pid)
    return None
