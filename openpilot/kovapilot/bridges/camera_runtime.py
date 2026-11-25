from collections import deque
from math import isfinite

from openpilot.kovapilot.bridges.camera_shadow import CameraShadow
from openpilot.kovapilot.config.kova_params import KovaParamsError, KovaParamsStore, ParamsKovaParamsBackend
from openpilot.kovapilot.navigation.camera_input import CameraInput, MAX_CAMERA_AGE_NS
from openpilot.kovapilot.navigation.camera_profile import CAMERA_PROFILE_KEY, PROFILE_ACCEL_MPS2, PROFILE_JERK_MPS3


CAMERA_EVENT_SERVICE = "kovaCameraEvents"
CAMERA_BATCH_LIMIT = 16
MAX_TRACKED_CAMERAS = 256
MAX_MOTION_SAMPLE_GAP_NS = 250_000_000


def read_camera_profile(params) -> int:
  try:
    value = KovaParamsStore(ParamsKovaParamsBackend(params)).values().get(CAMERA_PROFILE_KEY, 0)
  except (KovaParamsError, RuntimeError, OSError):
    return 0
  return value if type(value) is int and 0 <= value <= 3 else 0


def camera_cruise_speed(sm, cruise_max_kph: float, cruise_unset_kph: float, kph_to_mps: float) -> float | None:
  if not sm.all_checks(["carState", "carControl", "controlsState", "modelV2", "selfdriveState"]):
    return None
  state = sm["carState"]
  control = sm["carControl"]
  cancel = any(button.pressed and button.type == "cancel" for button in state.buttonEvents)
  set_speed = state.vCruise
  controls = sm["controlsState"]
  if (not state.canValid or not control.longActive or not sm["selfdriveState"].enabled or control.cruiseControl.override
      or state.gasPressed or state.brakePressed or cancel or controls.forceDecel or controls.longControlState == "off"
      or not isfinite(set_speed) or not 0 < set_speed < cruise_unset_kph):
    return None
  return min(set_speed, cruise_max_kph) * kph_to_mps


class CameraRuntime:
  def __init__(self, profile: int, *, decel_limit_mps2: float = PROFILE_ACCEL_MPS2, jerk_limit_mps3: float = PROFILE_JERK_MPS3,
               response_time_s: float = 0.0):
    self.profile = profile
    self.policy = CameraShadow(profile, decel_limit_mps2=decel_limit_mps2, jerk_limit_mps3=jerk_limit_mps3,
                               response_time_s=response_time_s)
    self.saturated = False
    self.motion: deque[tuple[int, float, float]] = deque()

  def invalidate(self) -> None:
    self.policy = CameraShadow(self.profile, decel_limit_mps2=self.policy.decel_limit_mps2, jerk_limit_mps3=self.policy.jerk_limit_mps3,
                               response_time_s=self.policy.response_time_s)
    self.motion.clear()

  def _record_motion(self, now_ns: int, ego_mps: float) -> None:
    distance_m = 0.0
    if self.motion:
      last_ns, last_distance_m, last_speed = self.motion[-1]
      gap_ns = now_ns - last_ns
      if 0 < gap_ns <= MAX_MOTION_SAMPLE_GAP_NS:
        distance_m = last_distance_m + (last_speed + ego_mps) * 0.5 * gap_ns / 1e9
      elif gap_ns == 0:
        return
      else:
        self.motion.clear()
    self.motion.append((now_ns, distance_m, ego_mps))
    while len(self.motion) > 1 and self.motion[1][0] <= now_ns - MAX_CAMERA_AGE_NS:
      self.motion.popleft()

  def _traveled_since(self, observed_ns: int) -> float:
    # With incomplete history keep the upstream distance; an estimate never confirms passage.
    if observed_ns < self.motion[0][0] or observed_ns >= self.motion[-1][0]:
      return 0.0
    after_ns, after_m, _ = self.motion[-1]
    for before_ns, before_m, _ in reversed(self.motion):
      if before_ns <= observed_ns:
        fraction = (observed_ns - before_ns) / (after_ns - before_ns)
        return self.motion[-1][1] - (before_m + fraction * (after_m - before_m))
      after_ns, after_m = before_ns, before_m
    return 0.0

  def update(self, event) -> None:
    if self.saturated:
      return
    if not event.valid or event.which() != CAMERA_EVENT_SERVICE:
      self.invalidate()
      return
    batch = event.kovaCameraEvents
    if len(batch) > CAMERA_BATCH_LIMIT:
      self.invalidate()
      return

    for item in batch:
      try:
        sample = CameraInput(item.source, item.session, item.eventId, item.sequence, item.observedMonoTime,
                             event.logMonoTime, str(item.state), bool(item.roadMatch and item.verifiedSinglePoint),
                             item.distanceM, item.targetMps)
      except (ValueError, TypeError, OverflowError):
        self.invalidate()
        return
      self.policy.update(sample)
      if max(len(self.policy.inputs.events), len(self.policy.initial_distances), len(self.policy.passed),
             len(self.policy.inputs.retired_sessions)) > MAX_TRACKED_CAMERAS:
        self.invalidate()
        self.saturated = True
        return

  def speed_cap(self, now_ns: int, ego_mps: float, cruise_mps: float | None) -> float | None:
    if self.saturated or cruise_mps is None or not isfinite(ego_mps) or ego_mps < 0:
      self.motion.clear()
      self.policy.approaches.clear()
      self.policy.recoveries.clear()
      return None
    self._record_motion(now_ns, ego_mps)
    report = self.policy.observe(now_ns, ego_mps, cruise_mps, None, traveled_since=self._traveled_since)
    selected = report["selected_candidate"]
    return selected["speed_mps"] if selected is not None else None
