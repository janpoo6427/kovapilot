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


def vehicle_capability(params) -> tuple[bool | None, bool]:
  from capnp import KjException
  from opendbc.car.structs import car

  if params.get_bool("IsReleaseBranch"):
    return False, False
  raw = params.get("CarParams")
  if raw is None:
    return None, False
  try:
    with car.CarParams.from_bytes(raw) as cp:
      return bool(cp.alphaLongitudinalAvailable and not cp.dashcamOnly and not cp.notCar), bool(cp.openpilotLongitudinalControl)
  except (ValueError, KjException):
    return None, False
