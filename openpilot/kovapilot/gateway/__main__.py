"""Run the KovaLink Gateway with optional authenticated Wi-Fi access."""

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path

from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.gateway import DEVICE_PACKAGES


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8082


def main() -> None:
  parser = argparse.ArgumentParser(description="KovaLink gateway")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  args = parser.parse_args()
  if not 1 <= args.port <= 65535:
    parser.error("port must be between 1 and 65535")

  if not PC and DEVICE_PACKAGES.is_dir():
    sys.path.insert(0, str(DEVICE_PACKAGES))

  from aiohttp import web

  from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend
  from openpilot.kovapilot.gateway.app import create_app
  from openpilot.kovapilot.gateway.camera import CameraSignaling
  from openpilot.kovapilot.gateway.git_maintenance import GitRepository
  from openpilot.kovapilot.gateway.lan import WIFI_INTERFACE, load_lan_config, serve_lan
  from openpilot.kovapilot.gateway.telemetry import CerealTelemetry

  settings_params = Params()
  device_state_params = Params()
  store = KovaParamsStore(ParamsKovaParamsBackend(settings_params))
  lan = None
  try:
    lan = load_lan_config(Path(Paths.persist_root()) / "kova" / "gateway")
  except (OSError, ValueError):
    cloudlog.warning("KovaLink Gateway Wi-Fi disabled: invalid TLS or credential configuration")
  app = create_app(
    store, lambda: device_state_params.get_bool("IsOffroad"),
    telemetry=CerealTelemetry(), camera=CameraSignaling(device_state_params),
    git=GitRepository(BASEDIR, lambda: device_state_params.get("UpdaterState")),
    lan_access=lan[0] if lan else None,
  )
  if lan is None:
    web.run_app(app, host=LOOPBACK_HOST, port=args.port, access_log=None)
  else:
    advertise = None
    if not PC:
      try:
        from openpilot.kovapilot.gateway.discovery import advertise_gateway
      except ImportError:
        cloudlog.warning("KovaLink Gateway DNS-SD unavailable")
      else:
        advertise = partial(
          advertise_gateway, WIFI_INTERFACE, args.port, lambda: cloudlog.warning("KovaLink Gateway DNS-SD unavailable")
        )
    asyncio.run(serve_lan(
      app, args.port, lan[1], lambda: cloudlog.warning("KovaLink Gateway Wi-Fi bind failed"), advertise=advertise,
    ))


if __name__ == "__main__":
  main()
