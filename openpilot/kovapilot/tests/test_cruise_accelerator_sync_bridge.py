import pytest

from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.kovapilot.bridges.cruise_accelerator_sync import CruiseAcceleratorSyncBridge
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import MIN_PRESS_SAMPLES
from openpilot.selfdrive.car.cruise import CRUISE_LONG_PRESS, VCruiseHelper


def setup_bridge(stock=False, *, enabled=True, stock_shadow=False, openpilot_long=True):
  helper = VCruiseHelper(car.CarParams(pcmCruise=stock, openpilotLongitudinalControl=openpilot_long and not stock))
  helper.v_cruise_kph = helper.v_cruise_cluster_kph = 80.0
  bridge = CruiseAcceleratorSyncBridge(helper)

  def sample(speed=80.0, pressed=False, *, long_active=True, valid=True, engaged=True, disengage=False, is_metric=True,
             stock_engaged=stock, **car_state):
    CS = car.CarState.new_message(vEgo=speed * CV.KPH_TO_MS, vEgoCluster=speed * CV.KPH_TO_MS, gasPressed=pressed,
                                 cruiseState={'available': True, 'enabled': stock_engaged, 'speed': 80 * CV.KPH_TO_MS,
                                              'speedCluster': 80 * CV.KPH_TO_MS}, **car_state)
    CC = car.CarControl.new_message(enabled=engaged, longActive=long_active)
    helper.update_v_cruise(CS, CC.enabled, is_metric=is_metric)
    return bridge.update(CS, CC, inputs_valid=valid, cruise_engaged=engaged, enabled=enabled, stock_shadow=stock_shadow,
                         disengage_on_accelerator=disengage, is_metric=is_metric)

  return helper, sample


def run_press(sample, **kwargs):
  for _ in range(MIN_PRESS_SAMPLES):
    assert sample(80.0, True, long_active=False, **kwargs) is None


def test_non_pcm_updates_planner_and_hud_setpoints_on_release():
  helper, sample = setup_bridge()
  sample()
  run_press(sample)
  assert helper.v_cruise_kph == helper.v_cruise_cluster_kph == 80.0
  assert sample(93.0, long_active=False) is None
  assert helper.v_cruise_kph == helper.v_cruise_cluster_kph == 93.0
  sample(95.0)
  assert helper.v_cruise_kph == helper.v_cruise_cluster_kph == 93.0


def test_imperial_sync_preserves_long_press_decrement():
  helper, sample = setup_bridge()
  sample(is_metric=False)
  run_press(sample, is_metric=False)
  sample(55.0 * CV.MPH_TO_KPH, is_metric=False)
  assert helper.v_cruise_kph == 88.0

  sample(is_metric=False, buttonEvents=[{'type': 'decelCruise', 'pressed': True}])
  for _ in range(CRUISE_LONG_PRESS):
    sample(is_metric=False)
  assert helper.v_cruise_kph == 80.0


def test_imperial_rounding_at_upper_limit_cannot_lower_the_setpoint():
  helper, sample = setup_bridge()
  helper.v_cruise_kph = helper.v_cruise_cluster_kph = 144.02
  sample(is_metric=False)
  run_press(sample, is_metric=False)
  sample(144.9, is_metric=False)
  assert helper.v_cruise_kph == 144.02


@pytest.mark.parametrize('blocker', [{'brakePressed': True}, {'regenBraking': True}, {'brakeHoldActive': True},
                                     {'parkingBrake': True}, {'standstill': True}, {'accFaulted': True},
                                     {'valid': False}, {'engaged': False}, {'disengage': True},
                                     {'buttonEvents': [{'type': 'cancel', 'pressed': True}]}])
def test_release_during_manual_intervention_or_invalid_state_is_discarded(blocker):
  helper, sample = setup_bridge()
  sample()
  run_press(sample)
  sample(93.0, **blocker)
  sample(94.0)
  assert helper.v_cruise_kph == 80.0


