"""The auto-engage preference is re-read while driving (janpoo 11/27: apply live where safe)."""
from openpilot.kovapilot.config import auto_engage_settings
from openpilot.kovapilot.config.kova_params import AUTO_ENGAGE_KEY
from openpilot.kovapilot.tests.test_auto_engage import _runtime


def _set_preference(runtime, value: bool) -> None:
  runtime.sd.auto_engage.params.get = lambda key, **kwargs: 40 if key == "InitialCruiseSpeedKph" else {
    "schemaVersion": 1, "revision": 1, "values": {AUTO_ENGAGE_KEY: int(value)},
  }


def test_turning_off_while_driving_stops_the_pending_automatic_start(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  runtime.depart()
  _set_preference(runtime, False)
  for _ in range(30):
    assert not runtime.step()
  status = auto_engage_settings.read_auto_engage_status()
  assert (status["value"], status["lateral"], status["longitudinal"]) == (0, "off", "off")


def test_turning_on_while_driving_waits_for_the_next_departure_from_park(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path, requested=False)
  runtime.depart()
  _set_preference(runtime, True)
  for _ in range(30):
    assert not runtime.step()
  status = auto_engage_settings.read_auto_engage_status()
  assert status["value"] == 1 and status["lateral"] == "waiting"

  runtime.cs.gearShifter = "park"
  runtime.cs.standstill = True
  runtime.cs.vEgo = 0.0
  for _ in range(3):
    assert not runtime.step()
  runtime.depart()
  for _ in range(10):
    assert not runtime.step()
  assert runtime.step()


def test_preference_is_not_read_every_frame(monkeypatch, tmp_path):
  runtime = _runtime(monkeypatch, tmp_path)
  reads = []
  original = runtime.sd.auto_engage.params.get

  def counting_get(key, **kwargs):
    if key != "InitialCruiseSpeedKph":
      reads.append(runtime.now_ns)
    return original(key, **kwargs)

  runtime.sd.auto_engage.params.get = counting_get
  for _ in range(50):
    runtime.step()
  assert 4 <= len(reads) <= 6
