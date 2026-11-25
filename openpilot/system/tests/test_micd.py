import sys
from types import SimpleNamespace

import pytest

from openpilot.system import micd


def test_micd_retries_open_and_start_and_closes_failed_stream(monkeypatch):
  class PortAudioError(Exception):
    pass

  events = []
  streams = iter((None, "unavailable", "working"))

  class Stream:
    samplerate = micd.SAMPLE_RATE
    channels = 1
    dtype = "float32"
    device = 0
    blocksize = micd.SAMPLE_BUFFER

    def __init__(self, name):
      self.name = name

    def start(self):
      events.append(("start", self.name))
      if self.name == "unavailable":
        raise PortAudioError("Wait timed out", -9987)

    def close(self):
      events.append(("close", self.name))

    def stop(self):
      events.append(("stop", self.name))

  def open_stream(**options):
    assert options["samplerate"] == micd.SAMPLE_RATE
    name = next(streams)
    events.append(("open", name))
    if name is None:
      raise PortAudioError("Input not ready", -9985)
    return Stream(name)

  sd = SimpleNamespace(
    PortAudioError=PortAudioError,
    InputStream=open_stream,
    _initialize=lambda: None,
    _terminate=lambda: None,
  )
  monkeypatch.setitem(sys.modules, "sounddevice", sd)
  monkeypatch.setattr(micd.time, "sleep", lambda delay: events.append(("sleep", delay)))
  monkeypatch.setattr(micd.cloudlog, "warning", lambda *_: None)
  monkeypatch.setattr(micd.cloudlog, "info", lambda *_: None)

  mic = micd.Mic.__new__(micd.Mic)

  def stop_after_first_update():
    events.append(("publish", "working"))
    raise KeyboardInterrupt

  mic.update = stop_after_first_update
  with pytest.raises(KeyboardInterrupt):
    mic.micd_thread()

  assert events == [
    ("open", None), ("sleep", 3),
    ("open", "unavailable"), ("start", "unavailable"), ("close", "unavailable"), ("sleep", 3),
    ("open", "working"), ("start", "working"), ("publish", "working"), ("stop", "working"), ("close", "working"),
  ]
