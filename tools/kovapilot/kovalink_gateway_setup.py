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


ROOT = Path(__file__).resolve().parents[2]


def check_runtime(directory: Path) -> None:
  check = "\n".join((
    "from pathlib import Path",
    "import sys",
    "sys.path.insert(0, sys.argv[1])",
    "import aiohttp.web",
    "if not Path(aiohttp.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()):",
    "  raise SystemExit('Gateway package directory is incomplete')",
  ))
  subprocess.run(
    [sys.executable, "-I", "-c", check, str(directory)],
    check=True,
  )


def install_runtime(directory: Path) -> None:
  if directory.is_dir():
    check_runtime(directory)
    return
  with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
    staging = Path(temporary)
    requirements = staging / "requirements.txt"
    with requirements.open("wb") as exported:
      subprocess.run(
        ["uv", "export", "--frozen", "--only-group", "kovalink-gateway", "--no-emit-project",
         "--format", "requirements-txt"],
        cwd=ROOT, stdout=exported, check=True,
      )
    target = staging / "python"
    subprocess.run(
      ["uv", "pip", "install", "--python", sys.executable, "--target", str(target),
       "--require-hashes", "--only-binary=:all:", "-r", str(requirements)],
      cwd=ROOT, check=True,
    )
    check_runtime(target)
    target.rename(directory)


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
  with (DEVICE_PACKAGES.parent / ".setup.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    install_runtime(DEVICE_PACKAGES)
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
  except (OSError, ValueError, subprocess.CalledProcessError, getpass.GetPassWarning):
    raise SystemExit("KovaLink setup failed. Check the operator guide and retry from an interactive terminal.") from None
