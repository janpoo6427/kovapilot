"""Install the pinned, official Tailscale ARM64 build on a comma that has none."""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO


VERSION = "1.104.7"
ARCHIVE = f"tailscale_{VERSION}_arm64"
URL = f"https://pkgs.tailscale.com/stable/{ARCHIVE}.tgz"
# Published by pkgs.tailscale.com as `{URL}.sha256`; the download must match it.
SHA256 = "d6bed2720310b328b861f943d7b4016c5358a9bad60575eb51e8143b79068484"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_BINARY_BYTES = 96 * 1024 * 1024
BINARIES = ("tailscale", "tailscaled")
# Room kept free on the target filesystem besides the two binaries.
SPACE_MARGIN = 16 * 1024 * 1024
TIMEOUT = 30
CHUNK = 1024 * 1024


def choose_directory(manual: Path, automatic: Path) -> Path:
  """A Tailscale installed by hand in [manual] (/persist, set up over SSH)
  keeps being used; otherwise the Gateway installs into [automatic] (/data),
  which it can write without remounting anything."""
  return manual if manual.exists() or manual.is_symlink() else automatic


class InstallError(Exception):
  """The installation did not happen; nothing was left behind in the target."""


Fetch = Callable[[str, float], BinaryIO]


def _https(url: str, timeout: float) -> BinaryIO:
  context = ssl.create_default_context()
  with suppress(ImportError):
    import certifi
    context.load_verify_locations(certifi.where())
  request = urllib.request.Request(url, headers={"User-Agent": "kovapilot-gateway"})
  return urllib.request.urlopen(request, timeout=timeout, context=context)


def _download(fetch: Fetch, url: str, expected: str, destination: BinaryIO) -> None:
  digest = hashlib.sha256()
  size = 0
  try:
    with fetch(url, TIMEOUT) as response:
      while chunk := response.read(CHUNK):
        size += len(chunk)
        if size > MAX_ARCHIVE_BYTES:
          raise InstallError("archive too large")
        digest.update(chunk)
        destination.write(chunk)
  except OSError as error:
    raise InstallError("download failed") from error
  if digest.hexdigest() != expected:
    raise InstallError("checksum mismatch")
  destination.flush()
  destination.seek(0)


def _extract(archive: BinaryIO, name: str, directory: Path) -> None:
  try:
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
      for binary in BINARIES:
        member = tar.getmember(f"{name}/{binary}")
        if not member.isreg() or member.size > MAX_BINARY_BYTES:
          raise InstallError("unexpected archive layout")
        source = tar.extractfile(member)
        if source is None:
          raise InstallError("unexpected archive layout")
        path = directory / binary
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with source, os.fdopen(fd, "wb") as out:
          shutil.copyfileobj(source, out, CHUNK)
          out.flush()
          os.fsync(out.fileno())
        os.chmod(path, 0o700)
  except (KeyError, tarfile.TarError, EOFError) as error:
    raise InstallError("unexpected archive layout") from error
  except OSError as error:
    raise InstallError("could not write the binaries") from error


def install(target: Path, scratch: Path, fetch: Fetch = _https, url: str = URL, expected: str = SHA256,
            name: str = ARCHIVE) -> None:
  """Download, verify and unpack into [target], which must not exist yet.

  The archive is kept in [scratch] (a large data partition) while it is
  checked; the binaries are unpacked next to [target] and renamed into place,
  so [target] either is a complete installation or does not exist.
  """
  if target.exists() or target.is_symlink():
    return
  parent = target.parent
  try:
    parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
  except OSError as error:
    raise InstallError("no install directory") from error
  with tempfile.TemporaryFile(dir=scratch) as archive:
    _download(fetch, url, expected, archive)
    try:
      with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        needed = sum(tar.getmember(f"{name}/{binary}").size for binary in BINARIES)
    except (KeyError, tarfile.TarError, EOFError) as error:
      raise InstallError("unexpected archive layout") from error
    archive.seek(0)
    if shutil.disk_usage(parent).free < needed + SPACE_MARGIN:
      raise InstallError("not enough space")
    temporary = Path(tempfile.mkdtemp(prefix=".tailscale-install.", dir=parent))
    try:
      _extract(archive, name, temporary)
      try:
        os.rename(temporary, target)
      except OSError as error:
        raise InstallError("could not finish the installation") from error
    finally:
      if temporary.exists():
        for binary in BINARIES:
          with suppress(FileNotFoundError):
            (temporary / binary).unlink()
        with suppress(OSError):
          temporary.rmdir()
