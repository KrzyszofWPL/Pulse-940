"""Unit tests for BacklightController, evdev PowerKeyStateMachine, and PowerMonitor."""

from __future__ import annotations

import os
import tempfile
import unittest

from pulse940.hal.backlight import BacklightController
from pulse940.hal.buttons import (
    KEY_POWER,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    PowerKeyStateMachine,
)
from pulse940.hal.lirc import MockLircDevice
from pulse940.hal.power import PowerMonitor
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
