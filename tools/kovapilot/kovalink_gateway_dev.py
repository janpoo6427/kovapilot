#!/usr/bin/env python3
"""Development-only KovaLink gateway runner for transport tests.

Runs the same loopback-only gateway application as ``python -m
openpilot.kovapilot.gateway`` but can load a fixture catalog. It refuses to
start unless ``PARAMS_ROOT`` points at an isolated Params directory, so it
never touches a device's real Params. It always binds 127.0.0.1.

  PARAMS_ROOT=/tmp/kova-e2e/params python tools/kovapilot/kovalink_gateway_dev.py \
    --port 18082 --catalog openpilot/kovapilot/tests/fixtures/kova_params_e2e.json
"""

import argparse
import json
import os
from pathlib import Path

from aiohttp import web

from openpilot.common.params import Params
from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend, validate_catalog
from openpilot.kovapilot.gateway.__main__ import LOOPBACK_HOST
from openpilot.kovapilot.gateway.app import create_app


def main() -> None:
  parser = argparse.ArgumentParser(description="KovaLink loopback gateway (development fixture runner)")
  parser.add_argument("--port", type=int, default=18082)
  parser.add_argument("--catalog", type=Path, help="fixture catalog JSON; default is the bundled catalog")
  args = parser.parse_args()
  if not 1 <= args.port <= 65535:
    parser.error("port must be between 1 and 65535")
  params_root = os.environ.get("PARAMS_ROOT", "")
  if not params_root or params_root.startswith("/data"):
    parser.error("set PARAMS_ROOT to an isolated directory (not /data)")

  catalog = None
  if args.catalog is not None:
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    validate_catalog(catalog)

  settings_params = Params()
  device_state_params = Params()
  store = KovaParamsStore(ParamsKovaParamsBackend(settings_params), catalog)
  app = create_app(store, lambda: device_state_params.get_bool("IsOffroad"))
  print(f"kovalink gateway dev runner on http://{LOOPBACK_HOST}:{args.port} params={params_root}", flush=True)
  web.run_app(app, host=LOOPBACK_HOST, port=args.port, access_log=None, print=None)


if __name__ == "__main__":
  main()
