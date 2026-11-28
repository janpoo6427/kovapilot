"""Cluster NAV icon: whether navigation info is being received (hudControl.navigationActive).

Navigation info is the car navigation road info (kovaNavigation.vehicleNaviValid: a speed limit or camera within
1 s, vehicle_navi.SIGNAL_TIMEOUT_NS) or a navigation app reporting to navcamd (kovaNavigation.source carrotV2 or
carrotLegacy: a packet within 4 s, carrot_link.APP_TIMEOUT_NS). kovaNavigation itself comes every 200 ms; older than
1 s (navcamd stopped) counts as none. opendbc shows the NAV icon green only while openpilot longitudinal is engaged.
"""

NAVIGATION_FRESH_NS = 1_000_000_000
APP_SOURCES = ("carrotV2", "carrotLegacy")


def navigation_active(navigation, navigation_mono_ns: int, now_ns: int) -> bool:
  """navigation: the last kovaNavigation (or None), navigation_mono_ns: its logMonoTime (0 never received)."""
  if navigation is None or navigation_mono_ns <= 0 or not 0 <= now_ns - navigation_mono_ns <= NAVIGATION_FRESH_NS:
    return False
  try:
    return str(navigation.source) in APP_SOURCES or bool(navigation.vehicleNaviValid)
  except (AttributeError, ValueError):
    return False
