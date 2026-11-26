"""KovaLink Gateway application for native loopback and local Wi-Fi."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import suppress
from subprocess import SubprocessError
from typing import Any, Protocol

from aiohttp import ClientError, web

from openpilot.kovapilot.config.kova_params import (
  AUTO_ENGAGE_KEY,
  CRUISE_SYNC_KEY,
  CURVE_SLOWDOWN_KEY,
  LEAD_RESTART_KEY,
  KovaParamsApplyPolicyError,
  KovaParamsReadOnlyError,
  KovaParamsRevisionConflict,
  KovaParamsStore,
  KovaParamsValidationError,
)
from openpilot.kovapilot.gateway.camera import camera_offer
from openpilot.kovapilot.gateway.device_restart import DeviceRestart, RestartBlocked
from openpilot.kovapilot.gateway.git_maintenance import GitApplyBlocked, GitFetchError, GitPlanChanged, GitRepository
from openpilot.kovapilot.gateway.lan import LAN_ACCESS_KEY, LAN_SOCKETS_KEY, LOOPBACK_GUARD_KEY, TAILNET_KEY, VIA_TAILNET
from openpilot.kovapilot.gateway.native_longitudinal import KEY as NATIVE_LONGITUDINAL_KEY, NativeLongitudinal
from openpilot.kovapilot.gateway.owner_pairing import OwnerPairing, PairingBlocked, PairingBusy, PairingCodeInvalid, PairingExpired
from openpilot.kovapilot.gateway.setting_dependencies import evaluate_dependencies


API_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
LOCAL_NETWORK_WRITE_RISKS = frozenset(("display", "comfort", "behavior", "control"))
KOVA_PARAMS_STORE_KEY = web.AppKey("kova_params_store", KovaParamsStore)
OFFROAD_PROVIDER_KEY = web.AppKey("offroad_provider", Callable[[], bool])
PARKED_PROVIDER_KEY = web.AppKey("parked_provider", Callable[[], bool])
APPLIED_CRUISE_PROVIDER_KEY = web.AppKey("applied_cruise_provider", Callable[[], int | None])
APPLIED_CURVE_PROVIDER_KEY = web.AppKey("applied_curve_provider", Callable[[], int | str | None])
APPLIED_LEAD_RESTART_PROVIDER_KEY = web.AppKey("applied_lead_restart_provider", Callable[[], int | None])
APPLIED_AUTO_ENGAGE_PROVIDER_KEY = web.AppKey("applied_auto_engage_provider", Callable[[], dict | None])
DEPENDENCY_CONDITIONS_KEY = web.AppKey("dependency_conditions", Callable[[], dict[str, str]])
TELEMETRY_KEY = web.AppKey("telemetry", Callable)
SOCKETS_KEY = web.AppKey("telemetry_sockets", set)
CAMERA_KEY = web.AppKey("camera", Callable)
GIT_KEY = web.AppKey("git", GitRepository)
GIT_LOCK_KEY = web.AppKey("git_lock", asyncio.Lock)
NATIVE_LONGITUDINAL = web.AppKey("native_longitudinal", NativeLongitudinal)
RESTART_KEY = web.AppKey("restart", DeviceRestart)
OWNER_PAIRING_KEY = web.AppKey("owner_pairing", OwnerPairing)


class OffroadProvider(Protocol):
  def __call__(self) -> bool: ...


def _error(status: int, code: str, message: str) -> web.Response:
  return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _is_owner(request: web.Request) -> bool:
  access = request.app.get(LAN_ACCESS_KEY)
  return bool(request.secure and access is not None and access.is_owner(request.headers.getall("Authorization", [])))


def _allowed_write_risks(request: web.Request) -> frozenset[str] | None:
  access = request.app.get(LAN_ACCESS_KEY)
  if request.secure and access is not None and access.token_digest is None and not _is_owner(request):
    return LOCAL_NETWORK_WRITE_RISKS
  return None


def _device_state(request: web.Request) -> dict[str, bool]:
  parked = request.app.get(PARKED_PROVIDER_KEY)
  return {"offroad": bool(request.app[OFFROAD_PROVIDER_KEY]()), "parked": bool(parked and parked())}


def _dependency_conditions(request: web.Request) -> dict[str, str]:
  provider = request.app.get(DEPENDENCY_CONDITIONS_KEY)
  return provider() if provider is not None else {}


def _curve_application(request: web.Request, conditions: dict[str, str]) -> int | str | None:
  longitudinal = conditions.get("openpilotLongitudinalApplied")
  if longitudinal == "unsupported":
    return "unsupported"
  if longitudinal in ("unmet", "pendingRestart"):
    return "awaitingLongitudinal"
  provider = request.app.get(APPLIED_CURVE_PROVIDER_KEY)
  applied = provider() if provider is not None else None
  # Without vehicle capability data, the planner cannot distinguish a disabled
  # longitudinal preference from an unsupported vehicle or planner.
  return None if longitudinal == "unknown" and applied == "unsupported" else applied


def _with_device_state(request: web.Request, document: dict[str, Any], device_state: dict[str, bool] | None = None) -> dict[str, Any]:
  document["device"] = _device_state(request) if device_state is None else device_state
  allowed = _allowed_write_risks(request)
  applied_cruise = request.app.get(APPLIED_CRUISE_PROVIDER_KEY)
  conditions = _dependency_conditions(request)
  for setting in document["settings"]:
    if allowed is not None and setting.get("risk") not in allowed:
      setting["access"] = "readOnly"
    provider = None
    if setting["key"] == CRUISE_SYNC_KEY and setting.get("savePolicy") == "anytime":
      provider = applied_cruise
      if provider is None:
        setting["applyState"] = "awaitingStart"
    elif setting["key"] == CURVE_SLOWDOWN_KEY:
      applied = _curve_application(request, conditions)
      if applied == "unsupported":
        setting["applyStatus"] = "unsupported"
        setting["access"] = "readOnly"
      elif applied == "awaitingLongitudinal":
        setting["applyStatus"] = "available"
        setting["applyState"] = applied
      elif APPLIED_CURVE_PROVIDER_KEY in request.app:
        setting["applyStatus"] = "available"
        if applied is None:
          setting["applyState"] = "awaitingStart"
        else:
          setting["appliedValue"] = applied
          setting["applyState"] = "applied" if applied == setting["value"] else "pendingParked"
    elif setting["key"] == LEAD_RESTART_KEY:
      provider = request.app.get(APPLIED_LEAD_RESTART_PROVIDER_KEY)
      if provider is None:
        setting["applyState"] = "awaitingStart"
    elif setting["key"] == AUTO_ENGAGE_KEY:
      auto_engage_provider = request.app.get(APPLIED_AUTO_ENGAGE_PROVIDER_KEY)
      auto_engage = auto_engage_provider() if auto_engage_provider is not None else None
      if auto_engage is None:
        setting["applyState"] = "awaitingStart"
      else:
        setting["appliedValue"] = auto_engage["value"]
        if auto_engage["lateral"] != "unsupported" or auto_engage["longitudinal"] != "unsupported":
          setting["applyState"] = "applied" if auto_engage["value"] == setting["value"] else "pendingRestart"
        setting["runtimeStates"] = [
          {"title": {"ko": "조향", "en": "Steering"}, "state": auto_engage["lateral"]},
          {"title": {"ko": "크루즈", "en": "Cruise"}, "state": auto_engage["longitudinal"]},
        ]
    if provider is not None:
      applied = provider()
      if applied is None:
        setting["applyState"] = "awaitingStart"
      else:
        setting["appliedValue"] = applied
        if applied == setting["value"]:
          setting["applyState"] = "applied"
        else:
          setting["applyState"] = "pendingParked" if setting["applyPolicy"] == "parked" else "pendingRestart"
  native = request.app.get(NATIVE_LONGITUDINAL)
  if native is not None:
    document["settings"].append(native.setting())
  evaluate_dependencies(document["settings"], conditions)
  return document


async def health(request: web.Request) -> web.Response:
  tailnet = request.get(VIA_TAILNET, False)
  body = {
    "status": "ok",
    "schemaVersion": API_SCHEMA_VERSION,
    "bindScope": "tailnet" if tailnet else "wifi" if request.secure else "loopback",
    "authentication": ("bearer" if tailnet else request.app[LAN_ACCESS_KEY].authentication) if request.secure
                      else "localProcessOnly",
  }
  if request.secure:
    access = request.app[LAN_ACCESS_KEY]
    pairing = request.app.get(OWNER_PAIRING_KEY)
    body.update(owner=_is_owner(request), ownerRegistered=access.owner_secret is not None,
                ownerPairing=not tailnet and pairing is not None and pairing.available)
  return web.json_response(body)


async def get_kova_params(request: web.Request) -> web.Response:
  store = request.app[KOVA_PARAMS_STORE_KEY]
  try:
    document = await asyncio.to_thread(store.document)
  except KovaParamsValidationError:
    return _error(503, "kova_params_unavailable", "stored settings failed validation")
  return web.json_response(_with_device_state(request, document))


def _parse_update_payload(payload: Any) -> tuple[str, Any, int]:
  if not isinstance(payload, dict):
    raise KovaParamsValidationError("request body must be a JSON object")
  expected_fields = {"key", "value", "expectedRevision"}
  if payload.get("key") == NATIVE_LONGITUDINAL_KEY:
    expected_fields.add("expectedValue")
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
  if key == CURVE_SLOWDOWN_KEY and _curve_application(request, _dependency_conditions(request)) == "unsupported":
    return _error(403, "update_forbidden", "curve slowdown is unsupported by the vehicle or planner")
  device_state = _device_state(request)
  try:
    if key == NATIVE_LONGITUDINAL_KEY and (native := request.app.get(NATIVE_LONGITUDINAL)) is not None:
      expected_value = payload["expectedValue"]
      if any(type(v) is not int or v not in (0, 1) for v in (value, expected_value)):
        raise KovaParamsValidationError("expected integer values 0 or 1")
      document = await asyncio.to_thread(store.document)
      if document["revision"] != expected_revision:
        raise KovaParamsRevisionConflict("settings changed")
      native.save(value, expected_value)
    else:
      document = await asyncio.to_thread(
        store.update,
        key,
        value,
        expected_revision,
        is_offroad=device_state["offroad"],
        is_parked=device_state["parked"],
        allowed_risks=_allowed_write_risks(request),
      )
  except KovaParamsRevisionConflict as exc:
    return _error(409, "revision_conflict", str(exc))
  except (KovaParamsApplyPolicyError, KovaParamsReadOnlyError) as exc:
    return _error(403, "update_forbidden", str(exc))
  except KovaParamsValidationError as exc:
    return _error(400, "invalid_value", str(exc))
  return web.json_response(_with_device_state(request, document, device_state))


def _tailnet_listener(request, local) -> bool:
  port = request.app.get(TAILNET_KEY)
  return port is not None and bool(local) and local[0] == "127.0.0.1" and local[1] == port


@web.middleware
async def local_client(request, handler):
  local = request.transport.get_extra_info("sockname") if request.transport else None
  if request.secure and _tailnet_listener(request, local):
    # The owner's phone over the VPN: every request needs the owner token.
    access = request.app.get(LAN_ACCESS_KEY)
    if access is None or "Origin" in request.headers:
      return _error(403, "tailnet_client_required", "use the KovaLink app over the comma VPN")
    if not access.is_owner(request.headers.getall("Authorization", [])):
      response = _error(401, "authentication_required", "the owner token is required over the VPN")
      response.headers["WWW-Authenticate"] = "Bearer"
      return response
    request[VIA_TAILNET] = True
    return await handler(request)
  if request.secure:
    access = request.app.get(LAN_ACCESS_KEY)
    if (access is None or not local or not access.accepts_peer(local[0], request.remote or "")
        or request.host not in (local[0], f"{local[0]}:{local[1]}") or "Origin" in request.headers):
      return _error(403, "wifi_client_required", "use the device Wi-Fi IPv4 address")
    auth = request.headers.getall("Authorization", [])
    if access.token_digest is not None and (len(auth) != 1 or not access.authenticates(auth[0])):
      response = _error(401, "authentication_required", "valid Gateway credentials are required")
      response.headers["WWW-Authenticate"] = "Bearer"
      return response
    if access.token_digest is None and auth and not access.is_owner(auth):
      # A phone that pairs again replaces the owner; the old token must fail loudly.
      response = _error(401, "authentication_required", "the owner token is not valid for this device")
      response.headers["WWW-Authenticate"] = "Bearer"
      return response
    return await handler(request)
  if request.remote not in ("127.0.0.1", "::1") or request.headers.get("Origin") is not None:
    return _error(403, "local_client_required", "only native loopback clients are supported")
  guard = request.app.get(LOOPBACK_GUARD_KEY)
  peer = request.transport.get_extra_info("peername") if request.transport else None
  if guard is not None and (not local or not peer or guard(local, peer)):
    # A tailnet peer reaches loopback ports through tailscaled.
    return _error(403, "local_client_required", "only native loopback clients are supported")
  host = request.host
  if host != "[::1]" and not host.startswith("[::1]:") and host.split(":", 1)[0].lower() not in ("localhost", "127.0.0.1"):
    return _error(403, "local_client_required", "only loopback host names are supported")
  return await handler(request)


async def telemetry_stream(request):
  source = request.app.get(TELEMETRY_KEY)
  if source is None:
    return _error(503, "telemetry_unavailable", "telemetry source is unavailable")
  sockets = request.app[SOCKETS_KEY]
  if len(sockets) >= 4:
    return _error(503, "telemetry_busy", "too many telemetry clients")
  ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1024, compress=False)
  sockets.add(ws)
  lan_sockets = request.app[LAN_SOCKETS_KEY]
  if request.secure and not request.get(VIA_TAILNET, False):
    lan_sockets.add(ws)

  async def publish():
    try:
      while not ws.closed:
        frame = source()
        if frame is not None:
          await asyncio.wait_for(ws.send_json(frame), 2)
        await asyncio.sleep(0.05)
    except (TimeoutError, ConnectionError):
      pass
    finally:
      await ws.close()

  try:
    await ws.prepare(request)
    publisher = asyncio.create_task(publish())
    try:
      async for _ in ws:
        await ws.close(code=1008, message=b"read only")
    finally:
      publisher.cancel()
      with suppress(asyncio.CancelledError):
        await publisher
  finally:
    sockets.discard(ws)
    lan_sockets.discard(ws)
  return ws


async def close_sockets(app):
  await asyncio.gather(*(ws.close(code=1001) for ws in tuple(app[SOCKETS_KEY]) if ws.prepared), return_exceptions=True)


async def stream_offer(request):
  signal = request.app.get(CAMERA_KEY)
  if signal is None:
    return _error(503, "camera_unavailable", "camera source is unavailable")
  if request.content_type != "application/json":
    return _error(415, "invalid_content_type", "expected application/json")
  try:
    offer = camera_offer(await request.json())
  except (ValueError, TypeError):
    return _error(400, "invalid_offer", "expected SDP and one supported road camera")
  try:
    return web.json_response(await signal(offer))
  except (ClientError, TimeoutError, ValueError, asyncio.IncompleteReadError):
    return _error(503, "camera_unavailable", "camera signaling did not complete")


def _git_authorized(request):
  return not request.secure or _is_owner(request)


def _pairing(request) -> OwnerPairing | None:
  pairing = request.app.get(OWNER_PAIRING_KEY)
  if request.get(VIA_TAILNET, False):
    return None
  return pairing if request.secure and pairing is not None and pairing.available else None


async def owner_pair(request):
  pairing = _pairing(request)
  if pairing is None:
    return _error(404, "owner_pairing_unavailable", "owner pairing needs automatic Wi-Fi access")
  try:
    seconds = await asyncio.to_thread(pairing.start)
  except PairingBlocked:
    return _error(409, "owner_pairing_offroad", "turn the vehicle off to pair a phone")
  except PairingBusy:
    return _error(429, "owner_pairing_busy", "a pairing code was just requested")
  except OSError:
    return _error(503, "owner_pairing_unavailable", "the device could not show a pairing code")
  asyncio.get_running_loop().call_later(seconds, pairing.expire)
  return web.json_response({"expiresIn": seconds}, headers={"Cache-Control": "no-store"})


async def owner_confirm(request):
  pairing = _pairing(request)
  if pairing is None:
    return _error(404, "owner_pairing_unavailable", "owner pairing needs automatic Wi-Fi access")
  try:
    body = await request.json()
  except (ValueError, TypeError):
    body = None
  if not isinstance(body, dict) or set(body) != {"code"} or not isinstance(body["code"], str) or \
     re.fullmatch(r"[0-9]{6}", body["code"]) is None:
    return _error(400, "invalid_request", "expected a six digit code")
  try:
    token = await asyncio.to_thread(pairing.confirm, body["code"])
  except PairingBlocked:
    return _error(409, "owner_pairing_offroad", "turn the vehicle off to pair a phone")
  except PairingExpired:
    return _error(410, "owner_pairing_expired", "request a new code on the device")
  except PairingCodeInvalid as exc:
    response = _error(403, "owner_pairing_code_invalid", "the code does not match")
    body = {"error": {"code": "owner_pairing_code_invalid", "message": "the code does not match",
                      "remainingAttempts": exc.remaining}}
    return web.json_response(body, status=response.status)
  except (OSError, ValueError):
    return _error(503, "owner_pairing_unavailable", "the device could not save the owner")
  return web.json_response({"token": token}, headers={"Cache-Control": "no-store"})


async def git_api(request):
  repo = request.app.get(GIT_KEY)
  if repo is None:
    return _error(503, "git_unavailable", "device repository is unavailable")
  action = request.match_info["action"]
  try:
    if action == "log":
      limit, cursor = int(request.query.get("limit", 20)), int(request.query.get("cursor", 0))
      if not 1 <= limit <= 100 or not 0 <= cursor <= 10_000:
        raise ValueError
      callback, args = repo.log, (limit, cursor)
    elif action == "status":
      callback, args = repo.status, (_git_authorized(request),)
    elif action == "branches":
      callback, args = repo.branches, ()
    else:
      body = await request.json()
      if not isinstance(body, dict) or set(body) - {"operation", "branch", "commit"}:
        raise ValueError
      callback, args = repo.plan, (body.get("operation", action), body.get("branch"), body.get("commit"), _git_authorized(request))
      if action in ("switch", "reset") and args[0] != action:
        raise ValueError
    lock = request.app[GIT_LOCK_KEY]
    if lock.locked():
      return _error(503, "git_busy", "repository inspection is in progress")
    async with lock:
      result = await asyncio.to_thread(callback, *args)
    if action == "status":
      restart = request.app.get(RESTART_KEY)
      result = {**result, "restartEnabled": restart is not None and restart.enabled(_git_authorized(request))}
    return web.json_response({"branches": result} if action == "branches" else result)
  except (ValueError, TypeError):
    return _error(400, "invalid_git_request", "invalid Git parameters or target")
  except GitFetchError:
    return _error(503, "git_unavailable", "device could not fetch the latest origin branches")
  except (OSError, SubprocessError):
    return _error(503, "git_unavailable", "device repository could not be inspected")


async def git_apply(request):
  repo = request.app.get(GIT_KEY)
  if repo is None or not repo.can_apply(_git_authorized(request)):
    return _error(403, "git_mutation_disabled", "offroad maintenance requires authorized access and an idle updater")
  lock = request.app[GIT_LOCK_KEY]
  if lock.locked():
    return _error(503, "git_busy", "repository inspection is in progress")
  try:
    async with lock:
      body = await request.json()
      if (not isinstance(body, dict) or set(body) - {"operation", "branch", "commit", "planDigest"} or
          not isinstance(body.get("planDigest"), str) or not re.fullmatch(r"[0-9a-f]{64}", body["planDigest"])):
        raise ValueError
      result = await asyncio.to_thread(repo.apply, body.get("operation"), body.get("branch"), body.get("commit"), body["planDigest"])
  except GitPlanChanged:
    return _error(409, "git_plan_changed", "refresh the preview before applying")
  except GitApplyBlocked:
    return _error(403, "git_apply_blocked", "the updater is busy or the working tree is not known and clean")
  except (ValueError, TypeError):
    return _error(400, "invalid_git_request", "invalid Git parameters or target")
  except (OSError, SubprocessError):
    return _error(503, "git_unavailable", "device repository could not be inspected")
  return web.json_response(result, status=202)


async def device_restart(request):
  restart = request.app.get(RESTART_KEY)
  if restart is None:
    return _error(503, "restart_unavailable", "device restart is unavailable")
  try:
    await asyncio.to_thread(restart.restart, _git_authorized(request))
  except RestartBlocked:
    return _error(403, "restart_blocked", "restart requires authorized access while offroad and not finalizing an update")
  except OSError:
    return _error(503, "restart_unavailable", "device restart could not be requested")
  return web.json_response({"restarting": True}, status=202)


def create_app(store: KovaParamsStore, offroad_provider: OffroadProvider, *, parked_provider: Callable[[], bool] | None = None,
               applied_cruise_provider: Callable[[], int | None] | None = None,
               applied_curve_provider: Callable[[], int | str | None] | None = None,
               applied_lead_restart_provider: Callable[[], int | None] | None = None,
               applied_auto_engage_provider: Callable[[], dict | None] | None = None,
               dependency_conditions: Callable[[], dict[str, str]] | None = None,
               telemetry=None, camera=None, git=None, lan_access=None, native_longitudinal=None,
               restart=None, owner_pairing=None) -> web.Application:
  app = web.Application(client_max_size=MAX_REQUEST_BYTES, middlewares=[local_client])
  app[KOVA_PARAMS_STORE_KEY] = store
  app[OFFROAD_PROVIDER_KEY] = offroad_provider
  if parked_provider is not None:
    app[PARKED_PROVIDER_KEY] = parked_provider
  if applied_cruise_provider is not None:
    app[APPLIED_CRUISE_PROVIDER_KEY] = applied_cruise_provider
  if applied_curve_provider is not None:
    app[APPLIED_CURVE_PROVIDER_KEY] = applied_curve_provider
  if applied_lead_restart_provider is not None:
    app[APPLIED_LEAD_RESTART_PROVIDER_KEY] = applied_lead_restart_provider
  if applied_auto_engage_provider is not None:
    app[APPLIED_AUTO_ENGAGE_PROVIDER_KEY] = applied_auto_engage_provider
  if dependency_conditions is not None:
    app[DEPENDENCY_CONDITIONS_KEY] = dependency_conditions
  app[SOCKETS_KEY] = set()
  app[LAN_SOCKETS_KEY] = set()
  if lan_access is not None:
    app[LAN_ACCESS_KEY] = lan_access
  app[GIT_LOCK_KEY] = asyncio.Lock()
  if telemetry is not None:
    app[TELEMETRY_KEY] = telemetry
  if camera is not None:
    app[CAMERA_KEY] = camera
  if git is not None:
    app[GIT_KEY] = git
  if native_longitudinal is not None:
    app[NATIVE_LONGITUDINAL] = native_longitudinal
  if restart is not None:
    app[RESTART_KEY] = restart
  if owner_pairing is not None:
    app[OWNER_PAIRING_KEY] = owner_pairing
  app.on_shutdown.append(close_sockets)
  app.router.add_get("/v1/health", health)
  app.router.add_get("/v1/kova-params", get_kova_params)
  app.router.add_post("/v1/kova-params/update", update_kova_params)
  app.router.add_get("/v1/telemetry", telemetry_stream)
  app.router.add_post("/v1/stream", stream_offer)
  app.router.add_get("/v1/git/{action:status|branches|log}", git_api)
  app.router.add_post("/v1/git/{action:plan|switch|reset}", git_api)
  app.router.add_post("/v1/git/apply", git_apply)
  app.router.add_post("/v1/device/restart", device_restart)
  app.router.add_post("/v1/owner/pair", owner_pair)
  app.router.add_post("/v1/owner/confirm", owner_confirm)
  return app
