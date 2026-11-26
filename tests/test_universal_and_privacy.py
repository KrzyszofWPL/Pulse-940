"""Tests for Flipper multi-protocol encoders, 3-mode Universal Sweep, RF radar & privacy."""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest

from pulse940.hal.lirc import MockLircDevice, fit_lirc_limits, validate_pulses
from pulse940.hal.power import PowerMonitor
from pulse940.ir.flipper import load_ir_signals
from pulse940.ir.protocols import SUPPORTED_PROTOCOLS, encode_flipper_protocol
from pulse940.ir.universal import UniversalLibrary, UniversalSweepEngine
from pulse940.net.bridge import CommandDispatcher
from pulse940.net.privacy import generate_opaque_id, generate_random_local_mac
from pulse940.rf.bluetooth import (
    BleScanner,
    detect_brands_from_ble,
    parse_bluetoothctl_devices,
    parse_bluetoothctl_info,
)
from pulse940.rf.rooms import load_room_profiles, match_room, save_room_profile
from pulse940.rf.wifi import WifiAp, WifiScanner, detect_brands_from_aps


class TestFlipperProtocolsAndLibrary(unittest.TestCase):
    def test_all_supported_protocols_produce_valid_lirc_arrays(self) -> None:
        samples = {
            "NEC": ("20 00 00 00", "02 00 00 00"),
            "NECext": ("01 FF 00 00", "12 ED 00 00"),
            "NEC42": ("51 00 00 00", "00 00 00 00"),
            "NEC42ext": ("51 12 01 00", "34 12 00 00"),
            "Samsung32": ("A2 00 00 00", "06 00 00 00"),
            "SIRC": ("10 00 00 00", "15 00 00 00"),
            "SIRC15": ("44 00 00 00", "12 00 00 00"),
            "SIRC20": ("3A 07 00 00", "32 00 00 00"),
            "RC5": ("10 00 00 00", "10 00 00 00"),
            "RC5X": ("1B 00 00 00", "0C 00 00 00"),
            "RC6": ("04 00 00 00", "0C 00 00 00"),
            "Kaseikyo": ("AC 02 20 00", "D1 03 00 00"),
            "Pioneer": ("A5 00 00 00", "1C 00 00 00"),
            "RCA": ("0F 00 00 00", "54 00 00 00"),
        }
        self.assertEqual(set(samples.keys()), set(SUPPORTED_PROTOCOLS))
        for proto, (addr, cmd) in samples.items():
            hz, duty, pulses = encode_flipper_protocol(proto, addr, cmd)
            self.assertLessEqual(duty, 33)
            cleaned = validate_pulses(pulses, hz, duty)
            self.assertGreater(len(cleaned), 10)
            self.assertEqual(len(cleaned) % 2, 1)

    def test_real_downloaded_universal_ir_assets_parse_cleanly(self) -> None:
        base = os.path.join("profiles", "universal")
        expected_min = {"projector.ir": 120, "tv.ir": 500, "audio.ir": 500, "ac.ir": 200}
        for fname, min_count in expected_min.items():
            path = os.path.join(base, fname)
            self.assertTrue(os.path.isfile(path), f"Missing {path}")
            sigs = load_ir_signals(path, skip_unsupported=False)
            self.assertGreaterEqual(len(sigs), min_count)
            for s in sigs:
                self.assertLessEqual(s.duty_cycle, 40)
                self.assertEqual(len(s.pulses_us) % 2, 1)


