"""Power supply & thermal telemetry via /sys/class/power_supply/ with mock fallback."""

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


class PowerMonitor:
    """Reads real Linux sysfs power telemetry or returns simulated QEMU values."""

    def __init__(self, sysfs_root: str = "/sys/class/power_supply") -> None:
        self.sysfs_root = sysfs_root

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
        if bat_dir is None:
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

        return PowerTelemetry(
            source=os.path.basename(bat_dir),
            voltage_mv=voltage_uv // 1000,
            current_ma=current_ua // 1000,
            capacity_pct=capacity,
            temp_c=(temp_tenths / 10.0) if temp_tenths is not None else 0.0,
            status=status,
        )
