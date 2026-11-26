"""Unit tests for BacklightController, evdev PowerKeyStateMachine, and PowerMonitor."""

from __future__ import annotations

import os
import struct
import tempfile
import unittest

from pulse940.hal.backlight import BacklightController
from pulse940.hal.buttons import (
    EV_KEY,
    KEY_POWER,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    PowerKeyStateMachine,
    parse_input_events,
)
from pulse940.hal.lirc import MockLircDevice
from pulse940.hal.power import PowerMonitor
from pulse940.hal.touch import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_TRACKING_ID,
    EV_ABS,
    EV_SYN,
    SYN_REPORT,
    TouchStateMachine,
)
from pulse940.net.bridge import CommandDispatcher


class TestBacklightController(unittest.TestCase):
    def test_mock_backlight_steps_and_toggle(self) -> None:
        bl = BacklightController(sysfs_root="/nonexistent/backlight")
        st0 = bl.get_state()
        self.assertEqual(st0.backend, "mock_backlight")
        self.assertEqual(st0.brightness, 128)
        self.assertFalse(st0.blanked)

        st_up = bl.step_up(10)
        self.assertEqual(st_up.brightness, 154)

        st_down = bl.step_down(10)
        self.assertEqual(st_down.brightness, 128)

        st_off = bl.toggle_blank()
        self.assertTrue(st_off.blanked)
        self.assertEqual(st_off.brightness, 0)

        st_on = bl.toggle_blank()
        self.assertFalse(st_on.blanked)
        self.assertEqual(st_on.brightness, 128)

        st_clamp = bl.set_brightness(999)
        self.assertEqual(st_clamp.brightness, 255)
        self.assertEqual(st_clamp.pct, 100)

    def test_sysfs_backlight_writes_brightness_and_bl_power(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "backlight")
            os.makedirs(dev)
            with open(os.path.join(dev, "max_brightness"), "w") as f:
                f.write("255\n")
            with open(os.path.join(dev, "brightness"), "w") as f:
                f.write("100\n")
            with open(os.path.join(dev, "bl_power"), "w") as f:
                f.write("0\n")

            bl = BacklightController(sysfs_root=tmp)
            self.assertEqual(bl.get_state().backend, "backlight")
            bl.toggle_blank()
            with open(os.path.join(dev, "brightness")) as f:
                self.assertEqual(f.read().strip(), "0")
            with open(os.path.join(dev, "bl_power")) as f:
                self.assertEqual(f.read().strip(), "4")

            bl.step_up(10)
            with open(os.path.join(dev, "bl_power")) as f:
                self.assertEqual(f.read().strip(), "0")
            with open(os.path.join(dev, "brightness")) as f:
                self.assertEqual(f.read().strip(), "26")


