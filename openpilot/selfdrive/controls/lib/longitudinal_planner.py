#!/usr/bin/env python3
from math import isfinite

import numpy as np

import openpilot.cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.cereal import log
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.curve_speed import CurveSpeed
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan, should_stop, smooth_value
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog

J_CRUISE_VALS = [1.6, 1.2, 0.8, 0.6]
J_CRUISE_BP = [0., 10.0, 25., 40.]
A_CRUISE_MIN = -0.5
# Hard floor for a speed cap that was planned with a stronger deceleration (stock navigation cameras).
A_SPEED_CAP_MIN = -1.2
# Such a cap ends a stronger deceleration; when the follower dips under it, climb back as
# (cap - v_ego) / this time so the car levels at the cap instead of swinging over it.
SPEED_CAP_SETTLE_TIME = 3.0
CRUISE_ACCEL_RESPONSE_TIME = 1.0
AGGRESSIVE_ACCEL_BOOST = 0.1  # m/s^2
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]


def get_cruise_accel(v_cruise, v_ego, a_cruise_prev, a_target_prev, dt, a_min=A_CRUISE_MIN, a_max=ACCEL_MAX):
  target_accel = smooth_value(v_cruise - v_ego, a_cruise_prev, tau=CRUISE_ACCEL_RESPONSE_TIME, dt=dt)
  target_accel = np.clip(target_accel, a_min, a_max)
  j_cruise = np.interp(v_ego, J_CRUISE_BP, J_CRUISE_VALS)
  target_accel = float(np.clip(target_accel, min(a_cruise_prev, a_target_prev) - j_cruise * dt, a_cruise_prev + j_cruise * dt))

  return target_accel


class LongitudinalPlanner:
  def __init__(self, CP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    self.fcw = False
    self.dt = dt
    self.curve_speed = CurveSpeed(dt)

    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.a_cruise = init_a
    self.a_speed_cap = None
    self.speed_cap_source = None
    self.output_a_target = init_a
    self.output_should_stop = False

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)

  def update(self, sm, *, camera_speed_cap=None, camera_eligible=False, curve_speed_cap=None, curve_eligible=False,
             lead_restart=None, now_ns=0, camera_decel_mps2=None):
    v_ego = sm['carState'].vEgo
    personality = sm['selfdriveState'].personality
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    if sm['controlsState'].forceDecel:
      v_cruise = 0.0

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET
    reset_state = reset_state or not v_cruise_initialized

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.output_a_target = np.clip(sm['carState'].aEgo, ACCEL_MIN, ACCEL_MAX)
      self.a_cruise = self.output_a_target
      self.curve_speed.reset()

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    self.mpc.set_weights(prev_accel_constraint, personality=personality)
    if self.CP.openpilotLongitudinalControl:
      self.mpc.set_cur_state(self.v_desired_filter.x, self.output_a_target)
    else:
      self.mpc.set_cur_state(v_ego, sm['carState'].aEgo)
    self.mpc.update(sm['radarState'], personality=personality)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Save starting point for next iteration
    a_prev = self.output_a_target

    action_t =  self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                              action_t=action_t)
    output_should_stop_mpc = should_stop(v_ego, output_a_target_mpc)
    curve_offset = self.curve_speed.update(sm['modelV2'], sm['vehicleParameters'].roll)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration + curve_offset
    if personality == log.LongitudinalPersonality.aggressive:
      speed_scale = np.interp(v_ego, [5.0, 10.0], [0.0, 1.0])
      accel_scale = np.interp(output_a_target_e2e, [-1.0, -0.5], [0.0, 1.0])
      output_a_target_e2e += AGGRESSIVE_ACCEL_BOOST * speed_scale * accel_scale
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    a_cruise_prev = self.a_cruise
    self.a_cruise = get_cruise_accel(v_cruise, v_ego, a_cruise_prev, self.output_a_target, self.dt)
    cruise_should_stop = should_stop(v_ego, self.a_cruise)

    candidates = [(output_a_target_mpc, self.mpc.source, output_should_stop_mpc),
                  (self.a_cruise, LongitudinalPlanSource.cruise, cruise_should_stop),
                  (output_a_target_e2e, LongitudinalPlanSource.e2e, output_should_stop_e2e)]

    if ((camera_eligible or curve_eligible) and self.CP.openpilotLongitudinalControl and not self.CP.dashcamOnly and
        not reset_state and not sm['controlsState'].forceDecel):
      caps = {"camera": (camera_speed_cap, camera_eligible), "curve": (curve_speed_cap, curve_eligible)}
      if self.speed_cap_source is not None and not caps[self.speed_cap_source][1]:
        self.a_speed_cap = None
        self.speed_cap_source = None
      selected = min(((cap, source) for source, (cap, eligible) in caps.items()
                      if eligible and cap is not None and isfinite(cap) and 0 < cap < v_cruise), default=None)
      if selected is not None:
        cap, self.speed_cap_source = selected
        cap_prev = self.a_speed_cap if self.a_speed_cap is not None else a_cruise_prev
        a_min, a_max = A_CRUISE_MIN, ACCEL_MAX
        if (self.speed_cap_source == "camera" and camera_decel_mps2 is not None and isfinite(camera_decel_mps2)
            and camera_decel_mps2 > -A_CRUISE_MIN):
          # The camera approach was planned for this deceleration; follow it, never beyond A_SPEED_CAP_MIN,
          # and settle onto the cap from below without overshooting it (the jerk limits still apply).
          a_min = max(-camera_decel_mps2, A_SPEED_CAP_MIN)
          a_max = min(ACCEL_MAX, max(0.0, (cap - v_ego) / SPEED_CAP_SETTLE_TIME))
        self.a_speed_cap = get_cruise_accel(cap, v_ego, cap_prev, self.output_a_target, self.dt, a_min, a_max)
      elif self.a_speed_cap is not None:
        # An expired cap is released at the existing positive cruise jerk limit.
        step = np.interp(v_ego, J_CRUISE_BP, J_CRUISE_VALS) * self.dt
        self.a_speed_cap = min(self.a_speed_cap + step, self.a_cruise)
        if self.a_speed_cap >= self.a_cruise:
          self.a_speed_cap = None
          self.speed_cap_source = None
      if self.a_speed_cap is not None:
        candidates.append((self.a_speed_cap, LongitudinalPlanSource.cruise, False))
    else:
      self.a_speed_cap = None
      self.speed_cap_source = None

    output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
    self.output_should_stop = any(should_stop for _, _, should_stop in candidates)
    if lead_restart is not None and self.CP.openpilotLongitudinalControl and not self.CP.pcmCruise and not self.CP.dashcamOnly and not self.CP.notCar:
      output_a_target, self.output_should_stop = lead_restart.constrain(
        sm, now_ns=now_ns, reset_state=reset_state, should_stop=self.output_should_stop,
        target_accel=output_a_target, previous_accel=a_prev, dt=self.dt,
      )
    self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)

    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.output_a_target + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime - sm.logMonoTime['modelV2']) / 1e9
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.present
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True

    pm.send('longitudinalPlan', plan_send)
