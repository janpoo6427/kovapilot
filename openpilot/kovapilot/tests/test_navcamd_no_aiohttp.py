"""navcamd must start under the comma system Python, which has no aiohttp.

2026-11-27 drive 0000079b: navcamd died on every start with
ModuleNotFoundError: No module named 'aiohttp' (gateway/lan.py), and
selfdrived then raised processNotRunning, which blocks engagement.
"""

import subprocess
import sys
import textwrap


def _run_without_aiohttp(body: str) -> subprocess.CompletedProcess:
  code = textwrap.dedent("""
    import sys

    class _BlockAiohttp:
      def find_spec(self, name, path=None, target=None):
        if name == "aiohttp" or name.startswith("aiohttp."):
          raise ModuleNotFoundError("No module named 'aiohttp'", name="aiohttp")
        return None

    sys.meta_path.insert(0, _BlockAiohttp())
    for name in list(sys.modules):
      if name == "aiohttp" or name.startswith("aiohttp."):
        del sys.modules[name]
  """) + textwrap.dedent(body)
  return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)


def test_navcamd_wifi_helpers_load_without_aiohttp():
  result = _run_without_aiohttp("""
    import inspect
    from openpilot.kovapilot.navigation import navcamd
    from openpilot.kovapilot.gateway.wifi import WIFI_INTERFACE, wifi_address

    assert WIFI_INTERFACE == "wlan0"
    wifi_address()  # returns None or an address; must not import aiohttp
    navcamd._udp_socket("127.0.0.1", 0).close()
    navcamd.Network().refresh(0)
    assert "gateway.lan" not in inspect.getsource(navcamd)
    assert "aiohttp" not in sys.modules
    assert "openpilot.kovapilot.gateway.lan" not in sys.modules
  """)
  assert result.returncode == 0, result.stdout + result.stderr


def test_lan_keeps_the_same_wifi_names():
  from openpilot.kovapilot.gateway import lan, wifi

  assert lan.WIFI_INTERFACE == wifi.WIFI_INTERFACE
  assert lan.PRIVATE_NETWORKS == wifi.PRIVATE_NETWORKS
  assert callable(lan.wifi_address)
