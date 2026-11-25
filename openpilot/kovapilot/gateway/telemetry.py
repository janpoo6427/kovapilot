"""Allowlisted road-scene snapshots from cereal."""

import math
import time


def _points(line):
  xs, ys, zs = line.x, line.y, line.z
  points = []
  for index in range(min(len(xs), len(ys), len(zs), 33)):
    x, y, z = xs[index], ys[index], zs[index]
    if all(math.isfinite(value) for value in (x, y, z)):
      points.append({"x": x, "y": y, "z": z})
  return points


def road_scene(sm, now_ns):
  def fresh(service, max_age_ns=500_000_000):
    return sm.valid[service] and sm.alive[service] and 0 <= now_ns - sm.logMonoTime[service] <= max_age_ns

  if not fresh("carState"):
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
  if fresh("selfdriveState"):
    scene["engaged"] = sm["selfdriveState"].enabled
  if fresh("modelV2"):
    model = sm["modelV2"]
    scene["plannedPath"] = _points(model.position)
    for key, index in (("leftLane", 1), ("rightLane", 2)):
      if index < min(len(model.laneLines), len(model.laneLineProbs)) and model.laneLineProbs[index] >= 0.5:
        scene[key] = _points(model.laneLines[index])
  return scene


class CerealTelemetry:
  def __init__(self):
    from openpilot.cereal.messaging import SubMaster

    self.sm = SubMaster(["carState", "selfdriveState", "modelV2"])

  def __call__(self):
    self.sm.update(0)
    return road_scene(self.sm, time.monotonic_ns())
