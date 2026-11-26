"""Restart the comma from KovaLink while it is offroad."""

from openpilot.kovapilot.config.git_update_request import KEY as GIT_UPDATE_KEY


REBOOT_KEY = "DoReboot"


class RestartBlocked(ValueError):
  pass


def restart_allowed(params, pc=False):
  """Offroad and the updater is not writing a new checkout.

  A restart then does what the device's own Reboot button does: manager
  reboots, and the launch script installs an update only when it was
  finalized completely. A Link update that is still downloading waits.
  """
  if pc or not params.get_bool("IsOffroad"):
    return False
  if str(params.get("UpdaterState") or "").startswith("finalizing"):
    return False
  request = params.get(GIT_UPDATE_KEY)
  return not (isinstance(request, dict) and request.get("phase") == "queued")


class DeviceRestart:
  def __init__(self, allowed=lambda: False, request=None):
    self.allowed = allowed
    self.request = request

  def enabled(self, authorized):
    return bool(authorized) and self.request is not None and self.allowed()

  def restart(self, authorized):
    request = self.request
    if request is None or not self.enabled(authorized):
      raise RestartBlocked("restart requires authorized access while offroad and not finalizing an update")
    request()
