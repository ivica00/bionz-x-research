"""Tests for tracked av-cam.bin denominators and deterministic SVG rendering."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research import avcam_dashboard as dashboard  # noqa: E402
from tools.research import render_avcam_status as renderer  # noqa: E402


class AvcamStatusSvgTests(unittest.TestCase):
    def test_tracked_shards_validate_against_denominators(self) -> None:
        base = dashboard.load_denominators(renderer.DEFAULT_MAP_DIR)
        shards = dashboard.load_shards(renderer.DEFAULT_MAP_DIR)
        dashboard.validate(shards, base)
        self.assertEqual(len(base["commands"]), 285)
        self.assertEqual(len(base["tags"]), 748)
        self.assertEqual(len(base["mmio"]), 1220)
        self.assertEqual(base["function_count"], 58144)

    def test_svg_is_valid_xml_and_matches_committed_file(self) -> None:
        svg = renderer.render_svg(renderer.DEFAULT_MAP_DIR)
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("ILCE-6400 av-cam.bin Deep-Analysis Progress", svg)
        self.assertEqual(svg, renderer.render_svg(renderer.DEFAULT_MAP_DIR))
        self.assertTrue(renderer.DEFAULT_OUTPUT.is_file())
        self.assertEqual(renderer.DEFAULT_OUTPUT.read_text(encoding="utf-8"), svg)


if __name__ == "__main__":
    unittest.main()
