"""After a first start the set speed is the current speed, so openpilot continues from the current speed and
acceleration when the driver releases the accelerator (janpoo 11/28 00:23: no sudden acceleration or braking).

While the pedal is held the engagement is in the accelerator override: the planner restarts from aEgo every step,
so the first target after the release continues the acceleration of that moment."""
import numpy as np
import pytest
from opendbc.car.honda.interface import CarInterface
from opendbc.car.honda.values import CAR
from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_MIN
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanner


def _messages(v, a, v_cruise_kph, released):
  model = log.ModelDataV2()
  model.action.desiredAcceleration = float(a + 0.5)  # like longitudinal_maneuvers: the model does not bind
  return {
    "radarState": log.RadarState(),
    "modelV2": model,
    "carState": car.CarState(vEgo=v, aEgo=a, standstill=v < 0.01, vCruise=float(v_cruise_kph)),
    "controlsState": log.ControlsState(longControlState=LongCtrlState.pid if released else LongCtrlState.off),
    "selfdriveState": log.SelfdriveState(personality=log.LongitudinalPersonality.standard),
    "vehicleParameters": log.VehicleParameters(),
  }


@pytest.mark.parametrize("a_driver, hold_s", [(0.5, 2.0), (1.0, 2.0), (1.5, 4.0)])
def test_release_after_a_first_start_continues_without_a_jump(a_driver, hold_s):
  planner = LongitudinalPlanner(CarInterface.get_non_essential_params(CAR.HONDA_CIVIC), init_v=0.0)
  v = 0.0
  for _ in range(round(hold_s / DT_MDL)):
    v += a_driver * DT_MDL
    # set speed follows the held pedal (rounded, at least V_CRUISE_MIN)
    planner.update(_messages(v, a_driver, max(V_CRUISE_MIN, round(v * CV.MS_TO_KPH)), released=False))
  v_set = max(V_CRUISE_MIN, round(v * CV.MS_TO_KPH))
  a = a_driver
  targets, speeds = [], []
  for _ in range(round(5.0 / DT_MDL)):
    planner.update(_messages(v, a, v_set, released=True))
    a = float(planner.output_a_target)
    targets.append(a)
    v = max(0.0, v + a * DT_MDL)
    speeds.append(v * CV.MS_TO_KPH)
  assert abs(targets[0] - a_driver) <= 0.3
  assert max(abs(b - a) for a, b in zip(targets, targets[1:], strict=False)) / DT_MDL <= 3.0
  assert min(targets) >= -0.5
  assert max(targets) <= max(a_driver, 1.0) + 0.1
  assert max(speeds) <= v_set + 4.0
  assert abs(np.mean(speeds[-20:]) - v_set) <= 1.5
