"""Unit tests for the bare-metal /dev/fb0 3-tab touch UI renderer & interactions."""

from __future__ import annotations

import unittest

from pulse940.hal.touch import TouchEvent
from pulse940.ui.fb_app import ApplianceUI, FramebufferCanvas, pack_pixel_bytes


class _FakeRpc:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.brightness = 128

    def call(self, payload: dict, timeout: float = 2.0) -> dict:
        self.calls.append(payload)
        cmd = payload.get("cmd")
        if cmd == "backlight":
            act = payload.get("action")
            if act == "up":
                self.brightness = min(255, self.brightness + 38)
            elif act == "down":
                self.brightness = max(8, self.brightness - 38)
            elif act == "set":
                self.brightness = int(payload["value"])
            elif act == "toggle":
                self.brightness = 0 if self.brightness > 0 else 128
            return {
                "ok": True,
                "backlight": {
                    "brightness": self.brightness,
                    "max_brightness": 255,
                    "pct": round(self.brightness * 100 / 255),
                    "blanked": self.brightness == 0,
                },
            }
        if cmd in ("tx_signal", "tx_nec"):
            return {"ok": True, "pulse_count": 67}
        if cmd == "last_tx":
            return {
                "ok": True,
                "last_tx": {
                    "carrier_hz": 38000,
                    "duty_cycle": 33,
                    "pulse_count": 67,
                    "total_duration_us": 67500,
                    "pulses_us": [9000, 4500, 560, 560, 560, 1690, 560],
                },
            }
        if cmd == "sweep_start":
            return {
                "ok": True,
                "sweep": {
                    "active": True,
                    "mode": payload.get("mode", "quick"),
                    "category": payload.get("category", "projector"),
                    "action": payload.get("action", "Power"),
                    "index": 1,
                    "total": 22,
                    "current_label": "Epson",
                    "current_protocol": "NECext",
                    "locked_signal": None,
                },
            }
        if cmd in ("sweep_stop", "sweep_lock"):
            return {
                "ok": True,
                "sweep": {
                    "active": False,
                    "locked_signal": {"protocol": "NECext", "label": "Epson"},
                },
            }
        if cmd == "rf_scan":
            return {
                "ok": True,
                "ap_count": 4,
                "ble_count": 2,
                "brands": ["Epson", "Samsung"],
                "matched_room": {"room_name": "Lecture_101"},
                "aps": [{"ssid": "LAB-AP", "bssid": "00:26:ab:11:22:33"}],
            }
        if cmd == "privacy_rotate":
            return {
                "ok": True,
                "privacy": {
                    "hostname": "dev-a1b2c3d4",
                    "wlan_mac": "02:11:22:33:44:55",
                    "avahi_active": False,
                    "bt_discoverable": False,
                    "stealth_ok": True,
                },
            }
        if cmd == "save_room":
            return {"ok": True}
        if cmd == "led":
            return {
                "ok": True,
                "led": {"backend": "mock_led", "color": "green", "rgb": [0, 255, 0]},
            }
        if cmd == "poweroff":
            return {"ok": True, "powering_off": True}
        return {"ok": True}


