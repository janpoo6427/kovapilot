# KovaLink Gateway

On comma, manager installs the bundled Gateway Python packages at boot in a
separate, low-priority process, including when the vehicle is already onroad.
The installation uses the checked-in AGNOS wheels and validates them against
`uv.lock` without network access. Later boots reuse the installed packages.
The Gateway starts automatically when its packages are ready.

For a first install, use the `master-jh` branch in the standard kovapilot
installation flow. A fresh checkout of that branch carries the wheels and
starts Gateway on the first normal AGNOS boot.

The native loopback endpoint is `http://127.0.0.1:8082`. Wi-Fi access uses
TLS on the current private IPv4 address of `wlan0`, for clients on that subnet.
The listener follows Wi-Fi address changes and closes old WebSockets. When
AGNOS restricts interface binding to privileged users, the regular Gateway
process binds the exact Wi-Fi address and enforces the same peer and
authentication checks on each request.

## Automatic Wi-Fi connection

Boot comma and open KovaLink on the same private Wi-Fi network. The device
creates its TLS identity automatically and announces `_kovalink._tcp` after
the listener binds. Link checks the Gateway's local network access mode and
pins the certificate for the connection. If discovery is unavailable, use
**IP로 연결** from the phone.

Devices on the same private subnet can use the Gateway's allowlisted
telemetry, camera and settings APIs. Automatic Wi-Fi connections can save
catalog settings marked writable, including control preferences. Each
setting follows its save and apply policies. Git maintenance uses the
authorized updater workflow below.
Use a network whose members you trust.

### Owner phone

Git apply, device restart and Tailscale need the owner phone. On automatic
Wi-Fi, register it from KovaLink with the vehicle off: **장치 연결 → 소유자로
등록** asks the comma for a code (`POST /v1/owner/pair`). The comma shows a
six-digit code as an offroad alert for two minutes. Typing it in KovaLink
(`POST /v1/owner/confirm`) returns a new owner token once. The Gateway keeps
only its SHA-256 in `owner.sha256` next to the automatic TLS identity.

The owner phone sends `Authorization: Bearer <token>` on every request. Other
phones on the subnet keep the automatic access above without a header. A
request that sends a wrong or replaced owner token gets 401. Five wrong codes
end the code. Pairing again with a new code from the comma screen replaces
the previous owner.

`GET /v1/health` on Wi-Fi also reports `owner` (this request is from the
owner), `ownerRegistered` and `ownerPairing` (the code flow is available).
With the bearer setup below, the provisioned token is the owner token and
code pairing is unavailable.

Settings with `savePolicy: anytime` accept authorized saves independently of
ignition and gear. Brightness and stock ACC diagnostics are live. Cruise
accelerator sync takes its saved value when `card` starts. The settings
response reports the running process's acknowledged value separately, with
`applyState` set to `applied`, `pendingRestart`, or `awaitingStart`.
Saving never restarts the process.

`engagement.auto_engage` can be saved while driving. `selfdrived` re-reads it
about once a second: turning it off stops any later automatic start at once,
and turning it on arms the next departure from Park. The Gateway reports the
value `selfdrived` last published as `appliedValue`, `applyState: applying`
until it matches, and per-axis `runtimeStates`. It works only with openpilot
longitudinal control applied.

`cruise.curve_slowdown` defaults to `0` (off) and uses `1` for on. On a
supported vehicle, authorized users can save the preference while openpilot
longitudinal control is off. The Gateway reports `awaitingLongitudinal` until
that dependency is active and `awaitingStart` if the planner has yet to
acknowledge a value. When the planner's `appliedValue` differs from the saved
`value`, the Gateway reports `pendingParked`. The running planner applies the
change in Park at a standstill with controls disengaged. An unsupported vehicle
or planner reports `applyStatus: unsupported` and the setting is read-only.

The `cruise.openpilot_longitudinal` switch shows the existing
`AlphaLongitudinalEnabled` preference as 0 or 1 and reports the effective
`CarParams.openpilotLongitudinalControl` separately. It is writable when
the current vehicle supports alpha longitudinal and the device sees Park,
standstill and disengaged controls. Writes include `expectedValue` for the
last reviewed preference. The next normal vehicle session performs the
standard capability checks and applies the preference.

## Optional bearer authentication

On a comma running this branch, switch the vehicle off and open an interactive
terminal as the regular device user. From `/data/openpilot`, run the setup:

