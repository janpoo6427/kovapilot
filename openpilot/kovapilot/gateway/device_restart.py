"""Restart the comma from KovaLink at any time."""


REBOOT_KEY = "DoReboot"


class RestartBlocked(ValueError):
  pass


def restart_allowed(pc=False):
  """Any time on the device, offroad or on (janpoo6427 2026-11-27).

  A restart does what the device's own Reboot button does: manager reboots,
  and the launch script installs an update only when it was finalized
  completely. While driving, a restart ends steering and longitudinal control
  at once; the app asks before it sends the request.
  """
  return not pc


class DeviceRestart:
  def __init__(self, allowed=lambda: False, request=None):
    self.allowed = allowed
    self.request = request

  def enabled(self, authorized):
    return bool(authorized) and self.request is not None and self.allowed()

  def restart(self, authorized):
    request = self.request
    if request is None or not self.enabled(authorized):
      raise RestartBlocked("restart requires authorized access")
    request()
