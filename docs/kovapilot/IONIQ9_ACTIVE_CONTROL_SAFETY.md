# IONIQ 9 exact-evidence lateral control

Status: historical vehicle-profile evidence; superseded by `HYUNDAI_CANFD_ADAS_CONTROL_STATUS.md` for the generic control program.

## Scope

This change introduced the capability-driven Hyundai CAN-FD `LFA_ALT` angle-control path for the observed Korean IONIQ 9 configuration. The consolidated branch also contains a separately gated longitudinal staging implementation. It does not port Carrot safety relaxations, HUD/CCNC replacement, TCS/MDPS retransmission, radar writes, ECU disable, communication-control, or tester-present traffic.

The observed Carrot behavior is a packet and topology reference, not a safety authority.

## Runtime authorization

Lateral staging requires all of the following:

1. Exact radar, camera, and ADAS firmware matches.
2. Every required passive bus-role/address/DLC tuple matches.
3. The existing development-mode opt-in is enabled.

Lateral staging requires the reviewed source gate, a non-release build, and exact firmware/topology evidence. If the source gate is off, the build is a release, or any evidence is missing or different, the interface selects `dashcamOnly` and `SafetyModel.noOutput`. With the independent source-level longitudinal staging flag at its default `False`, the interface clears the LONG safety bit and forces:

- `alphaLongitudinalAvailable=False`
- `openpilotLongitudinalControl=False`
- `pcmCruise=True`

MDPS firmware is not part of the exact match because the approved read-only query received no response on the tested paths. This remains a vehicle-validation blocker; runtime MDPS angle, liveness, and fault fields are safety inputs, but they do not establish firmware identity.

## Transmit boundary

The lateral safety allowlist contains one command:

- `0xCB`, bus 0 (ECAN), DLC 24, nominal 100 Hz

Stock camera `0xCB` on bus 2 is received with checksum/counter/liveness validation and blocked from forwarding while the replacement is active. In the default lateral-only mode, stock `0x1A0` is not blocked and continues to provide factory ACC/braking behavior.

The replacement changes only counter, angle-control state, requested angle, and angle torque-reduction gain. Bytes 7–23, including FCA/ESA fields, must exactly match the latest valid stock camera frame. No `0x12A`, `0x1A0`, button, TCS, MDPS, HUD, or diagnostic command is permitted by this profile.

## Angle safety contract

- Active state is exactly 2 with gain raw 250; inactive state is exactly 1 with gain 0.
- Active control requires MDPS compensated angle samples strictly inside ±60 degrees.
- The controller clamps active planner requests to ±59.9 degrees; at a measured ±60-degree boundary it sends measured-follow inactive state.
- Active control is blocked when driver torque samples exceed ±250.
- Requested angle has a hard ±175-degree encoding/inactive clamp.
- Speed-dependent wind-up/down rates are enforced.
- Active request must remain within ±10 degrees of MDPS measured angle.
- Inactive request must follow the measured angle, clamped to the encoding limit.
- MDPS invalid angle or warning/LKA/ACI fault clears controls.
- CRC, monotonic 8-bit counter with one-frame resynchronization, nominal frequency, relay malfunction, forwarding, controls-off, state, gain, and stock payload are checked.
- A rejected protocol/measurement/frequency frame cannot advance the desired-angle limiter state.

The 60-degree active boundary is intentionally more conservative than the observed Carrot implementation, which often reduces gain rather than becoming inactive at higher angle or during driver intervention.

## Evidence limits

Downloaded recent logs support the normal `0xCB` layout, 100 Hz rate, state values, CRC, counter, inactive clamp, and MDPS response. They do not prove:

- MDPS firmware identity;
- physical SET/RES/CANCEL transition behavior;
- dashboard DTC absence after this implementation;
- AEB, FCW, or emergency-steering behavior during an actual event;
- cold-start firmware-query repeatability;
- safe vehicle actuation.

Preserving FCA/ESA bytes removes a known overwrite hazard, but it is not end-to-end collision-safety validation.

## Verification

Latest local regression bundle:

- 1,859 passed
- 517 skipped
- 9,366 subtests passed
- Ruff passed
- `git diff --check` passed

The consolidated branch subsequently completed the full macOS SCons build after selecting the project Python/Cap'n Proto tools, materializing SHA-256-verified public LFS models with implicit HF token use disabled, and allowing the local Metal compile outside the sandbox. See `HYUNDAI_CANFD_ADAS_CONTROL_STATUS.md` for the current regression counts and exact commit.

## Release and vehicle gate

Merging this implementation does not authorize active vehicle use. Before any parked transmit test, an independent safety review must close the remaining blockers and a device build must confirm default noOutput behavior, exact-match behavior, no LONG bit, and the single-message TX boundary. Road testing remains prohibited until the staged validation packet explicitly advances it.