```sh
cd /data/openpilot
bash tools/kovapilot/kovalink_gateway_setup.sh
```

The launcher uses the AGNOS Python environment used by manager. It can also
prepare the packages if the offroad boot process has not finished. It uses
the operator's token and verifies the bearer configuration. Later boots
reuse the same registration.

For the initial setup, generate a random token in your password manager with
64 lowercase hexadecimal characters (32 random bytes). Paste it into the
hidden prompt. Both preparation paths use the device's `uv` and the bundled
AGNOS wheels to install only Gateway packages into `/data/kova/gateway/python`.

On the phone, use a KovaLink build with authenticated Wi-Fi support. From
**장치 연결**, select the discovered device and use the token and public
certificate pin from setup. After the TLS listener binds, the Gateway
announces `_kovalink._tcp` through Avahi on the same device interface. If the
network blocks discovery, use **IP로 연결** with the address on the comma
screen. The phone needs a network route to that address.

The tool generates these files under `/persist/kova/gateway`. If the Gateway
was already running, restart comma after adding this configuration. When
that directory exists, the Gateway requires a valid bearer configuration to
start its Wi-Fi listener.

- `server.pem` contains the device TLS certificate and any certificate chain.
- `server.key` contains the matching private key.
- `token.sha256` contains the lowercase hex SHA-256 of the ASCII bearer token.
  The token itself is 64 lowercase hex characters from 32 random bytes.

The private key and digest must be owned by the Gateway process user with mode
`0600` or more restrictive. Use a trusted channel to provision the client token
and the device certificate or public-key pin. Restart the Gateway after changing
or removing this configuration to revoke an old token.

Enter the comma's current Wi-Fi IPv4 address as the Gateway host. For example,
with `192.168.43.20`, HTTP APIs use `https://192.168.43.20:8082` and telemetry
uses `wss://192.168.43.20:8082/v1/telemetry`. HTTP and WebSocket handshakes
require `Authorization: Bearer <token>` in bearer mode, including
`GET /v1/health`.
Clients must authenticate the server before sending the token. Use the
trusted certificate with an IP SAN matching the current address, or an
explicitly provisioned device identity pin if the client supports it. After an
IP change, a client using standard IP SAN validation needs a certificate for
the new address.

KovaLink verifies the provisioned public-key pin before sending the bearer
token. Finder results identify candidate endpoints. Registration still needs
the trusted pin and token.

## Refreshing the boot packages

When the Gateway dependency group changes, export its locked requirements.
Download its CPython 3.12 Linux arm64 wheels into an empty staging directory
and replace the files under `openpilot/kovapilot/gateway/wheels/agnos` with
that set. Keep the wheel archives intact so their package metadata and
licenses stay with the distribution.

```sh
requirements=$(mktemp)
wheel_staging=$(mktemp -d)
uv --offline export --frozen --only-group kovalink-gateway --no-emit-project \
  --format requirements-txt > "$requirements"
uv run --no-project --with pip python -m pip download --require-hashes \
  --only-binary=:all: --implementation cp --python-version 3.12 --abi cp312 \
  --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 \
  --dest "$wheel_staging" -r "$requirements"
```

| Endpoint | Contract |
| --- | --- |
| `GET /v1/health` | Gateway version and connection scope |
| `POST /v1/owner/pair` | Automatic Wi-Fi, offroad: show a six-digit owner code on the comma |
| `POST /v1/owner/confirm` | `{"code"}` from the comma screen; returns the owner token once |
| `GET /v1/kova-params` | Settings document and device state |
| `POST /v1/kova-params/update` | Revision-checked settings update |
| `WS /v1/telemetry` | Flat scene JSON at up to 20 Hz when a fresh carState is available |
| `POST /v1/stream` | SDP answer for a single road or wideRoad camera offer |
| `GET /v1/git/status` | Current branch, head, upstream, dirty flag and updater state |
| `GET /v1/git/branches` | Refreshes origin branch refs, then returns their commits |
| `GET /v1/git/log?limit=20&cursor=0` | Current HEAD history and next offset |
| `POST /v1/git/plan` | Validated switch or reset preview |
| `POST /v1/git/switch`, `POST /v1/git/reset` | Operation-specific preview aliases |
| `POST /v1/git/apply` | Queue a reviewed branch or commit for the updater (Park or offroad) |
| `POST /v1/device/restart` | Ask manager to reboot the device, at any time |

