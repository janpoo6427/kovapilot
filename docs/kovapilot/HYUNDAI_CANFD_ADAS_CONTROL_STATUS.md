# Hyundai CAN-FD ADAS-harness angle and longitudinal control

Status: canonical implementation branch, default off, vehicle actuation NO-GO.

## Program scope

The target is openpilot engagement plus lateral and longitudinal control on Hyundai CAN-FD platforms that use angle steering and an ADAS-harness connection. IONIQ 9 is the first evidence-backed vehicle profile, not the runtime program name or a generic authorization for other vehicles.

## Canonical branch

- kovapilot: `feature/hyundai-canfd-adas-control`
- owning opendbc: `feature/hyundai-canfd-adas-control@9c88c8f3`
- base kovapilot master: `6105e1e1d`

This branch supersedes the implementation branches `feature/ioniq9-port`, `feature/ioniq9-active-control`, `feature/ioniq9-angle-o4`, and the temporary local ADAS-long profile branch. The angle-o4 implementation is retained only as comparison evidence; its alternative controller/safety code is not merged into the canonical runtime.

## Selection contract

Runtime behavior is selected from typed capability, exact radar/camera/ADAS firmware, and passive bus-role/address/DLC evidence. A vehicle-name branch or the presence of `0xCB` alone never authorizes output. Every additional vehicle must define and validate its own evidence-backed profile.

If exact evidence is absent or different, the interface selects `dashcamOnly + SafetyModel.noOutput`. Carrot and Sunny remain wire-behavior and test-structure references, not safety authorities. Carrot safety relaxations are not included.

The reviewed lateral staging source gate is enabled only on this development branch. A non-release build and exact angle capability remain mandatory; source gate OFF, release builds, and every evidence mismatch remain `dashcamOnly + noOutput`. This gate is independent from the longitudinal setting and adds no LONG safety permission.

## Lateral staging

The lateral path sends only `LFA_ALT 0xCB`, ECAN bus 0, DLC 24, nominal 100 Hz. It preserves the stock camera FCA/ESA payload and replaces only counter, angle state, requested angle, gain, and checksum.

The panda contract checks exact address/bus/DLC, stock payload, CRC, counter, frequency, forwarding/relay state, driver torque, MDPS angle/fault state, measured error, active state/gain, and angle/rate limits. The initial validated profile bounds active control inside ±60 degrees; inactive output follows measured angle with zero authority.

The bursty HDA2 blind-spot frame is decoded without making the full powertrain parser invalid while it is quiet.

## Longitudinal staging

Longitudinal remains behind `ENABLE_CANFD_LONGITUDINAL_STAGING = False`. Enabling it requires a source change and new build; no Param or KovaLink setting can enable it.

Even with the source flag changed, activation additionally requires the development opt-in and exact `CANFD_LONGITUDINAL_CANDIDATE` evidence. The initial profile describes an ADAS physical harness with stock SCC on CAM and replacement SCC on ECAN, without ECU disable or diagnostic traffic.

The staged controller output is limited to:

- `0xCB`, ECAN, DLC 24, 100 Hz
- `SCC_CONTROL 0x1A0`, ECAN, DLC 32, 50 Hz

The `0x1A0` generator starts its counter after the observed stock counter, regenerates CRC, and changes only the explicit longitudinal actuation allowlist. All other DBC-defined object, warning, fault, and reserved fields come from the live stock CAM template. Loss of the 50 Hz stock template invalidates the camera parser.

No `0x12A`, CCNC/TCS synthesis, ADRV/FCA warning synthesis, tester-present, communication-control, radar disable, or ADAS disable is added.

## Verification

- Hyundai interface/controller plus CAN-FD safety: `1098 passed, 310 skipped, 742 subtests passed`
- Ruff: passed
- `git diff --check`: passed
- full macOS SCons build: 100% passed, including model/warp generation, longitudinal MPC, replay/Cabana tools, and panda firmware

The public LFS download was run with implicit Hugging Face token use disabled and verified each object against the repository pointer size and SHA-256.

## Offline shadow boundary

The MacBook workspace currently has no local full route for the initial vehicle profile. Unit/contract shadow generation is covered, but route-level stock-versus-generated `0x1A0` diff and AEB/FCW event replay cannot be claimed until a private full rlog is made available locally. Raw route data, identifiers, location, and video stay outside Git and Vault.

## Remaining blockers

- MDPS firmware identity
- dashboard DTC after the canonical build
- physical SET/RES/CANCEL and driver override transitions
- cold-start and ignition/sleep repetition
- end-to-end AEB/FCW and emergency-steering preservation
- parked/offroad device validation and subsequent closed-area approval

The successful local build and tests do not authorize device installation, panda flashing, sendcan, or vehicle actuation.
