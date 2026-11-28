"""Run the KovaLink Gateway with optional authenticated Wi-Fi access."""

import argparse
import asyncio
import sys
from functools import cache, partial
from pathlib import Path
from subprocess import SubprocessError

from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.kovapilot.gateway import DEVICE_PACKAGES
from openpilot.kovapilot.gateway.runtime import prepare_tls_identity


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8082


@cache
def vpn_device_info() -> dict:
  from openpilot.common.version import get_build_metadata

  metadata = get_build_metadata()
  return {"branch": metadata.channel, "version": metadata.openpilot.version, "commit": metadata.openpilot.git_commit}


def main() -> None:
  parser = argparse.ArgumentParser(description="KovaLink gateway")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  args = parser.parse_args()
  if not 1 <= args.port <= 65535:
    parser.error("port must be between 1 and 65535")

  if not PC and DEVICE_PACKAGES.is_dir():
    sys.path.insert(0, str(DEVICE_PACKAGES))

  from aiohttp import web

  from openpilot.kovapilot.config.git_update_request import KEY as GIT_UPDATE_KEY
  from openpilot.kovapilot.config.kova_params import KovaParamsStore, ParamsKovaParamsBackend
  from openpilot.kovapilot.gateway.app import create_app
  from openpilot.kovapilot.gateway.camera import CameraSignaling
  from openpilot.kovapilot.gateway.device_restart import REBOOT_KEY, DeviceRestart, restart_allowed
  from openpilot.kovapilot.gateway.git_maintenance import GitRepository
  from openpilot.kovapilot.gateway.lan import LOOPBACK_GUARD_KEY, TAILNET_KEY, TAILNET_PORT, WIFI_INTERFACE, load_lan_config, serve_lan
  from openpilot.kovapilot.gateway.native_longitudinal import NativeLongitudinal, vehicle_capability, vehicle_dependency_conditions
  from openpilot.kovapilot.gateway.owner_pairing import OwnerPairing, show_pairing_code
  from openpilot.kovapilot.gateway.tailscale import Tailscale
  from openpilot.kovapilot.gateway.tailscale_install import choose_directory, install as install_tailscale
  from openpilot.kovapilot.gateway.telemetry import CerealTelemetry

  settings_params = Params()
  device_state_params = Params()
  store = KovaParamsStore(ParamsKovaParamsBackend(settings_params))
  lan = None
  provisioned = Path(Paths.persist_root()) / "kova" / "gateway"
  try:
    if PC or provisioned.exists() or provisioned.is_symlink():
      lan = load_lan_config(provisioned)
    else:
      lan = load_lan_config(prepare_tls_identity(), require_token=False)
  except (OSError, ValueError, SubprocessError):
    cloudlog.warning("KovaLink Gateway Wi-Fi disabled: invalid TLS or credential configuration")
  telemetry = CerealTelemetry()
  def is_offroad() -> bool:
    return device_state_params.get_bool("IsOffroad")

  def parked_or_offroad() -> bool:
    # Many installs power the comma down with the ignition, so changes from
    # KovaLink also work stopped in Park with openpilot disengaged.
    return is_offroad() or telemetry.is_parked_and_disengaged()

  owner_pairing = None
  if not PC and lan is not None and lan[0].token_digest is None:
    owner_pairing = OwnerPairing(lan[0], is_offroad, partial(show_pairing_code, device_state_params))
  app = create_app(
    store, is_offroad,
    parked_provider=telemetry.is_parked_and_disengaged, applied_cruise_provider=telemetry.applied_cruise_sync,
    applied_curve_provider=telemetry.applied_curve_slowdown,
    applied_nav_turn_provider=telemetry.applied_nav_turn_slowdown,
    applied_auto_engage_provider=telemetry.applied_auto_engage,
    dependency_conditions=partial(vehicle_dependency_conditions, device_state_params),
    telemetry=telemetry, camera=CameraSignaling(device_state_params),
    native_longitudinal=NativeLongitudinal(device_state_params, partial(vehicle_capability, device_state_params),
                                           telemetry.is_parked_and_disengaged),
    git=GitRepository(BASEDIR, lambda: device_state_params.get("UpdaterState"),
                      maintenance_state=lambda: device_state_params.get(GIT_UPDATE_KEY),
                      apply_allowed=lambda: (not PC and parked_or_offroad()
                                             and not device_state_params.get_bool("DisableUpdates")
                                             and not device_state_params.get_bool("UpdateAvailable")
                                             and device_state_params.get("UpdaterState") == "idle"),
                      enqueue=lambda job: device_state_params.put(GIT_UPDATE_KEY, job, block=True)),
    lan_access=lan[0] if lan else None,
    restart=DeviceRestart(partial(restart_allowed, PC),
                          lambda: device_state_params.put_bool(REBOOT_KEY, True, block=True)),
    owner_pairing=owner_pairing,
  )
  if not PC and lan is not None:
    tailscale_directory = choose_directory(provisioned.parent / "tailscale", DEVICE_PACKAGES.parent.parent / "tailscale")
    tailscale = Tailscale(tailscale_directory, lan[0], parked_or_offroad, telemetry, vpn_device_info,
                          installer=partial(install_tailscale, tailscale_directory, DEVICE_PACKAGES.parent))
    tailscale.attach(app)
    # The owner's phone over the VPN: TLS on 127.0.0.1:TAILNET_PORT, and the
    # plain loopback API refuses what tailscaled forwards.
    app[TAILNET_KEY] = TAILNET_PORT
    app[LOOPBACK_GUARD_KEY] = tailscale.from_daemon
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
