"""Navigation guidance and automatic lane change state for the KovaLink telemetry frame.

Display only: every reader falls back to nothing or "unknown" instead of raising.
"""
import math
import time

# carrot_serv._update_tbt turn_type_mapping, as KovaLink TbtManeuver names. Rotaries and
# toll gates have no KovaLink sign; the app shows their guidance text.
_MANEUVERS = {
  **dict.fromkeys((12, 16), "turnLeft"),
  **dict.fromkeys((13, 19), "turnRight"),
  **dict.fromkeys((102, 105, 112, 115, 7, 44, 17, 75, 76, 118), "slightLeft"),
  **dict.fromkeys((101, 104, 111, 114, 6, 43, 73, 74, 123, 124, 117), "slightRight"),
  14: "uTurn",
  201: "arrive",
  **dict.fromkeys((51, 52, 53, 54, 55), "straight"),
}
NAVIGATION_FRESH_NS = 1_000_000_000  # navcamd publishes kovaNavigation at 5 Hz
SETTING_KEY = "steering.lane_change_mode"
# setting: the old on / off field for apps before the three modes (on = automatic); mode: the setting itself
LEGACY_SETTING = {2: "on", 1: "off", 0: "off"}
MODES = {0: "off", 1: "nudge", 2: "auto"}
SETTING_REFRESH_NS = 1_000_000_000
LANE_CHANGE_STATES = ("off", "preLaneChange", "laneChangeStarting", "laneChangeFinishing")
AUTO_STATES = ("none", "pending", "waiting", "returning")


def maneuver(turn_type: int) -> str:
  return _MANEUVERS.get(turn_type, "unknown")


def _distance(value) -> float | None:
  return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


def navigation_fields(navigation, now_ns: int) -> dict:
  """navigation (connection), tbt (KovaLink TbtInstruction) and speedLimitKph from kovaNavigation."""
  try:
    source = str(navigation.source)
    received = int(navigation.lastReceivedMonoTime)
    status = {"source": source if source in ("carrotV2", "carrotLegacy") else "none",
              "appVersion": str(navigation.appVersion) or None,
              "lastReceivedAgeMs": (now_ns - received) // 1_000_000 if 0 < received <= now_ns else None}
    fields = {"navigation": status}
    if source != "carrotV2":
      return fields
    status.update(guidanceActive=bool(navigation.guidanceActive), offRoute=bool(navigation.offRoute))
    if 10 <= navigation.roadLimitKph <= 200:
      fields["speedLimitKph"] = float(navigation.roadLimitKph)
    current, following = navigation.current, navigation.next
    distance = _distance(current.distanceM)
    if current.valid and distance is not None:
      tbt = {"maneuver": maneuver(current.turnType), "turnType": int(current.turnType), "distanceMeters": distance,
             "roadName": str(current.roadName) or None, "mainText": str(current.mainText) or None,
             "nearDirection": str(current.nearDirection) or None}
      following_distance = _distance(following.distanceM)
      if following.valid and following_distance is not None:
        # carrot xDistToTurnNext: the next guide point counts from the current one.
        tbt.update(nextManeuver=maneuver(following.turnType), nextTurnType=int(following.turnType),
                   nextDistanceMeters=distance + following_distance, nextRoadName=str(following.roadName) or None)
      if navigation.routeValid:
        tbt.update(remainingDistanceMeters=float(navigation.remainingDistanceM),
                   remainingSeconds=int(navigation.remainingTimeS))
      fields["tbt"] = tbt
    return fields
  except (AttributeError, TypeError, ValueError):
    return {}


def _optional(reader, name: str):
  """A field this build may not have (automatic lane change schema); None when absent."""
  try:
    return getattr(reader, name)
  except (AttributeError, ValueError):
    return None


def auto_lane_change_fields(model, car, setting: int | None) -> dict:
  """autoLaneChange: the setting, the lane change and the blind-spot state, "unknown" where this build has none."""
  try:
    state = {"setting": LEGACY_SETTING.get(setting, "unknown"), "mode": MODES.get(setting, "unknown")}
    if model is not None:
      meta = model.meta
      lane = str(meta.laneChangeState)
      state["laneChange"] = lane if lane in LANE_CHANGE_STATES else "unknown"
      direction = str(meta.laneChangeDirection)
      state["direction"] = direction if direction in ("left", "right") else "none"
      auto = _optional(meta, "kovaAutoLaneChange")
      state["auto"] = str(auto) if auto is not None and str(auto) in AUTO_STATES else "unknown"
    else:
      state.update(laneChange="unknown", direction="none", auto="unknown")
    if car is not None:
      fresh = _optional(car, "blindspotFresh")
      state["blindSpotFresh"] = bool(fresh) if fresh is not None else None
      state["blindSpotLeft"], state["blindSpotRight"] = bool(car.leftBlindspot), bool(car.rightBlindspot)
    return {"autoLaneChange": state}
  except (AttributeError, TypeError, ValueError):
    return {"autoLaneChange": {"setting": "unknown", "mode": "unknown", "laneChange": "unknown", "direction": "none",
                               "auto": "unknown"}}


class AutoLaneChangeSetting:
  """The saved steering.lane_change_mode value, read at most once a second; None when this build has no such setting."""

  def __init__(self, read=None, clock=time.monotonic_ns):
    self.read, self.clock = read or _read_setting, clock
    self.value: int | None = None
    self.read_ns: int | None = None

  def __call__(self) -> int | None:
    now = self.clock()
    if self.read_ns is None or not 0 <= now - self.read_ns < SETTING_REFRESH_NS:
      self.read_ns = now
      try:
        self.value = self.read()
      except Exception:
        self.value = None
    return self.value


def _read_setting() -> int | None:
  from openpilot.common.params import Params
  from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend
  value = KovaParamsStore(ParamsKovaParamsBackend(Params())).values().get(SETTING_KEY)
  return value if type(value) is int and value in MODES else None
