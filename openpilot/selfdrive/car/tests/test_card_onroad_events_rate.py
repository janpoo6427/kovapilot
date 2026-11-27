"""KOVA: card gates the accelerator cruise sync on its onroadEvents subscription. selfdrived sends onroadEvents
every second and on every change, so the gas press/release changes push the average rate above the 1 Hz service
rate. CAN-FD angle steering car rlog 0000079c 2026-11-27 segment 66: two gas releases at 71 and 81 km/h above the
60 km/h set speed did not sync because that average-rate check failed at the release."""
import pytest

from openpilot.cereal import messaging
from openpilot.selfdrive.car import card

# onroadEvents receive times (s, from 4000 s) recorded in that drive; gas released at 21.03 s and 29.33 s
EVENT_TIMES = [0.32, 1.32, 1.63, 2.32, 3.32, 3.36, 3.37, 4.32, 5.32, 6.32, 7.32, 8.32, 9.32, 10.32, 11.32, 12.32,
               13.32, 13.79, 14.32, 15.32, 16.13, 16.32, 17.32, 18.32, 19.32, 20.32, 21.0, 21.32, 22.29, 22.32,
               23.32, 24.32, 25.32, 26.32, 27.32, 28.32, 29.27, 29.32, 30.32]
RELEASES = (2103, 2933)  # 10 ms card frames
CHECKED = ['carControl', 'onroadEvents']


class _Stop(Exception):
  pass


def card_submaster(monkeypatch):
  made = []
  real = messaging.SubMaster

  def submaster(*args, **kwargs):
    made.append(real(*args, **kwargs))
    return made[-1]

  def stop(*args, **kwargs):
    raise _Stop

  monkeypatch.setattr(messaging, 'sub_sock', lambda *args, **kwargs: None)
  monkeypatch.setattr(card.messaging, 'SubMaster', submaster)
  monkeypatch.setattr(card.messaging, 'PubMaster', stop)
  with pytest.raises(_Stop):
    card.Car()
  return made[0]


def message(service):
  msg = messaging.new_message(service) if service != 'onroadEvents' else messaging.new_message(service, 0)
  msg.valid = True
  return msg


def drive(sm, event_times, frames):
  events = iter(event_times)
  next_event = next(events, None)
  checks = []
  for frame in range(frames):
    t = frame / 100.
    msgs = [message('carControl')]
    if next_event is not None and next_event <= t + 1e-6:
      msgs.append(message('onroadEvents'))  # conflated socket: one message per update
    while next_event is not None and next_event <= t + 1e-6:
      next_event = next(events, None)
    sm.update_msgs(t, msgs)
    checks.append(sm.all_checks(CHECKED))
  return checks


def test_event_changes_do_not_block_cruise_sync_checks(monkeypatch):
  checks = drive(card_submaster(monkeypatch), EVENT_TIMES, 3100)
  for frame in RELEASES:
    assert checks[frame], frame
  assert all(checks[50:]), [frame for frame, ok in enumerate(checks) if frame >= 50 and not ok][:10]


def test_stale_or_invalid_events_still_block(monkeypatch):
  sm = card_submaster(monkeypatch)
  # selfdrived stops sending: onroadEvents not alive after 10 s
  checks = drive(sm, EVENT_TIMES[:5], 1500)
  assert checks[400] and not checks[1499]

  sm = card_submaster(monkeypatch)
  drive(sm, EVENT_TIMES, 500)
  bad = message('onroadEvents')
  bad.valid = False
  sm.update_msgs(5.0, [message('carControl'), bad])
  assert not sm.all_checks(CHECKED)


def test_carcontrol_rate_still_checked(monkeypatch):
  sm = card_submaster(monkeypatch)
  assert sm.ignore_average_freq == ['onroadEvents']
  events = iter(EVENT_TIMES)
  next_event = next(events, None)
  for frame in range(0, 1000, 10):  # carControl at 10 Hz instead of 100 Hz
    t = frame / 100.
    msgs = [message('carControl')]
    if next_event is not None and next_event <= t + 1e-6:
      msgs.append(message('onroadEvents'))  # conflated socket: one message per update
    while next_event is not None and next_event <= t + 1e-6:
      next_event = next(events, None)
    sm.update_msgs(t, msgs)
  assert not sm.all_checks(CHECKED)
