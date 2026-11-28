"""Set speed by the cluster speed (janpoo 11/28 10:39: the cruise set speed follows the speed the instrument cluster shows).

carState.vEgoCluster is the speed the cluster shows (opendbc: CAN Hyundai CLU15, CAN-FD CRUISE_BUTTONS_ALT CLUSTER_SPEED;
the wheel speed when a car reports none). The set speed the driver sees and changes (vCruiseCluster, cruise buttons,
accelerator sync, the first start at the current speed) is compared with that speed. The planner gets the set speed in
wheel speed units (vCruise) through the measured ratio wheel / cluster speed, so the car holds the speed at which the
cluster shows the set speed. IONIQ 9 rlog 0000079f segment 20 (2026-11-28): set 86, wheel 86, cluster 89-90.
Navigation, camera and curve speed caps stay wheel speed caps, as before.
"""
import math

from openpilot.common.realtime import DT_CTRL

RATIO_MIN_SPEED = 3.0  # m/s, both speeds; slower, the integer cluster speed is too coarse and the ratio is held
RATIO_TIME_CONSTANT = 2.0  # s, smooths the integer cluster steps (1 km/h is 1.2 % at 86 km/h)
# wheel / cluster speed outside this window is not the cluster speed of this car: the ratio is held
RATIO_MIN = 0.8
RATIO_MAX = 1.05
SET_SPEED_VALID_MAX_KPH = 250.0  # V_CRUISE_UNSET (255) and -1 are passed through


def cluster_speed(CS) -> float:
  """The speed the cluster shows, m/s; the wheel speed when the car reports no cluster speed."""
  return CS.vEgoCluster if CS.vEgoCluster > 0.0 else CS.vEgo


class ClusterSpeedRatio:
  def __init__(self, dt: float = DT_CTRL):
    self.alpha = dt / (RATIO_TIME_CONSTANT + dt)
    self.ratio = 1.0

  def update(self, v_ego: float, v_ego_cluster: float) -> float:
    if (math.isfinite(v_ego) and math.isfinite(v_ego_cluster) and v_ego > RATIO_MIN_SPEED and
        v_ego_cluster > RATIO_MIN_SPEED):
      sample = v_ego / v_ego_cluster
      if RATIO_MIN <= sample <= RATIO_MAX:
        self.ratio += self.alpha * (sample - self.ratio)
    return self.ratio

  def planner_kph(self, set_kph: float) -> float:
    """Set speed on the cluster to the planner set speed in wheel speed units."""
    if not (math.isfinite(set_kph) and 0.0 < set_kph < SET_SPEED_VALID_MAX_KPH):
      return set_kph
    return set_kph * self.ratio
