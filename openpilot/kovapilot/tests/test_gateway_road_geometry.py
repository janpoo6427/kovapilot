"""Road view geometry in the KovaLink telemetry frame (schema 2)."""

import math
from types import SimpleNamespace

from openpilot.kovapilot.gateway.telemetry import LANE_MIN_PROB, road_scene


def _line(y0, slope=0., n=33):
  xs = [192 * (i / 32) ** 2 for i in range(n)]
  return SimpleNamespace(x=xs, y=[y0 + slope * x for x in xs], z=[1.2] * n)


def _model(**overrides):
  model = SimpleNamespace(
    frameId=77,
    position=_line(0., slope=0.05),
    laneLines=[_line(-5.4), _line(-1.8), _line(1.7), _line(5.3)],
    laneLineProbs=[0.1, 0.9, 0.6, LANE_MIN_PROB],
    roadEdges=[_line(-7.), _line(6.8)],
    roadEdgeStds=[0.4, 1.9],
  )
  for key, value in overrides.items():
    setattr(model, key, value)
  return model


def _scene(model):
  class Sample(dict):
    valid = {"carState": True, "selfdriveState": True, "modelV2": True}
    alive = valid
    logMonoTime = {"carState": 1_000_000_000, "selfdriveState": 1_000_000_000, "modelV2": 1_000_000_000}

  car = SimpleNamespace(vEgo=20., steeringAngleDeg=0., vCruise=100., gearShifter="drive", leftBlinker=False,
                        rightBlinker=False, brakePressed=False, leftBlindspot=False, rightBlindspot=False)
  return road_scene(Sample(carState=car, selfdriveState=SimpleNamespace(enabled=True), modelV2=model), 1_000_000_001)


def test_model_geometry_is_left_positive_with_lane_slots_and_edge_stds():
  scene = _scene(_model())
  assert scene["schemaVersion"] == 2
  assert scene["modelFrameId"] == 77
  # The model's y points right; the app frame's y points left.
  assert scene["plannedPath"][-1]["y"] == round(-0.05 * 192, 2)
  lanes = scene["laneLines"]
  assert [lane["index"] for lane in lanes] == [1, 2, 3]  # 0.1 is below LANE_MIN_PROB
  assert lanes[0]["points"][0]["y"] == 1.8 and lanes[1]["points"][0]["y"] == -1.7
  assert lanes[0]["probability"] == 0.9
  assert scene["roadEdges"][0][0]["y"] == 7.0 and scene["roadEdges"][1][0]["y"] == -6.8
  assert scene["roadEdgeStds"] == [0.4, 1.9]
  assert "leftLane" not in scene and "rightLane" not in scene
  assert all(set(point) == {"x", "y"} for point in scene["plannedPath"])


def test_bad_model_values_are_dropped_not_raised():
  path = _line(0.)
  path.y[3] = math.nan
  path.x[5] = math.inf
  scene = _scene(_model(position=path, laneLineProbs=[math.nan, 0.9], roadEdgeStds=[0.3]))
  assert len(scene["plannedPath"]) == 31
  assert [lane["index"] for lane in scene["laneLines"]] == [1]
  assert "roadEdges" not in scene  # one edge std missing: no half road boundary

  class Broken:
    frameId = 3

    @property
    def position(self):
      raise RuntimeError("bad message")

  scene = _scene(Broken())
  assert scene is not None and scene["speedMps"] == 20.
  assert "laneLines" not in scene


def test_empty_model_arrays_give_empty_geometry():
  empty = SimpleNamespace(x=[], y=[], z=[])
  scene = _scene(_model(position=empty, laneLines=[], laneLineProbs=[], roadEdges=[], roadEdgeStds=[]))
  assert scene["plannedPath"] == [] and scene["laneLines"] == []
  assert "roadEdges" not in scene
