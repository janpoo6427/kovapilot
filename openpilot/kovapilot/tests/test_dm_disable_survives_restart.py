"""The three-cancel DM disable stays set across restarts, ignition cycles and drives.

Only the driver repeating the same parked gesture turns monitoring back on.
"""
from dataclasses import replace

import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.params import Params, ParamKeyFlag
from openpilot.common.prefix import OpenpilotPrefix
import openpilot.system.manager.manager as manager
from openpilot.system.manager.process_config import driver_monitoring_model
from openpilot.kovapilot.bridges.driver_monitoring import disabled_state_packet
from openpilot.kovapilot.config.driver_monitoring import (DRIVER_MONITORING_CONFIG, SESSION_DISABLED_PARAM,
                                                          DriverMonitoringDisableReason, disable_reason)
from openpilot.kovapilot.engagement.dm_session_disable import (DriverMonitoringSessionGesture, SessionDisableContext,
                                                               toggled_session_disabled)

SESSION = DriverMonitoringDisableReason.SESSION
NONE = DriverMonitoringDisableReason.NONE


@pytest.fixture
def params(monkeypatch):
  # the boot log helper is unrelated here and its binary is not built in test checkouts
  monkeypatch.setattr(manager, "save_bootlog", lambda: None)
  monkeypatch.setenv("PREPAREONLY", "1")
  with OpenpilotPrefix():
    yield Params()


def three_cancels(params, start: float, **overrides) -> bool:
  """One parked three-cancel sequence, handled the way dmonitoringd does it."""
  gesture = DriverMonitoringSessionGesture(DRIVER_MONITORING_CONFIG)
  reason = disable_reason(params)
  for i in range(3):
    values = {"now": start + i + 0.05, "event_time": start + i, "can_valid": True, "selfdrive_valid": True,
              "selfdrive_enabled": False, "parked": True, "dm_lockout": False, "dm_alert_three": False,
              "disable_reason": reason}
    values.update(overrides)
    ctx = SessionDisableContext(**values)
    if gesture.update(ctx, [("cancel", True)]):
      params.put_bool(SESSION_DISABLED_PARAM, toggled_session_disabled(reason), block=True)
      return True
    gesture.update(replace(ctx, event_time=ctx.event_time + 0.01, now=ctx.now + 0.01), [("cancel", False)])
  return False


def restart_manager():
  """Boot / openpilot restart: manager_init clears its CLEAR_ON_* params (PREPAREONLY stops before processes)."""
  manager.main()


def ignition_cycle(params):
  """Ignition on, drive (onroad), park (offroad) - the clears manager_thread does on each edge."""
  params.clear_all(ParamKeyFlag.CLEAR_ON_IGNITION_ON)
  params.clear_all(ParamKeyFlag.CLEAR_ON_ONROAD_TRANSITION)
  params.clear_all(ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION)


def fresh_dmonitoringd_state():
  """What a newly started dmonitoringd publishes: it reads the reason from Params at start."""
  reason = disable_reason(Params())
  if reason == NONE:
    return log.DriverMonitoringState.DisableReason.none
  return disabled_state_packet(reason, False, valid=True).driverMonitoringState.disableReason


def test_dm_off_stays_off_across_restarts_ignition_and_drives(params):
  assert three_cancels(params, 10.0)
  assert disable_reason(params) == SESSION

  for cycle in range(3):
    restart_manager()
    assert disable_reason(Params()) == SESSION, cycle
    ignition_cycle(params)
    assert disable_reason(Params()) == SESSION, cycle
    # a new drive: dmonitoringd starts again and the driver engages
    assert fresh_dmonitoringd_state() == log.DriverMonitoringState.DisableReason.session, cycle
    assert not three_cancels(params, 100.0 + cycle * 10, selfdrive_enabled=True, parked=False)
    assert disable_reason(Params()) == SESSION, cycle
    assert not driver_monitoring_model(True, Params(), car.CarParams.new_message())


def test_driver_turning_dm_back_on_stays_on(params):
  assert three_cancels(params, 10.0)
  restart_manager()
  ignition_cycle(params)
  assert disable_reason(Params()) == SESSION

  # only the same parked gesture turns it back on
  assert three_cancels(params, 50.0)
  assert disable_reason(params) == NONE

  for cycle in range(3):
    restart_manager()
    ignition_cycle(params)
    assert disable_reason(Params()) == NONE, cycle
    assert fresh_dmonitoringd_state() == log.DriverMonitoringState.DisableReason.none, cycle
    assert driver_monitoring_model(True, Params(), car.CarParams.new_message())
