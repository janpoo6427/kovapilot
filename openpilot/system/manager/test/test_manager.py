#!/usr/bin/env python3

import os
import unittest

from opendbc.car.structs import car
from openpilot.common.test import OpenpilotTestCase
from openpilot.common.params import Params
import openpilot.system.manager.manager as manager
from openpilot.system.manager.process_config import driver_monitoring_model, driverview, managed_processes, procs
from openpilot.common.hardware import HARDWARE

os.environ['FAKEUPLOAD'] = "1"


class TestManager(OpenpilotTestCase):
  def setup_method(self):
    HARDWARE.set_power_save(False)

    # ensure clean CarParams
    params = Params()
    params.clear_all()

  def teardown_method(self):
    manager.manager_cleanup()

  def test_duplicate_procs(self):
    assert len(procs) == len(managed_processes), "Duplicate process names"

  def test_driver_monitoring_session_disable_stops_only_model(self):
    params = Params()
    CP = car.CarParams.new_message()
    params.put_bool("IsDriverViewEnabled", False, block=True)
    params.remove("DriverMonitoringSessionDisabled")

    assert driver_monitoring_model(True, params, CP)
    assert driverview(True, params, CP)

    params.put_bool("DriverMonitoringSessionDisabled", True, block=True)
    assert not driver_monitoring_model(True, params, CP)
    assert driverview(True, params, CP)

    params.put_bool("IsDriverViewEnabled", True, block=True)
    assert driver_monitoring_model(False, params, CP)
    assert driverview(False, params, CP)

  def test_set_params_with_default_value(self):
    params = Params()
    params.clear_all()

    os.environ['PREPAREONLY'] = '1'
    manager.main()
    for k in params.all_keys():
      default_value = params.get_default_value(k)
      if default_value is not None:
        assert params.get(k) == default_value
    assert params.get("OpenpilotEnabledToggle")
    assert params.get("RouteCount") == 0


if __name__ == "__main__":
  unittest.main()
