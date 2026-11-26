"""Unit tests for SensorHub, HapticVibrator, and 3D wireframe tactical scanner."""

from __future__ import annotations

import struct
import unittest

from pulse940.hal.haptics import HapticVibrator
from pulse940.hal.sensors import (
    SensorHub,
    SensorSnapshot,
    compute_orientation,
    parse_prox_samples,
    parse_vec3_samples,
)
from pulse940.ui.fb_app import THEMES, FramebufferCanvas
from pulse940.ui.scanner3d import Scanner3DState, project_3d, render_scanner_3d


class TestSensorsAnd3DScanner(unittest.TestCase):
    def test_mock_sensors_haptics_and_binary_parsers(self) -> None:
        raw_vec = struct.pack("<6i", 10, 20, 30, -100, 200, 650000)
        self.assertEqual(parse_vec3_samples(raw_vec), (-100, 200, 650000))
        self.assertEqual(parse_prox_samples(struct.pack("<2I", 123, 45000)), 45000)

        hdg, pitch, roll = compute_orientation(0.0, 0.0, 9.8, 20.0, 0.0, -40.0)
        self.assertGreaterEqual(hdg, 0.0)
        self.assertLess(hdg, 360.0)
        self.assertAlmostEqual(pitch, 0.0, places=1)
        self.assertAlmostEqual(roll, 0.0, places=1)

        hub = SensorHub(force_mock=True)
        hub.start()
        snap = hub.read()
        self.assertEqual(snap.backend, "mock_sensors")
        self.assertGreater(snap.mag_total_ut, 0.0)
        hub.stop()

        hap = HapticVibrator(force_mock=True)
        self.assertEqual(hap.backend, "mock_haptic")
        self.assertTrue(hap.buzz(30))
        self.assertEqual(hap.buzz_count, 1)
        hap.enabled = False
        self.assertFalse(hap.buzz(30))
        hap.close()

    def test_3d_projection_shadow_detection_and_all_submodes(self) -> None:
        pt = project_3d(0.0, 0.0, 0.0, 0.0, 0.4, 360, 500, 400.0)
        self.assertIsNotNone(pt)
        self.assertEqual(pt[0], 360)

        canvas = FramebufferCanvas(width=720, height=1280, stride=2944)
        st = Scanner3DState()
        st.update_rf(
            {
                "aps": [
                    {"bssid": "aa:bb:cc:11:22:01", "rssi_dbm": -52, "ssid": "LAB-AP"},
                    {"bssid": "aa:bb:cc:11:22:02", "rssi_dbm": -78, "ssid": "EPSON-PJ"},
                ],
                "ble_devices": [
                    {"mac": "00:12:fb:11:22:33", "rssi_dbm": -58, "name": "SamsungTV", "brand": "Samsung"},
                ],
            },
            now=1.0,
        )
        self.assertEqual(len(st.objects), 3)

        # Sharp RSSI drop triggers RF SHADOW alert
        st.update_rf(
            {
                "aps": [
                    {"bssid": "aa:bb:cc:11:22:01", "rssi_dbm": -66, "ssid": "LAB-AP"},
                ]
            },
            now=2.0,
        )
        self.assertGreater(st.shadow_alert_until, 2.0)

        snap_alert = SensorSnapshot(
            backend="mock_sensors",
            heading_deg=45.0,
            pitch_deg=30.0,
            roll_deg=2.0,
            accel_x=0.0,
            accel_y=4.9,
            accel_z=8.5,
            mag_x_ut=70.0,
            mag_y_ut=30.0,
            mag_z_ut=45.0,
            mag_total_ut=88.5,
            prox_raw=52000,
            prox_blocked=True,
            timestamp=2.0,
        )

        theme = THEMES[-1]  # CRIMSON theme
        self.assertEqual(theme.name, "CRIMSON")
        for submode in ("rf3d", "emf", "geiger"):
            st.submode = submode
            ev = render_scanner_3d(canvas, st, snap_alert, theme, now=2.1)
            self.assertTrue(ev["prox_alert"])
            self.assertTrue(ev["emf_alert"])
        self.assertTrue(any(canvas.buf[2944 * 300 : 2944 * 750]))


if __name__ == "__main__":
    unittest.main()