class TestUniversalSweepModes(unittest.TestCase):
    def test_quick_full_and_deep_modes_with_stop_and_lock(self) -> None:
        lirc = MockLircDevice()
        lib = UniversalLibrary(base_dir=os.path.join("profiles", "universal"))
        eng = UniversalSweepEngine(lirc=lirc, library=lib)

        # Quick mode runs top 22
        st = eng.start_sweep(
            mode="quick",
            category="projector",
            action="Power",
            priority_brands=["Sony"],
            cooldown_ms=80,
        )
        self.assertEqual(st["mode"], "quick")
        self.assertEqual(st["total"], 22)
        time.sleep(0.18)
        locked = eng.lock_current()
        self.assertFalse(locked["active"])
        self.assertIsNotNone(locked["locked_signal"])
        self.assertGreaterEqual(lirc.tx_count, 1)

        # Deep NEC 256 has 256 commands
        st_deep = eng.start_sweep(mode="deep", nec_address=0x04, cooldown_ms=80)
        self.assertEqual(st_deep["total"], 256)
        stopped = eng.stop_sweep()
        self.assertFalse(stopped["active"])

    def test_brand_priority_moves_detected_vendor_first(self) -> None:
        lib = UniversalLibrary(base_dir=os.path.join("profiles", "universal"))
        sigs = lib.get_signals("projector", "Power", priority_brands=["Sony"])
        self.assertTrue(len(sigs) >= 20)
        self.assertIn(sigs[0].protocol, ("SIRC", "SIRC15", "SIRC20"))

    def test_fit_lirc_limits_and_sweep_error_resilience(self) -> None:
        lib = UniversalLibrary(base_dir=os.path.join("profiles", "universal"))
        proj_sigs = lib.get_signals("projector", "Power")
        # Verify real long captures in projector.ir fit within LIRC kernel bounds
        for sig in proj_sigs:
            fitted = fit_lirc_limits(list(sig.pulses_us))
            self.assertLessEqual(len(fitted), 511)
            self.assertLessEqual(sum(fitted), 900_000)
            self.assertEqual(len(fitted) % 2, 1)

        ac_sigs = load_ir_signals(os.path.join("profiles", "universal", "ac.ir"))
        long_ac = [s for s in ac_sigs if len(s.pulses_us) > 512]
        self.assertTrue(long_ac)
        fitted_ac = fit_lirc_limits(list(long_ac[0].pulses_us))
        self.assertEqual(len(fitted_ac), 511)
        self.assertLessEqual(sum(fitted_ac), 900_000)

        # Verify sweep skips individual TX errors and completes remaining signals
        class _FlakyLirc(MockLircDevice):
            def send_raw(self, pulses_us, carrier_hz=38000, duty_cycle=33):
                if self.tx_count == 1:
                    self.tx_count += 1
                    raise OSError(22, "Invalid argument")
                return super().send_raw(pulses_us, carrier_hz, duty_cycle)

        flaky = _FlakyLirc()
        eng = UniversalSweepEngine(lirc=flaky, library=lib)
        eng.start_sweep(mode="quick", category="projector", action="Power", cooldown_ms=80)
        for _ in range(40):
            st = eng.get_status()
            if not st["active"]:
                break
            time.sleep(0.1)
        st = eng.get_status()
        self.assertFalse(st["active"])
        self.assertEqual(st["index"], 22)
        self.assertEqual(st["total"], 22)
        self.assertEqual(st["skipped"], 1)


class TestRadarRoomSaveAndPrivacy(unittest.TestCase):
    def test_wifi_oui_ble_brand_radar_and_save_room(self) -> None:
        aps = [
            WifiAp(bssid="00:26:ab:12:34:56", rssi_dbm=-55, ssid="PROJ-ROOM"),
            WifiAp(bssid="aa:bb:cc:dd:ee:01", rssi_dbm=-70, ssid="Corp-WiFi"),
        ]
        self.assertIn("Epson", detect_brands_from_aps(aps))

        devs = parse_bluetoothctl_devices(
            "Device 00:12:FB:11:22:33 [TV] Q80 Series\nDevice AA:BB:CC:11:22:33 Unknown\n"
        )
        self.assertEqual(len(devs), 2)
        info = parse_bluetoothctl_info(
            "00:12:fb:11:22:33",
            "  Name: CrystalUHD\n  RSSI: 0xffffffc2 (-62)\n  ManufacturerData Key: 0x0075\n",
        )
        self.assertEqual(info.brand, "Samsung")
        self.assertEqual(info.rssi_dbm, -62)
        self.assertIn("Samsung", detect_brands_from_ble([info]))

        with tempfile.TemporaryDirectory() as tmp:
            rpath = os.path.join(tmp, "rooms.local.json")
            save_room_profile(rpath, "Sala_A1", "profiles/demo_projector.ir", aps)
            loaded = load_room_profiles(rpath)
            matched = match_room(aps, loaded)
            self.assertIsNotNone(matched)
            self.assertEqual(matched.room_name, "Sala_A1")

    def test_opaque_identity_and_bridge_sweep_commands(self) -> None:
        oid = generate_opaque_id()
        self.assertRegex(oid, r"^dev-[0-9a-f]{8}$")
        self.assertNotIn("pulse", oid)
        mac = generate_random_local_mac()
        self.assertTrue(re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac))
        first_octet = int(mac.split(":")[0], 16)
        self.assertEqual(first_octet & 0x03, 0x02)  # locally administered + unicast

        with tempfile.TemporaryDirectory() as tmp:
            disp = CommandDispatcher(
                lirc=MockLircDevice(),
                power=PowerMonitor("/nonexistent/ps", "/nonexistent/th"),
                wifi=WifiScanner(force_mock=True),
                ble=BleScanner(force_mock=True),
                rooms_save_path=os.path.join(tmp, "rooms.local.json"),
            )
            rf = disp.handle({"cmd": "rf_scan", "include_ble": True})
            self.assertTrue(rf["ok"])
            self.assertIn("Epson", rf["brands"])
            self.assertIn("Samsung", rf["brands"])

            sr = disp.handle({"cmd": "save_room", "name": "TestLab"})
            self.assertTrue(sr["ok"])

            sw = disp.handle(
                {"cmd": "sweep_start", "mode": "quick", "category": "projector", "action": "Power"}
            )
            self.assertTrue(sw["ok"])
            disp.handle({"cmd": "sweep_stop"})


if __name__ == "__main__":
    unittest.main()
