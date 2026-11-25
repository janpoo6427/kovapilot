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
telemetry, camera and settings APIs. Wi-Fi connections using only network
access can write display, comfort and behavior settings subject to their
save policies. Control settings are read-only in this mode. Git
apply stays disabled. Use a network whose members you trust.

Settings with `savePolicy: anytime` accept authorized saves independently of
ignition and gear. Brightness and stock ACC diagnostics are live. Cruise
accelerator sync takes its saved value when `card` starts. The settings
response reports the running process's acknowledged value separately, with
`applyState` set to `applied`, `pendingRestart`, or `awaitingStart`.
Saving never restarts the process.

`cruise.curve_slowdown` defaults to `0` (off) and uses `1` for on. It applies to vehicles with openpilot
longitudinal control. Authorized saves remain in `pendingParked` until the
running planner confirms the vehicle is stationary in Park with controls
disengaged. The planner then applies the value without a restart.

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
| `GET /v1/kova-params` | Settings document and device state |
| `POST /v1/kova-params/update` | Revision-checked settings update |
| `WS /v1/telemetry` | Flat scene JSON at up to 20 Hz when a fresh carState is available |
| `POST /v1/stream` | SDP answer for a single road or wideRoad camera offer |
| `GET /v1/git/status` | Current branch, head, upstream, dirty flag and updater state |
| `GET /v1/git/branches` | Locally observed origin branches and their commits |
| `GET /v1/git/log?limit=20&cursor=0` | Current HEAD history and next offset |
| `POST /v1/git/plan` | Validated switch or reset preview |
| `POST /v1/git/switch`, `POST /v1/git/reset` | Operation-specific preview aliases |
| `POST /v1/git/apply` | Returns HTTP 403 until authenticated updater integration is available |

Telemetry frames include `schemaVersion: 1`, `monotonicTimeNanos` and
`speedMps`. The frame can also include gear, turn signals, brake status,
steering angle, cruise set speed, engagement and model path or lane points.
Stale source data is omitted. Camera signaling takes `sdp` and `cameras`.
The proxy sends the daemon an allowlisted request with empty cereal bridges.

If Git cannot inspect the working tree, status and previews return
`dirtyKnown: false`. Previews compare committed snapshots and can show the
file count with local changes still unknown.

Git plans accept an `operation` of `switch` or `reset`, and a `branch` from
the observed origin refs. Reset previews also require a full `commit` SHA
reachable from the selected ref. Plans return the target, current HEAD,
dirty status, affected file count, restart requirement and a digest.
`mutationEnabled` remains false. KovaLink should treat these operations as
previews and keep the apply control disabled.
