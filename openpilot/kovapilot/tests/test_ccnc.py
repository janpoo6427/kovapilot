import unittest

from openpilot.kovapilot.hud.ccnc import LFA_ACTIVE, LFA_HIDDEN, LFA_STANDBY, MAX_CONTROL_AGE_NS, ccnc_steering_icon


class TestCcncSteeringIcon(unittest.TestCase):
  def setUp(self):
    self.active = {
      "control_age_ns": 0,
      "can_valid": True,
      "steering_fault": False,
      "lateral_active": True,
      "steering_request_active": True,
    }

  def test_active_request_updates_hidden_or_standby_icon(self):
    for icon in (LFA_HIDDEN, LFA_STANDBY):
      with self.subTest(icon=icon):
        self.assertEqual(ccnc_steering_icon(icon, **self.active), LFA_ACTIVE)

  def test_unconfirmed_request_preserves_stock_icon(self):
    for key, value in (
      ("control_age_ns", None),
      ("control_age_ns", -1),
      ("control_age_ns", MAX_CONTROL_AGE_NS + 1),
      ("can_valid", False),
      ("steering_fault", True),
      ("lateral_active", False),
      ("steering_request_active", False),
    ):
      with self.subTest(key=key, value=value):
        self.assertEqual(ccnc_steering_icon(LFA_STANDBY, **(self.active | {key: value})), LFA_STANDBY)

  def test_display_update_at_freshness_boundary(self):
    self.active["control_age_ns"] = MAX_CONTROL_AGE_NS
    self.assertEqual(ccnc_steering_icon(LFA_HIDDEN, **self.active), LFA_ACTIVE)

  def test_other_stock_icons_are_preserved(self):
    for icon in range(LFA_ACTIVE, 8):
      for lateral_active in (True, False):
        with self.subTest(icon=icon, lateral_active=lateral_active):
          self.assertEqual(ccnc_steering_icon(icon, **(self.active | {"lateral_active": lateral_active})), icon)
