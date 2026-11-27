"""IONIQ 9 (Hyundai CAN-FD angle): held at the 175 degree angle limit -> steerSaturated at any speed."""
from openpilot.cereal import log
from opendbc.car.structs import car
from opendbc.car.car_helpers import interfaces
from opendbc.car.hyundai.values import CAR as HYUNDAI, CarControllerParams
from opendbc.car.nissan.values import CAR as NISSAN
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.controls.lib.latcontrol_angle import (LatControlAngle, angle_held_at_limit, get_angle_hold_limit,
                                                                steer_saturated_alert)


def _controller(car_name, angle=True):
  CarInterface = interfaces[car_name]
  CP = CarInterface.get_non_essential_params(car_name)
  if angle:
    CP.steerControlType = car.CarParams.SteerControlType.angle
  CI = CarInterface(CP)
  return LatControlAngle(CP.as_reader(), CI, DT_CTRL), VehicleModel(CP), CP.as_reader()


def _run(controller, VM, speed, curvature, frames, pressed=False, limited=True, active=True):
  CS = car.CarState.new_message()
  CS.vEgo = speed
  CS.steeringPressed = pressed
  params = log.VehicleParameters.new_message()
  saturated = []
  for _ in range(frames):
    _, angle_des, lac_log = controller.update(active, CS, VM, params, limited, curvature, False, 0.2)
    saturated.append(lac_log.saturated)
  return angle_des, saturated


def test_hold_limit_only_for_hyundai_angle():
  _, _, CP = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  assert get_angle_hold_limit(CP) == CarControllerParams.ANGLE_LIMITS.STEER_ANGLE_MAX == 175.0
  _, _, torque_CP = _controller(HYUNDAI.KIA_SELTOS, angle=False)
  assert get_angle_hold_limit(torque_CP) is None
  _, _, nissan_CP = _controller(NISSAN.NISSAN_LEAF)
  assert get_angle_hold_limit(nissan_CP) is None
  assert not angle_held_at_limit(None, 300.0)
  assert not angle_held_at_limit(175.0, 177.5)
  assert angle_held_at_limit(175.0, 177.6)
  assert angle_held_at_limit(175.0, -177.6)


def test_low_speed_hold_saturates_after_steer_limit_timer():
  for speed in (5 / 3.6, 10 / 3.6):
    controller, VM, CP = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
    angle_des, saturated = _run(controller, VM, speed, -0.1, 100)
    assert abs(angle_des) > 177.5
    timer_frames = round(CP.steerLimitTimer / DT_CTRL)
    assert not any(saturated[:timer_frames - 1])
    assert all(saturated[timer_frames + 1:])
    # same request, the other way
    controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
    _, saturated = _run(controller, VM, speed, 0.1, 100)
    assert saturated[-1]


def test_low_speed_no_hold_no_saturation():
  controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  # request inside the limit, limited by the controller rate: the usual speed condition applies
  angle_des, saturated = _run(controller, VM, 10 / 3.6, -0.02, 100)
  assert abs(angle_des) < 175.0
  assert not any(saturated)


def test_hold_with_steering_pressed_or_inactive_does_not_saturate():
  controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  _, saturated = _run(controller, VM, 10 / 3.6, -0.1, 100, pressed=True)
  assert not any(saturated)
  controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  _, saturated = _run(controller, VM, 10 / 3.6, -0.1, 100, active=False)
  assert not any(saturated)


def test_other_angle_cars_keep_speed_condition():
  controller, VM, _ = _controller(NISSAN.NISSAN_LEAF)
  _, saturated = _run(controller, VM, 10 / 3.6, -0.1, 300)
  assert not any(saturated)


def test_high_speed_saturation_unchanged():
  controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  _, saturated = _run(controller, VM, 20.0, -0.002, 100, limited=True)
  assert saturated[-1]
  controller, VM, _ = _controller(HYUNDAI.HYUNDAI_IONIQ_9)
  _, saturated = _run(controller, VM, 20.0, -0.002, 100, limited=False)
  assert not any(saturated)


def test_steer_saturated_alert():
  # held at the limit: no lateral accel / undershooting condition
  assert steer_saturated_alert(True, False, False, True)
  # otherwise the usual undershooting while turning
  assert steer_saturated_alert(True, True, True, False)
  assert not steer_saturated_alert(True, True, False, False)
  assert not steer_saturated_alert(True, False, True, False)
  assert not steer_saturated_alert(False, True, True, True)
