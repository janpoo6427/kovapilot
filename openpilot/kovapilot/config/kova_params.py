"""Validated KOVA settings catalog and revisioned runtime value storage."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from threading import RLock
from typing import Any, Protocol


KOVA_PARAMS_KEY = "KovaParams"
CRUISE_SYNC_KEY = "cruise.accelerator_sync"
CRUISE_SYNC_SHADOW_KEY = "cruise.accelerator_sync_stock_shadow"
LEGACY_PARAM_KEYS = {
  CRUISE_SYNC_KEY: "CruiseAcceleratorSync",
  CRUISE_SYNC_SHADOW_KEY: "CruiseAcceleratorSyncStockShadow",
}
CATALOG_PATH = Path(__file__).with_name("kova_params.json")


class KovaParamsError(ValueError):
  pass


class KovaParamsValidationError(KovaParamsError):
  pass


class KovaParamsRevisionConflict(KovaParamsError):
  pass


class KovaParamsApplyPolicyError(KovaParamsError):
  pass


class KovaParamsReadOnlyError(KovaParamsError):
  pass


class KovaParamsBackend(Protocol):
  def get(self, key: str) -> Any: ...
  def put(self, key: str, value: Any, block: bool = False) -> None: ...


class ParamsKovaParamsBackend:
  def __init__(self, params: Any | None = None) -> None:
    if params is None:
      from openpilot.common.params import Params
      params = Params()
    self._params = params

  def get(self, key: str) -> Any:
    return self._params.get(key)

  def put(self, key: str, value: Any, block: bool = False) -> None:
    self._params.put(key, value, block=block)

  def legacy_bool(self, key: str) -> bool | None:
    legacy_key = LEGACY_PARAM_KEYS.get(key)
    return self._params.get(legacy_key) if legacy_key is not None else None


def load_bundled_catalog() -> dict[str, Any]:
  with CATALOG_PATH.open(encoding="utf-8") as catalog_file:
    catalog = json.load(catalog_file)
  validate_catalog(catalog)
  return catalog


def _validate_localized_text(value: Any, locales: set[str], field: str) -> None:
  if not isinstance(value, dict) or not value:
    raise KovaParamsValidationError(f"{field} must be a non-empty localized object")
  if not locales.issubset(value) or any(not isinstance(value[locale], str) or not value[locale] for locale in locales):
    raise KovaParamsValidationError(f"{field} must contain non-empty text for every locale")


def _is_number(value: Any) -> bool:
  return type(value) in (int, float) and math.isfinite(value)


def _enum_contains(options: Any, value: Any) -> bool:
  # Exact type match: JSON true must not select option 1, and 1.0 must not select 1.
  return isinstance(options, list) and any(type(option) is type(value) and option == value for option in options)


def _validate_value(setting: dict[str, Any], value: Any) -> None:
  setting_type = setting["type"]
  valid = {
    "boolean": type(value) is bool,
    "integer": type(value) is int,
    "number": _is_number(value),
    "string": isinstance(value, str),
    "enum": _enum_contains(setting.get("options"), value),
  }.get(setting_type, False)
  if not valid:
    raise KovaParamsValidationError(f"invalid value for {setting['key']}: expected {setting_type}")
  if setting_type in ("integer", "number"):
    minimum = setting.get("minimum")
    maximum = setting.get("maximum")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
      raise KovaParamsValidationError(f"value out of range for {setting['key']}: allowed {minimum}..{maximum}")


def _validate_constraints(setting: dict[str, Any], locales: set[str]) -> None:
  key = setting["key"]
  setting_type = setting.get("type")
  if setting_type in ("integer", "number"):
    bound_type_ok = (lambda bound: type(bound) is int) if setting_type == "integer" else _is_number
    for field in ("minimum", "maximum"):
      if field in setting and not bound_type_ok(setting[field]):
        raise KovaParamsValidationError(f"{field} for {key} must be a finite {setting_type}")
    if "minimum" in setting and "maximum" in setting and setting["minimum"] > setting["maximum"]:
      raise KovaParamsValidationError(f"minimum for {key} must not exceed maximum")
    if "step" in setting and (not bound_type_ok(setting["step"]) or setting["step"] <= 0):
      raise KovaParamsValidationError(f"step for {key} must be a positive {setting_type}")
    if "unit" in setting:
      _validate_localized_text(setting["unit"], locales, f"setting {key} unit")
  elif any(field in setting for field in ("minimum", "maximum", "step", "unit")):
    raise KovaParamsValidationError(f"numeric constraints are only valid for integer or number settings: {key}")

  if setting_type == "enum":
    options = setting.get("options")
    if not isinstance(options, list) or not options or any(type(option) not in (str, int) for option in options):
      raise KovaParamsValidationError(f"options for {key} must be a non-empty string or integer array")
    if len({(type(option), option) for option in options}) != len(options):
      raise KovaParamsValidationError(f"options for {key} must be unique")
    option_titles = setting.get("optionTitles")
    if option_titles is not None:
      if not isinstance(option_titles, dict) or set(option_titles) != {str(option) for option in options}:
        raise KovaParamsValidationError(f"optionTitles for {key} must title every option")
      for option, title in option_titles.items():
        _validate_localized_text(title, locales, f"setting {key} option {option} title")
  elif "options" in setting or "optionTitles" in setting:
    raise KovaParamsValidationError(f"options are only valid for enum settings: {key}")


def validate_catalog(catalog: Any) -> None:
  if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
    raise KovaParamsValidationError("catalog schemaVersion must be 1")
  locales_value = catalog.get("locales")
  if not isinstance(locales_value, list) or not locales_value or any(not isinstance(locale, str) for locale in locales_value):
    raise KovaParamsValidationError("catalog locales must be a non-empty string array")
  locales = set(locales_value)
  if len(locales) != len(locales_value) or catalog.get("defaultLocale") not in locales:
    raise KovaParamsValidationError("catalog locales must be unique and include defaultLocale")

  groups = catalog.get("groups")
  settings = catalog.get("settings")
  if not isinstance(groups, list) or not isinstance(settings, list):
    raise KovaParamsValidationError("catalog groups and settings must be arrays")
  group_ids: set[str] = set()
  for group in groups:
    if not isinstance(group, dict) or not isinstance(group.get("id"), str) or group["id"] in group_ids:
      raise KovaParamsValidationError("group ids must be unique strings")
    group_ids.add(group["id"])
    _validate_localized_text(group.get("title"), locales, f"group {group['id']} title")
    _validate_localized_text(group.get("description"), locales, f"group {group['id']} description")

  setting_keys: set[str] = set()
  for setting in settings:
    if not isinstance(setting, dict) or not isinstance(setting.get("key"), str) or setting["key"] in setting_keys:
      raise KovaParamsValidationError("setting keys must be unique strings")
    setting_keys.add(setting["key"])
    if setting.get("groupId") not in group_ids:
      raise KovaParamsValidationError(f"unknown group for {setting['key']}")
    if setting.get("access") not in ("readOnly", "readWrite"):
      raise KovaParamsValidationError(f"invalid access for {setting['key']}")
    if setting.get("applyPolicy") not in ("immediate", "offroad", "parked", "restart"):
      raise KovaParamsValidationError(f"invalid applyPolicy for {setting['key']}")
    if setting.get("availability") not in ("available", "pendingConsumer", "unsupported"):
      raise KovaParamsValidationError(f"invalid availability for {setting['key']}")
    _validate_localized_text(setting.get("title"), locales, f"setting {setting['key']} title")
    _validate_localized_text(setting.get("description"), locales, f"setting {setting['key']} description")
    _validate_constraints(setting, locales)
    _validate_value(setting, setting.get("defaultValue"))


class KovaParamsStore:
  def __init__(self, backend: KovaParamsBackend, catalog: dict[str, Any] | None = None) -> None:
    self._backend = backend
    self._catalog = deepcopy(catalog) if catalog is not None else load_bundled_catalog()
    validate_catalog(self._catalog)
    self._settings = {setting["key"]: setting for setting in self._catalog["settings"]}
    self._lock = RLock()

  def _default_value(self, key: str) -> Any:
    if isinstance(self._backend, ParamsKovaParamsBackend):
      legacy = self._backend.legacy_bool(key)
      if type(legacy) is bool:
        return legacy
    return self._settings[key]["defaultValue"]

  def _default_state(self) -> dict[str, Any]:
    return {
      "schemaVersion": self._catalog["schemaVersion"],
      "revision": 0,
      "values": {key: self._default_value(key) for key in self._settings},
    }

  def _load_state(self) -> dict[str, Any]:
    state = self._backend.get(KOVA_PARAMS_KEY)
    if state is None:
      return self._default_state()
    if not isinstance(state, dict) or state.get("schemaVersion") != self._catalog["schemaVersion"]:
      raise KovaParamsValidationError("stored KovaParams has an unsupported schemaVersion")
    revision = state.get("revision")
    values = state.get("values")
    if type(revision) is not int or revision < 0 or not isinstance(values, dict) or not set(values).issubset(self._settings):
      raise KovaParamsValidationError("stored KovaParams has an invalid revision or value set")
    for key, value in values.items():
      _validate_value(self._settings[key], value)
    migrated_state = deepcopy(state)
    for key in self._settings.keys() - values.keys():
      migrated_state["values"][key] = self._default_value(key)
    return migrated_state

  def values(self) -> dict[str, Any]:
    with self._lock:
      return self._load_state()["values"]

  def document(self) -> dict[str, Any]:
    with self._lock:
      state = self._load_state()
      document = deepcopy(self._catalog)
      document["revision"] = state["revision"]
      for setting in document["settings"]:
        setting["value"] = state["values"][setting["key"]]
        setting["applyStatus"] = setting["availability"]
      return document

  def update(self, key: str, value: Any, expected_revision: int, *, is_offroad: bool, is_parked: bool = False,
             allowed_risks: frozenset[str] | None = None) -> dict[str, Any]:
    if type(expected_revision) is not int or expected_revision < 0:
      raise KovaParamsValidationError("expected_revision must be a non-negative integer")
    setting = self._settings.get(key)
    if setting is None:
      raise KovaParamsValidationError(f"unknown setting: {key}")
    if setting["access"] != "readWrite" or (allowed_risks is not None and setting.get("risk") not in allowed_risks):
      raise KovaParamsReadOnlyError(f"setting is read-only: {key}")
    if setting["applyPolicy"] == "offroad" and not is_offroad:
      raise KovaParamsApplyPolicyError(f"setting can only be changed offroad: {key}")
    if setting["applyPolicy"] == "parked" and not (is_offroad or is_parked):
      raise KovaParamsApplyPolicyError(f"setting can only be changed when parked or offroad: {key}")
    _validate_value(setting, value)

    with self._lock:
      state = self._load_state()
      if state["revision"] != expected_revision:
        raise KovaParamsRevisionConflict(f"revision conflict: expected {expected_revision}, current {state['revision']}")
      state["values"][key] = value
      state["revision"] += 1
      self._backend.put(KOVA_PARAMS_KEY, state, block=True)
      return self.document()
