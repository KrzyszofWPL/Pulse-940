"""Unit tests for PWM waveform renderer and TUI state logic."""

from __future__ import annotations

import unittest

from pulse940.tui.app import TuiState, render_screen
from pulse940.tui.waveform import render_waveform


class TestTui(unittest.TestCase):
    def test_render_waveform_empty_and_valid(self) -> None:
        self.assertIn("no signal", render_waveform([]))
        wf = render_waveform([9000, 4500, 560, 560, 560, 1690, 560], width=32)
        lines = wf.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("HI |", lines[0])
        self.assertIn("LO |", lines[1])

    def test_tui_state_navigation_and_stealth(self) -> None:
        st = TuiState(items=["Power", "Freeze", "HDMI1"])
        self.assertEqual(st.selected(), "Power")
        st.move_down()
        self.assertEqual(st.selected(), "Freeze")
        st.move_up()
        st.move_up()
        self.assertEqual(st.selected(), "HDMI1")

        st.toggle_stealth()
        self.assertTrue(st.stealth_blank)
        out = render_screen(st, None, None)
        self.assertNotIn("PULSE 940", out)

        st.toggle_stealth()
        out2 = render_screen(st, {"lirc_backend": "mock_lirc"}, None)
        self.assertIn("PULSE 940", out2)


if __name__ == "__main__":
    unittest.main()