def test_held_button_blocks_sync_until_release():
  helper, sample = setup_bridge()
  sample(buttonEvents=[{'type': 'gapAdjustCruise', 'pressed': True}])
  sample()
  run_press(sample)
  sample(93.0)
  assert helper.v_cruise_kph == 80.0
  sample(buttonEvents=[{'type': 'gapAdjustCruise', 'pressed': False}])
  sample()
  run_press(sample)
  sample(93.0)
  assert helper.v_cruise_kph == 93.0


@pytest.mark.parametrize(('stock', 'enabled', 'shadow', 'reported_target'),
                         [(False, False, False, None), (True, True, False, None), (True, False, True, 93.0)])
def test_inactive_feature_and_stock_shadow_preserve_existing_setpoints(stock, enabled, shadow, reported_target):
  helper, sample = setup_bridge(stock, enabled=enabled, stock_shadow=shadow)
  sample()
  run_press(sample)
  assert sample(93.0) == reported_target
  assert sample(94.0) is None
  assert helper.v_cruise_kph == pytest.approx(80.0)
  assert helper.v_cruise_cluster_kph == pytest.approx(80.0)


def test_no_update_without_openpilot_longitudinal_control():
  helper, sample = setup_bridge(openpilot_long=False)
  sample()
  run_press(sample)
  sample(93.0)
  assert helper.v_cruise_kph == 80.0


def test_stock_disengagement_suppresses_shadow_target():
  helper, sample = setup_bridge(stock=True, stock_shadow=True)
  sample()
  run_press(sample)
  assert sample(93.0, stock_engaged=False) is None
  assert sample(94.0) is None
  assert helper.v_cruise_kph == pytest.approx(80.0)


def test_non_pcm_set_speed_follows_while_the_pedal_is_held():
  helper, sample = setup_bridge()
  sample()
  run_press(sample)
  seen = []
  for speed in (81.0, 83.0, 86.0, 85.0, 90.0, 88.0):
    assert sample(speed, True, long_active=False) is None
    seen.append((helper.v_cruise_kph, helper.v_cruise_cluster_kph))
  # planner and HUD set speeds climb together while held, never back down
  assert seen == [(81.0, 81.0), (83.0, 83.0), (86.0, 86.0), (86.0, 86.0), (90.0, 90.0), (90.0, 90.0)]
  # releasing below the highest speed keeps it
  assert sample(87.0, long_active=False) is None
  assert helper.v_cruise_kph == helper.v_cruise_cluster_kph == 90.0
  sample(95.0)
  assert helper.v_cruise_kph == 90.0


def test_imperial_follow_stays_on_the_button_grid():
  helper, sample = setup_bridge()
  sample(is_metric=False)
  run_press(sample, is_metric=False)
  for mph in (51, 53, 55):
    sample(mph * CV.MPH_TO_KPH, True, long_active=False, is_metric=False)
    assert helper.v_cruise_kph == pytest.approx(mph * 1.6)
  sample(55 * CV.MPH_TO_KPH, long_active=False, is_metric=False)
  assert helper.v_cruise_kph == pytest.approx(88.0)


@pytest.mark.parametrize("blocker", [{"brakePressed": True}, {"standstill": True}, {"accFaulted": True},
                                     {"valid": False}, {"engaged": False}, {"disengage": True},
                                     {"buttonEvents": [{"type": "gapAdjustCruise", "pressed": True}]}])
def test_follow_stops_at_a_blocker_while_held(blocker):
  helper, sample = setup_bridge()
  sample()
  run_press(sample)
  sample(85.0, True, long_active=False)
  assert helper.v_cruise_kph == 85.0
  sample(88.0, True, long_active=False, **blocker)
  sample(90.0, True, long_active=False)
  sample(92.0, long_active=False)
  assert helper.v_cruise_kph == 85.0


def test_stock_shadow_reports_each_followed_step_once_without_changing_setpoints():
  helper, sample = setup_bridge(True, enabled=False, stock_shadow=True)
  sample()
  run_press(sample)
  assert [sample(speed, True) for speed in (85.0, 85.3, 87.0, 86.0)] == [85.0, None, 87.0, None]
  assert sample(86.0) is None
  assert helper.v_cruise_kph == pytest.approx(80.0)
  assert helper.v_cruise_cluster_kph == pytest.approx(80.0)
