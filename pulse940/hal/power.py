"""Power supply & thermal telemetry via /sys/class/power_supply/ with thermal & mock fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerTelemetry:
    """Snapshot of battery/power state."""

    source: str
    voltage_mv: int
    current_ma: int
    capacity_pct: int
    temp_c: float
    status: str


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_thermal_temp_c(thermal_root: str = "/sys/class/thermal") -> float | None:
    if not os.path.isdir(thermal_root):
        return None
    zones: dict[str, int] = {}
    for entry in sorted(os.listdir(thermal_root)):
        if not entry.startswith("thermal_zone"):
            continue
        zpath = os.path.join(thermal_root, entry)
        ztype = _read_str(os.path.join(zpath, "type"))
        ztemp = _read_int(os.path.join(zpath, "temp"))
        if ztype and ztemp is not None:
            zones[ztype] = ztemp

    for preferred in ("pm8937-thermal", "aoss-thermal", "cpu0-thermal"):
        if preferred in zones:
            return round(zones[preferred] / 1000.0, 1)
    if zones:
        first_val = next(iter(zones.values()))
        return round(first_val / 1000.0, 1)
    return None


class PowerMonitor:
    """Reads real Linux sysfs power telemetry or returns thermal/QEMU fallback."""

    def __init__(
        self,
        sysfs_root: str = "/sys/class/power_supply",
        thermal_root: str = "/sys/class/thermal",
    ) -> None:
        self.sysfs_root = sysfs_root
        self.thermal_root = thermal_root

    def _find_battery_dir(self) -> str | None:
        if not os.path.isdir(self.sysfs_root):
            return None
        for entry in sorted(os.listdir(self.sysfs_root)):
            full = os.path.join(self.sysfs_root, entry)
            supply_type = _read_str(os.path.join(full, "type"))
            if supply_type == "Battery":
                return full
        return None

    def read(self) -> PowerTelemetry:
        bat_dir = self._find_battery_dir()
        thermal_temp = _read_thermal_temp_c(self.thermal_root)

        if bat_dir is None:
            if thermal_temp is not None:
                return PowerTelemetry(
                    source="thermal_only",
                    voltage_mv=0,
                    current_ma=0,
                    capacity_pct=0,
                    temp_c=thermal_temp,
                    status="NoGauge",
                )
            return PowerTelemetry(
                source="mock_qemu",
                voltage_mv=4120,
                current_ma=-145,
                capacity_pct=88,
                temp_c=31.4,
                status="Discharging",
            )

        voltage_uv = _read_int(os.path.join(bat_dir, "voltage_now")) or 0
        current_ua = _read_int(os.path.join(bat_dir, "current_now")) or 0
        capacity = _read_int(os.path.join(bat_dir, "capacity")) or 0
        temp_tenths = _read_int(os.path.join(bat_dir, "temp"))
        status = _read_str(os.path.join(bat_dir, "status")) or "Unknown"

        if temp_tenths is not None:
            temp_c = round(temp_tenths / 10.0, 1)
        elif thermal_temp is not None:
            temp_c = thermal_temp
        else:
            temp_c = 0.0

        return PowerTelemetry(
            source=os.path.basename(bat_dir),
            voltage_mv=voltage_uv // 1000,
            current_ma=current_ua // 1000,
            capacity_pct=capacity,
            temp_c=temp_c,
            status=status,
        )
