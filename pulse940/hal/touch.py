"""Pure-Python Linux evdev capacitive touchscreen reader."""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # Windows dev/test environment
    fcntl = None  # type: ignore[assignment]

from pulse940.hal.buttons import INPUT_EVENT_STRUCT, parse_input_events

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0x00

ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

BTN_TOUCH = 0x14A

# _IOR('E', 0x40 + code, struct input_absinfo[24 bytes])
_EVIOCGABS_BASE = 0x80184540
_ABSINFO_STRUCT = struct.Struct("6i")


@dataclass(frozen=True)
class TouchEvent:
    """Single-finger normalized touchscreen event in screen pixels."""

    kind: str  # "down", "move", "up"
    x: int
    y: int


def find_touch_event_device(sysfs_input: str = "/sys/class/input") -> str | None:
    """Return /dev/input/event* path for the capacitive touchscreen, if present."""
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
        if any(tok in name for tok in ("touch", "goodix", "ft5", "synaptics", "ts")):
            dev_path = os.path.join("/dev/input", entry)
            if os.path.exists(dev_path):
                return dev_path
    return None


def _read_abs_bounds(fd: int, code: int) -> tuple[int, int] | None:
    buf = bytearray(_ABSINFO_STRUCT.size)
    try:
        fcntl.ioctl(fd, _EVIOCGABS_BASE + code, buf)
        _, mn, mx, _, _, _ = _ABSINFO_STRUCT.unpack(buf)
        if mx > mn:
            return mn, mx
    except OSError:
        return None
    return None


class TouchStateMachine:
    """Normalizes Linux Type-B multitouch & Type-A single-touch evdev streams."""

    def __init__(
        self,
        screen_w: int = 720,
        screen_h: int = 1280,
        x_bounds: tuple[int, int] = (0, 719),
        y_bounds: tuple[int, int] = (0, 1279),
    ) -> None:
        self.screen_w = max(1, screen_w)
        self.screen_h = max(1, screen_h)
        self.x_min, self.x_max = x_bounds
        self.y_min, self.y_max = y_bounds
        self._slot = 0
        self._active = False
        self._was_active = False
        self._raw_x = x_bounds[0]
        self._raw_y = y_bounds[0]
        self._dirty = False

    def _scale_x(self, raw_x: int) -> int:
        span = max(1, self.x_max - self.x_min)
        px = round((raw_x - self.x_min) * (self.screen_w - 1) / span)
        return max(0, min(self.screen_w - 1, px))

    def _scale_y(self, raw_y: int) -> int:
        span = max(1, self.y_max - self.y_min)
        py = round((raw_y - self.y_min) * (self.screen_h - 1) / span)
        return max(0, min(self.screen_h - 1, py))

    def feed(self, events: list[tuple[int, int, int]]) -> list[TouchEvent]:
        out: list[TouchEvent] = []
        for ev_type, code, value in events:
            if ev_type == EV_ABS:
                if code == ABS_MT_SLOT:
                    self._slot = value
                elif code == ABS_MT_TRACKING_ID and self._slot == 0:
                    if value >= 0:
                        self._active = True
                    else:
                        self._active = False
                    self._dirty = True
                elif code == ABS_MT_POSITION_X and self._slot == 0:
                    self._raw_x = value
                    self._dirty = True
                elif code == ABS_MT_POSITION_Y and self._slot == 0:
                    self._raw_y = value
                    self._dirty = True
                elif code == ABS_X:
                    self._raw_x = value
                    self._dirty = True
                elif code == ABS_Y:
                    self._raw_y = value
                    self._dirty = True
            elif ev_type == EV_KEY and code == BTN_TOUCH:
                self._active = value != 0
                self._dirty = True
            elif ev_type == EV_SYN and code == SYN_REPORT and self._dirty:
                sx = self._scale_x(self._raw_x)
                sy = self._scale_y(self._raw_y)
                if self._active and not self._was_active:
                    out.append(TouchEvent("down", sx, sy))
                elif self._active and self._was_active:
                    out.append(TouchEvent("move", sx, sy))
                elif not self._active and self._was_active:
                    out.append(TouchEvent("up", sx, sy))
                self._was_active = self._active
                self._dirty = False
        return out


class EvdevTouchReader:
    """Non-blocking touchscreen event reader backed by /dev/input/event*."""

    def __init__(self, screen_w: int = 720, screen_h: int = 1280) -> None:
        self.fd: int | None = None
        self.device_path = find_touch_event_device()
        x_bounds = (0, max(1, screen_w - 1))
        y_bounds = (0, max(1, screen_h - 1))

        if self.device_path is not None and sys.platform != "win32":
            try:
                self.fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
                xb = _read_abs_bounds(self.fd, ABS_MT_POSITION_X) or _read_abs_bounds(
                    self.fd, ABS_X
                )
                yb = _read_abs_bounds(self.fd, ABS_MT_POSITION_Y) or _read_abs_bounds(
                    self.fd, ABS_Y
                )
                if xb is not None:
                    x_bounds = xb
                if yb is not None:
                    y_bounds = yb
            except OSError:
                self.close()

        self._sm = TouchStateMachine(
            screen_w=screen_w,
            screen_h=screen_h,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
        )

    def read_events(self) -> list[TouchEvent]:
        if self.fd is None:
            return []
        ev_size = INPUT_EVENT_STRUCT.size
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(self.fd, ev_size * 32)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        if not chunks:
            return []
        return self._sm.feed(parse_input_events(b"".join(chunks)))

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
