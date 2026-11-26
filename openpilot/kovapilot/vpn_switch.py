"""The comma screen tells the KovaLink Gateway that the user cut the comma's
network (Wi-Fi forgotten or switched, tethering turned on). The VPN goes off
with it and stays off until the owner turns it on again in KovaLink. Both
files live in /dev/shm, so a reboot connects the VPN again by itself."""

from contextlib import suppress
from pathlib import Path

RUNTIME_DIR = Path("/dev/shm")
# Written by the network screen, consumed by the Gateway.
NETWORK_CUT = "kova_vpn_network_cut"
# Present while the VPN is off on purpose (until KovaLink turns it on).
VPN_OFF = "kova_vpn_off"


def user_cut_network(directory: Path = RUNTIME_DIR) -> None:
  """Never raises: the network screen keeps working whatever happens here."""
  with suppress(Exception):
    (directory / NETWORK_CUT).write_text("1\n", encoding="ascii")
