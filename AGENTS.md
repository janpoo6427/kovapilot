# kovapilot Repository Rules

Read `D:\kova\AGENTS.md` and the linked Vault rules before working in this repository.

## Repository boundaries

- `origin` is the independent kovapilot repository.
- `up-openpilot` is the authoritative comma.ai upstream baseline.
- `up-carrot` is a reference-analysis remote only. Do not merge, cherry-pick, or copy code from it without an explicit design and license review.
- Keep custom implementation under `openpilot/kovapilot/` where practical. Use narrow bridges into upstream code.
- Treat `opendbc_repo`, `panda`, `msgq_repo`, `rednose_repo`, `teleoprtc_repo`, and `tinygrad_repo` as pinned submodules. Follow any nested `AGENTS.md` inside them.

## Safety boundary

- Do not alter panda/opendbc safety, CAN actuation, steering, braking, acceleration commands, or engagement behavior without an approved safety task packet.
- New control behavior must default off and progress through unit/contract tests, simulation or replay, and explicit device/vehicle gates.
- Analyze behavior from current source. Fork behavior is reference material, not a correctness oracle.

## Required project references

- `D:\kova\kova-vault\20-Architecture\OPENPILOT_BASELINE.md`
- `D:\kova\kova-vault\20-Architecture\KOVAPILOT_DIRECTION.md`
- `D:\kova\kova-vault\10-Rules\GIT_AND_UPSTREAM.md`
- `D:\kova\kova-vault\10-Rules\SAFETY_AND_EVIDENCE.md`
- Repository details under `docs/kovapilot/`

Use WSL2/Linux or macOS for openpilot setup, build, and tests. Do not treat Windows-native Git/LFS output as equivalent evidence.
