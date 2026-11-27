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

The first sample is `steering.always_on` (`항시 조향`). It defaults to `0`, can be saved anytime and applies from the next start: card sets `ALT_EXP_ALWAYS_ON_LATERAL` (1024) in `CarParams.alternativeExperience` on supported Hyundai/Kia/Genesis vehicles, panda safety then allows lateral control after a driver engagement without cruise (also while braking, which only ends cruise; never at standstill or with invalid RX checks), and selfdrived runs `kovapilot/bridges/always_lateral_runtime.py`.

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

## Automatic engagement

`engagement.auto_engage` defaults to Off. Changes are saved while stationary in Park with driver assistance disengaged and loaded at the start of the next vehicle session. The Gateway exposes steering and cruise separately in `runtimeStates`.

The automatic request is made at the moment the accelerator is pressed in Drive after an observed Park state (janpoo 11/27 22:28; before, it waited for the release, when the panda authority from the press may already be gone). The control readiness checks (except the accelerator override of the press itself and panda authority) must have held for 1 s before the press, and the current speed must be in the normal cruise initialization range at the press. The request then waits up to 0.5 s for panda to report control authority from that press, and the existing state machine starts in the accelerator override. CANCEL, a complete disengagement, or a restart of selfdrived in the same vehicle session ends automatic attempts for that session.

During an active engagement, the existing control loop continues to handle temporary steering and accelerator overrides. The automatic request does not grant independent steering or stock cruise authority.

### Pedal resume (Hyundai/Kia/Genesis CAN FD with openpilot longitudinal)

With `engagement.auto_engage` On at the start of a vehicle session, card also sets the panda flag `ALT_EXP_PEDAL_RESUME`. Panda then allows controls on the driver's own brake release or accelerator press seen on the car bus, while TCS reports cruise available and no CANCEL was pressed since the last SET/RES. Brake, CANCEL and cruise unavailable still end control authority at once; if openpilot does not engage, the heartbeat check ends it within about 3 seconds.

`selfdrived` requests engagement at those moments with the carrot automatic-cruise start conditions (`selfdrive/car/cruise.py` `_update_cruise_state`): brake release while moving; brake release at a standstill with a lead closer than 20 m and the wheel under 20 degrees; accelerator press from 30 km/h, unless the accelerator disengages (`DisengageOnAccelerator`); an accelerator press with a lead closer than 0.8 s of travel only below 1 m/s. Carrot starts on the accelerator release instead; its tap start (shorter than 0.4 s, at any speed) is not made, because a tap cannot be told apart at the press. Engaging while the accelerator is still pressed starts in the normal accelerator override, so openpilot accelerates only after the release. On such an engagement (first start or pedal resume, with no cruise button during the press) the set speed starts at the current speed with the normal SET rule (rounded, at least 40 km/h or `InitialCruiseSpeedKph`, at most 145 km/h), and with `cruise.accelerator_sync` On it follows the speed while the pedal stays held, like the accelerator sync during cruise, and keeps the value reached after the release. It does not start with a turn signal on, the wheel at 70 degrees or more, brake hold or the parking brake, outside Drive or in the first 20 s in Drive, within 5 s of holding the accelerator while slowing, or after CANCEL until SET/RES. The MAIN and LFA buttons do not block it. The request waits up to 0.5 s for panda authority and goes through the normal entry checks. Carrot's other automatic starts (soft hold at a stop, forward collision distance, desired speed, traffic light) are not made.

## Automatic lane change

`steering.auto_lane_change` (`자동 차선 변경`) defaults to `0`. Off keeps the stock `DesireHelper` (turn signal, then a nudge on the wheel). On, `openpilot/kovapilot/lateral/auto_lane_change.py` lets a lane change start without the nudge 1 s after the driver turns on one turn signal, only while openpilot is engaged (not always-on steering alone), at 30 km/h or more (`AUTO_LANE_CHANGE_MIN_SPEED`), with driver monitoring off or not warning, and once per turn signal. Braking or steering the other way before the start cancels it for that turn signal. It applies at once outside a lane change. The Gateway condition `blindSpotMonitor` is `met` on Hyundai/Kia/Genesis vehicles with `HAS_BSM`.

