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
    "Philips",
    "Hisense",
    "TCL",
    "Daikin",
    "Mitsubishi",
    "Midea",
    "Gree",
    "Bose",
    "Sonos",
    "Yamaha",
    "Roku",
    "Apple",
    "Google",
    "Xiaomi",
)

# Representative IEEE MAC OUI prefixes -> Brand name (lowercase colon prefix)
OUI_BRANDS: dict[str, str] = {
    "00:00:48": "Epson",
    "00:26:ab": "Epson",
    "44:d2:44": "Epson",
    "64:eb:8c": "Epson",
    "a4:ee:57": "Epson",
    "00:12:fb": "Samsung",
    "00:15:b9": "Samsung",
    "50:85:69": "Samsung",
    "8c:79:f5": "Samsung",
    "cc:b1:1a": "Samsung",
    "f4:09:d8": "Samsung",
    "00:1c:62": "LG",
    "00:e0:91": "LG",
    "34:4d:f7": "LG",
    "58:a2:b5": "LG",
    "78:5d:c8": "LG",
    "a8:23:fe": "LG",
    "00:1d:ba": "Sony",
    "00:24:be": "Sony",
    "04:5d:4b": "Sony",
    "30:52:cb": "Sony",
    "ac:9b:0a": "Sony",
    "00:0f:3d": "Panasonic",
    "00:80:45": "Panasonic",
    "8c:c1:21": "Panasonic",
    "00:0d:c5": "BenQ",
    "18:db:f2": "BenQ",
    "00:1c:91": "Optoma",
    "00:04:1f": "NEC",
    "00:19:e0": "NEC",
    "00:17:ab": "Philips",
    "1c:5a:6b": "Philips",
    "00:26:ba": "Hisense",
    "80:78:71": "Hisense",
    "4c:bc:98": "TCL",
    "d8:5d:e2": "TCL",
    "38:8b:59": "Daikin",
    "00:26:62": "Mitsubishi",
    "c4:07:2f": "Midea",
    "f4:91:1e": "Gree",
    "04:52:c7": "Bose",
    "2c:41:a1": "Bose",
    "00:0e:58": "Sonos",
    "5c:aa:fd": "Sonos",
    "00:a0:de": "Yamaha",
    "ac:ae:19": "Roku",
    "b8:3e:59": "Roku",
    "28:cf:da": "Apple",
    "3c:15:c2": "Apple",
    "f4:f5:d8": "Google",
    "54:60:09": "Google",
    "64:cc:2e": "Xiaomi",
    "7c:1d:d9": "Xiaomi",
}

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


def detect_brand_by_oui(mac: str) -> str | None:
    """Return manufacturer brand matching the first 3 octets (OUI) of a MAC."""
    norm = mac.strip().lower().replace("-", ":")
    if len(norm) >= 8:
        return OUI_BRANDS.get(norm[:8])
    return None


def detect_brands_from_aps(aps: list[WifiAp]) -> list[str]:
    """Detect AV/HVAC brands from both AP SSIDs and BSSID MAC OUI prefixes."""
    hits = detect_brands([ap.ssid for ap in aps if ap.ssid])
    seen = set(hits)
    for ap in aps:
        brand = detect_brand_by_oui(ap.bssid)
        if brand and brand not in seen:
            seen.add(brand)
            hits.append(brand)
    return hits


class WifiScanner:
    """Strictly passive Wi-Fi scanner using `iw scan passive` / `scan dump`."""

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
            ["iw", "dev", self.iface, "scan", "passive"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return f"iw_passive:{self.iface}", parse_iw_scan(proc.stdout)

        # Fallback to cached kernel scan results if NetworkManager owns the scan lock (-16)
        dump = subprocess.run(
            ["iw", "dev", self.iface, "scan", "dump"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return f"iw_dump:{self.iface}", parse_iw_scan(dump.stdout)
