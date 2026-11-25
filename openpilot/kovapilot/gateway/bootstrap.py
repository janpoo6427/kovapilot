"""Install Gateway dependencies in a dedicated offroad manager process."""

import os
import subprocess
import time

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.gateway.runtime import install_runtime


def prepare_offroad(params: Params) -> None:
  while params.get_bool("IsOffroad"):
    try:
      install_runtime()
    except (OSError, ValueError, subprocess.SubprocessError):
      cloudlog.warning("KovaLink Gateway runtime preparation failed; retrying offroad")
      time.sleep(60)
    else:
      return


def main() -> None:
  os.nice(10)
  # Let the manager receive the initial device state before requesting packages.
  time.sleep(3)
  prepare_offroad(Params())