class TestFramebufferUI(unittest.TestCase):
    def test_bgrx_pixel_packing_and_all_tabs_render(self) -> None:
        px = pack_pixel_bytes((0xAA, 0xBB, 0xCC), r_off=16, g_off=8, b_off=0, bpp=32)
        self.assertEqual(px, bytes([0xCC, 0xBB, 0xAA, 0x00]))

        canvas = FramebufferCanvas(
            width=720, height=1280, stride=2944, bpp=32, r_off=16, g_off=8, b_off=0
        )
        ui = ApplianceUI(width=720, height=1280)
        ui.status_data = {
            "lirc_backend": "native_lirc",
            "power": {"capacity_pct": 92, "voltage_mv": 4330, "temp_c": 38.5},
            "backlight": {"pct": 50, "blanked": False},
            "privacy": {
                "hostname": "dev-1a2b3c4d",
                "wlan_mac": "02:aa:bb:cc:dd:ee",
                "avahi_active": False,
                "bt_discoverable": False,
                "stealth_ok": True,
            },
            "sweep": {"active": False, "index": 0, "total": 0},
        }
        ui.signals = ["Power", "Freeze", "HDMI1", "AV_Mute"]
        ui.last_tx_data = {
            "carrier_hz": 38000,
            "duty_cycle": 33,
            "pulse_count": 7,
            "total_duration_us": 17430,
            "pulses_us": [9000, 4500, 560, 560, 560, 1690, 560],
        }
        for tab_name in ("universal", "remote", "radar", "scan3d"):
            ui.tab = tab_name
            ui.render(canvas, now=10.0)
            self.assertEqual(len(canvas.buf), 2944 * 1280)
            self.assertTrue(any(canvas.buf[: 2944 * 100]))

    def test_touch_3_tabs_sweeps_radar_and_poweroff_confirm(self) -> None:
        ui = ApplianceUI(width=720, height=1280)
        ui.status_data = {"sweep": {"active": False}}
        ui.signals = ["Power", "Freeze"]
        rpc = _FakeRpc()

        # Brightness controls + header LED color pill
        self.assertTrue(ui.handle_touch(TouchEvent("down", 380, 56), rpc, now=0.9))
        self.assertEqual(rpc.calls[-1], {"cmd": "led", "action": "cycle"})
        self.assertTrue(ui.handle_touch(TouchEvent("down", 630, 168), rpc, now=1.0))
        self.assertEqual(rpc.calls[-1]["action"], "up")
        self.assertTrue(ui.handle_touch(TouchEvent("move", 360, 168), rpc, now=1.1))
        self.assertEqual(rpc.calls[-1]["action"], "set")

        # Default tab: UNIVERSAL -> select TV + QUICK SWEEP -> STOP & LOCK
        self.assertTrue(ui.handle_touch(TouchEvent("down", 250, 330), rpc, now=1.2))
        self.assertEqual(ui.category, "tv")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 200, 520), rpc, now=1.3))
        self.assertEqual(rpc.calls[-1]["cmd"], "sweep_start")
        self.assertEqual(rpc.calls[-1]["mode"], "quick")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 360, 780), rpc, now=1.4))
        self.assertEqual(rpc.calls[-1]["cmd"], "sweep_lock")

        # Switch to REMOTE tab (x=270) and tap first signal card
        self.assertTrue(ui.handle_touch(TouchEvent("down", 270, 250), rpc, now=1.5))
        self.assertEqual(ui.tab, "remote")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 120, 520), rpc, now=1.6))
        self.assertEqual(rpc.calls[-2], {"cmd": "tx_signal", "name": "Power"})

        # Switch to RADAR tab (x=440), run passive Wi-Fi/BLE scan, rotate privacy ID
        self.assertTrue(ui.handle_touch(TouchEvent("down", 440, 250), rpc, now=1.7))
        self.assertEqual(ui.tab, "radar")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 360, 330), rpc, now=1.8))
        self.assertEqual(rpc.calls[-1]["cmd"], "rf_scan")
        self.assertEqual(ui.priority_brands, ["Epson", "Samsung"])
        self.assertTrue(ui.handle_touch(TouchEvent("down", 560, 990), rpc, now=1.9))
        self.assertEqual(rpc.calls[-1]["cmd"], "privacy_rotate")

        # Switch to 3D SCAN tab (x=610), toggle EMF 3D, IMU, Haptic, and CRIMSON theme
        self.assertTrue(ui.handle_touch(TouchEvent("down", 610, 250), rpc, now=1.92))
        self.assertEqual(ui.tab, "scan3d")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 360, 800), rpc, now=1.94))
        self.assertEqual(ui.scanner3d.submode, "emf")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 360, 890), rpc, now=1.96))
        self.assertFalse(ui.scanner3d.imu_mode)
        self.assertTrue(ui.handle_touch(TouchEvent("down", 560, 890), rpc, now=1.98))
        self.assertFalse(ui.haptics.enabled)

        # Two-tap POWER OFF safety confirmation
        self.assertTrue(ui.handle_touch(TouchEvent("down", 600, 1200), rpc, now=2.0))
        self.assertFalse(any(c.get("cmd") == "poweroff" for c in rpc.calls))
        self.assertTrue(ui.handle_touch(TouchEvent("down", 600, 1200), rpc, now=2.5))
        self.assertTrue(any(c.get("cmd") == "poweroff" for c in rpc.calls))


if __name__ == "__main__":
    unittest.main()
