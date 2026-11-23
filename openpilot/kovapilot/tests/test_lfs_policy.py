from pathlib import Path
from types import SimpleNamespace
import sys
from io import BytesIO
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT))
import lfs


OID_SOURCE = "1" * 64
OID_CUSTOM = "2" * 64


def pre_push_input() -> bytes:
  return f"refs/heads/master {'a' * 40} refs/heads/master {'0' * 40}\n".encode()


def run_pre_push(objects, source_objects, custom_bucket=None):
  stdin = SimpleNamespace(buffer=BytesIO(pre_push_input()))
  with patch.object(lfs.sys, "stdin", stdin), \
       patch.object(lfs, "git", return_value=b"commit-oid\n"), \
       patch.object(lfs, "cat_small", return_value=[(str(i), lfs.pointer(oid, size)) for i, (oid, size) in enumerate(objects.items())]), \
       patch.object(lfs, "object_info", return_value=source_objects), \
       patch.object(lfs, "CUSTOM_BUCKET", custom_bucket), \
       patch.object(lfs, "upload") as upload:
    lfs.pre_push("origin")
    return upload


def test_download_buckets_keep_source_first_and_are_unique():
  with patch.object(lfs, "SOURCE_BUCKET", "commaai/openpilot-lfs"), \
       patch.object(lfs, "CUSTOM_BUCKET", "janpoo6427/kovapilot-lfs"):
    assert lfs.download_buckets() == ("commaai/openpilot-lfs", "janpoo6427/kovapilot-lfs")

  with patch.object(lfs, "SOURCE_BUCKET", "same/bucket"), patch.object(lfs, "CUSTOM_BUCKET", "same/bucket"):
    assert lfs.download_buckets() == ("same/bucket",)


def test_pre_push_skips_objects_already_in_source_bucket():
  upload = run_pre_push({OID_SOURCE: 10}, {f"sha256/{OID_SOURCE}": 10})
  upload.assert_not_called()


def test_pre_push_requires_custom_bucket_only_for_new_objects():
  with pytest.raises(SystemExit, match="KOVAPILOT_LFS_BUCKET"):
    run_pre_push({OID_CUSTOM: 20}, {})


def test_pre_push_uploads_only_objects_missing_from_source_bucket():
  upload = run_pre_push(
    {OID_SOURCE: 10, OID_CUSTOM: 20},
    {f"sha256/{OID_SOURCE}": 10},
    "janpoo6427/kovapilot-lfs",
  )
  upload.assert_called_once_with({OID_CUSTOM: 20}, "janpoo6427/kovapilot-lfs")


def test_pre_push_rejects_source_size_mismatch():
  with pytest.raises(RuntimeError, match="invalid source bucket object"):
    run_pre_push({OID_SOURCE: 10}, {f"sha256/{OID_SOURCE}": 11})
