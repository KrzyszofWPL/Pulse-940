"""Unit and integration tests for HAL and TCP JSON bridge."""

from __future__ import annotations

import json
import socket
import unittest

from pulse940.hal.lirc import MockLircDevice, create_lirc_device, validate_pulses
from pulse940.hal.power import PowerMonitor
from pulse940.net.bridge import BridgeServer, CommandDispatcher


class TestLircHal(unittest.TestCase):
    def test_validate_pulses_accepts_odd_positive(self) -> None:
        cleaned = validate_pulses([9000, 4500, 560], 38000, 33)
        self.assertEqual(cleaned, [9000, 4500, 560])

    def test_validate_pulses_rejects_even_length(self) -> None:
        with self.assertRaises(ValueError):
            validate_pulses([9000, 4500], 38000, 33)

    def test_validate_pulses_rejects_zero_or_negative(self) -> None:
        with self.assertRaises(ValueError):
            validate_pulses([9000, 0, 560], 38000, 33)
        with self.assertRaises(ValueError):
            validate_pulses([9000, -10, 560], 38000, 33)

    def test_mock_lirc_records_tx(self) -> None:
        mock = create_lirc_device(device_path="/nonexistent/lirc0")
        self.assertIsInstance(mock, MockLircDevice)
        res = mock.send_raw([9000, 4500, 560], carrier_hz=38000, duty_cycle=33)
        self.assertTrue(res.ok)
        self.assertEqual(res.total_duration_us, 14060)
        self.assertEqual(mock.tx_count, 1)


class TestPowerHal(unittest.TestCase):
    def test_power_mock_fallback(self) -> None:
        mon = PowerMonitor(sysfs_root="/nonexistent/power_supply")
        tel = mon.read()
        self.assertEqual(tel.source, "mock_qemu")
        self.assertGreater(tel.voltage_mv, 3000)


class TestBridgeServer(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = CommandDispatcher(
            lirc=MockLircDevice(),
            power=PowerMonitor(sysfs_root="/nonexistent/power_supply"),
        )
        self.server = BridgeServer(self.dispatcher, host="127.0.0.1", port=0)
        self.server.start(background=True)

    def tearDown(self) -> None:
        self.server.stop()

    def _rpc(self, payload: dict) -> dict:
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=2) as s:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = s.recv(4096).decode("utf-8")
            return json.loads(data.strip())

    def test_ping_status_and_tx_raw(self) -> None:
        self.assertTrue(self._rpc({"cmd": "ping"})["pong"])

        status = self._rpc({"cmd": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["lirc_backend"], "mock_lirc")

        tx = self._rpc({"cmd": "tx_raw", "pulses": [9000, 4500, 560]})
        self.assertTrue(tx["ok"])
        self.assertEqual(tx["pulse_count"], 3)

        bad_tx = self._rpc({"cmd": "tx_raw", "pulses": [9000, 4500]})
        self.assertFalse(bad_tx["ok"])
        self.assertIn("odd length", bad_tx["error"])

        last = self._rpc({"cmd": "last_tx"})
        self.assertTrue(last["ok"])
        self.assertEqual(last["last_tx"]["total_duration_us"], 14060)


if __name__ == "__main__":
    unittest.main()
