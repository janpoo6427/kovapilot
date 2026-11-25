"""CCNC steering-wheel indication for offline display validation."""

LFA_HIDDEN = 0
LFA_STANDBY = 1
LFA_ACTIVE = 2
MAX_CONTROL_AGE_NS = 200_000_000


def ccnc_steering_icon(stock_icon: int, *,
                       control_age_ns: int | None, can_valid: bool, steering_fault: bool,
                       lateral_active: bool, steering_request_active: bool) -> int:
  """Return the proposed LFA icon. A missing age denotes invalid or absent control data."""
  if stock_icon not in (LFA_HIDDEN, LFA_STANDBY):
    return stock_icon
  if control_age_ns is None or not 0 <= control_age_ns <= MAX_CONTROL_AGE_NS:
    return stock_icon
  if can_valid and not steering_fault and lateral_active and steering_request_active:
    return LFA_ACTIVE
  return stock_icon
