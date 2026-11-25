import argparse
from dataclasses import asdict
import json
from pathlib import Path


def observe_replay(events):
  from openpilot.common.constants import CV
  from openpilot.kovapilot.bridges.longitudinal_shadow import LongitudinalShadow
  from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
  from openpilot.selfdrive.test.process_replay.migration import migrate, migrate_carState

  services = {"carParams", "carState", "carControl", "controlsState", "modelV2"}
  inputs = sorted((event for event in events if event.which() in services), key=lambda event: event.logMonoTime)
  shadow = LongitudinalShadow(V_CRUISE_MAX, V_CRUISE_UNSET, CV.KPH_TO_MS)
  # Old logs published the driver set speed on controlsState. Use the same migration as process replay.
  for event in migrate(inputs, [migrate_carState]):
    observation = shadow.update(event)
    if observation is not None:
      yield observation


def main() -> None:
  parser = argparse.ArgumentParser(description="Record hypothetical deceleration candidates from a local openpilot rlog as JSONL.")
  parser.add_argument("rlog", type=Path, help="Local rlog, rlog.zst or rlog.bz2")
  args = parser.parse_args()
  if not args.rlog.is_file():
    parser.error("a local rlog file is required")

  from openpilot.tools.lib.logreader import LogReader

  observed_frames = 0
  for observation in observe_replay(LogReader(str(args.rlog.resolve()), only_union_types=True)):
    print(json.dumps(asdict(observation), allow_nan=False))
    observed_frames += 1
  if not observed_frames:
    parser.error("the log contains no modelV2 frames")


if __name__ == "__main__":
  main()
