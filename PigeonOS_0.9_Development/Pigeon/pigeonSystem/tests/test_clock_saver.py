"""Clock saver SVG layout + weather cache helpers."""

from __future__ import annotations

import os
import sys
import unittest
import xml.etree.ElementTree as ET

_SYS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYS_ROOT not in sys.path:
    sys.path.insert(0, _SYS_ROOT)

from PIL import Image, ImageDraw

from pigeon.widgets import clock_saver as cs  # noqa: E402
from pigeon import weather as wx  # noqa: E402


class ClockSaverSvgTests(unittest.TestCase):
    def test_asset_exists(self) -> None:
        path = cs.default_clock_saver_svg_path()
        self.assertTrue(path.is_file(), f"missing {path}")

    def test_apply_state_sets_date_and_degrees(self) -> None:
        path = cs.default_clock_saver_svg_path()
        root = cs._svg_tree_from_path(path)
        cs._apply_clock_saver_svg_state(root, color_hex="#58ff00")
        date_el = cs._find_by_logical_id(
            root, "today_month_year_text", "tday_month_year_text"
        )
        self.assertIsNotNone(date_el)
        self.assertTrue("".join(date_el.itertext()).strip())
        left = cs._find_by_logical_id(root, "degrees_left_stroke")
        right = cs._find_by_logical_id(
            root, "degrees_right_stroke", "degrees_rifght_stroke"
        )
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)
        self.assertEqual((left.get("fill") or "").lower(), "none")
        self.assertEqual((right.get("fill") or "").lower(), "none")
        self.assertEqual((left.get("stroke") or "").lower(), "#58ff00")

    def test_composite_returns_full_frame(self) -> None:
        (frame, rect), (empty, _er) = cs.clock_saver_composite_bgra(
            shadow_bgr=None,
            layer_opacity=1.0,
        )
        self.assertEqual(frame.shape[0], cs.DESIGN_H)
        self.assertEqual(frame.shape[1], cs.DESIGN_W)
        self.assertEqual(rect, (0, 0, cs.DESIGN_W, cs.DESIGN_H))
        self.assertEqual(empty.shape[0], 1)

    def test_fixed_cells_keep_block_width_stable(self) -> None:
        """Narrow glyphs (1) and wide glyphs (0/8) must share one cell pitch."""
        font_path = cs.resolve_digital7_font() or cs.resolve_ui_font_bold()
        probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        font = cs._fit_digital7_fixed_cells(
            font_path,
            8,
            max_w=700,
            max_h=200,
            prefer_sz=180,
        )
        cell_w, _cell_h = cs._cell_metrics(probe, font, cs._HHMMSS_CHAR_SET)
        for sample in ("11:11:11", "08:08:08", "00:00:00"):
            self.assertEqual(len(sample) * cell_w, 8 * cell_w)


class WeatherCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        with wx._lock:
            wx._cache_high = None
            wx._cache_low = None
            wx._cache_zip = ""
            wx._cache_mono = 0.0

    def test_store_and_read_cache(self) -> None:
        wx._store(wx.WeatherTemps(high_f=89, low_f=77, zip_code="21704"))
        got = wx.cached_weather_temps()
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.high_f, 89)
        self.assertEqual(got.low_f, 77)


if __name__ == "__main__":
    unittest.main()
