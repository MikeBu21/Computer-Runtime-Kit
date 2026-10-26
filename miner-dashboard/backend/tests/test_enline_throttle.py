# -*- coding: utf-8 -*-
"""Enline-Playwright: Ressourcen-Blocker ohne echten Browser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Playwright sitzt nur im Energy-Monitor-venv – für den Prädikat-Test mocken.
sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.sync_api", MagicMock())

_EM = Path(__file__).resolve().parent.parent / "energy_monitor"
if str(_EM) not in sys.path:
    sys.path.insert(0, str(_EM))

from enline_reader import should_block_enline_resource  # noqa: E402


class TestShouldBlockEnlineResource(unittest.TestCase):
    def test_blocks_images_and_fonts(self):
        self.assertTrue(should_block_enline_resource("image", "https://cloud.enline.at/logo.png"))
        self.assertTrue(should_block_enline_resource("font", "https://fonts.gstatic.com/x.woff2"))
        self.assertTrue(should_block_enline_resource("media", "https://cdn.example/chart.mp4"))
        self.assertFalse(should_block_enline_resource("stylesheet", "https://cloud.enline.at/app.css"))

    def test_blocks_analytics_even_as_script(self):
        self.assertTrue(
            should_block_enline_resource("script", "https://www.google-analytics.com/analytics.js")
        )

    def test_keeps_document_script_xhr(self):
        self.assertFalse(should_block_enline_resource("document", "https://cloud.enline.at/power"))
        self.assertFalse(should_block_enline_resource("script", "https://cloud.enline.at/app.js"))
        self.assertFalse(should_block_enline_resource("xhr", "https://cloud.enline.at/api/values"))
        self.assertFalse(should_block_enline_resource("fetch", "https://cloud.enline.at/api/values"))
        self.assertFalse(should_block_enline_resource("websocket", "wss://cloud.enline.at/live"))


if __name__ == "__main__":
    unittest.main()
