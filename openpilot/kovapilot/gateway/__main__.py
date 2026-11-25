"""Run the first loopback-only KovaLink gateway."""

import argparse

from aiohttp import web

from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend
from openpilot.kovapilot.gateway.app import create_app
from openpilot.kovapilot.gateway.camera import CameraSignaling
from openpilot.kovapilot.gateway.git_maintenance import GitRepository
from openpilot.kovapilot.gateway.telemetry import CerealTelemetry


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8082


def main() -> None:
  parser = argparse.ArgumentParser(description="KovaLink loopback gateway")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  args = parser.parse_args()
  if not 1 <= args.port <= 65535:
    parser.error("port must be between 1 and 65535")

  settings_params = Params()
  device_state_params = Params()
  store = KovaParamsStore(ParamsKovaParamsBackend(settings_params))
  app = create_app(
    store, lambda: device_state_params.get_bool("IsOffroad"),
    telemetry=CerealTelemetry(), camera=CameraSignaling(device_state_params),
    git=GitRepository(BASEDIR, lambda: device_state_params.get("UpdaterState")),
  )
  web.run_app(app, host=LOOPBACK_HOST, port=args.port, access_log=None)


if __name__ == "__main__":
  main()
