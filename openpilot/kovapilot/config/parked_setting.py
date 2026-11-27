"""Vehicle-state checks for applying parked settings."""


def fresh_message(sm, now_ns: int, service: str) -> bool:
  return sm.valid[service] and sm.alive[service] and 0 <= now_ns - sm.logMonoTime[service] <= 500_000_000


def stopped_in_park(car) -> bool:
  return bool(car.canValid and car.standstill and str(car.gearShifter) == "park" and abs(car.vEgo) < 0.1)


def parked_for_settings(sm, now_ns: int) -> bool:
  if not (fresh_message(sm, now_ns, "carState") and fresh_message(sm, now_ns, "selfdriveState")):
    return False
  return stopped_in_park(sm["carState"]) and not sm["selfdriveState"].enabled


PARKED_AND_DISENGAGED_SERVICES = ("carState", "carControl", "selfdriveState")


def parked_and_disengaged(sm, now_ns: int) -> bool:
  """Stopped in Park with steering and longitudinal control released.

  The gate for changes from KovaLink while the vehicle is on: settings with a
  Park policy, the openpilot longitudinal switch, Git maintenance and the VPN.
  """
  if not parked_for_settings(sm, now_ns) or not fresh_message(sm, now_ns, "carControl"):
    return False
  control = sm["carControl"]
  return not (control.enabled or control.latActive or control.longActive or sm["selfdriveState"].active)
