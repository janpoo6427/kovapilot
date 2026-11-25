"""Install the bundled Gateway dependencies in a dedicated manager process."""

import os
import subprocess
import time

from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.gateway.runtime import install_runtime


def prepare_runtime() -> None:
  while True:
    try:
      install_runtime()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
      code = getattr(error, "errno", None) or getattr(error, "returncode", None)
      cloudlog.warning(f"KovaLink Gateway runtime preparation failed ({type(error).__name__}, code {code}). Retrying.")
      time.sleep(60)
    else:
      return


def main() -> None:
  os.nice(10)
  prepare_runtime()
