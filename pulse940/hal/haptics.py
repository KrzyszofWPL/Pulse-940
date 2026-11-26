"""Tactile force-feedback haptic vibrator HAL (/dev/input/event* FF_RUMBLE)."""

from __future__ import annotations

import os
import struct
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # Windows dev/test environment
    fcntl = None  # type: ignore[assignment]

from pulse940.hal.buttons import INPUT_EVENT_STRUCT

EV_FF = 0x15
FF_RUMBLE = 0x50
EVIOCSFF = 0x40304580  # _IOW('E', 0x80, struct ff_effect[48B on LP64])


def find_vibrator_device(sysfs_input: str = "/sys/class/input") -> str | None:
    """Locate /dev/input/event* for gpio-vibrator / vibrator if present."""
    if sys.platform == "win32" or not os.path.isdir(sysfs_input):
        return None
    for entry in sorted(os.listdir(sysfs_input)):
        if not entry.startswith("event"):
            continue
        name_path = os.path.join(sysfs_input, entry, "device", "name")
        try:
            with open(name_path, "r", encoding="utf-8") as fh:
                name = fh.read().strip().lower()
        except OSError:
            continue
        if "vibrator" in name:
            dev_path = os.path.join("/dev/input", entry)
            if os.path.exists(dev_path):
                return dev_path
    return None


class HapticVibrator:
    """Rate-limited tactile feedback driver with silent mock fallback."""

    def __init__(self, force_mock: bool = False) -> None:
        self.enabled = True
        self._lock = threading.Lock()
        self._last_ts = 0.0
        self.buzz_count = 0
        self._dev_path = None if force_mock else find_vibrator_device()
        self.backend = "evdev_ff" if self._dev_path else "mock_haptic"
        self._fd: int | None = None
        self._effect_id: int | None = None

        if self._dev_path and fcntl is not None:
            try:
                self._fd = os.open(self._dev_path, os.O_RDWR)
            except OSError:
                self.backend = "mock_haptic"
                self._fd = None

    def buzz(self, duration_ms: int = 35, strength: int = 0xA000) -> bool:
        """Emit a short tactile click, respecting rate limiting and enabled toggle."""
        if not self.enabled:
            return False
        now = time.monotonic()
        with self._lock:
            if (now - self._last_ts) < 0.18:
                return False
            self._last_ts = now
            self.buzz_count += 1
            if self._fd is None or fcntl is None:
                return True
            dur = max(15, min(150, int(duration_ms)))
            amp = max(0x2000, min(0xFFFF, int(strength)))
            try:
                # struct ff_effect on 64-bit Linux: 48 bytes
                eff_id = self._effect_id if self._effect_id is not None else -1
                buf = bytearray(
                    struct.pack(
                        "=HhHHHHHHH28x",
                        FF_RUMBLE,
                        eff_id,
                        0,
                        0,
                        0,
                        dur,
                        0,
                        amp,
                        amp,
                    )
                )
                fcntl.ioctl(self._fd, EVIOCSFF, buf)
                self._effect_id = struct.unpack_from("=h", buf, 2)[0]
                ev = INPUT_EVENT_STRUCT.pack(0, 0, EV_FF, self._effect_id, 1)
                os.write(self._fd, ev)
                return True
            except OSError:
                return False

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
