from openpilot.kovapilot.config.kova_params import KovaParamsApplyPolicyError, KovaParamsRevisionConflict

KEY = "cruise.openpilot_longitudinal"
PARAM = "AlphaLongitudinalEnabled"


class NativeLongitudinal:
  def __init__(self, params, capability, parked):
    self.params = params
    self.capability = capability
    self.parked = parked

  def setting(self):
    supported, applied = self.capability()
    requested = int(self.params.get_bool(PARAM))
    status = "unsupported" if supported is False else "available"
    item = {
      "key": KEY, "groupId": "cruise", "order": 290, "type": "enum", "control": "switch",
      "options": [0, 1], "defaultValue": 0, "value": requested, "applyPolicy": "restart", "risk": "control",
      "optionTitles": {"0": {"ko": "끔", "en": "Off"}, "1": {"ko": "켬", "en": "On"}},
      "access": "readWrite" if supported else "readOnly",
      "availability": status, "applyStatus": status,
      "title": {"ko": "openpilot 종방향 제어", "en": "openpilot longitudinal control"},
      "description": {
        "ko": "지원 차량에서 가속과 감속을 openpilot에 맡깁니다. P 정차 중 저장하면 다음 차량 세션에 적용됩니다.",
        "en": "Use openpilot for acceleration and braking on supported vehicles. Save while stationary in Park for the next vehicle session.",
      },
    }
    if supported:
      item["savePolicy"] = "parked"
      item["appliedValue"] = int(applied)
      item["applyState"] = "applied" if requested == int(applied) else "pendingRestart"
    elif supported is None:
      item["applyState"] = "awaitingStart"
    return item

  def save(self, value: int, expected_value: int) -> None:
    if not self.capability()[0]:
      raise KovaParamsApplyPolicyError("openpilot longitudinal control is unsupported")
    if not self.parked():
      raise KovaParamsApplyPolicyError("setting can only be changed when parked and disengaged")
    if int(self.params.get_bool(PARAM)) != expected_value:
      raise KovaParamsRevisionConflict("longitudinal preference changed")
    self.params.put_bool(PARAM, value == 1, block=True)


def _vehicle_longitudinal_state(params) -> tuple[bool | None, bool, bool | None]:
  from capnp import KjException
  from opendbc.car.structs import car

  release_branch = params.get_bool("IsReleaseBranch")
  raw = params.get("CarParams")
  if raw is not None:
    try:
      with car.CarParams.from_bytes(raw) as cp:
        usable = not cp.dashcamOnly and not cp.notCar
        return (bool(usable and not release_branch and cp.alphaLongitudinalAvailable),
                bool(usable and cp.openpilotLongitudinalControl), bool(cp.pcmCruise) if usable else None)
    except (ValueError, KjException):
      pass
  return None, False, None


def vehicle_capability(params) -> tuple[bool | None, bool]:
  if params.get_bool("IsReleaseBranch"):
    return False, False
  supported, applied, _ = _vehicle_longitudinal_state(params)
  return supported, applied


def vehicle_dependency_conditions(params) -> dict[str, str]:
  supported, applied, pcm_cruise = _vehicle_longitudinal_state(params)
  if supported is None:
    longitudinal = "unknown"
  elif applied:
    longitudinal = "met"
  elif not supported:
    longitudinal = "unsupported"
  elif params.get_bool(PARAM):
    longitudinal = "pendingRestart"
  else:
    longitudinal = "unmet"
  if pcm_cruise is None:
    openpilot_speed_owner = stock_speed_owner = "unknown"
  elif pcm_cruise:
    openpilot_speed_owner = "unsupported" if applied else longitudinal
    stock_speed_owner = "met"
  else:
    openpilot_speed_owner, stock_speed_owner = "met", "unsupported"
  return {
    "openpilotLongitudinalApplied": longitudinal,
    "openpilotCruiseSpeedOwner": openpilot_speed_owner,
    "stockCruiseSpeedOwner": stock_speed_owner,
    "acceleratorDisengagementOff": "unmet" if params.get_bool("DisengageOnAccelerator") else "met",
    "verifiedCameraInput": "pendingConsumer",
    "independentLateralControl": "pendingConsumer",
  }
