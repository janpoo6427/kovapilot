from openpilot.cereal import log
from openpilot.kovapilot.config.longitudinal_personality import (
  DEFAULT_PERSONALITY,
  get_longitudinal_personality,
  migrate_longitudinal_personality,
  normalize_longitudinal_personality,
)


class FakeParams:
  def __init__(self, value):
    self.value = value
    self.puts = []

  def get(self, key, return_default=False):
    assert key == "LongitudinalPersonality"
    assert return_default
    return self.value

  def put(self, key, value, block=False):
    self.puts.append((key, value, block))
    self.value = value


def test_normalize_accepts_upstream_personalities():
  for value in log.LongitudinalPersonality.schema.enumerants.values():
    assert normalize_longitudinal_personality(int(value)) == int(value)


def test_normalize_rejects_legacy_and_malformed_values():
  assert DEFAULT_PERSONALITY == int(log.LongitudinalPersonality.standard)
  for value in (4, -1, 100, None, "1"):
    assert normalize_longitudinal_personality(value) == DEFAULT_PERSONALITY


def test_get_is_defensive_without_mutating():
  params = FakeParams(4)
  assert get_longitudinal_personality(params) == DEFAULT_PERSONALITY
  assert params.puts == []


def test_migration_repairs_legacy_value_once():
  params = FakeParams(4)
  assert migrate_longitudinal_personality(params)
  assert params.puts == [("LongitudinalPersonality", DEFAULT_PERSONALITY, True)]
  assert not migrate_longitudinal_personality(params)
