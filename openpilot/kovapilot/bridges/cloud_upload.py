"""Narrow bridge between upstream-derived services and kovapilot policy."""

from openpilot.kovapilot.config.cloud_privacy import COMMA_CONNECT_ENABLED, COMMA_ROUTE_UPLOAD_ENABLED


def comma_connect_enabled() -> bool:
  return COMMA_CONNECT_ENABLED


def route_upload_enabled() -> bool:
  return COMMA_ROUTE_UPLOAD_ENABLED
