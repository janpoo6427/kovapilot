"""Vehicle-state checks for applying parked settings."""


def fresh_message(sm, now_ns: int, service: str) -> bool:
  return sm.valid[service] and sm.alive[service] and 0 <= now_ns - sm.logMonoTime[service] <= 500_000_000


def parked_for_settings(sm, now_ns: int) -> bool:
  if not (fresh_message(sm, now_ns, "carState") and fresh_message(sm, now_ns, "selfdriveState")):
    return False
  car = sm["carState"]
  return bool(car.canValid and car.standstill and str(car.gearShifter) == "park" and abs(car.vEgo) < 0.1
              and not sm["selfdriveState"].enabled)
