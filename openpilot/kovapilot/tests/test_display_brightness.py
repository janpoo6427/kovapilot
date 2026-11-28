import pytest

from openpilot.kovapilot.config.kova_params import KovaParamsStore, KovaParamsValidationError, load_bundled_catalog
from openpilot.kovapilot.display.brightness import (
  AUTO_DARK_MIN,
  DEFAULT_FIXED,
  DEFAULT_SETTINGS,
  FIXED_KEY,
  MODE_AUTO,
  MODE_AUTO_DARK,
  MODE_FIXED,
  MODE_KEY,
  MODES,
  DisplayBrightnessSettings,
  load_display_brightness,
  onroad_brightness,
  settings_from_values,
)

AUTO_MINS = (30.0, 18.0)  # tizi/tici and mici upstream BRIGHTNESS_MIN
FILTERED = (0.0, 10.0, 18.0, 30.0, 42.5, 50.0, 77.7, 100.0)


class MemoryBackend:
  def __init__(self, value=None):
    self.value = value

  def get(self, key):
    return self.value

  def put(self, key, value, block=False):
    self.value = value


class FakeParams:
  def __init__(self, value=None, exc=None):
    self.value = value
    self.exc = exc

  def get(self, key):
    if self.exc is not None:
      raise self.exc
    return self.value


def _run(filtered, settings, onroad=True, auto_valid=True, alert=False, auto_min=30.0):
  return onroad_brightness(filtered, settings, onroad=onroad, auto_valid=auto_valid, alert_active=alert, auto_min=auto_min)


@pytest.mark.parametrize("auto_min", AUTO_MINS)
@pytest.mark.parametrize("onroad", (True, False))
@pytest.mark.parametrize("auto_valid", (True, False))
@pytest.mark.parametrize("alert", (True, False))
def test_auto_is_exact_upstream_passthrough(auto_min, onroad, auto_valid, alert):
  for filtered in FILTERED:
    assert _run(filtered, DEFAULT_SETTINGS, onroad, auto_valid, alert, auto_min) == filtered


@pytest.mark.parametrize("mode", MODES)
def test_offroad_is_unchanged_for_every_mode(mode):
  settings = DisplayBrightnessSettings(mode=mode, fixed=10)
  for filtered in FILTERED:
    assert _run(filtered, settings, onroad=False) == filtered


@pytest.mark.parametrize("fixed", (10, 25, 50, 100))
@pytest.mark.parametrize("auto_valid", (True, False))
def test_fixed_uses_fixed_value(fixed, auto_valid):
  settings = DisplayBrightnessSettings(mode=MODE_FIXED, fixed=fixed)
  for filtered in FILTERED:
    assert _run(filtered, settings, auto_valid=auto_valid) == float(fixed)


def test_fixed_out_of_range_is_clamped():
  assert _run(50.0, DisplayBrightnessSettings(mode=MODE_FIXED, fixed=1)) == 10.0
  assert _run(50.0, DisplayBrightnessSettings(mode=MODE_FIXED, fixed=400)) == 100.0


@pytest.mark.parametrize("mode", (MODE_FIXED, MODE_AUTO_DARK))
@pytest.mark.parametrize("auto_min", AUTO_MINS)
def test_alert_is_never_dimmer_than_auto(mode, auto_min):
  settings = DisplayBrightnessSettings(mode=mode, fixed=10)
  for filtered in FILTERED:
    with_alert = _run(filtered, settings, alert=True, auto_min=auto_min)
    assert with_alert >= filtered
    assert with_alert >= _run(filtered, settings, alert=False, auto_min=auto_min)


def test_fixed_can_still_be_brighter_than_auto_during_alert():
  assert _run(40.0, DisplayBrightnessSettings(mode=MODE_FIXED, fixed=90), alert=True) == 90.0


@pytest.mark.parametrize("auto_min", AUTO_MINS)
def test_auto_dark_end_points_and_order(auto_min):
  settings = DisplayBrightnessSettings(mode=MODE_AUTO_DARK)
  assert _run(auto_min, settings, auto_min=auto_min) == pytest.approx(AUTO_DARK_MIN)
  assert _run(100.0, settings, auto_min=auto_min) == pytest.approx(100.0)
  assert _run(0.0, settings, auto_min=auto_min) == pytest.approx(AUTO_DARK_MIN)

  previous = None
  for step in range(101):
    filtered = auto_min + (100.0 - auto_min) * step / 100.0
    value = _run(filtered, settings, auto_min=auto_min)
    assert AUTO_DARK_MIN - 1e-9 <= value <= filtered + 1e-9
    if previous is not None:
      assert value >= previous
    previous = value


