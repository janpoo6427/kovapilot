from opendbc.car import gen_empty_fingerprint
from opendbc.car.hyundai.fingerprints import FW_VERSIONS
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR, HyundaiCanFDStockSafetyStatus
from opendbc.car.structs import CarParams

from openpilot.kovapilot.gateway.native_longitudinal import NativeLongitudinal, vehicle_capability, vehicle_dependency_conditions


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))


def _ioniq9_car_params(alpha_long: bool, is_release: bool = False) -> bytes:
  fingerprint = gen_empty_fingerprint()
  can = CanBus(None, fingerprint)
  config = CAR.HYUNDAI_IONIQ_9.config
  for requirement in (*config.steering_config.required_messages, *config.longitudinal_config.required_messages):
    fingerprint[getattr(can, requirement.role.value)][requirement.address] = requirement.dlc
  car_fw = [CarParams.CarFw(ecu=ecu, address=address, subAddress=0, fwVersion=versions[0])
            for (ecu, address, _), versions in FW_VERSIONS[CAR.HYUNDAI_IONIQ_9].items()]
  CP = CarInterface.get_params(str(CAR.HYUNDAI_IONIQ_9), fingerprint, car_fw, alpha_long, is_release, False)
  return CP.to_bytes()


def test_ioniq9_longitudinal_is_offered_but_stays_opt_in():
  # Before 2026-11-27 the opendbc source gate reported IONIQ 9 as unsupported.
  off = FakeParams({"CarParams": _ioniq9_car_params(False)})
  assert vehicle_capability(off) == (True, False)
  assert vehicle_dependency_conditions(off)["openpilotLongitudinalApplied"] == "unmet"

  pending = FakeParams({"CarParams": _ioniq9_car_params(False), "AlphaLongitudinalEnabled": True})
  assert vehicle_dependency_conditions(pending)["openpilotLongitudinalApplied"] == "pendingRestart"

  on = FakeParams({"CarParams": _ioniq9_car_params(True), "AlphaLongitudinalEnabled": True})
  assert vehicle_capability(on) == (True, True)
  assert vehicle_dependency_conditions(on)["openpilotLongitudinalApplied"] == "met"

  release = FakeParams({"CarParams": _ioniq9_car_params(True, is_release=True), "IsReleaseBranch": True})
  assert vehicle_capability(release) == (False, False)


def test_longitudinal_description_states_stock_aeb_fcw_honestly():
  # IONIQ 9 stock AEB/FCW preservation is still unverified on the car.
  assert CAR.HYUNDAI_IONIQ_9.config.longitudinal_config.stock_aeb_fcw_status == HyundaiCanFDStockSafetyStatus.UNVERIFIED
  item = NativeLongitudinal(FakeParams({}), lambda: (True, False), lambda: True).setting()
  for language in ("ko", "en"):
    description = item["description"][language]
    assert "AEB" in description and "FCW" in description
  assert "확인되지 않은" in item["description"]["ko"]
  assert "not been verified" in item["description"]["en"]
