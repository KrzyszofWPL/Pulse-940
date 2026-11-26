"""Unit tests for the bare-metal /dev/fb0 touch UI renderer & interactions."""

from __future__ import annotations

import unittest

from pulse940.hal.touch import TouchEvent
from pulse940.ui.fb_app import ApplianceUI, FramebufferCanvas, pack_pixel_bytes


class _FakeRpc:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.brightness = 128

    def call(self, payload: dict, timeout: float = 1.8) -> dict:
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
        if cmd == "tx_signal":
            return {"ok": True, "pulse_count": 67}
        if cmd == "tx_nec":
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
        if cmd == "macro":
            return {"ok": True, "pulse_count": 67, "taps": 2}
        if cmd == "rf_scan":
            return {
                "ok": True,
                "ap_count": 5,
                "matched_room": {"room_name": "Lecture_101"},
            }
        if cmd == "list_signals":
            return {"ok": True, "signals": ["Power", "HDMI1"]}
        if cmd == "poweroff":
            return {"ok": True, "powering_off": True}
        return {"ok": True}


class TestFramebufferUI(unittest.TestCase):
    def test_bgrx_pixel_packing_and_stride_rendering(self) -> None:
        # Redmi 4A fb0 format: R=16, G=8, B=0 -> BGRX little endian
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
        }
        ui.signals = ["Power", "Freeze", "HDMI1", "AV_Mute"]
        ui.last_tx_data = {
            "carrier_hz": 38000,
            "duty_cycle": 33,
            "pulse_count": 7,
            "total_duration_us": 17430,
            "pulses_us": [9000, 4500, 560, 560, 560, 1690, 560],
        }
        ui.render(canvas, now=10.0)
        self.assertEqual(len(canvas.buf), 2944 * 1280)
        self.assertTrue(any(canvas.buf[: 2944 * 100]))

    def test_touch_controls_brightness_signals_and_poweroff_confirm(self) -> None:
        ui = ApplianceUI(width=720, height=1280)
        ui.signals = ["Power", "Freeze"]
        rpc = _FakeRpc()

        # Tap brightness '+' button
        self.assertTrue(ui.handle_touch(TouchEvent("down", 630, 190), rpc, now=1.0))
        self.assertEqual(rpc.calls[-1]["action"], "up")

        # Drag brightness slider
        self.assertTrue(ui.handle_touch(TouchEvent("move", 360, 190), rpc, now=1.1))
        self.assertEqual(rpc.calls[-1]["action"], "set")

        # Tap first signal card ("Power")
        self.assertTrue(ui.handle_touch(TouchEvent("down", 120, 520), rpc, now=1.2))
        self.assertEqual(rpc.calls[-2], {"cmd": "tx_signal", "name": "Power"})

        # Cycle theme
        old_idx = ui.theme_idx
        self.assertTrue(ui.handle_touch(TouchEvent("down", 260, 1200), rpc, now=1.3))
        self.assertNotEqual(ui.theme_idx, old_idx)

        # Tap IR TEST
        self.assertTrue(ui.handle_touch(TouchEvent("down", 440, 1200), rpc, now=1.4))
        nec_calls = [c for c in rpc.calls if c.get("cmd") == "tx_nec"]
        self.assertEqual(len(nec_calls), 3)

        # Two-tap POWER OFF safety confirmation
        self.assertTrue(ui.handle_touch(TouchEvent("down", 600, 1200), rpc, now=2.0))
        self.assertFalse(any(c.get("cmd") == "poweroff" for c in rpc.calls))
        self.assertTrue(ui.handle_touch(TouchEvent("down", 600, 1200), rpc, now=2.5))
        self.assertTrue(any(c.get("cmd") == "poweroff" for c in rpc.calls))


if __name__ == "__main__":
    unittest.main()