def test_auto_dark_without_light_estimate_keeps_upstream_value():
  settings = DisplayBrightnessSettings(mode=MODE_AUTO_DARK)
  for filtered in FILTERED:
    assert _run(filtered, settings, auto_valid=False) == filtered


def test_unknown_mode_falls_back_to_upstream():
  assert _run(42.0, DisplayBrightnessSettings(mode=9)) == 42.0


@pytest.mark.parametrize("values, expected", [
  (None, DEFAULT_SETTINGS),
  ([], DEFAULT_SETTINGS),
  ({}, DEFAULT_SETTINGS),
  ({MODE_KEY: MODE_FIXED, FIXED_KEY: 35}, DisplayBrightnessSettings(MODE_FIXED, 35)),
  ({MODE_KEY: MODE_AUTO_DARK}, DisplayBrightnessSettings(MODE_AUTO_DARK, DEFAULT_FIXED)),
  ({MODE_KEY: "fixed", FIXED_KEY: 35}, DisplayBrightnessSettings(MODE_AUTO, 35)),
  ({MODE_KEY: True, FIXED_KEY: 35}, DisplayBrightnessSettings(MODE_AUTO, 35)),
  ({MODE_KEY: MODE_FIXED, FIXED_KEY: True}, DisplayBrightnessSettings(MODE_FIXED, DEFAULT_FIXED)),
  ({MODE_KEY: MODE_FIXED, FIXED_KEY: 35.0}, DisplayBrightnessSettings(MODE_FIXED, DEFAULT_FIXED)),
  ({MODE_KEY: MODE_FIXED, FIXED_KEY: 5}, DisplayBrightnessSettings(MODE_FIXED, DEFAULT_FIXED)),
  ({MODE_KEY: MODE_FIXED, FIXED_KEY: 101}, DisplayBrightnessSettings(MODE_FIXED, DEFAULT_FIXED)),
])
def test_settings_reader_is_tolerant(values, expected):
  assert settings_from_values(values) == expected


def test_load_display_brightness_never_raises():
  assert load_display_brightness(FakeParams(exc=RuntimeError("boom"))) == DEFAULT_SETTINGS
  assert load_display_brightness(FakeParams(None)) == DEFAULT_SETTINGS
  assert load_display_brightness(FakeParams("not json")) == DEFAULT_SETTINGS
  state = {"schemaVersion": 1, "revision": 3, "values": {MODE_KEY: MODE_FIXED, FIXED_KEY: 20}}
  assert load_display_brightness(FakeParams(state)) == DisplayBrightnessSettings(MODE_FIXED, 20)


def test_catalog_declares_display_settings_matching_module():
  catalog = load_bundled_catalog()
  settings = {item["key"]: item for item in catalog["settings"]}
  mode = settings[MODE_KEY]
  fixed = settings[FIXED_KEY]

  assert any(group["id"] == "display" for group in catalog["groups"])
  assert mode["type"] == "enum" and tuple(mode["options"]) == MODES and mode["defaultValue"] == MODE_AUTO
  assert fixed["type"] == "integer" and fixed["defaultValue"] == DEFAULT_FIXED
  assert (fixed["minimum"], fixed["maximum"]) == (10, 100)
  for item in (mode, fixed):
    assert item["risk"] == "display"
    assert item["applyPolicy"] == "immediate"
    assert item["availability"] == "available"
    assert item["access"] == "readWrite"


def test_display_settings_can_change_onroad_and_round_trip_to_ui_reader():
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  store.update(MODE_KEY, MODE_FIXED, 0, is_offroad=False)
  store.update(FIXED_KEY, 25, 1, is_offroad=False)

  assert load_display_brightness(FakeParams(backend.value)) == DisplayBrightnessSettings(MODE_FIXED, 25)
  assert backend.value["values"]["steering.always_on"] == 0


@pytest.mark.parametrize("key, value", [(MODE_KEY, "night"), (MODE_KEY, 3), (MODE_KEY, True), (FIXED_KEY, 5), (FIXED_KEY, 105), (FIXED_KEY, "50")])
def test_store_rejects_invalid_display_values(key, value):
  with pytest.raises(KovaParamsValidationError):
    KovaParamsStore(MemoryBackend()).update(key, value, 0, is_offroad=False)


