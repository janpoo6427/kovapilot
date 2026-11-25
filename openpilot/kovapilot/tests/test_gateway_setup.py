import os

import pytest

from openpilot.kovapilot.gateway.lan import load_lan_config
from tools.kovapilot import kovalink_gateway_setup as setup


def test_setup_reuses_owned_directory_and_rejects_a_group_writable_one(tmp_path):
  parent = tmp_path / "kova"
  parent.mkdir(mode=0o700)
  setup.prepare_directory(parent)
  parent.chmod(0o770)
  with pytest.raises(ValueError, match="private to writes"):
    setup.prepare_directory(parent)


def test_setup_creates_credentials_accepted_by_gateway_and_reuses_identity(tmp_path, monkeypatch):
  gateway = tmp_path / "gateway"
  gateway.mkdir()
  token = os.urandom(32).hex()
  monkeypatch.setattr(setup.getpass, "getpass", lambda _: token)

  setup.provision_tls(gateway)
  pin = setup.certificate_pin(gateway / "server.pem")
  access, _ = load_lan_config(gateway)
  assert len(pin) == 64
  assert access.authenticates(f"Bearer {token}")
  assert not access.authenticates("")
  assert {file.name for file in gateway.iterdir()} == {"server.pem", "server.key", "token.sha256"}
  assert all(token.encode() not in file.read_bytes() for file in gateway.iterdir())

  def unexpected_prompt(_):
    pytest.fail("already provisioned credentials must be reused")

  monkeypatch.setattr(setup.getpass, "getpass", unexpected_prompt)
  setup.provision_tls(gateway)
  assert setup.certificate_pin(gateway / "server.pem") == pin


def test_setup_rejects_malformed_token_before_writing_credentials(tmp_path, monkeypatch):
  gateway = tmp_path / "gateway"
  monkeypatch.setattr(setup.getpass, "getpass", lambda _: "invalid")

  with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
    setup.provision_tls(gateway)

  assert not gateway.exists()
