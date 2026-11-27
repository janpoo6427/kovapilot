import math

from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.selfdrive.controls.lib.latcontrol import LatControl

# TODO This is speed dependent
STEER_ANGLE_SATURATION_THRESHOLD = 2.5  # Degrees


def get_angle_hold_limit(CP):
  # kovapilot: Hyundai CAN-FD angle cars (IONIQ 9) hold the command at the opendbc angle limit (175 degrees)
  # instead of releasing, so a request past it is saturation at any speed.
  if CP.brand == "hyundai" and CP.steerControlType == car.CarParams.SteerControlType.angle:
    from opendbc.car.hyundai.values import CarControllerParams
    return CarControllerParams.ANGLE_LIMITS.STEER_ANGLE_MAX
  return None


def angle_held_at_limit(hold_limit, angle_steers_des):
  # the sent angle can not go past the limit, so a request this far past it is held at the limit
  return hold_limit is not None and abs(angle_steers_des) > hold_limit + STEER_ANGLE_SATURATION_THRESHOLD


def steer_saturated_alert(saturated, undershooting, turning, held_at_limit):
  # steerSaturated: held at the angle limit, or the usual undershooting while turning
  return saturated and (held_at_limit or (undershooting and turning))


class LatControlAngle(LatControl):
  def __init__(self, CP, CI, dt):
    super().__init__(CP, CI, dt)
    self.sat_check_min_speed = 5.
    self.use_steer_limited_by_safety = CP.brand in ("tesla", "hyundai")
    self.angle_hold_limit = get_angle_hold_limit(CP)

  def update(self, active, CS, VM, params, steer_limited_by_safety, desired_curvature, curvature_limited, lat_delay):
    angle_log = log.ControlsState.LateralAngleState.new_message()

    if not active:
      angle_log.active = False
      angle_steers_des = float(CS.steeringAngleDeg)
    else:
      angle_log.active = True
      angle_steers_des = math.degrees(VM.get_steer_from_curvature(-desired_curvature, CS.vEgo, params.roll))
      angle_steers_des += params.angleOffsetDeg

    if self.use_steer_limited_by_safety:
      # these cars' carcontrollers calculate max lateral accel and jerk, so we can rely on carOutput for saturation
      angle_control_saturated = steer_limited_by_safety
    else:
      # for cars which use a method of limiting torque such as a torque signal (Nissan and Toyota)
      # or relying on EPS (Ford Q3), carOutput does not capture maxing out torque  # TODO: this can be improved
      angle_control_saturated = abs(angle_steers_des - CS.steeringAngleDeg) > STEER_ANGLE_SATURATION_THRESHOLD
    held_at_limit = active and angle_held_at_limit(self.angle_hold_limit, angle_steers_des)
    angle_log.saturated = bool(self._check_saturation(angle_control_saturated or held_at_limit, CS, False, curvature_limited,
                                                      any_speed=held_at_limit))
    angle_log.steeringAngleDeg = float(CS.steeringAngleDeg)
    angle_log.steeringAngleDesiredDeg = angle_steers_des
    return 0, float(angle_steers_des), angle_log
