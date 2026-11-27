# IONIQ 9 exact-evidence lateral control

Status: historical vehicle-profile evidence; superseded by `HYUNDAI_CANFD_ADAS_CONTROL_STATUS.md` for the generic control program.

2026-11-27: the exact firmware match below was removed. CAN-FD angle steering and its longitudinal profile are now chosen from the passive message topology alone, the same for every Hyundai CAN-FD car.

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

- Active state is exactly 2 with gain raw 1–250 (the MDPS torque ceiling); inactive state is exactly 1 with gain 0.
- Active control continues up to the ±175-degree command limit. The controller clamps planner requests to ±175 degrees and holds the command there instead of releasing; a request past the limit raises `steerSaturated` ("Take Control" / "Turn Exceeds Steering Limit") after 0.4 s at any speed, unless the driver steered in the last 2 s.
- The controller sends measured-follow inactive state only when the measured angle is past ±183 degrees (175 + the 8-degree measured error), i.e. the driver turned the wheel past the limit.
- Driver torque and `steeringPressed` do not release control. They lower the MDPS torque ceiling (`ADAS_ACIAnglTqRedcGainVal`) by a speed-dependent table (floor 0.1–0.3, ceiling 0.85, down 0.014 per frame). After the driver lets go the ceiling recovers from floor to ceiling in 0.5 s; an intervention (`steeringPressed` 0.3 s) within 5 s of a full recovery slows the next recovery to 1, 2 and 3 s, and 5 s without one resets it.
- panda does not block active frames on driver torque.
- panda bounds active requests by lateral acceleration and jerk (vehicle model, 3.59 m/s² and 3.59 m/s³ at the lowest seen speed − 1 m/s; IONIQ 9 slip factor, steer ratio 16.02, wheelbase 3.13 m) instead of a fixed angle and speed table. The controller uses the same 3.6 m/s² / 3.6 m/s³ limits, 1.2 degrees per frame and the speed table.
- Requested angle has a hard ±175-degree encoding/inactive clamp.
- Active request must remain within ±10 degrees of MDPS measured angle (controller: ±8 degrees).
- Inactive request must follow the measured angle, clamped to the encoding limit.
- MDPS invalid angle or warning/LKA/ACI fault clears controls.
- CRC, monotonic 8-bit counter with one-frame resynchronization, nominal frequency, relay malfunction, forwarding, controls-off, state, gain, and stock payload are checked.
- A rejected protocol/measurement/frequency frame cannot advance the desired-angle limiter state.

The earlier ±60-degree active boundary and the driver-torque release (225 for one frame, or `steeringPressed`) were removed on 2026-11-27 (ramp and tight-turn releases). Carrot and sunnypilot also command up to 175 degrees on `0xCB` and reduce gain instead of releasing.

Sources (behavior reference; the code is ours):

- Torque reduction gain curve from sunnypilot opendbc `hkg-angle-steering-2025` `compute_torque_reduction_gain` (MIT).
- Recovery time from carrot `47b1d34` `carcontroller.py` (MIT), without its model-uncertainty term.
- panda lateral acceleration/jerk check follows the sunnypilot `hkg-angle-steering-2025` behavior with the same IONIQ 9 values.

Vehicle limits not yet confirmed by logs: the DBC lists the `0xCB` requested-angle range as 0–119.9 ("Capped at 119.9"); whether the MDPS follows requests above 120 degrees, and whether it faults between 120 and 175 degrees, still needs vehicle logs.

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
