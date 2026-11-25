# KOVA Params contract

`kova_params` is the versioned user-settings contract shared through the KovaLink gateway. The bundled catalog owns presentation metadata; the persistent `KovaParams` entry owns only current values and a monotonic revision.

## Catalog

The catalog is `openpilot/kovapilot/config/kova_params.json` and follows `kova_params.schema.json`.

- `groups` organize settings without encoding UI layout.
- `title` and `description` contain text for every declared locale.
- `type` and `defaultValue` define the value contract.
- `risk`, `applyPolicy`, and `availability` support safety-aware gateway behavior.
- Unknown catalog fields are allowed so later metadata can be added compatibly.
- `integer` and `number` settings may declare inclusive `minimum`/`maximum`, a positive `step`, and a localized `unit`. The store rejects defaults and updates outside the range.
- `enum` settings must declare non-empty unique `options` (strings or integers) and may declare localized `optionTitles` for every option. Values match options with the exact JSON type, so `true` never selects option `1`.

The first sample is `steering.always_on` (`항시 조향`). It defaults to `false`, can only be changed offroad, and reports `pendingConsumer`. Its desired value can be stored and returned through the gateway, but it does not enable steering behavior until a separately reviewed vehicle-capability consumer exists.

## Persistent state

The `KovaParams` Params key is JSON:

```json
{
  "schemaVersion": 1,
  "revision": 1,
  "values": {
    "steering.always_on": true
  }
}
```

Writes require the caller's `expectedRevision`, reject stale updates, validate the exact declared type, enforce the apply policy, and use a blocking Params write. Invalid stored state fails closed and is not silently replaced.

When a catalog revision adds a setting, an older state that lacks that key receives the new setting's default without an eager storage write. Unknown stored keys still fail closed so removed or misspelled control settings cannot be ignored silently. The gateway process must own one shared `KovaParamsStore` instance as the single writer; its lock and revision check serialize writes within that process.

The gateway read response should use `KovaParamsStore.document()`, which returns the full catalog plus `revision`, each current `value`, and `applyStatus`. The gateway adds a read-only `device` object (`{"offroad": bool}`, read from the device's own `IsOffroad`) to read and successful update responses so KovaLink can lock offroad-only settings before sending; the update handler still enforces the policy itself. The gateway write handler should call `update()` with the device's authoritative offroad state; it must not trust an offroad flag supplied by KovaLink.

The first transport integration is the loopback-only `aiohttp` application in `openpilot/kovapilot/gateway`. It exposes `GET /v1/health`, `GET /v1/kova-params`, and `POST /v1/kova-params/update`. The manager process remains disabled unless persistent `KovaLinkGatewayEnabled` is explicitly set. LAN bind, authentication, telemetry, WebRTC proxy, and Git operations are later gated stages.

## Development transport test

`tools/kovapilot/kovalink_gateway_dev.py` runs the same loopback-only application with an optional fixture catalog (`openpilot/kovapilot/tests/fixtures/kova_params_e2e.json`). It refuses to start without an isolated `PARAMS_ROOT` and always binds `127.0.0.1`. KovaLink's end-to-end transport test drives it and the real `python -m openpilot.kovapilot.gateway` entry point.
