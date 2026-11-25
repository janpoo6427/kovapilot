from pathlib import Path
import struct
import unittest

from openpilot.kovapilot.branding import BRAND_NAME, BRAND_SPINNER_ASSET


ROOT = Path(__file__).parents[3]


class TestBranding(unittest.TestCase):
  def test_brand_contract(self):
    self.assertEqual(BRAND_NAME, "kovapilot")
    self.assertEqual(BRAND_SPINNER_ASSET, "images/kovapilot_icon_white.png")

  def test_spinner_asset_is_square_rgba_png(self):
    asset = ROOT / "openpilot" / "selfdrive" / "assets" / BRAND_SPINNER_ASSET
    data = asset.read_bytes()

    self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    self.assertEqual((width, height), (512, 512))
    self.assertEqual(bit_depth, 8)
    self.assertEqual(color_type, 6)  # truecolor with alpha


if __name__ == "__main__":
  unittest.main()
