"""Wi-Fi interface lookup without the KovaLink Gateway web packages.

navcamd runs under the comma system Python, which does not ship aiohttp.
lan.py imports aiohttp at module load, so the Wi-Fi helpers navcamd needs
live here and lan.py re-uses them.
"""

import fcntl
import ipaddress
import socket
import struct


WIFI_INTERFACE = "wlan0"
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
IFF_UP_RUNNING = 0x1 | 0x40


def wifi_address(interface_name: str = WIFI_INTERFACE,
                 private_networks: tuple[ipaddress.IPv4Network, ...] = PRIVATE_NETWORKS) -> ipaddress.IPv4Interface | None:
  request = struct.pack("256s", interface_name.encode())
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
      flags = struct.unpack_from("H", fcntl.ioctl(sock, SIOCGIFFLAGS, request), 16)[0]
      if flags & IFF_UP_RUNNING != IFF_UP_RUNNING:
        return None
      address = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFADDR, request)[20:24])
      mask = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFNETMASK, request)[20:24])
      interface = ipaddress.IPv4Interface(f"{address}/{mask}")
  except (OSError, ValueError):
    return None
  return interface if any(interface.ip in network for network in private_networks) else None
