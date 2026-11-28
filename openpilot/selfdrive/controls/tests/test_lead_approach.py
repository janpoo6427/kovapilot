import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.selfdrive.controls.lib import longitudinal_planner as lp
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import STOP_DISTANCE, get_T_FOLLOW
from openpilot.selfdrive.test.longitudinal_maneuvers.maneuver import Maneuver

KPH = 1 / 3.6
PERSONALITIES = [log.LongitudinalPersonality.aggressive, log.LongitudinalPersonality.standard,
                 log.LongitudinalPersonality.relaxed, log.LongitudinalPersonality.moreRelaxed]


def lead(d_rel, v_lead, present=True):
  return log.RadarState.LeadData(dRel=d_rel, vLead=v_lead, vLeadK=v_lead, present=present)


def run(personality, v_ego_kph, d0, speed_lead_kph, breakpoints, duration, cruise_kph=None, check=True):
  cruise_kph = v_ego_kph if cruise_kph is None else cruise_kph
  m = Maneuver("", duration=duration, initial_speed=v_ego_kph * KPH, lead_relevancy=True, initial_distance_lead=d0,
               speed_lead_values=[v * KPH for v in speed_lead_kph], breakpoints=breakpoints,
               cruise_values=[cruise_kph * KPH] * len(breakpoints), personality=personality)
  valid, logs = m.evaluate()
  assert valid or not check
  return logs


def approach(personality, v_ego_kph, v_lead_kph, d0, duration=90., cruise_kph=None):
  logs = run(personality, v_ego_kph, d0, [v_lead_kph, v_lead_kph], [0., duration], duration, cruise_kph)
  a, d = logs[:, 5], logs[:, 6]
  start = np.where(a <= -0.3)[0]
  return (d[start[0]] if len(start) else np.nan), a.min(), d.min(), d[-1]


def steady_gap(personality, v_kph):
  return get_T_FOLLOW(personality) * v_kph * KPH + STOP_DISTANCE


class TestApproachDecel:
  def test_constant_decel_to_following_gap(self):
    t_follow = get_T_FOLLOW(log.LongitudinalPersonality.moreRelaxed)
    v_ego, v_lead, d_rel = 80 * KPH, 50 * KPH, 120.
    excess = d_rel - (t_follow * v_lead + STOP_DISTANCE)
    assert lp.get_approach_decel(lead(d_rel, v_lead), v_ego, t_follow) == pytest.approx((v_ego - v_lead)**2 / (2 * excess))

  def test_not_for_equal_faster_close_or_missing_lead(self):
    t_follow = get_T_FOLLOW(log.LongitudinalPersonality.standard)
    v = 80 * KPH

    def gap(v_lead):
      return t_follow * v_lead + STOP_DISTANCE

    assert lp.get_approach_decel(lead(gap(v) + 50., v), v, t_follow) is None                    # same speed
    assert lp.get_approach_decel(lead(gap(v + 3.) + 50., v + 3.), v, t_follow) is None          # lead faster
    assert lp.get_approach_decel(lead(gap(v - 0.9) + 50., v - 0.9), v, t_follow) is None        # under 1 m/s closing
    assert lp.get_approach_decel(lead(gap(v - 8.) + 9., v - 8.), v, t_follow) is None           # already near the gap
    assert lp.get_approach_decel(lead(gap(v - 8.) + 50., v - 8., present=False), v, t_follow) is None
    assert lp.get_approach_decel(lead(gap(v - 8.) + 50., v - 8.), v, t_follow) is not None
    # once started it keeps going closer and slower (hysteresis)
    assert lp.get_approach_decel(lead(gap(v - 0.8) + 5., v - 0.8), v, t_follow, active=True) is not None
    assert lp.get_approach_decel(lead(gap(v - 0.8) + 2., v - 0.8), v, t_follow, active=True) is None


@pytest.mark.parametrize("personality", PERSONALITIES)
class TestApproachManeuvers:
  def test_slower_lead_starts_earlier_and_gentler(self, personality):
    # 60 -> 30 km/h from 160 m: before this change braking began at 78~90 m, peaking at -0.65~-0.72 m/s^2
    start_d, min_a, min_d, end_d = approach(personality, 60, 30, 160.)
    assert start_d >= 120.
    assert min_a >= -0.45
    assert end_d == pytest.approx(steady_gap(personality, 30), abs=0.3)
    assert min_d >= steady_gap(personality, 30) - 0.3

  def test_fast_approach_from_far_is_gentler(self, personality):
    # 80 -> 30 km/h from 200 m: before this change braking began at 137~156 m, peaking at -0.95~-1.04 m/s^2
    start_d, min_a, min_d, end_d = approach(personality, 80, 30, 200.)
    assert start_d >= 185.
    assert min_a >= -0.8
    assert end_d == pytest.approx(steady_gap(personality, 30), abs=0.3)

  def test_equal_speed_gap_unchanged(self, personality):
    # cruise set faster than the lead, so the car closes in from 100 m and then follows at the lead speed
    _, _, _, end_d = approach(personality, 80, 80, 100., duration=90., cruise_kph=100)
    assert end_d == pytest.approx(steady_gap(personality, 80), abs=0.5)

  @pytest.mark.parametrize("name,v_ego,d0,speeds,bps", [
    ("cut-in 80->50 at 20 m", 80, 20., [50, 50], [0., 30.]),
    ("cut-in 100->60 at 30 m", 100, 30., [60, 60], [0., 30.]),
    ("cut-in 80->30 at 35 m", 80, 35., [30, 30], [0., 30.]),
    ("cut-in then brakes", 80, 25., [80, 40, 40], [0., 3.7, 30.]),
    ("lead brakes 80->0 at 5 m/s^2", 80, None, [80, 80, 0], [0., 10., 14.44]),
    ("lead brakes 100->30 at 4 m/s^2", 100, None, [100, 100, 30], [0., 10., 14.86]),
  ])
  def test_close_and_braking_leads_unchanged(self, personality, name, v_ego, d0, speeds, bps, monkeypatch):
    d0 = steady_gap(personality, v_ego) if d0 is None else d0
    # check=False: braking at 5 m/s^2 from a near gap cannot be avoided at -3.5 m/s^2 (both versions alike)
    new = run(personality, v_ego, d0, speeds, bps, 30., check=False)
    monkeypatch.setattr(lp, "APPROACH_DECEL_MAX", 0.0)  # never starts: the planner without this feature
    old = run(personality, v_ego, d0, speeds, bps, 30., check=False)
    np.testing.assert_allclose(new, old, atol=1e-9, err_msg=name)
