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
- Bundled settings use numeric values. `enum` settings declare unique integer `options` and may declare localized `optionTitles`. Options `[0, 1]` with `"control": "switch"` render as a switch in KovaLink. Values match the exact JSON type.
- Optional `dependencies` describe prerequisites. A stored setting dependency uses `setting` and `equals`. A device condition uses `condition`, with an optional `setting` linking to the related preference. Each entry has a localized `title`.

The first sample is `steering.always_on` (`항시 조향`). It defaults to `0`, can only be changed offroad, and reports `pendingConsumer`. A vehicle-capability consumer must exist before the setting can affect steering.

## Persistent state

The `KovaParams` Params key is JSON:

```json
{
  "schemaVersion": 1,
  "revision": 1,
  "values": {
    "steering.always_on": 1
  }
}
```

Writes require the caller's `expectedRevision`, reject stale updates, validate the exact declared type, enforce the apply policy, and use a blocking Params write. Invalid stored state fails closed and is not silently replaced.

Before starting the managed processes, the manager initializes missing settings and persists older switch and brightness selections as numbers. It preserves the revision and skips the write when the settings already use the current format. Once the manager has started, the gateway owns user updates through one shared `KovaParamsStore`. Its lock and revision check serialize writes within that process.

The gateway read response should use `KovaParamsStore.document()`, which returns the full catalog plus `revision`, each current `value`, and `applyStatus`. The gateway adds a read-only `device` object (`{"offroad": bool}`, read from the device's own `IsOffroad`) to read and successful update responses so KovaLink can lock offroad-only settings before sending; the update handler still enforces the policy itself. The gateway write handler should call `update()` with the device's authoritative offroad state; it must not trust an offroad flag supplied by KovaLink.

The `aiohttp` Gateway in `openpilot/kovapilot/gateway` exposes `GET /v1/health`, `GET /v1/kova-params`, and `POST /v1/kova-params/update`. It starts automatically on comma. Local-network sessions can save writable catalog settings, including control preferences. The Gateway enforces each setting's access and policies. See [KovaLink Gateway](KOVALINK_GATEWAY.md) for the transport modes.

The Gateway appends a `state` to each dependency in read and successful update responses. States are `met`, `unmet`, `pendingRestart`, `pendingConsumer`, `unsupported`, and `unknown`. For openpilot longitudinal control, it distinguishes the saved preference from the mode selected by the current vehicle session. Dependency results explain availability in the app and do not change stored preferences or replace the runtime's existing control checks.

## Development transport test

`tools/kovapilot/kovalink_gateway_dev.py` runs the same loopback-only application with an optional fixture catalog (`openpilot/kovapilot/tests/fixtures/kova_params_e2e.json`). It refuses to start without an isolated `PARAMS_ROOT` and always binds `127.0.0.1`. KovaLink's end-to-end transport test drives it and the real `python -m openpilot.kovapilot.gateway` entry point.

## Display: onroad screen brightness

`display.onroad_brightness_mode` uses `0` for auto, `1` for auto (dark), and `2` for fixed. It defaults to `0`. `display.onroad_brightness_fixed` uses integers from 10 to 100 percent, with a step of 5 and a default of 50. Both settings have `risk: display` and `applyPolicy: immediate`, so KovaLink may change them while driving.

- The consumer is `openpilot/kovapilot/display/brightness.py`. The UI reads `KovaParams` in its 5 Hz params thread with a tolerant reader: an unreadable or invalid value falls back to `auto` instead of failing, because this is display-only.
- The upstream auto-brightness pipeline is unchanged (camera exposure, CIE curve, 10 s filter, backlight floor of 30 %, UI dim overlay below the floor). `onroad_brightness()` only maps the filtered upstream value to the selected target, right before the floor/dim step in `Device._update_brightness`.
- `auto`: identical to upstream for every input.
- `auto_dark`: rescales the upstream range `[BRIGHTNESS_MIN, 100]` to `[10, 100]`, so low light gets darker and daylight stays at 100 %. Without a live road-camera light estimate it keeps the upstream value.
- `fixed`: uses the fixed value. Values below 30 % are produced by the dim overlay, not the backlight.
- While an onroad alert is shown (`selfdriveState.alertSize != none`) the result is never dimmer than upstream auto. Offroad brightness, the sleep timeout and display power are unchanged. Onroad screen-off is intentionally not offered.
