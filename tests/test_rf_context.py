"""Unit tests for passive Wi-Fi (iw) parsing, brand radar, and room fingerprinting."""

from __future__ import annotations

import os
import unittest

from pulse940.rf.rooms import load_room_profiles, match_room
from pulse940.rf.wifi import WifiScanner, detect_brands, parse_iw_scan

SAMPLE_IW_SCAN = """BSS aa:bb:cc:11:22:01(on wlan0)
\tlast seen: 1234.567s [boottime]
\tfreq: 5180
\tsignal: -48.00 dBm
\tSSID: LAB-AP-101
BSS aa:bb:cc:11:22:02(on wlan0)
\tfreq: 2437
\tsignal: -64.00 dBm
\tSSID: EPSON-PJ-DEMO
"""


class TestRfContext(unittest.TestCase):
    def test_parse_iw_scan_and_brand_radar(self) -> None:
        aps = parse_iw_scan(SAMPLE_IW_SCAN)
        self.assertEqual(len(aps), 2)
        self.assertEqual(aps[0].bssid, "aa:bb:cc:11:22:01")
        self.assertEqual(aps[0].rssi_dbm, -48)
        self.assertEqual(aps[1].ssid, "EPSON-PJ-DEMO")

        brands = detect_brands([ap.ssid for ap in aps] + ["BenQ-BLE-01"])
        self.assertEqual(brands, ["Epson", "BenQ"])

    def test_room_fingerprinting_match(self) -> None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "profiles",
            "rooms.example.json",
        )
        rooms = load_room_profiles(path)
        source, aps = WifiScanner(force_mock=True).scan()
        self.assertEqual(source, "mock_iw")

        match = match_room(aps, rooms)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.room_name, "Lab-101-Demo")
        self.assertGreaterEqual(match.score, 90.0)
        self.assertEqual(match.matched_bssids, 2)


if __name__ == "__main__":
    unittest.main()
