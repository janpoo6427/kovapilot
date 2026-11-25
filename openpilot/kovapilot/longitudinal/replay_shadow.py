import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from openpilot.kovapilot.config.kova_params import CURVE_SLOWDOWN_KEY, KovaParamsStore
from openpilot.kovapilot.navigation.camera_profile import CAMERA_PROFILE_KEY


class ReplayParams:
  """Read-only backend for an exported KovaParams state."""
  def __init__(self, state):
    self.state = state

  def get(self, key):
    return self.state


def read_replay_values(path: Path | None) -> dict[str, Any]:
  state = json.loads(path.read_text(encoding="utf-8-sig")) if path is not None else None
  if path is not None and not isinstance(state, dict):
    raise ValueError("KovaParams must contain a state object")
  return KovaParamsStore(ReplayParams(state)).values()


def read_camera_profile(path: Path | None) -> int:
  return read_replay_values(path)[CAMERA_PROFILE_KEY]


def observe_replay(events, *, camera_inputs=None, camera_profile: int = 0, geometry_only: bool = False, curve_enabled: bool = True):
  from openpilot.common.constants import CV
  from openpilot.kovapilot.bridges.camera_shadow import CameraShadow
  from openpilot.kovapilot.bridges.longitudinal_shadow import LongitudinalShadow
  from openpilot.kovapilot.longitudinal.shadow import is_fresh
  from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
  from openpilot.selfdrive.test.process_replay.migration import migrate, migrate_carState

  services = {"carParams", "carState", "carControl", "controlsState", "selfdriveState", "modelV2"}
  inputs = sorted((event for event in events if event.which() in services), key=lambda event: event.logMonoTime)
  shadow = LongitudinalShadow(V_CRUISE_MAX, V_CRUISE_UNSET, CV.KPH_TO_MS, geometry_only=geometry_only, curve_enabled=curve_enabled)
  cameras = CameraShadow(camera_profile) if camera_inputs is not None else None
  navigation = iter(camera_inputs or ())
  pending = next(navigation, None)
  car_state = None
  # Old logs published the driver set speed on controlsState. Use the same migration as process replay.
  for event in migrate(inputs, [migrate_carState]):
    if cameras is not None and event.which() == "carState":
      car_state = event
    observation = shadow.update(event)
    if observation is None:
      continue
    if cameras is None:
      yield observation
      continue
    now_ns = event.logMonoTime
    while pending is not None and pending.received_ns <= now_ns:
      cameras.update(pending)
      pending = next(navigation, None)
    ego_mps = None
    if car_state is not None and car_state.valid and car_state.carState.canValid and is_fresh(now_ns, car_state.logMonoTime):
      ego_mps = car_state.carState.vEgo
    report = cameras.observe(now_ns, ego_mps, observation.cruise_speed_mps, observation.candidate)
    yield {**asdict(observation), "navigation": report}


def main() -> None:
  parser = argparse.ArgumentParser(description="Record hypothetical deceleration candidates from a local openpilot rlog as JSONL.")
  parser.add_argument("rlog", type=Path, help="Local rlog, rlog.zst or rlog.bz2")
  parser.add_argument("--navigation", type=Path, help="Local normalized camera fixture in JSONL")
  parser.add_argument("--kova-params", type=Path,
                      help="KovaParams state JSON. Without it, replay evaluates curves in diagnostic mode and disables camera slowdown.")
  parser.add_argument("--geometry-only", action="store_true", help="Inspect geometry on passive recordings without selecting a curve cap")
  args = parser.parse_args()
  if not args.rlog.is_file():
    parser.error("a local rlog file is required")

  from openpilot.tools.lib.logreader import LogReader
  from openpilot.kovapilot.navigation.camera_input import read_camera_inputs

  try:
    camera_inputs = read_camera_inputs(args.navigation) if args.navigation is not None else None
    values = read_replay_values(args.kova_params) if args.kova_params is not None else {}
  except (OSError, ValueError) as exc:
    parser.error(str(exc))

  observed_frames = 0
  events = LogReader(str(args.rlog.resolve()), only_union_types=True)
  for observation in observe_replay(events, camera_inputs=camera_inputs, camera_profile=values.get(CAMERA_PROFILE_KEY, 0),
                                    geometry_only=args.geometry_only, curve_enabled=values.get(CURVE_SLOWDOWN_KEY, 1) == 1):
    payload = observation if isinstance(observation, dict) else asdict(observation)
    print(json.dumps(payload, allow_nan=False))
    observed_frames += 1
  if not observed_frames:
    parser.error("the log contains no modelV2 frames")


if __name__ == "__main__":
  main()
