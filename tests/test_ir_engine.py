"""Unit and integration tests for NEC encoder, Flipper .ir parser, and macros."""

from __future__ import annotations

import os
import time
import unittest

from pulse940.hal.lirc import MockLircDevice
from pulse940.ir.flipper import IrSignal, load_ir_file, parse_ir_text
from pulse940.ir.macros import IrMacroEngine
from pulse940.ir.nec import encode_nec, encode_necext


class TestNecEncoder(unittest.TestCase):
    def test_nec_frame_structure_and_length(self) -> None:
        pulses = encode_nec(0x04, 0x08)
        # 2 (header) + 32*2 (bits) + 1 (stop mark) = 67
        self.assertEqual(len(pulses), 67)
        self.assertEqual(pulses[0], 9000)
        self.assertEqual(pulses[1], 4500)
        self.assertEqual(pulses[-1], 560)
        self.assertTrue(all(p > 0 for p in pulses))

    def test_necext_frame_length(self) -> None:
        pulses = encode_necext(0x5583, 0x6D92)
        self.assertEqual(len(pulses), 67)


class TestFlipperParser(unittest.TestCase):
    def test_parse_raw_and_parsed_blocks(self) -> None:
        sample = """Filetype: IR signals file
Version: 1
#
name: Power
type: parsed
protocol: NEC
address: 04 00 00 00
command: 08 00 00 00
#
name: CustomRaw
type: raw
frequency: 38000
duty_cycle: 0.330000
data: 9000 4500 560 560 560 1000
"""
        signals = parse_ir_text(sample)
        self.assertIn("Power", signals)
        self.assertIn("CustomRaw", signals)
        self.assertEqual(len(signals["Power"].pulses_us), 67)
        # Even-length raw data (6) gets normalized to odd length (5) for LIRC
        self.assertEqual(len(signals["CustomRaw"].pulses_us), 5)
        self.assertEqual(signals["CustomRaw"].duty_cycle, 33)

    def test_load_demo_projector_ir(self) -> None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "profiles",
            "demo_projector.ir",
        )
        signals = load_ir_file(path)
        self.assertIn("HDMI1", signals)
        self.assertIn("Freeze", signals)
        self.assertIn("LAN", signals)


class TestMacroEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = MockLircDevice()
        self.engine = IrMacroEngine(lirc=self.mock)
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "profiles",
            "demo_projector.ir",
        )
        self.engine.load_profile(path)

    def tearDown(self) -> None:
        self.engine.stop_loop()
        self.engine.cancel_scheduled()

    def test_stealth_macros_and_double_tap(self) -> None:
        res_src = self.engine.discrete_source("USB")
        self.assertTrue(res_src.ok)
        res_rev = self.engine.revert()
        self.assertTrue(res_rev.ok)
        res_frz = self.engine.freeze()
        self.assertTrue(res_frz.ok)
        res_mute = self.engine.av_mute()
        self.assertTrue(res_mute.ok)

        taps = self.engine.double_tap_power(delay_ms=50)
        self.assertEqual(len(taps), 2)
        self.assertEqual(self.mock.tx_count, 6)

    def test_loop_and_schedule_and_probe(self) -> None:
        self.engine.start_loop("USB", interval_s=0.2)
        time.sleep(0.35)
        stopped = self.engine.stop_loop()
        self.assertTrue(stopped)
        self.assertGreaterEqual(self.mock.tx_count, 1)

        before = self.mock.tx_count
        self.engine.schedule_tx("HDMI1", delay_s=0.1)
        time.sleep(0.25)
        self.assertEqual(self.mock.tx_count, before + 1)

        candidates = [
            ("Epson", IrSignal("Power", "parsed", 38000, 33, tuple(encode_nec(0x04, 0x08)))),
            ("BenQ", IrSignal("Power", "parsed", 38000, 33, tuple(encode_nec(0x10, 0x20)))),
        ]
        self.assertEqual(self.engine.probe_start(candidates), 2)
        brand, _ = self.engine.probe_next()
        self.assertEqual(brand, "Epson")
        self.assertEqual(self.engine.probe_lock(), "Epson")


if __name__ == "__main__":
    unittest.main()
