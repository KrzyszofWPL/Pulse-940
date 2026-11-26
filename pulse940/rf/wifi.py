"""Passive Wi-Fi BSSID + RSSI scanner (via iw) and AV/HVAC brand radar."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

KNOWN_BRANDS: tuple[str, ...] = (
    "Epson",
    "BenQ",
    "Optoma",
    "NEC",
    "Sony",
    "Panasonic",
    "Christie",
    "ViewSonic",
    "Acer",
    "LG",
    "Samsung",
    "Daikin",
    "Mitsubishi",
    "Gree",
)

_BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})")
_SIGNAL_RE = re.compile(r"^\s*signal:\s*(-?\d+(?:\.\d+)?)\s*dBm")
_FREQ_RE = re.compile(r"^\s*freq:\s*(\d+)")
_SSID_RE = re.compile(r"^\s*SSID:\s*(.*)$")


@dataclass(frozen=True)
class WifiAp:
    """Single observed Wi-Fi access point from passive scan."""

    bssid: str
    rssi_dbm: int
    ssid: str = ""
    freq_mhz: int = 0


def parse_iw_scan(output: str) -> list[WifiAp]:
    """Parse `iw dev <iface> scan` output into a list of WifiAp."""
    aps: list[WifiAp] = []
    cur_bssid: str | None = None
    cur_rssi = -100
    cur_ssid = ""
    cur_freq = 0

    def _flush() -> None:
        if cur_bssid is not None:
            aps.append(
                WifiAp(
                    bssid=cur_bssid.lower(),
                    rssi_dbm=cur_rssi,
                    ssid=cur_ssid,
                    freq_mhz=cur_freq,
                )
            )

    for line in output.splitlines():
        m_bss = _BSS_RE.match(line)
        if m_bss:
            _flush()
            cur_bssid = m_bss.group(1)
            cur_rssi = -100
            cur_ssid = ""
            cur_freq = 0
            continue
        if cur_bssid is None:
            continue
        if m_sig := _SIGNAL_RE.match(line):
            cur_rssi = round(float(m_sig.group(1)))
        elif m_freq := _FREQ_RE.match(line):
            cur_freq = int(m_freq.group(1))
        elif m_ssid := _SSID_RE.match(line):
            cur_ssid = m_ssid.group(1).strip()

    _flush()
    return sorted(aps, key=lambda ap: ap.rssi_dbm, reverse=True)


def detect_brands(names: list[str]) -> list[str]:
    """Return known AV/HVAC brand names matched in SSID or BLE advertisements."""
    hits: list[str] = []
    seen: set[str] = set()
    for raw in names:
        low = raw.lower()
        for brand in KNOWN_BRANDS:
            if brand.lower() in low and brand not in seen:
                seen.add(brand)
                hits.append(brand)
    return hits


class WifiScanner:
    """Passive Wi-Fi scanner using `iw` with automatic QEMU mock fallback."""

    def __init__(self, iface: str = "wlan0", force_mock: bool = False) -> None:
        self.iface = iface
        self.force_mock = force_mock

    def is_hardware_available(self) -> bool:
        if self.force_mock:
            return False
        return os.path.isdir(f"/sys/class/net/{self.iface}/wireless")

    def scan(self) -> tuple[str, list[WifiAp]]:
        if not self.is_hardware_available():
            mock_aps = [
                WifiAp(bssid="aa:bb:cc:11:22:01", rssi_dbm=-48, ssid="LAB-AP-101", freq_mhz=5180),
                WifiAp(bssid="aa:bb:cc:11:22:02", rssi_dbm=-64, ssid="EPSON-PJ-DEMO", freq_mhz=2437),
                WifiAp(bssid="aa:bb:cc:11:22:03", rssi_dbm=-79, ssid="Guest-WiFi", freq_mhz=2412),
            ]
            return "mock_iw", mock_aps

        proc = subprocess.run(
            ["iw", "dev", self.iface, "scan"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return f"iw:{self.iface}", parse_iw_scan(proc.stdout)