Telemetry frames include `schemaVersion: 2`, `monotonicTimeNanos` and
`speedMps`. The frame can also include gear, turn signals, brake status,
steering angle, cruise set speed, engagement and model geometry.
Stale source data is omitted. Model geometry is display only and uses the
app frame: `x` forward, `y` left, meters rounded to cm (schema 1 sent the
model's right-positive `y`): `plannedPath`, `laneLines` (`index` 0-3 far left
to far right, `probability`, `points`; lines under 0.2 are left out),
`roadEdges` with `roadEdgeStds` (both edges or none) and `modelFrameId`. Camera signaling takes `sdp` and `cameras`.
The proxy sends the daemon an allowlisted request with empty cereal bridges.

If Git cannot inspect the working tree, status and previews return
`dirtyKnown: false`. Previews compare committed snapshots and can show the
file count with local changes still unknown.

Git plans refresh origin before resolving an `operation` of `switch` or
`reset` against the current remote branch refs. Fetches update only
`refs/remotes/origin/*`; the checkout and `FETCH_HEAD` are left in place.
If a fetch fails or reaches its deadline, the request returns `git_unavailable`.
Reset previews also require a full `commit` SHA
reachable from the selected ref. Plans return the target, current HEAD,
dirty status, affected file count, restart requirement and a digest.
`mutationEnabled` indicates whether the updater accepts maintenance
from this client. It requires bearer authorization over Wi-Fi, or native
loopback, the device offroad or stopped in Park with steering and
longitudinal control released (the check Park settings use), updates not
disabled, no update already waiting and an idle updater. Otherwise
`mutationBlock` is `authorization` or `parkedOrUpdaterBusy` (gateways before
2026-11-27 said `offroadOrUpdaterBusy` and required offroad). Many installs
power the comma down with the ignition, so Park is enough. With a Link
request queued or staged, manager keeps `updated` running with the vehicle on;
it starts staging only offroad or in Park and fails the request if neither
holds right before finalizing. Send the preview's operation, branch,
optional reset commit and `planDigest` to `POST /v1/git/apply`. The server
rechecks the installed checkout, clean worktree and fetched origin target.
HTTP 202 returns the queued maintenance request. Status reports queued,
staged, installed or failed. The updater stages the reviewed commit. The
prepared update is eligible for installation at the next device restart.
The status and branch
APIs avoid changing Git refs while a Link update is queued or staged.

Telemetry frames also carry, display only:

- `navigation`: `source` (`carrotV2` = carrot modified TMAP over WebSocket v2 on TCP 7714,
  `carrotLegacy` = UDP 7706, cameras only, or `none`), `appVersion`, `lastReceivedAgeMs`, and for
  v2 `guidanceActive` and `offRoute` (from navcamd `kovaNavigation`, sent while it is under 1 s old).
- `tbt` (v2 with a current guide point): `maneuver` (KovaLink sign from the TMAP turn code, `unknown`
  for rotaries and toll gates), `turnType`, `distanceMeters`, `mainText`, `roadName`, `nearDirection`,
  `nextManeuver`/`nextTurnType`/`nextDistanceMeters` (from the car)/`nextRoadName`,
  `remainingDistanceMeters`, `remainingSeconds`; `speedLimitKph` is the app road speed limit.
- `autoLaneChange`: `setting` (`on`/`off`/`unknown`), `laneChange` (modelV2 lane change state),
  `direction`, `auto` (`modelV2.meta.kovaAutoLaneChange` when the build has it, else `unknown`),
  `blindSpotLeft`/`blindSpotRight` and `blindSpotFresh` (`null` when the build has no such field).

`GET /v1/git/status` also returns `restartEnabled`. `POST /v1/device/restart`
sets `DoReboot`, the same request as the device's Reboot button, and returns
HTTP 202 `{"restarting": true}`. It needs the same authorization as apply
and nothing else (janpoo6427 2026-11-27): it works offroad, with the vehicle
on and while driving, where it ends steering and longitudinal control at
once, and while an update is written. Without authorization it returns 403
`restart_blocked`. The launch script installs only an update that was
finalized completely (`.overlay_consistent`), so a restart during a download
or finalize keeps the installed version and the queued Link request starts
again. A staged Link update is installed by that restart.

Boot migrates old external Git LFS filter configuration to the included LFS
client when the external command is missing.
