"""Stock MAIN/LFA sync setting (steering.stock_hda_sync, janpoo 11/29 01:39 and 01:45): default on, the panda flag
only on Hyundai CAN FD angle steering with camera SCC and openpilot longitudinal; off keeps the old behavior."""
import json
from pathlib import Path
from types import SimpleNamespace

from opendbc.car.hyundai.values import HyundaiFlags
from opendbc.car.structs import car
from opendbc.safety import ALTERNATIVE_EXPERIENCE

from openpilot.kovapilot.vehicle.stock_hda_sync import STOCK_HDA_SYNC_KEY, apply_stock_hda_sync, read_stock_hda_sync

ANGLE_LONG = int(HyundaiFlags.CANFD | HyundaiFlags.CANFD_CAMERA_SCC | HyundaiFlags.CANFD_ANGLE_STEERING |
                 HyundaiFlags.CANFD_LONGITUDINAL_CANDIDATE | HyundaiFlags.EV)


def _cp(**kwargs):
  values = {"brand": "hyundai", "flags": ANGLE_LONG, "openpilotLongitudinalControl": True,
            "safetyConfigs": [{"safetyModel": "hyundaiCanfd", "safetyParam": 1}]}
  values.update(kwargs)
  return car.CarParams.new_message(**values)


def _set(cp):
  return bool(cp.alternativeExperience & ALTERNATIVE_EXPERIENCE.STOCK_HDA_SYNC)


def test_setting_default_on_and_restart_applied():
  schema = json.loads((Path(__file__).parents[1] / "config" / "kova_params.json").read_text())
  setting = next(s for s in schema["settings"] if s["key"] == STOCK_HDA_SYNC_KEY)
  assert setting["defaultValue"] == 1
  assert setting["options"] == [0, 1]
  assert setting["applyPolicy"] == "restart"


def test_flag_only_on_angle_steering_camera_scc_openpilot_longitudinal():
  for cp, expected in (
    (_cp(), True),
    (_cp(openpilotLongitudinalControl=False), False),
    (_cp(flags=ANGLE_LONG & ~int(HyundaiFlags.CANFD_ANGLE_STEERING)), False),
    (_cp(flags=ANGLE_LONG & ~int(HyundaiFlags.CANFD_CAMERA_SCC)), False),
    (_cp(flags=ANGLE_LONG & ~int(HyundaiFlags.CANFD_LONGITUDINAL_CANDIDATE)), False),
    (_cp(flags=ANGLE_LONG | int(HyundaiFlags.CANFD_LKA_STEER_MSG)), False),
    (_cp(flags=ANGLE_LONG | int(HyundaiFlags.CANFD_ALT_BUTTONS)), False),
    (_cp(flags=int(HyundaiFlags.CANFD)), False),
    (_cp(brand="toyota"), False),
    (_cp(passive=True), False),
    (_cp(safetyConfigs=[{"safetyModel": "noOutput"}]), False),
  ):
    assert apply_stock_hda_sync(cp, True) == expected
    assert _set(cp) == expected
    assert not apply_stock_hda_sync(cp, False)
    assert not _set(cp)


def test_setting_off_leaves_other_flags():
  cp = _cp(alternativeExperience=ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL | ALTERNATIVE_EXPERIENCE.PEDAL_RESUME)
  assert apply_stock_hda_sync(cp, True)
  assert not apply_stock_hda_sync(cp, False)
  assert cp.alternativeExperience == ALTERNATIVE_EXPERIENCE.ALWAYS_ON_LATERAL | ALTERNATIVE_EXPERIENCE.PEDAL_RESUME


def test_read_setting():
  assert read_stock_hda_sync(SimpleNamespace(values=lambda: {STOCK_HDA_SYNC_KEY: 1}))
  assert not read_stock_hda_sync(SimpleNamespace(values=lambda: {STOCK_HDA_SYNC_KEY: 0}))
  assert not read_stock_hda_sync(SimpleNamespace(values=dict))

  def broken():
    raise RuntimeError("params")
  assert not read_stock_hda_sync(SimpleNamespace(values=broken))
