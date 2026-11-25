# KovaLink Gateway

The gateway runs on `127.0.0.1:8082`. The manager starts it when
`KovaLinkGatewayEnabled` is set. This development transport accepts native
loopback clients. Pairing and authenticated LAN access are future steps.

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

Git plans accept an `operation` of `switch` or `reset`, and a `branch` from
the observed origin refs. Reset previews also require a full `commit` SHA
reachable from the selected ref. Plans return the target, current HEAD,
dirty status, affected file count, restart requirement and a digest.
`mutationEnabled` remains false. KovaLink should treat these operations as
previews and keep the apply control disabled.