class TestPowerKeyStateMachine(unittest.TestCase):
    def test_volume_and_power_short_vs_long(self) -> None:
        sm = PowerKeyStateMachine(long_press_s=2.5)
        self.assertEqual(sm.on_event(KEY_VOLUMEUP, 1, 1.0), "vol_up")
        self.assertEqual(sm.on_event(KEY_VOLUMEDOWN, 2, 1.1), "vol_down")

        # Short press (< 2.5s) -> power_short on release
        self.assertIsNone(sm.on_event(KEY_POWER, 1, 2.0))
        self.assertIsNone(sm.poll_timeout(3.0))
        self.assertEqual(sm.on_event(KEY_POWER, 0, 3.2), "power_short")

        # Long press (>= 2.5s) -> power_long on poll_timeout, nothing on release
        self.assertIsNone(sm.on_event(KEY_POWER, 1, 10.0))
        self.assertEqual(sm.poll_timeout(12.6), "power_long")
        self.assertIsNone(sm.poll_timeout(13.0))
        self.assertIsNone(sm.on_event(KEY_POWER, 0, 13.1))

    def test_step_down_respects_min_brightness_floor(self) -> None:
        bl = BacklightController(sysfs_root="/nonexistent/backlight")
        bl.set_brightness(20)
        st = bl.step_down(15, min_brightness=8)
        self.assertEqual(st.brightness, 8)
        self.assertFalse(st.blanked)
        st2 = bl.step_down(15, min_brightness=8)
        self.assertEqual(st2.brightness, 8)

    def test_parse_24byte_lp64_evdev_events(self) -> None:
        lp64_struct = struct.Struct("<qqHHi")
        raw = (
            lp64_struct.pack(100, 1000, EV_KEY, KEY_VOLUMEUP, 1)
            + lp64_struct.pack(100, 2000, EV_KEY, KEY_VOLUMEDOWN, 1)
            + lp64_struct.pack(101, 0, EV_KEY, KEY_POWER, 1)
            + lp64_struct.pack(101, 150000, EV_KEY, KEY_POWER, 0)
            + b"\xaa\xbb"  # trailing partial chunk ignored safely
        )
        events = parse_input_events(raw, ev_struct=lp64_struct)
        self.assertEqual(
            events,
            [
                (EV_KEY, KEY_VOLUMEUP, 1),
                (EV_KEY, KEY_VOLUMEDOWN, 1),
                (EV_KEY, KEY_POWER, 1),
                (EV_KEY, KEY_POWER, 0),
            ],
        )

    def test_touch_state_machine_type_b(self) -> None:
        tsm = TouchStateMachine(
            screen_w=720, screen_h=1280, x_bounds=(0, 719), y_bounds=(0, 1279)
        )
        evs = tsm.feed(
            [
                (EV_ABS, ABS_MT_TRACKING_ID, 1),
                (EV_ABS, ABS_MT_POSITION_X, 360),
                (EV_ABS, ABS_MT_POSITION_Y, 640),
                (EV_SYN, SYN_REPORT, 0),
                (EV_ABS, ABS_MT_POSITION_X, 400),
                (EV_SYN, SYN_REPORT, 0),
                (EV_ABS, ABS_MT_TRACKING_ID, -1),
                (EV_SYN, SYN_REPORT, 0),
            ]
        )
        self.assertEqual([(e.kind, e.x, e.y) for e in evs], [
            ("down", 360, 640),
            ("move", 400, 640),
            ("up", 400, 640),
        ])


class TestPowerMonitorThermalAndGauge(unittest.TestCase):
    def test_cw2015_plus_thermal_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ps_dir = os.path.join(tmp, "ps")
            bat = os.path.join(ps_dir, "cw2015-battery")
            th_dir = os.path.join(tmp, "th")
            tz0 = os.path.join(th_dir, "thermal_zone0")
            os.makedirs(bat)
            os.makedirs(tz0)

            with open(os.path.join(bat, "type"), "w") as f:
                f.write("Battery\n")
            with open(os.path.join(bat, "voltage_now"), "w") as f:
                f.write("3985000\n")
            with open(os.path.join(bat, "capacity"), "w") as f:
                f.write("74\n")
            with open(os.path.join(bat, "status"), "w") as f:
                f.write("Discharging\n")

            with open(os.path.join(tz0, "type"), "w") as f:
                f.write("pm8937-thermal\n")
            with open(os.path.join(tz0, "temp"), "w") as f:
                f.write("36400\n")

            mon = PowerMonitor(sysfs_root=ps_dir, thermal_root=th_dir)
            tel = mon.read()
            self.assertEqual(tel.source, "cw2015-battery")
            self.assertEqual(tel.voltage_mv, 3985)
            self.assertEqual(tel.capacity_pct, 74)
            self.assertEqual(tel.temp_c, 36.4)

    def test_bridge_backlight_command(self) -> None:
        disp = CommandDispatcher(
            lirc=MockLircDevice(),
            power=PowerMonitor(
                sysfs_root="/nonexistent/ps", thermal_root="/nonexistent/th"
            ),
            backlight=BacklightController(sysfs_root="/nonexistent/bl"),
        )
        res = disp.handle({"cmd": "backlight", "action": "up", "step_pct": 10})
        self.assertTrue(res["ok"])
        self.assertEqual(res["backlight"]["brightness"], 154)
        st = disp.handle({"cmd": "status"})
        self.assertIn("backlight", st)


if __name__ == "__main__":
    unittest.main()
