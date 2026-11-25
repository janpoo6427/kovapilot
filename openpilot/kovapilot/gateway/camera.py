"""Bounded signaling adapter for the device-local WebRTC daemon."""

import asyncio
import json

from aiohttp import ClientConnectorError, ClientError, ClientSession, ClientTimeout


def camera_offer(payload):
  if not isinstance(payload, dict):
    raise ValueError("expected a JSON object")
  cameras, sdp = payload.get("cameras"), payload.get("sdp")
  if not isinstance(cameras, list) or len(cameras) != 1 or cameras[0] not in ("road", "wideRoad"):
    raise ValueError("expected one road camera")
  if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > 60_000:
    raise ValueError("invalid SDP offer")
  return {"sdp": sdp, "cameras": cameras, "enabled": True, "raw": False,
          "bridge_services_in": [], "bridge_services_out": []}


class CameraSignaling:
  def __init__(self, params):
    self.params = params
    self.lock = asyncio.Lock()

  async def __call__(self, offer):
    if self.lock.locked():
      raise ClientError("signaling busy")
    async with self.lock:
      start_stream = not self.params.get_bool("IsLiveStreaming")
      if start_stream:
        self.params.put_bool("IsLiveStreaming", True, block=True)
      handed_off = False
      try:
        async with asyncio.timeout(10), ClientSession(timeout=ClientTimeout(total=9), trust_env=False) as session:
          for attempt in range(16):
            try:
              async with session.post("http://127.0.0.1:5001/stream", json=offer, allow_redirects=False) as response:
                if response.status != 200:
                  raise ClientError("signaling rejected")
                length = response.content_length
                if length is None or not 0 < length <= 64 * 1024 or response.headers.get("Content-Encoding"):
                  raise ClientError("invalid signaling response length")
                answer = json.loads(await response.content.readexactly(length))
                if not isinstance(answer, dict) or answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str):
                  raise ClientError("invalid signaling answer")
                handed_off = True
                return {"sdp": answer["sdp"], "type": "answer"}
            except ClientConnectorError:
              if attempt == 15:
                raise
              await asyncio.sleep(0.25)
      finally:
        if start_stream and not handed_off:
          self.params.put_bool("IsLiveStreaming", False, block=True)
