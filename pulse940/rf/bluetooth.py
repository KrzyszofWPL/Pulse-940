"""Passive Bluetooth Low Energy (BLE) AV/TV brand scanner via BlueZ bluetoothctl."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from pulse940.rf.wifi import detect_brand_by_oui, detect_brands

# Bluetooth SIG 16-bit Company Identifiers -> Vendor Brand
BLE_COMPANY_BRANDS: dict[int, str] = {
    0x0006: "Microsoft",
    0x004C: "Apple",
    0x0075: "Samsung",
    0x009E: "Bose",
    0x00C4: "LG",
    0x00E0: "Google",
    0x012D: "Sony",
    0x038F: "Xiaomi",
    0x058E: "Epson",
}

_DEV_LINE_RE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s*(.*)$")
_NAME_RE = re.compile(r"^\s*(?:Name|Alias):\s*(.+)$")
_RSSI_RE = re.compile(r"^\s*RSSI:\s*0x[0-9a-fA-F]+\s*\((-?\d+)\)")
_RSSI_DEC_RE = re.compile(r"^\s*RSSI:\s*(-?\d+)\b")
_MFG_RE = re.compile(r"^\s*ManufacturerData Key:\s*0x([0-9a-fA-F]+)")


@dataclass(frozen=True)
class BleDevice:
    """Single observed BLE advertisement / cached BlueZ device."""

    mac: str
    name: str = ""
    rssi_dbm: int = -100
    manufacturer_id: int | None = None
    brand: str | None = None


def parse_bluetoothctl_devices(output: str) -> list[tuple[str, str]]:
    """Parse `bluetoothctl devices` output into [(mac_lower, name), ...]."""
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        m = _DEV_LINE_RE.match(line.strip())
        if m:
            mac = m.group(1).lower()
            name = m.group(2).strip()
            out.append((mac, name))
    return out


def parse_bluetoothctl_info(mac: str, output: str, fallback_name: str = "") -> BleDevice:
    """Parse `bluetoothctl info <mac>` output into a BleDevice with brand detection."""
    name = fallback_name
    rssi = -100
    mfg_id: int | None = None

    for line in output.splitlines():
        if m_name := _NAME_RE.match(line):
            cand = m_name.group(1).strip()
            if cand and cand.lower().replace(":", "-") != mac.lower().replace(":", "-"):
                name = cand
        elif m_rssi := _RSSI_RE.match(line):
            rssi = int(m_rssi.group(1))
        elif m_rssi_dec := _RSSI_DEC_RE.match(line):
            rssi = int(m_rssi_dec.group(1))
        elif m_mfg := _MFG_RE.match(line):
            mfg_id = int(m_mfg.group(1), 16)

    brand: str | None = None
    if mfg_id is not None:
        brand = BLE_COMPANY_BRANDS.get(mfg_id)
    if brand is None and name:
        matched = detect_brands([name])
        if matched:
            brand = matched[0]
    if brand is None:
        brand = detect_brand_by_oui(mac)

    return BleDevice(
        mac=mac.lower(),
        name=name,
        rssi_dbm=rssi,
        manufacturer_id=mfg_id,
        brand=brand,
    )


def detect_brands_from_ble(devices: list[BleDevice]) -> list[str]:
    """Collect unique AV/consumer brands observed across BLE devices."""
    hits: list[str] = []
    seen: set[str] = set()
    for dev in devices:
        for cand in (dev.brand, detect_brand_by_oui(dev.mac)):
            if cand and cand not in seen:
                seen.add(cand)
                hits.append(cand)
        if dev.name:
            for b in detect_brands([dev.name]):
                if b not in seen:
                    seen.add(b)
                    hits.append(b)
    return hits


class BleScanner:
    """Passive BLE advertisement scanner via `bluetoothctl` with mock fallback."""

    def __init__(self, adapter: str = "hci0", force_mock: bool = False) -> None:
        self.adapter = adapter
        self.force_mock = force_mock

    def is_hardware_available(self) -> bool:
        if self.force_mock:
            return False
        return os.path.isdir(f"/sys/class/bluetooth/{self.adapter}")

    def scan(self, duration_s: int = 3) -> tuple[str, list[BleDevice]]:
        if not self.is_hardware_available():
            mock_devs = [
                BleDevice(
                    mac="00:12:fb:aa:11:22",
                    name="[TV] Samsung Q70",
                    rssi_dbm=-58,
                    manufacturer_id=0x0075,
                    brand="Samsung",
                ),
            ]
            return "mock_ble", mock_devs

        subprocess.run(
            ["bluetoothctl", "--timeout", str(max(1, min(6, duration_s))), "scan", "le"],
            capture_output=True,
            text=True,
            timeout=max(3, duration_s + 3),
            check=False,
        )
        devs_proc = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        parsed = parse_bluetoothctl_devices(devs_proc.stdout)
        results: list[BleDevice] = []
        for mac, name in parsed[:15]:
            info_proc = subprocess.run(
                ["bluetoothctl", "info", mac],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            results.append(parse_bluetoothctl_info(mac, info_proc.stdout, fallback_name=name))
        results.sort(key=lambda d: d.rssi_dbm, reverse=True)
        return f"ble:{self.adapter}", results
