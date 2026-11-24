"""Loopback-only KovaLink gateway application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from aiohttp import web

from openpilot.kovapilot.config.kova_params import (
  KovaParamsApplyPolicyError,
  KovaParamsReadOnlyError,
  KovaParamsRevisionConflict,
  KovaParamsStore,
  KovaParamsValidationError,
)


API_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
KOVA_PARAMS_STORE_KEY = web.AppKey("kova_params_store", KovaParamsStore)
OFFROAD_PROVIDER_KEY = web.AppKey("offroad_provider", Callable[[], bool])


class OffroadProvider(Protocol):
  def __call__(self) -> bool: ...


def _error(status: int, code: str, message: str) -> web.Response:
  return web.json_response({"error": {"code": code, "message": message}}, status=status)


async def health(request: web.Request) -> web.Response:
  return web.json_response({
    "status": "ok",
    "schemaVersion": API_SCHEMA_VERSION,
    "bindScope": "loopback",
    "authentication": "localProcessOnly",
  })


async def get_kova_params(request: web.Request) -> web.Response:
  store = request.app[KOVA_PARAMS_STORE_KEY]
  try:
    document = await asyncio.to_thread(store.document)
  except KovaParamsValidationError:
    return _error(503, "kova_params_unavailable", "stored settings failed validation")
  return web.json_response(document)


def _parse_update_payload(payload: Any) -> tuple[str, Any, int]:
  if not isinstance(payload, dict):
    raise KovaParamsValidationError("request body must be a JSON object")
  expected_fields = {"key", "value", "expectedRevision"}
  if set(payload) != expected_fields:
    raise KovaParamsValidationError("request fields must be key, value, and expectedRevision")
  key = payload["key"]
  if not isinstance(key, str) or not key:
    raise KovaParamsValidationError("key must be a non-empty string")
  revision = payload["expectedRevision"]
  if type(revision) is not int or revision < 0:
    raise KovaParamsValidationError("expectedRevision must be a non-negative integer")
  return key, payload["value"], revision


async def update_kova_params(request: web.Request) -> web.Response:
  try:
    payload = await request.json()
    key, value, expected_revision = _parse_update_payload(payload)
  except KovaParamsValidationError as exc:
    return _error(400, "invalid_request", str(exc))
  except (ValueError, TypeError):
    return _error(400, "invalid_json", "request body must be valid JSON")

  store = request.app[KOVA_PARAMS_STORE_KEY]
  is_offroad = request.app[OFFROAD_PROVIDER_KEY]()
  try:
    document = await asyncio.to_thread(
      store.update,
      key,
      value,
      expected_revision,
      is_offroad=is_offroad,
    )
  except KovaParamsRevisionConflict as exc:
    return _error(409, "revision_conflict", str(exc))
  except (KovaParamsApplyPolicyError, KovaParamsReadOnlyError) as exc:
    return _error(403, "update_forbidden", str(exc))
  except KovaParamsValidationError as exc:
    return _error(400, "invalid_value", str(exc))
  return web.json_response(document)


def create_app(store: KovaParamsStore, offroad_provider: OffroadProvider) -> web.Application:
  app = web.Application(client_max_size=MAX_REQUEST_BYTES)
  app[KOVA_PARAMS_STORE_KEY] = store
  app[OFFROAD_PROVIDER_KEY] = offroad_provider
  app.router.add_get("/v1/health", health)
  app.router.add_get("/v1/kova-params", get_kova_params)
  app.router.add_post("/v1/kova-params/update", update_kova_params)
  return app
