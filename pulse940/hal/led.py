"""Pure-Python Awinic AW2013 I2C RGB notification LED controller with mock fallback."""

from __future__ import annotations

import logging
import os
import sys
import threading

try:
    import fcntl
except ImportError:  # Windows dev/test environment
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("pulse940.hal.led")

I2C_SLAVE = 0x0703
AW2013_CHIP_ID = 0x33

REG_RSTR = 0x00
REG_GCR = 0x01
REG_LCTR = 0x30
REG_LCFG0 = 0x31
REG_LCFG1 = 0x32
REG_LCFG2 = 0x33
REG_PWM0 = 0x34
REG_PWM1 = 0x35
REG_PWM2 = 0x36

# LCFGx = 0x01 selects manual PWM mode + 5 mA max current tier (safe, zero heat).
AW2013_LCFG_5MA = 0x01
AW2013_IMAX_MA = 5

LED_PRESET_ORDER: tuple[str, ...] = (
    "white",
    "green",
    "amber",
    "cyan",
    "red",
    "blue",
    "purple",
    "off",
)

LED_PRESETS: dict[str, tuple[int, int, int]] = {
    "white": (255, 255, 255),
    "green": (0, 255, 0),
    "amber": (255, 160, 0),
    "cyan": (0, 255, 255),
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
    "purple": (200, 0, 255),
    "off": (0, 0, 0),
}


class RgbLedController:
    """Controls Redmi 4A bottom AW2013 RGB LED over /dev/i2c-0 with safe 5mA cap."""

    def __init__(
        self,
        i2c_bus: str = "/dev/i2c-0",
        i2c_addr: int = 0x45,
        default_color: str = "white",
        force_mock: bool = False,
    ) -> None:
        self.i2c_bus = i2c_bus
        self.i2c_addr = i2c_addr
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._color = default_color if default_color in LED_PRESETS else "white"
        self._pulse_count = 0
        self._is_lit = False

        if not force_mock and self._probe_aw2013():
            self.backend = "aw2013_i2c0"
            self._hw_off()
        else:
            self.backend = "mock_led"

    def _probe_aw2013(self) -> bool:
        if sys.platform == "win32" or fcntl is None or not os.path.exists(self.i2c_bus):
            return False
        fd: int | None = None
        try:
            fd = os.open(self.i2c_bus, os.O_RDWR)
            fcntl.ioctl(fd, I2C_SLAVE, self.i2c_addr)
            os.write(fd, bytes((REG_RSTR,)))
            chip_id = os.read(fd, 1)
            return len(chip_id) == 1 and chip_id[0] == AW2013_CHIP_ID
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _hw_write_rgb(self, rgb: tuple[int, int, int]) -> None:
        if self.backend != "aw2013_i2c0" or fcntl is None:
            return
        r, g, b = (max(0, min(255, int(v))) for v in rgb)
        mask = (1 if r else 0) | (2 if g else 0) | (4 if b else 0)
        fd: int | None = None
        try:
            fd = os.open(self.i2c_bus, os.O_RDWR)
            fcntl.ioctl(fd, I2C_SLAVE, self.i2c_addr)
            if mask == 0:
                os.write(fd, bytes((REG_LCTR, 0x00)))
                os.write(fd, bytes((REG_PWM0, 0x00)))
                os.write(fd, bytes((REG_PWM1, 0x00)))
                os.write(fd, bytes((REG_PWM2, 0x00)))
                os.write(fd, bytes((REG_GCR, 0x00)))
            else:
                os.write(fd, bytes((REG_GCR, 0x01)))
                os.write(fd, bytes((REG_LCFG0, AW2013_LCFG_5MA)))
                os.write(fd, bytes((REG_LCFG1, AW2013_LCFG_5MA)))
                os.write(fd, bytes((REG_LCFG2, AW2013_LCFG_5MA)))
                os.write(fd, bytes((REG_PWM0, r)))
                os.write(fd, bytes((REG_PWM1, g)))
                os.write(fd, bytes((REG_PWM2, b)))
                os.write(fd, bytes((REG_LCTR, mask)))
        except OSError as exc:
            log.warning("AW2013 I2C write failed: %s", exc)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _hw_off(self) -> None:
        self._hw_write_rgb((0, 0, 0))
        self._is_lit = False

    def _cancel_timer_unlocked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def get_state(self) -> dict:
        with self._lock:
            return {
                "backend": self.backend,
                "color": self._color,
                "rgb": list(LED_PRESETS[self._color]),
                "available_colors": list(LED_PRESET_ORDER),
                "imax_ma": AW2013_IMAX_MA,
                "pulse_count": self._pulse_count,
                "lit": self._is_lit,
            }

    def set_color(self, color: str) -> dict:
        key = color.strip().lower()
        if key not in LED_PRESETS:
            raise ValueError(
                f"Unknown LED color {color!r}; expected one of {', '.join(LED_PRESET_ORDER)}"
            )
        with self._lock:
            self._color = key
            if key == "off":
                self._cancel_timer_unlocked()
                self._hw_off()
        if key != "off":
            self.pulse(duration_ms=140, color=key)
        return self.get_state()

    def cycle_color(self) -> dict:
        with self._lock:
            idx = LED_PRESET_ORDER.index(self._color)
            nxt = LED_PRESET_ORDER[(idx + 1) % len(LED_PRESET_ORDER)]
        return self.set_color(nxt)

    def pulse(self, duration_ms: int = 60, color: str | None = None) -> None:
        with self._lock:
            key = (color or self._color).strip().lower()
            if key not in LED_PRESETS or key == "off":
                return
            rgb = LED_PRESETS[key]
            self._cancel_timer_unlocked()
            self._hw_write_rgb(rgb)
            self._is_lit = True
            self._pulse_count += 1
            dur_s = max(0.02, min(2.0, duration_ms / 1000.0))
            timer = threading.Timer(dur_s, self._timer_off)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _timer_off(self) -> None:
        with self._lock:
            self._timer = None
            self._hw_off()

    def off(self) -> None:
        with self._lock:
            self._cancel_timer_unlocked()
            self._hw_off()
