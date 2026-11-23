from pathlib import Path

import pytest

from openpilot.kovapilot.bridges import cloud_upload
from openpilot.kovapilot.config import cloud_privacy
from openpilot.system.athena import athenad, manage_athenad
from openpilot.system.loggerd import uploader


def test_cloud_features_default_off():
  assert cloud_privacy.COMMA_CONNECT_ENABLED is False
  assert cloud_privacy.COMMA_ROUTE_UPLOAD_ENABLED is False
  assert cloud_upload.comma_connect_enabled() is False
  assert cloud_upload.route_upload_enabled() is False


def test_uploader_main_returns_before_side_effects(monkeypatch):
  monkeypatch.setattr(uploader, "route_upload_enabled", lambda: False)
  monkeypatch.setattr(uploader, "clear_locks", lambda _: pytest.fail("route files must not be touched"))
  monkeypatch.setattr(uploader, "set_core_affinity", lambda _: pytest.fail("uploader must return before setup"))
  uploader.main()


def test_uploader_blocks_direct_upload(monkeypatch, tmp_path: Path):
  monkeypatch.setattr(uploader, "route_upload_enabled", lambda: False)
  monkeypatch.setattr(uploader, "Api", lambda _: object())
  monkeypatch.setattr(uploader, "Params", lambda: object())
  route_file = tmp_path / "qlog"
  route_file.write_bytes(b"route data")

  instance = uploader.Uploader("redacted", str(tmp_path))
  assert instance.upload("qlog", "route/qlog", str(route_file), 1, False) is False
  with pytest.raises(RuntimeError, match="disabled by kovapilot policy"):
    instance.do_upload("route/qlog", str(route_file))


def test_athena_rejects_upload_without_queueing(monkeypatch):
  monkeypatch.setattr(athenad, "route_upload_enabled", lambda: False)
  before = athenad.upload_queue.qsize()

  response = athenad.uploadFilesToUrls([{"fn": "qlog.zst", "url": "https://example.invalid/upload", "headers": {}}])

  assert response == {"enqueued": 0, "items": [], "error": "uploads disabled by kovapilot policy"}
  assert athenad.upload_queue.qsize() == before


def test_athena_main_returns_before_side_effects(monkeypatch):
  monkeypatch.setattr(athenad, "comma_connect_enabled", lambda: False)
  monkeypatch.setattr(athenad, "set_core_affinity", lambda _: pytest.fail("athena must return before setup"))
  monkeypatch.setattr(athenad, "Params", lambda: pytest.fail("athena must not read identity params"))
  athenad.main()


def test_athena_manager_returns_before_side_effects(monkeypatch):
  monkeypatch.setattr(manage_athenad, "comma_connect_enabled", lambda: False)
  monkeypatch.setattr(manage_athenad, "Params", lambda: pytest.fail("athena manager must not read identity params"))
  manage_athenad.main()


def test_athena_direct_upload_is_blocked(monkeypatch):
  monkeypatch.setattr(athenad, "route_upload_enabled", lambda: False)
  item = athenad.UploadItem(path="unused", url="https://example.invalid/upload", headers={}, created_at=0, id="test")

  with pytest.raises(RuntimeError, match="disabled by kovapilot policy"):
    athenad._do_upload(item)
