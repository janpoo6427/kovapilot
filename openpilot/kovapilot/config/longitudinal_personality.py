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
