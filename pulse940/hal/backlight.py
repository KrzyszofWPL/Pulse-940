"""Sysfs backlight control with QEMU/Windows mock fallback."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class BacklightState:
    """Snapshot of display backlight state."""

    backend: str
    brightness: int
    max_brightness: int
    pct: int
    blanked: bool


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _write_int(path: str, value: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{value}\n")


class BacklightController:
    """Controls Linux /sys/class/backlight/* or falls back to in-memory mock."""

    def __init__(self, sysfs_root: str = "/sys/class/backlight") -> None:
        self.sysfs_root = sysfs_root
        self._lock = threading.Lock()
        self._bl_dir = self._find_backlight_dir()
        if self._bl_dir is not None:
            self._backend = os.path.basename(self._bl_dir)
            self._max = _read_int(os.path.join(self._bl_dir, "max_brightness")) or 255
            cur = _read_int(os.path.join(self._bl_dir, "brightness"))
            self._mock_brightness = cur if cur is not None else self._max // 2
        else:
            self._backend = "mock_backlight"
            self._max = 255
            self._mock_brightness = 128
        self._saved_brightness = (
            self._mock_brightness if self._mock_brightness > 0 else max(16, self._max // 2)
        )

    def _find_backlight_dir(self) -> str | None:
        if not os.path.isdir(self.sysfs_root):
            return None
        for entry in sorted(os.listdir(self.sysfs_root)):
            full = os.path.join(self.sysfs_root, entry)
            if os.path.isfile(os.path.join(full, "brightness")) and os.path.isfile(
                os.path.join(full, "max_brightness")
            ):
                return full
        return None

    def _current_brightness_unlocked(self) -> int:
        if self._bl_dir is not None:
            val = _read_int(os.path.join(self._bl_dir, "brightness"))
            if val is not None:
                return max(0, min(self._max, val))
        return self._mock_brightness

    def _build_state_unlocked(self, val: int) -> BacklightState:
        pct = round((val / self._max) * 100) if self._max > 0 else 0
        return BacklightState(
            backend=self._backend,
            brightness=val,
            max_brightness=self._max,
            pct=pct,
            blanked=(val == 0),
        )

    def get_state(self) -> BacklightState:
        with self._lock:
            return self._build_state_unlocked(self._current_brightness_unlocked())

    def set_brightness(self, value: int) -> BacklightState:
        with self._lock:
            clamped = max(0, min(self._max, int(value)))
            if clamped > 0:
                self._saved_brightness = clamped

            if self._bl_dir is not None:
                bl_power_path = os.path.join(self._bl_dir, "bl_power")
                has_bl_power = os.path.isfile(bl_power_path)
                if clamped == 0:
                    _write_int(os.path.join(self._bl_dir, "brightness"), 0)
                    if has_bl_power:
                        _write_int(bl_power_path, 4)
                else:
                    if has_bl_power:
                        _write_int(bl_power_path, 0)
                    _write_int(os.path.join(self._bl_dir, "brightness"), clamped)
            else:
                self._mock_brightness = clamped

            return self._build_state_unlocked(clamped)

    def step_up(self, step_pct: int = 10) -> BacklightState:
        with self._lock:
            cur = self._current_brightness_unlocked()
            step = max(1, round(self._max * step_pct / 100))
            nxt = step if cur == 0 else min(self._max, cur + step)
        return self.set_brightness(nxt)

    def step_down(self, step_pct: int = 10, min_brightness: int = 0) -> BacklightState:
        with self._lock:
            cur = self._current_brightness_unlocked()
            floor = max(0, min(self._max, int(min_brightness)))
            if cur <= floor:
                nxt = cur if cur == 0 else floor
            else:
                step = max(1, round(self._max * step_pct / 100))
                nxt = max(floor, cur - step)
        return self.set_brightness(nxt)

    def toggle_blank(self) -> BacklightState:
        with self._lock:
            cur = self._current_brightness_unlocked()
            if cur == 0:
                target = max(16, self._max // 8, self._saved_brightness)
                target = min(self._max, target)
            else:
                self._saved_brightness = cur
                target = 0
        return self.set_brightness(target)
