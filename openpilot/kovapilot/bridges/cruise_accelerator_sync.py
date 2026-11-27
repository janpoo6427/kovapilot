from opendbc.car.structs import car
from openpilot.common.constants import CV
from openpilot.kovapilot.longitudinal.cruise_accelerator_sync import CruiseAcceleratorSync
from openpilot.selfdrive.car.cruise import IMPERIAL_INCREMENT, VCruiseHelper, V_CRUISE_MAX, V_CRUISE_MIN


ButtonType = car.CarState.ButtonEvent.Type
CRUISE_BUTTONS = (ButtonType.accelCruise, ButtonType.decelCruise, ButtonType.cancel, ButtonType.mainCruise,
                  ButtonType.setCruise, ButtonType.resumeCruise, ButtonType.gapAdjustCruise)


class CruiseAcceleratorSyncBridge:
  def __init__(self, cruise: VCruiseHelper):
    self.cruise = cruise
    self.policy = CruiseAcceleratorSync()
    self.held_buttons: set[int] = set()
    # the current accelerator press has not been joined by a cruise button
    self.press_without_buttons = False
    self.gas_prev = False
    self.engaged_prev = False
    # cruise engaged while the accelerator press is still held (first start, pedal resume, or SET/RES)
    self.engaged_on_press = False

  def initialize_on_accelerator(self, CS, is_metric: bool) -> None:
    """Call right after VCruiseHelper.initialize_v_cruise on the step openpilot engages (CS from that step).

    Engaged by an accelerator press without cruise buttons (first start or pedal resume): the set speed starts at
    the current speed, also from a stop, instead of the initial cruise speed (at least 40 km/h), floored at
    V_CRUISE_MIN like the cruise buttons, and then follows the held pedal (janpoo 11/27 23:12)."""
    if self.cruise.CP.pcmCruise or not (CS.gasPressed and self.press_without_buttons and not self.held_buttons):
      return
    speed_kph = CS.vEgo * CV.MS_TO_KPH
    if is_metric:
      target_kph = float(round(speed_kph))
    else:
      target_kph = round(speed_kph * CV.KPH_TO_MPH) * IMPERIAL_INCREMENT
    self.cruise.v_cruise_kph = self.cruise.v_cruise_cluster_kph = float(min(max(target_kph, V_CRUISE_MIN), V_CRUISE_MAX))

  def update(self, CS, CC, *, inputs_valid: bool, cruise_engaged: bool, enabled: bool, stock_shadow: bool,
             disengage_on_accelerator: bool, is_metric: bool) -> float | None:
    stock = self.cruise.CP.pcmCruise
    manual_input = bool(self.held_buttons)
    for button in CS.buttonEvents:
      button_type = button.type.raw
      if button_type in CRUISE_BUTTONS:
        manual_input = True
        if button.pressed:
          self.held_buttons.add(button_type)
        else:
          self.held_buttons.discard(button_type)

    if CS.gasPressed and not self.gas_prev:
      self.press_without_buttons = not manual_input
    elif not CS.gasPressed or manual_input:
      self.press_without_buttons = False
    if not CS.gasPressed or not cruise_engaged or manual_input:
      self.engaged_on_press = False
    elif not self.engaged_prev and not stock:
      # also when a SET/RES press engaged with the pedal held: the set speed follows the pedal (janpoo 11/27 23:12)
      self.engaged_on_press = True
    self.gas_prev = CS.gasPressed
    self.engaged_prev = cruise_engaged
    if self.engaged_on_press:
      self.policy.allow_held_press()

    if stock:
      mode_allowed = stock_shadow and CS.cruiseState.enabled and not CC.cruiseControl.cancel
    else:
      mode_allowed = enabled and self.cruise.CP.openpilotLongitudinalControl

    stopped_or_braking = (CS.standstill or CS.cruiseState.standstill or CS.brakePressed or CS.regenBraking or
                          CS.brakeHoldActive or CS.parkingBrake)
    eligible = (mode_allowed and inputs_valid and cruise_engaged and not disengage_on_accelerator and
                CS.cruiseState.available and not stopped_or_braking and not CS.accFaulted and
                not (manual_input or CS.buttonEnable or CC.cruiseControl.resume))
    step_kph = 1.0 if is_metric else CV.MPH_TO_KPH
    target_kph = self.policy.update(
      eligible=eligible,
      can_arm=stock or CC.longActive,
      pressed=CS.gasPressed,
      speed_kph=CS.vEgo * CV.MS_TO_KPH,
      release_speed_kph=(CS.vEgoCluster if stock else CS.vEgo) * CV.MS_TO_KPH,
      set_speed_kph=self.cruise.v_cruise_cluster_kph if stock else self.cruise.v_cruise_kph,
      step_kph=step_kph,
      max_speed_kph=V_CRUISE_MAX,
    )
    if target_kph is not None:
      if stock:
        return target_kph
      if not is_metric:
        # Match VCruiseHelper's imperial grid so a subsequent long SET/RES press advances correctly.
        target_kph = round(target_kph * CV.KPH_TO_MPH) * IMPERIAL_INCREMENT
      target_kph = round(target_kph, 1)
      if target_kph > self.cruise.v_cruise_kph:
        self.cruise.v_cruise_kph = self.cruise.v_cruise_cluster_kph = target_kph
        # while the pedal is still held the set speed keeps following; this change is ours, not the driver's
        self.policy.follow_setpoint(self.cruise.v_cruise_kph, step_kph)
    return None
