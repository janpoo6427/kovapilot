"""Stock (car) navigation speed-camera warning from Hyundai/Kia CAN.

Follows carrot's default vehicle path (VehicleSpeedCameraControlMode 1,
VehicleNaviCanControl 0): the car reports only the camera speed limit and
that a camera warning is active, without a distance. carrot then assumes a
virtual distance of limit x VehicleSpeedCameraDistanceTime (default 6.0 s,
i.e. 360 m for 60 km/h) and holds the limit until the warning ends.

The virtual endpoint is fixed in wheel odometry when the warning starts, so the
remaining distance shrinks with the distance actually driven. How far before the
camera the car starts the warning is not documented (carrot only links a real
distance within the virtual endpoint + 40 m), so plannerd may decelerate these
approaches harder than app cameras (camera_profile.STOCK_NAVI_DECEL_MPS2).
"""
from dataclasses import dataclass, field

from openpilot.kovapilot.navigation.carrot_link import MAX_LIMIT_KPH, MIN_LIMIT_KPH


NAVI_HU_ADDR = 0x544  # CAN: Navi_HU (hyundai_can.dbc BO_ 1348)
HDA_INFO_4A3_ADDR = 0x4A3  # CAN-FD E-CAN: carrot hyundai_canfd.dbc BO_ 1187 HDA_INFO_4A3
VEHICLE_SOURCE = "vehicle_navi"
MPH_TO_KPH = 1.609344
CAMERA_DISTANCE_TIME_S = 6.0
CONTROLLED_ACCESS_LINK_CLASSES = frozenset((1, 2, 3))  # freeway, IC, JC
SIGNAL_TIMEOUT_NS = 1_000_000_000
PASSED_REPORT_NS = 1_000_000_000


@dataclass(frozen=True)
class NaviWarning:
  limit_kph: float
  camera: bool
  controlled_access: bool = False


def decode_navi_hu(dat: bytes) -> NaviWarning | None:
  # SpeedLim_Nav_Clu 7|8@0+ (byte 0), SpeedLim_Nav_Cam 30|1@0+ (byte 3 bit 6)
  if len(dat) != 8:
    return None
  limit = dat[0]
  camera = bool((dat[3] >> 6) & 1)
  return NaviWarning(float(limit) if limit < 255 and camera else 0.0, camera)


def decode_hda_info_4a3(dat: bytes, is_metric: bool) -> NaviWarning | None:
  # LinkClass 0|3@1+, SPEED_LIMIT 15|8@0+ (byte 1), MapSource 27|3@1+
  if len(dat) != 8:
    return None
  limit = dat[1]
  link_class = dat[0] & 0x7
  map_source = (dat[3] >> 3) & 0x7
  speed = float(limit) if 0 < limit < 255 else 0.0
  if not is_metric:
    speed *= MPH_TO_KPH
  return NaviWarning(speed, map_source == 2, link_class in CONTROLLED_ACCESS_LINK_CLASSES)


@dataclass
class VehicleCamera:
  event_id: str
  limit_kph: float
  target_odo_m: float
  passed_ns: int | None = None


@dataclass
class VehicleCameraTracker:
  source: str
  session: str
  current: VehicleCamera | None = None
  retired: list[tuple[VehicleCamera, str, int]] = field(default_factory=list)
  next_id: int = 0
  sequence: int = 0

  def _retire(self, state: str, now_ns: int) -> None:
    if self.current is not None:
      self.retired.append((self.current, state, now_ns))
      self.current = None

  def update(self, warning: NaviWarning | None, now_ns: int, odo_m: float, *, suppressed: bool) -> None:
    """warning None: signal missing or stale. suppressed: the local app is connected (carrot gives it priority)."""
    if suppressed or warning is None:
      self._retire("clear", now_ns)
      return
    active = (warning.camera and MIN_LIMIT_KPH <= warning.limit_kph <= MAX_LIMIT_KPH and
              not (warning.controlled_access and round(warning.limit_kph) == 30))
    if not active:
      self._retire("passed", now_ns)
      return
    if self.current is not None and self.current.limit_kph != warning.limit_kph:
      self._retire("clear", now_ns)
    if self.current is None:
      self.next_id += 1
      self.current = VehicleCamera(f"car-{self.next_id}", warning.limit_kph, odo_m + warning.limit_kph * CAMERA_DISTANCE_TIME_S)

  def step(self, now_ns: int, odo_m: float) -> list[dict]:
    events = []
    keep = []
    for camera, state, retired_ns in self.retired:
      if state == "clear":
        events.append(self._event(camera, now_ns, "clear", 0.0))
      elif now_ns - retired_ns <= PASSED_REPORT_NS:
        events.append(self._event(camera, now_ns, "passed", 0.0))
        keep.append((camera, state, retired_ns))
    self.retired = keep
    if self.current is not None:
      # Hold the limit at the virtual endpoint until the car ends the warning.
      events.append(self._event(self.current, now_ns, "present", max(0.0, self.current.target_odo_m - odo_m)))
    return events

  def _event(self, camera: VehicleCamera, now_ns: int, state: str, distance_m: float) -> dict:
    self.sequence += 1
    return {"source": self.source, "session": self.session, "eventId": camera.event_id, "sequence": self.sequence,
            "observedMonoTime": now_ns, "state": state, "roadMatch": True, "verifiedSinglePoint": True,
            "distanceM": float(distance_m), "targetMps": float(camera.limit_kph / 3.6) if state != "clear" else 0.0}
