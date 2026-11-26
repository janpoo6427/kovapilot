"""Register the owner phone of an automatic Wi-Fi Gateway with a code shown on the comma."""

from __future__ import annotations

import hmac
import math
import secrets
import time
from collections.abc import Callable

from openpilot.kovapilot.gateway.lan import LanAccess


CODE_SECONDS = 120
MAX_ATTEMPTS = 5
MIN_START_INTERVAL = 3.0
OFFROAD_ALERT = "Offroad_KovaLinkPairing"
ALERT_TEXT = "KovaLink pairing code: %1. Enter it in the KovaLink app within 2 minutes."


def show_pairing_code(params, code: str | None) -> None:
  """Show the code as an offroad alert on the comma screen, or take it down."""
  if code is None:
    params.remove(OFFROAD_ALERT)
  else:
    params.put(OFFROAD_ALERT, {"text": ALERT_TEXT, "severity": 0, "extra": code}, block=True)


class PairingBlocked(Exception):
  """Pairing needs the automatic Wi-Fi mode with the vehicle off."""


class PairingBusy(Exception):
  """A code was requested moments ago."""


class PairingExpired(Exception):
  """There is no live code (never started, timed out or too many tries)."""


class PairingCodeInvalid(Exception):
  def __init__(self, remaining: int):
    super().__init__(remaining)
    self.remaining = remaining


class OwnerPairing:
  """One live code at a time. The code only appears on the comma screen."""

  def __init__(self, access: LanAccess, offroad: Callable[[], bool], show_code: Callable[[str | None], None], *,
               clock: Callable[[], float] = time.monotonic, new_code: Callable[[], str] | None = None,
               new_token: Callable[[], str] | None = None):
    self.access = access
    self.offroad = offroad
    self.show_code = show_code
    self.clock = clock
    self.new_code = new_code or (lambda: f"{secrets.randbelow(10 ** 6):06d}")
    self.new_token = new_token or (lambda: secrets.token_hex(32))
    self.code: str | None = None
    self.expires = 0.0
    self.attempts = 0
    self.started = -math.inf

  @property
  def available(self) -> bool:
    return self.access.token_digest is None

  def _clear(self) -> None:
    if self.code is not None:
      self.code = None
      try:
        self.show_code(None)
      except OSError:
        pass  # The alert clears on the next manager start.

  def expire(self) -> None:
    if self.code is not None and self.clock() >= self.expires:
      self._clear()

  def start(self) -> int:
    if not self.available or not self.offroad():
      raise PairingBlocked
    now = self.clock()
    if now - self.started < MIN_START_INTERVAL:
      raise PairingBusy
    self.started = now
    self.code = self.new_code()
    self.expires = now + CODE_SECONDS
    self.attempts = 0
    self.show_code(self.code)
    return CODE_SECONDS

  def confirm(self, code: str) -> str:
    if not self.available or not self.offroad():
      raise PairingBlocked
    self.expire()
    if self.code is None:
      raise PairingExpired
    self.attempts += 1
    if not hmac.compare_digest(code.encode("ascii"), self.code.encode("ascii")):
      remaining = MAX_ATTEMPTS - self.attempts
      if remaining <= 0:
        self._clear()
        raise PairingExpired
      raise PairingCodeInvalid(remaining)
    token = self.new_token()
    self.access.set_owner(token)
    self._clear()
    return token