The side lane is judged by side-lane sources combined in `side_lane_clear`: the blind-spot monitor (required: `carState.blindspotFresh`, clear on that side for 1 s), the model's side lane (next lane line probability > 0.5 and 2.5 m to it and to the road edge 1 s ahead, for 0.2 s) and `RadarTrackSource`, which has no data yet and changes nothing. Without a blind-spot monitor or with its frame stopped (`blindspotFresh` false for 0.5 s) only the nudge works. During an automatic lane change, a blind-spot warning on the target side, braking, steering the other way, disengaging or a stale blind-spot frame cancels it; before the car crosses the line modeld replaces the model curvature with a pure-pursuit curvature to the original lane centre (at most 2 m/s²) until the car is within 0.3 m of the centre for 1 s and the model's lane change probability is below 0.02. After crossing, the lane change finishes in the new lane. `modelV2.meta.kovaAutoLaneChange` (`pending`, `waiting`, `returning`) drives the `autoLaneChange` alert.

## Navigation turn signal

`navigation.auto_blinker` (`내비 자동 방향지시등`) defaults to `0`. `openpilot/kovapilot/navigation/nav_blinker.py` turns the carrot-compatible app's fork and exit guidance (`nTBTTurnType`, `nTBTDist`, `nRoadLimitSpeed`) into a turn-signal request, which would then go through the automatic lane change conditions. No vehicle can turn on the turn signal from openpilot yet, so the Gateway condition `blinkerOutput` is `unsupported` and nothing sends a turn-signal frame (reasons per vehicle in the module docstring).

## Lead restart response

`cruise.lead_restart_response` defaults to `0` (Off). Values `1`, `2`, and `3` tune the initial departure delay and acceleration ramp during an active openpilot longitudinal session. Preferences are applied while stopped in Park with driver assistance disengaged. The Gateway reports the planner's applied value separately from the stored preference.

All three profiles require the same continuously tracked, radar-matched lead to move away before adding a departure response. The response can hold a monitored stop or reduce positive acceleration after the normal planner permits movement. The normal stop decision, deceleration requests, driver inputs, and accelerator limits take priority. Off and stock ACC keep their existing behavior.

## Display: onroad screen brightness

`display.onroad_brightness_mode` uses `0` for auto, `1` for auto (dark), and `2` for fixed. It defaults to `0`. `display.onroad_brightness_fixed` uses integers from 10 to 100 percent, with a step of 5 and a default of 50. Both settings have `risk: display` and `applyPolicy: immediate`, so KovaLink may change them while driving.

- The consumer is `openpilot/kovapilot/display/brightness.py`. The UI reads `KovaParams` in its 5 Hz params thread with a tolerant reader: an unreadable or invalid value falls back to `auto` instead of failing, because this is display-only.
- The upstream auto-brightness pipeline is unchanged (camera exposure, CIE curve, 10 s filter, backlight floor of 30 %, UI dim overlay below the floor). `onroad_brightness()` only maps the filtered upstream value to the selected target, right before the floor/dim step in `Device._update_brightness`.
- `auto`: identical to upstream for every input.
- `auto_dark`: rescales the upstream range `[BRIGHTNESS_MIN, 100]` to `[10, 100]`, so low light gets darker and daylight stays at 100 %. Without a live road-camera light estimate it keeps the upstream value.
- `fixed`: uses the fixed value. Values below 30 % are produced by the dim overlay, not the backlight.
- While an onroad alert is shown (`selfdriveState.alertSize != none`) the result is never dimmer than upstream auto. Offroad brightness, the sleep timeout and display power are unchanged. Onroad screen-off is intentionally not offered.
