from openpilot.cereal import log


PARAM = "LongitudinalPersonality"
DEFAULT_PERSONALITY = int(log.LongitudinalPersonality.standard)
VALID_PERSONALITIES = frozenset(int(value) for value in log.LongitudinalPersonality.schema.enumerants.values())


def normalize_longitudinal_personality(value) -> int:
  return int(value) if isinstance(value, int) and int(value) in VALID_PERSONALITIES else DEFAULT_PERSONALITY


def get_longitudinal_personality(params) -> int:
  return normalize_longitudinal_personality(params.get(PARAM, return_default=True))


def migrate_longitudinal_personality(params) -> bool:
  value = params.get(PARAM, return_default=True)
  normalized = normalize_longitudinal_personality(value)
  if value == normalized:
    return False

  params.put(PARAM, normalized, block=True)
  return True


# Distance button levels. Stock openpilot has three. A car whose own cruise gap has four levels
# (CarState.pcmCruiseGap reported 4 at least once) gets a fourth, farthest level (moreRelaxed).
GAP_LEVELS = 3
FOUR_GAP_LEVELS = 4
# Internal, not a setting: fingerprint of the car that reported gap level 4, so it starts with four levels next drive.
FOUR_LEVEL_CAR_PARAM = "KovaGapFourLevelCar"


def clamp_personality(personality: int, levels: int) -> int:
  # a level this car does not have (moreRelaxed on a three level car) falls back to the default, as before it existed
  return int(personality) if 0 <= int(personality) < levels else DEFAULT_PERSONALITY


class CruiseGapLevels:
  def __init__(self, params, car_fingerprint: str):
    self._params = params
    self._car = str(car_fingerprint or "")
    remembered = bool(self._car) and params.get(FOUR_LEVEL_CAR_PARAM) == self._car
    self.levels = FOUR_GAP_LEVELS if remembered else GAP_LEVELS
    self._car_gap_prev = 0

  def update(self, car_gap) -> int | None:
    """Feed the gap level the car reports (0 unknown). On a four level car, return the personality matching a new car
    level one to one (level n -> personality n - 1), else None. Three level cars always return None."""
    car_gap = int(car_gap)
    if car_gap == FOUR_GAP_LEVELS and self.levels != FOUR_GAP_LEVELS:
      self.levels = FOUR_GAP_LEVELS
      if self._car:
        self._params.put(FOUR_LEVEL_CAR_PARAM, self._car)
    follow = None
    if self.levels == FOUR_GAP_LEVELS and 1 <= car_gap <= FOUR_GAP_LEVELS and car_gap != self._car_gap_prev:
      follow = car_gap - 1
    self._car_gap_prev = car_gap
    return follow

  def next_personality(self, personality: int) -> int:
    # distance button: one level closer, wrapping from the closest to the farthest
    return (int(personality) - 1) % self.levels