# 2026-11-28 janpoo: in the automatic modes the set brightness is the maximum.
CAPS = (10, 20, 35, 50, 100)


@pytest.mark.parametrize("cap", CAPS)
@pytest.mark.parametrize("auto_valid", (True, False))
@pytest.mark.parametrize("auto_min", AUTO_MINS)
def test_auto_is_upstream_capped_by_set_brightness(cap, auto_valid, auto_min):
  settings = DisplayBrightnessSettings(mode=MODE_AUTO, fixed=cap)
  for filtered in FILTERED:
    assert _run(filtered, settings, auto_valid=auto_valid, auto_min=auto_min) == min(filtered, float(cap))


@pytest.mark.parametrize("cap", CAPS)
@pytest.mark.parametrize("auto_min", AUTO_MINS)
def test_auto_dark_is_capped_by_set_brightness(cap, auto_min):
  capped = DisplayBrightnessSettings(mode=MODE_AUTO_DARK, fixed=cap)
  uncapped = DisplayBrightnessSettings(mode=MODE_AUTO_DARK, fixed=100)
  for filtered in FILTERED:
    expected = min(_run(filtered, uncapped, auto_min=auto_min), float(cap))
    assert _run(filtered, capped, auto_min=auto_min) == pytest.approx(expected)
    assert _run(filtered, capped, auto_valid=False, auto_min=auto_min) == min(filtered, float(cap))


def test_auto_cap_follows_upstream_below_the_cap():
  settings = DisplayBrightnessSettings(mode=MODE_AUTO, fixed=60)
  assert _run(42.5, settings) == 42.5
  assert _run(60.0, settings) == 60.0
  assert _run(77.7, settings) == 60.0


@pytest.mark.parametrize("mode", (MODE_AUTO, MODE_AUTO_DARK))
def test_auto_cap_is_lifted_while_an_alert_is_shown(mode):
  capped = DisplayBrightnessSettings(mode=mode, fixed=10)
  uncapped = DisplayBrightnessSettings(mode=mode, fixed=100)
  for filtered in FILTERED:
    assert _run(filtered, capped, alert=True) == pytest.approx(_run(filtered, uncapped, alert=True))
    assert _run(filtered, capped, alert=True) >= filtered
  assert _run(77.7, DisplayBrightnessSettings(mode=MODE_AUTO, fixed=10), alert=True) == 77.7


@pytest.mark.parametrize("mode", (MODE_AUTO, MODE_AUTO_DARK))
def test_auto_cap_out_of_range_is_clamped(mode):
  assert _run(77.7, DisplayBrightnessSettings(mode=mode, fixed=1)) == 10.0
  assert _run(77.7, DisplayBrightnessSettings(mode=mode, fixed=400)) == pytest.approx(_run(77.7, DisplayBrightnessSettings(mode=mode, fixed=100)))


def test_new_install_default_keeps_auto_equal_to_upstream():
  assert DEFAULT_FIXED == 100
  settings = {item["key"]: item for item in load_bundled_catalog()["settings"]}
  assert settings[FIXED_KEY]["defaultValue"] == 100
  backend = MemoryBackend()
  KovaParamsStore(backend).initialize_storage()
  assert backend.value["values"][FIXED_KEY] == 100
  assert load_display_brightness(FakeParams(backend.value)) == DisplayBrightnessSettings(MODE_AUTO, 100)


def test_saved_brightness_is_kept_and_becomes_the_auto_maximum():
  state = {"schemaVersion": 1, "revision": 30, "values": {MODE_KEY: MODE_AUTO, FIXED_KEY: 20}}
  backend = MemoryBackend(state)
  KovaParamsStore(backend).initialize_storage()
  assert backend.value["values"][MODE_KEY] == MODE_AUTO
  assert backend.value["values"][FIXED_KEY] == 20
  settings = load_display_brightness(FakeParams(backend.value))
  assert settings == DisplayBrightnessSettings(MODE_AUTO, 20)
  assert _run(77.7, settings) == 20.0


def test_set_brightness_has_no_mode_condition_and_saves_in_auto():
  settings = {item["key"]: item for item in load_bundled_catalog()["settings"]}
  assert not settings[FIXED_KEY].get("dependencies")
  backend = MemoryBackend()
  store = KovaParamsStore(backend)
  store.update(FIXED_KEY, 40, 0, is_offroad=False)
  assert load_display_brightness(FakeParams(backend.value)) == DisplayBrightnessSettings(MODE_AUTO, 40)
