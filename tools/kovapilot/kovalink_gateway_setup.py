#!/usr/bin/env python3
"""Provision KovaLink once from a trusted local operator session."""

import fcntl
import getpass
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

from openpilot.common.hardware import PC
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.kovapilot.gateway import DEVICE_PACKAGES
from openpilot.kovapilot.gateway.runtime import install_runtime, prepare_directory


def provision_tls(directory: Path) -> None:
  if directory.exists() and any(directory.iterdir()):
    return
  with warnings.catch_warnings():
    warnings.simplefilter("error", getpass.GetPassWarning)
    token = getpass.getpass("Paste the 64-character KovaLink token from your password manager: ")
  if re.fullmatch(r"[0-9a-f]{64}", token) is None:
    raise ValueError("the token must be 64 lowercase hexadecimal characters")

  with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
    staging = Path(temporary) / "gateway"
    staging.mkdir(mode=0o700)
    key = staging / "server.key"
    digest = staging / "token.sha256"
    subprocess.run(
      ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
       "-nodes", "-days", "825", "-subj", "/CN=KovaLink Gateway",
       "-keyout", str(key), "-out", str(staging / "server.pem")],
      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    key.chmod(0o600)
    digest.write_text(hashlib.sha256(token.encode("ascii")).hexdigest() + "\n", encoding="ascii")
    digest.chmod(0o600)
    staging.rename(directory)


def certificate_pin(certificate: Path) -> str:
  public_key = subprocess.check_output(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"])
  der_key = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public_key)
  return hashlib.sha256(der_key).hexdigest()


def main() -> None:
  if PC or os.geteuid() == 0:
    raise SystemExit("Run this on the comma as its regular user.")
  params = Params()
  if not params.get_bool("IsOffroad"):
    raise SystemExit("Switch the vehicle off before setting up KovaLink.")

  config = Path(Paths.persist_root()) / "kova" / "gateway"
  prepare_directory(config.parent, allow_sudo=True)
  install_runtime()
  with (DEVICE_PACKAGES.parent / ".setup.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    sys.path.insert(0, str(DEVICE_PACKAGES))
    from openpilot.kovapilot.gateway.lan import load_lan_config

    provision_tls(config)
    if load_lan_config(config) is None:
      raise ValueError("missing Gateway credentials")
    pin = certificate_pin(config / "server.pem")
    if not params.get_bool("IsOffroad"):
      raise SystemExit("Switch the vehicle off and rerun setup to enable KovaLink.")
    params.put_bool("KovaLinkGatewayEnabled", True, block=True)
  print(f"Gateway enabled. Enter this certificate pin in KovaLink: {pin}")


if __name__ == "__main__":
  try:
    main()
  except ValueError as error:
    raise SystemExit(f"KovaLink setup failed: {error}") from None
  except (OSError, subprocess.SubprocessError, getpass.GetPassWarning) as error:
    raise SystemExit(f"KovaLink setup failed ({type(error).__name__}). Retry from an interactive terminal.") from None
