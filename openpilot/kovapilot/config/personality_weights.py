"""Driving personality weights: features that follow LongitudinalPersonality (the comma Driving Personality, the
distance button levels 1-4: aggressive, standard, relaxed, moreRelaxed). Every table is keyed by personality and
standard is the behaviour before these weights existed. Safety conditions (the blind-spot clear time, the model
lane check, panda limits) do not follow the personality and are not here."""
from openpilot.cereal import log
from openpilot.kovapilot.config.longitudinal_personality import DEFAULT_PERSONALITY, VALID_PERSONALITIES

P = log.LongitudinalPersonality

# Automatic lane change (steering.lane_change_mode 2): seconds after the turn signal comes on before it may start.
LANE_CHANGE_START_DELAY_S = {
  P.aggressive: 0.7,
  P.standard: 1.0,
  P.relaxed: 1.3,
  P.moreRelaxed: 1.5,
}

# Following a stopped lead away (openpilot longitudinal): (hold after the departure is confirmed, ns; acceleration
# ramp, m/s^3), see kovapilot/longitudinal/lead_restart.py. None adds no limit: the planner as it is, which was the
# default of the removed cruise.lead_restart_response (Off). The limit can only make a departure later and softer,
# so aggressive stays on the planner as it is (its shorter following time already departs earlier). relaxed and
# moreRelaxed use the old Normal (2) and Slow (1) reactions.
LEAD_RESTART_REACTION = {
  P.aggressive: None,
  P.standard: None,
  P.relaxed: (300_000_000, 0.75),
  P.moreRelaxed: (650_000_000, 0.45),
}


def personality_key(personality) -> int:
  """The table key for a personality from a message (enum), an int or nothing; unknown means standard."""
  raw = getattr(personality, "raw", personality)
  return raw if type(raw) is int and raw in VALID_PERSONALITIES else DEFAULT_PERSONALITY


def personality_value(table: dict, personality):
  return table[personality_key(personality)]
