"""Prepare the isolated Gateway Python packages."""

import fcntl
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from openpilot.common.basedir import BASEDIR
from openpilot.kovapilot.gateway import DEVICE_PACKAGES


DEVICE_IDENTITY = DEVICE_PACKAGES.parent / "identity"
DEVICE_WHEELS = Path(__file__).with_name("wheels") / "agnos"


def runtime_ready(directory: Path = DEVICE_PACKAGES) -> bool:
  return (directory / "aiohttp" / "__init__.py").is_file()


def prepare_directory(directory: Path, *, allow_sudo: bool = False) -> None:
  try:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
  except PermissionError:
    if not allow_sudo:
      raise
    subprocess.run(
      ["sudo", "install", "-d", "-m", "0700", "-o", str(os.geteuid()), "-g", str(os.getegid()), str(directory)],
      check=True,
    )
  info = directory.lstat()
  if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
    raise ValueError("Gateway setup directories must be owned by the regular device user and private to writes")


def create_tls_keypair(directory: Path) -> None:
  key = directory / "server.key"
  subprocess.run(
    ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
     "-nodes", "-days", "825", "-subj", "/CN=KovaLink Gateway",
     "-keyout", str(key), "-out", str(directory / "server.pem")],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15,
  )
  key.chmod(0o600)


def prepare_tls_identity(directory: Path = DEVICE_IDENTITY) -> Path:
  prepare_directory(directory.parent)
  with (directory.parent / ".identity.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if directory.exists() or directory.is_symlink():
      prepare_directory(directory)
    else:
      with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        staging = Path(temporary) / "identity"
        staging.mkdir(mode=0o700)
        create_tls_keypair(staging)
        staging.rename(directory)
  return directory


def check_runtime(directory: Path) -> None:
  check = "\n".join((
    "from pathlib import Path",
    "import sys",
    "sys.path.insert(0, sys.argv[1])",
    "import aiohttp.web",
    "if not Path(aiohttp.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()):",
    "  raise SystemExit('Gateway package directory is incomplete')",
  ))
  subprocess.run([sys.executable, "-I", "-c", check, str(directory)], check=True, timeout=15)


def install_runtime(directory: Path = DEVICE_PACKAGES) -> None:
  prepare_directory(directory.parent)
  with (directory.parent / ".runtime.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if runtime_ready(directory):
      check_runtime(directory)
      return
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
      staging = Path(temporary)
      requirements = subprocess.run(
        ["uv", "--offline", "export", "--frozen", "--only-group", "kovalink-gateway", "--no-emit-project",
         "--format", "requirements-txt"],
        cwd=BASEDIR, stdout=subprocess.PIPE, check=True, timeout=15,
      ).stdout
      subprocess.run(
        ["uv", "--offline", "pip", "install", "--python", sys.executable, "--target", str(staging),
         "--no-index", "--find-links", str(DEVICE_WHEELS),
         "--require-hashes", "--only-binary=:all:", "-r", "-"],
        cwd=BASEDIR, input=requirements, check=True, timeout=120,
      )
      check_runtime(staging)
      # In AGNOS the installer can leave a target owned by a different user.
      # Renaming in the same parent avoids rewriting the directory's .. entry.
      staging.rename(directory)
