"""Pure-Python Linux evdev listener for physical phone buttons."""

from __future__ import annotations

import logging
import os
import select
import struct
import sys
import threading
import time
from typing import Callable

log = logging.getLogger("pulse940.hal.buttons")

EV_KEY = 1
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
KEY_POWER = 116

# Native Linux struct input_event: timeval (ll) + type (H) + code (H) + value (i)
# 24 bytes on 64-bit Linux (aarch64/x86_64), 16 bytes on 32-bit Linux.
INPUT_EVENT_STRUCT = struct.Struct("llHHi")


def parse_input_events(
    chunk: bytes, ev_struct: struct.Struct = INPUT_EVENT_STRUCT
) -> list[tuple[int, int, int]]:
    """Unpack raw Linux input_event bytes into (type, code, value) tuples."""
    ev_size = ev_struct.size
    limit = len(chunk) - (len(chunk) % ev_size)
    events: list[tuple[int, int, int]] = []
    for offset in range(0, limit, ev_size):
        _, _, ev_type, code, value = ev_struct.unpack_from(chunk, offset)
        events.append((ev_type, code, value))
    return events


class PowerKeyStateMachine:
    """Tracks Volume Up/Down presses and short vs long Power key presses."""

    def __init__(self, long_press_s: float = 2.5) -> None:
        self.long_press_s = long_press_s
        self._power_press_ts: float | None = None
        self._long_fired: bool = False

    def on_event(self, code: int, value: int, now: float) -> str | None:
        if code == KEY_VOLUMEUP and value in (1, 2):
            return "vol_up"
        if code == KEY_VOLUMEDOWN and value in (1, 2):
            return "vol_down"
        if code == KEY_POWER:
            if value == 1:
                self._power_press_ts = now
                self._long_fired = False
            elif value == 0:
                was_pressed = self._power_press_ts is not None
                fired = self._long_fired
                self._power_press_ts = None
                self._long_fired = False
                if was_pressed and not fired:
                    return "power_short"
        return None

    def poll_timeout(self, now: float) -> str | None:
        if (
            self._power_press_ts is not None
            and not self._long_fired
            and (now - self._power_press_ts) >= self.long_press_s
        ):
            self._long_fired = True
            return "power_long"
        return None


def find_button_event_devices(sysfs_input: str = "/sys/class/input") -> list[str]:
    """Return /dev/input/event* paths for physical hardware key devices."""
    if sys.platform == "win32" or not os.path.isdir(sysfs_input):
        return []
    devices: list[str] = []
    for entry in sorted(os.listdir(sysfs_input)):
        if not entry.startswith("event"):
            continue
        name_path = os.path.join(sysfs_input, entry, "device", "name")
        try:
            with open(name_path, "r", encoding="utf-8") as fh:
                name = fh.read().strip().lower()
        except OSError:
            continue
        if any(tok in name for tok in ("pwrkey", "resin", "gpio-keys", "key")):
            dev_path = os.path.join("/dev/input", entry)
            if os.path.exists(dev_path):
                devices.append(dev_path)
    return devices


class EvdevButtonMonitor:
    """Monitors Linux /dev/input/event* hardware buttons in a daemon thread."""

    def __init__(
        self,
        on_vol_up: Callable[[], None] | None = None,
        on_vol_down: Callable[[], None] | None = None,
        on_power_short: Callable[[], None] | None = None,
        on_power_long: Callable[[], None] | None = None,
        long_press_s: float = 2.5,
    ) -> None:
        self._callbacks: dict[str, Callable[[], None] | None] = {
            "vol_up": on_vol_up,
            "vol_down": on_vol_down,
            "power_short": on_power_short,
            "power_long": on_power_long,
        }
        self._sm = PowerKeyStateMachine(long_press_s=long_press_s)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fds: list[int] = []

    def _dispatch(self, action: str | None) -> None:
        if action is None:
            return
        cb = self._callbacks.get(action)
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("Button callback %s failed", action)

    def start(self) -> None:
        if sys.platform == "win32":
            return
        paths = find_button_event_devices()
        if not paths:
            log.info("No hardware button evdev devices found; button monitor idle")
            return
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self._fds.append(fd)
            except OSError as exc:
                log.warning("Could not open %s: %s", path, exc)
        if not self._fds:
            return

        log.info(
            "Listening for physical buttons on %s (struct=%dB)",
            ", ".join(paths),
            INPUT_EVENT_STRUCT.size,
        )
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        ev_size = INPUT_EVENT_STRUCT.size
        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select(self._fds, [], [], 0.15)
            except (OSError, ValueError):
                break
            for fd in readable:
                while True:
                    try:
                        chunk = os.read(fd, ev_size * 8)
                    except BlockingIOError:
                        break
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    for ev_type, code, value in parse_input_events(chunk):
                        if ev_type == EV_KEY:
                            action = self._sm.on_event(code, value, time.monotonic())
                            if action is not None:
                                log.info(
                                    "Button key event: code=%d value=%d -> %s",
                                    code,
                                    value,
                                    action,
                                )
                                self._dispatch(action)
            timeout_action = self._sm.poll_timeout(time.monotonic())
            if timeout_action is not None:
                log.info("Button timeout event -> %s", timeout_action)
                self._dispatch(timeout_action)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
