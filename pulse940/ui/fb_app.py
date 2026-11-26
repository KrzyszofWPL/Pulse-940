"""Bare-metal /dev/fb0 capacitive touch laboratory UI for Pulse 940."""

from __future__ import annotations

import argparse
import json
import mmap
import os
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # Windows dev/test environment
    fcntl = None  # type: ignore[assignment]

from pulse940.hal.touch import EvdevTouchReader, TouchEvent
from pulse940.ui.font8x8 import get_glyph_spans

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
KDSETMODE = 0x4B3A
KD_TEXT = 0
KD_GRAPHICS = 1

NEC_IR_TEST_ADDR = 0x04
NEC_IR_TEST_CMD = 0x08


@dataclass(frozen=True)
class Theme:
    name: str
    bg: tuple[int, int, int]
    panel: tuple[int, int, int]
    panel_alt: tuple[int, int, int]
    border: tuple[int, int, int]
    accent: tuple[int, int, int]
    accent_dim: tuple[int, int, int]
    text: tuple[int, int, int]
    text_dim: tuple[int, int, int]
    warn: tuple[int, int, int]
    danger: tuple[int, int, int]
    ok: tuple[int, int, int]


THEMES: list[Theme] = [
    Theme(
        name="AMBER LAB",
        bg=(10, 12, 14),
        panel=(20, 24, 28),
        panel_alt=(32, 26, 16),
        border=(84, 62, 24),
        accent=(255, 176, 32),
        accent_dim=(135, 88, 14),
        text=(245, 242, 232),
        text_dim=(156, 152, 142),
        warn=(255, 200, 64),
        danger=(235, 72, 68),
        ok=(82, 214, 120),
    ),
    Theme(
        name="PHOSPHOR",
        bg=(7, 13, 10),
        panel=(14, 26, 20),
        panel_alt=(18, 38, 28),
        border=(34, 96, 62),
        accent=(64, 238, 128),
        accent_dim=(24, 118, 62),
        text=(228, 248, 234),
        text_dim=(132, 168, 146),
        warn=(245, 198, 68),
        danger=(235, 78, 72),
        ok=(92, 248, 150),
    ),
    Theme(
        name="CYBER ICE",
        bg=(8, 13, 20),
        panel=(16, 26, 40),
        panel_alt=(22, 38, 58),
        border=(42, 88, 138),
        accent=(56, 204, 255),
        accent_dim=(26, 98, 138),
        text=(234, 244, 255),
        text_dim=(140, 166, 194),
        warn=(255, 196, 64),
        danger=(240, 74, 84),
        ok=(80, 224, 156),
    ),
]


def pack_pixel_bytes(
    rgb: tuple[int, int, int],
    r_off: int = 16,
    g_off: int = 8,
    b_off: int = 0,
    bpp: int = 32,
) -> bytes:
    """Pack an (r, g, b) tuple according to Linux fb_var_screeninfo offsets."""
    r, g, b = rgb
    val = ((r & 0xFF) << r_off) | ((g & 0xFF) << g_off) | ((b & 0xFF) << b_off)
    if bpp == 16:
        r5 = (r >> 3) & 0x1F
        g6 = (g >> 2) & 0x3F
        b5 = (b >> 3) & 0x1F
        val16 = (r5 << 11) | (g6 << 5) | b5
        return struct.pack("<H", val16)
    return struct.pack("<I", val)


class FramebufferCanvas:
    """Fast double-buffered 2D renderer over Linux /dev/fb0 or headless buffer."""

    def __init__(
        self,
        width: int = 720,
        height: int = 1280,
        stride: int = 2944,
        bpp: int = 32,
        r_off: int = 16,
        g_off: int = 8,
        b_off: int = 0,
    ) -> None:
        self.width = width
        self.height = height
        self.stride = stride
        self.bpp = bpp
        self.bytes_pp = max(2, bpp // 8)
        self.r_off = r_off
        self.g_off = g_off
        self.b_off = b_off
        self.buf = bytearray(stride * height)
        self._color_cache: dict[tuple[int, int, int], bytes] = {}
        self._fb_fd: int | None = None
        self._fb_map: mmap.mmap | None = None
        self._tty_fd: int | None = None

    @classmethod
    def open_fb0(cls, fb_path: str = "/dev/fb0") -> "FramebufferCanvas":
        fd = os.open(fb_path, os.O_RDWR)
        vinfo = bytearray(256)
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, vinfo)
        xres, yres, _, _, _, _, bpp, _ = struct.unpack_from("8I", vinfo, 0)
        r_off, _, _ = struct.unpack_from("3I", vinfo, 32)
        g_off, _, _ = struct.unpack_from("3I", vinfo, 44)
        b_off, _, _ = struct.unpack_from("3I", vinfo, 56)

        stride = xres * max(2, bpp // 8)
        stride_sysfs = "/sys/class/graphics/fb0/stride"
        if os.path.isfile(stride_sysfs):
            try:
                with open(stride_sysfs, "r", encoding="utf-8") as fh:
                    stride = int(fh.read().strip())
            except (OSError, ValueError):
                pass
        else:
            finfo = bytearray(256)
            try:
                fcntl.ioctl(fd, FBIOGET_FSCREENINFO, finfo)
                cand = struct.unpack_from("I", finfo, 48)[0]
                if cand >= xres * max(2, bpp // 8):
                    stride = cand
            except OSError:
                pass

        canvas = cls(
            width=xres,
            height=yres,
            stride=stride,
            bpp=bpp,
            r_off=r_off,
            g_off=g_off,
            b_off=b_off,
        )
        canvas._fb_fd = fd
        canvas._fb_map = mmap.mmap(
            fd,
            stride * yres,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
        )
        try:
            tfd = os.open("/dev/tty1", os.O_RDWR)
            fcntl.ioctl(tfd, KDSETMODE, KD_GRAPHICS)
            canvas._tty_fd = tfd
        except OSError:
            pass
        return canvas

    def _px(self, rgb: tuple[int, int, int]) -> bytes:
        cached = self._color_cache.get(rgb)
        if cached is None:
            cached = pack_pixel_bytes(
                rgb,
                r_off=self.r_off,
                g_off=self.g_off,
                b_off=self.b_off,
                bpp=self.bpp,
            )
            self._color_cache[rgb] = cached
        return cached

    def clear(self, rgb: tuple[int, int, int]) -> None:
        self.fill_rect(0, 0, self.width, self.height, rgb)

    def fill_rect(
        self, x: int, y: int, w: int, h: int, rgb: tuple[int, int, int]
    ) -> None:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        span_w = x1 - x0
        row_bytes = self._px(rgb) * span_w
        byte_x0 = x0 * self.bytes_pp
        byte_x1 = x1 * self.bytes_pp
        stride = self.stride
        buf = self.buf
        for py in range(y0, y1):
            off = py * stride + byte_x0
            buf[off : off + (byte_x1 - byte_x0)] = row_bytes

    def draw_rect_border(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        rgb: tuple[int, int, int],
        thickness: int = 2,
    ) -> None:
        t = max(1, thickness)
        self.fill_rect(x, y, w, t, rgb)
        self.fill_rect(x, y + h - t, w, t, rgb)
        self.fill_rect(x, y, t, h, rgb)
        self.fill_rect(x + w - t, y, t, h, rgb)

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        rgb: tuple[int, int, int],
        scale: int = 2,
        spacing: int = 1,
    ) -> None:
        if not text:
            return
        px = self._px(rgb)
        scale = max(1, scale)
        adv = (8 + spacing) * scale
        stride = self.stride
        bpp = self.bytes_pp
        buf = self.buf
        max_w = self.width
        max_h = self.height

        cur_x = x
        for ch in text:
            if cur_x >= max_w:
                break
            if cur_x + 8 * scale > 0 and ch != " ":
                for row_i, col_start, run_len in get_glyph_spans(ch):
                    rx0 = cur_x + col_start * scale
                    rx1 = rx0 + run_len * scale
                    if rx1 <= 0 or rx0 >= max_w:
                        continue
                    cx0 = max(0, rx0)
                    cx1 = min(max_w, rx1)
                    run_bytes = px * (cx1 - cx0)
                    ry0 = y + row_i * scale
                    ry1 = min(max_h, ry0 + scale)
                    for py in range(max(0, ry0), ry1):
                        off = py * stride + cx0 * bpp
                        buf[off : off + len(run_bytes)] = run_bytes
            cur_x += adv

    def draw_text_centered(
        self,
        cx: int,
        cy: int,
        text: str,
        rgb: tuple[int, int, int],
        scale: int = 2,
        spacing: int = 1,
    ) -> None:
        if not text:
            return
        adv = (8 + spacing) * scale
        tw = len(text) * adv - spacing * scale
        th = 8 * scale
        self.draw_text(cx - tw // 2, cy - th // 2, text, rgb, scale=scale, spacing=spacing)

    def flush(self) -> None:
        if self._fb_map is not None:
            self._fb_map.seek(0)
            self._fb_map.write(self.buf)

    def close(self) -> None:
        if self._tty_fd is not None:
            try:
                fcntl.ioctl(self._tty_fd, KDSETMODE, KD_TEXT)
            except OSError:
                pass
            try:
                os.close(self._tty_fd)
            except OSError:
                pass
            self._tty_fd = None
        if self._fb_map is not None:
            try:
                self._fb_map.close()
            except OSError:
                pass
            self._fb_map = None
        if self._fb_fd is not None:
            try:
                os.close(self._fb_fd)
            except OSError:
                pass
            self._fb_fd = None


@dataclass(frozen=True)
class RectButton:
    action: str
    arg: str
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < (self.x + self.w) and self.y <= py < (self.y + self.h)


class RpcClient:
    """Short-timeout synchronous JSON line client for pulse940.daemon."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9000) -> None:
        self.host = host
        self.port = port

    def call(self, payload: dict, timeout: float = 1.8) -> dict:
        with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8").strip())


def _read_sysfs_brightness() -> int | None:
    try:
        with open("/sys/class/backlight/backlight/brightness", "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


class ApplianceUI:
    """Layout, hit-testing, state, and framebuffer renderer for Pulse 940."""

    def __init__(self, width: int = 720, height: int = 1280) -> None:
        self.width = width
        self.height = height
        self.theme_idx = 0
        self.status_data: dict | None = None
        self.last_tx_data: dict | None = None
        self.signals: list[str] = []
        self.feedback: str = "READY // TOUCH OR VOL+/-"
        self.active_flash_key: str | None = None
        self.active_flash_until: float = 0.0
        self.poweroff_confirm_until: float = 0.0
        self.buttons: list[RectButton] = []
        self.slider_rect: tuple[int, int, int, int] = (146, 174, 428, 42)
        self._build_static_layout()

    @property
    def theme(self) -> Theme:
        return THEMES[self.theme_idx]

    def cycle_theme(self) -> Theme:
        self.theme_idx = (self.theme_idx + 1) % len(THEMES)
        return self.theme

    def _build_static_layout(self) -> None:
        self._fixed_buttons: list[RectButton] = [
            RectButton("bl_down", "", 36, 162, 92, 62),
            RectButton("bl_slider", "", 146, 158, 428, 70),
            RectButton("bl_up", "", 592, 162, 92, 62),
            # 3 macro / RF quick action buttons
            RectButton("macro_revert", "", 24, 948, 214, 96),
            RectButton("macro_double", "", 253, 948, 214, 96),
            RectButton("rf_scan", "", 482, 948, 214, 96),
            # 4 bottom dock control buttons
            RectButton("screen_off", "", 24, 1146, 162, 106),
            RectButton("theme", "", 194, 1146, 162, 106),
            RectButton("ir_test", "", 364, 1146, 162, 106),
            RectButton("power_off", "", 534, 1146, 162, 106),
        ]

    def _signal_buttons(self) -> list[RectButton]:
        btns: list[RectButton] = []
        cols = 2
        rows = 4
        start_x = 24
        start_y = 472
        gap = 14
        card_w = (self.width - 48 - gap) // cols  # 329
        card_h = 104
        items = self.signals[: cols * rows]
        for idx, sig in enumerate(items):
            r = idx // cols
            c = idx % cols
            bx = start_x + c * (card_w + gap)
            by = start_y + r * (card_h + gap)
            btns.append(RectButton("tx_signal", sig, bx, by, card_w, card_h))
        return btns

    def all_buttons(self) -> list[RectButton]:
        return self._signal_buttons() + self._fixed_buttons

    def hit_test(self, x: int, y: int) -> RectButton | None:
        for btn in self.all_buttons():
            if btn.contains(x, y):
                return btn
        return None

    def slider_value_for_x(self, x: int) -> int:
        sx, _, sw, _ = self.slider_rect
        rel = max(0.05, min(1.0, (x - sx) / max(1, sw)))
        return max(12, min(255, round(rel * 255)))

    def _draw_battery_icon(
        self,
        canvas: FramebufferCanvas,
        x: int,
        y: int,
        pct: int,
        t: Theme,
    ) -> None:
        fill_col = t.ok if pct >= 35 else (t.warn if pct >= 15 else t.danger)
        canvas.draw_rect_border(x, y, 52, 26, t.border, thickness=2)
        canvas.fill_rect(x + 52, y + 7, 5, 12, t.border)
        inner_w = max(0, min(44, round(44 * max(0, min(100, pct)) / 100)))
        if inner_w > 0:
            canvas.fill_rect(x + 4, y + 4, inner_w, 18, fill_col)

    def _draw_waveform_box(
        self,
        canvas: FramebufferCanvas,
        x: int,
        y: int,
        w: int,
        h: int,
        t: Theme,
    ) -> None:
        canvas.fill_rect(x, y, w, h, t.panel)
        canvas.draw_rect_border(x, y, w, h, t.border, thickness=2)

        tx = self.last_tx_data
        if tx:
            freq = tx.get("carrier_hz", 38000)
            duty = tx.get("duty_cycle", 33)
            cnt = tx.get("pulse_count", 0)
            dur = tx.get("total_duration_us", 0) / 1000.0
            header = f"PWM {freq // 1000}KHz {duty}% | {cnt}P {dur:.1f}ms"
        else:
            header = "PWM OSCILLOSCOPE // IDLE"
        canvas.draw_text(x + 16, y + 14, header, t.accent, scale=2)

        gx, gy, gw, gh = x + 16, y + 46, w - 32, h - 62
        canvas.fill_rect(gx, gy, gw, gh, t.bg)
        canvas.draw_rect_border(gx, gy, gw, gh, t.border, thickness=1)

        # Subtle reference center & 25%/75% grid lines
        for frac in (0.25, 0.5, 0.75):
            ly = gy + int(gh * frac)
            canvas.fill_rect(gx + 1, ly, gw - 2, 1, t.panel)

        pulses = (tx or {}).get("pulses_us") or []
        if not pulses:
            canvas.draw_text_centered(
                gx + gw // 2, gy + gh // 2, "NO RECENT TRANSMISSION", t.text_dim, scale=2
            )
            return

        hi_y = gy + 18
        lo_y = gy + gh - 22
        total = max(1, sum(pulses))
        cur_t = 0
        prev_level_y = lo_y
        prev_px = gx + 4
        plot_w = gw - 8

        for idx, dur in enumerate(pulses):
            level_y = hi_y if idx % 2 == 0 else lo_y
            # Vertical transition edge
            if idx > 0 and level_y != prev_level_y:
                top = min(prev_level_y, level_y)
                canvas.fill_rect(prev_px, top, 2, abs(level_y - prev_level_y) + 2, t.accent)

            cur_t += dur
            end_px = gx + 4 + min(plot_w, round((cur_t / total) * plot_w))
            seg_w = max(2, end_px - prev_px)
            canvas.fill_rect(prev_px, level_y, seg_w, 3, t.accent)
            if idx % 2 == 0:
                canvas.fill_rect(prev_px, level_y + 3, seg_w, lo_y - level_y - 3, t.accent_dim)
            prev_px = max(prev_px, end_px)
            prev_level_y = level_y

    def render(self, canvas: FramebufferCanvas, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        t = self.theme
        canvas.clear(t.bg)

        status = self.status_data or {}
        pwr = status.get("power", {})
        bl = status.get("backlight", {})
        backend = str(status.get("lirc_backend", "OFFLINE")).upper()
        pct = int(pwr.get("capacity_pct", 0))
        mv = int(pwr.get("voltage_mv", 0))
        temp = float(pwr.get("temp_c", 0.0))
        bl_pct = int(bl.get("pct", 50))
        bl_blanked = bool(bl.get("blanked", False))

        # 1. Top Header Bar (y=16..114)
        canvas.fill_rect(24, 16, self.width - 48, 98, t.panel)
        canvas.draw_rect_border(24, 16, self.width - 48, 98, t.border, thickness=2)
        canvas.draw_text(42, 32, "PULSE 940", t.accent, scale=3)

        lirc_ok = "NATIVE" in backend
        badge_col = t.ok if lirc_ok else t.warn
        badge_txt = "IR HARDWARE" if lirc_ok else backend[:11]
        canvas.draw_text(42, 74, badge_txt, badge_col, scale=2)

        # Battery Telemetry (right side of header)
        self._draw_battery_icon(canvas, 456, 32, pct, t)
        canvas.draw_text(526, 33, f"{pct:3d}%", t.text, scale=3)
        canvas.draw_text(456, 74, f"{mv}mV  {temp:.1f}C", t.text_dim, scale=2)

        # 2. Brightness Control Card (y=128..236)
        canvas.fill_rect(24, 128, self.width - 48, 108, t.panel)
        canvas.draw_rect_border(24, 128, self.width - 48, 108, t.border, thickness=2)
        canvas.draw_text(42, 138, f"DISPLAY BRIGHTNESS: {bl_pct}%", t.text_dim, scale=2)

        # Minus button (36, 162, 92, 62)
        canvas.fill_rect(36, 162, 92, 62, t.panel_alt)
        canvas.draw_rect_border(36, 162, 92, 62, t.accent_dim, thickness=2)
        canvas.draw_text_centered(82, 193, "-", t.accent, scale=4)

        # Slider bar
        sx, sy, sw, sh = self.slider_rect
        canvas.fill_rect(sx, sy, sw, sh, t.bg)
        canvas.draw_rect_border(sx, sy, sw, sh, t.border, thickness=2)
        fill_pct = 0 if bl_blanked else max(0, min(100, bl_pct))
        fill_w = max(0, round((sw - 8) * fill_pct / 100))
        if fill_w > 0:
            canvas.fill_rect(sx + 4, sy + 4, fill_w, sh - 8, t.accent)

        # Plus button (592, 162, 92, 62)
        canvas.fill_rect(592, 162, 92, 62, t.panel_alt)
        canvas.draw_rect_border(592, 162, 92, 62, t.accent_dim, thickness=2)
        canvas.draw_text_centered(638, 193, "+", t.accent, scale=4)

        # 3. Live PWM Oscilloscope (y=250..446)
        self._draw_waveform_box(canvas, 24, 250, self.width - 48, 196, t)

        # 4. IR Signals Touch Grid (y=462..934)
        sig_btns = self._signal_buttons()
        if not sig_btns:
            canvas.fill_rect(24, 472, self.width - 48, 456, t.panel)
            canvas.draw_rect_border(24, 472, self.width - 48, 456, t.border, thickness=2)
            canvas.draw_text_centered(
                self.width // 2, 700, "WAITING FOR IR PROFILE...", t.text_dim, scale=2
            )
        else:
            for btn in sig_btns:
                is_flashing = (
                    self.active_flash_key == f"sig:{btn.arg}"
                    and now < self.active_flash_until
                )
                bg_col = t.accent if is_flashing else t.panel
                fg_col = t.bg if is_flashing else t.text
                bd_col = t.accent if is_flashing else t.border
                canvas.fill_rect(btn.x, btn.y, btn.w, btn.h, bg_col)
                canvas.draw_rect_border(btn.x, btn.y, btn.w, btn.h, bd_col, thickness=2)
                label = btn.arg.upper().replace("_", " ")[:15]
                canvas.draw_text_centered(
                    btn.x + btn.w // 2, btn.y + btn.h // 2 - 8, label, fg_col, scale=2
                )
                sub_col = t.bg if is_flashing else t.accent_dim
                canvas.draw_text_centered(
                    btn.x + btn.w // 2, btn.y + btn.h - 22, "TAP TO TX", sub_col, scale=1
                )

        # 5. Quick Actions Row (y=948..1044)
        macro_specs = [
            ("macro_revert", "REVERT", "HDMI 1", 24),
            ("macro_double", "2X PWR", "SHUTDOWN", 253),
            ("rf_scan", "RF SCAN", "WIFI ROOM", 482),
        ]
        for action, title, sub, bx in macro_specs:
            is_flashing = (
                self.active_flash_key == action and now < self.active_flash_until
            )
            bg_col = t.accent if is_flashing else t.panel_alt
            fg_col = t.bg if is_flashing else t.accent
            canvas.fill_rect(bx, 948, 214, 96, bg_col)
            canvas.draw_rect_border(bx, 948, 214, 96, t.accent_dim, thickness=2)
            canvas.draw_text_centered(bx + 107, 984, title, fg_col, scale=2)
            canvas.draw_text_centered(
                bx + 107, 1018, sub, t.bg if is_flashing else t.text_dim, scale=1
            )

        # 6. Status Ticker Bar (y=1060..1128)
        canvas.fill_rect(24, 1060, self.width - 48, 68, t.panel)
        canvas.draw_rect_border(24, 1060, self.width - 48, 68, t.border, thickness=2)
        canvas.draw_text(40, 1086, self.feedback[:36], t.text, scale=2)

        # 7. Bottom Hardware Dock (y=1146..1252)
        pwr_confirm = now < self.poweroff_confirm_until
        dock_specs = [
            ("screen_off", "SCREEN", "OFF", 24, t.panel, t.border, t.text),
            ("theme", "THEME", t.name[:8], 194, t.panel, t.border, t.accent),
            ("ir_test", "IR LED", "TEST TX", 364, t.panel_alt, t.accent_dim, t.ok),
            (
                "power_off",
                "CONFIRM" if pwr_confirm else "POWER",
                "TAP NOW" if pwr_confirm else "OFF",
                534,
                t.danger if pwr_confirm else t.panel,
                t.danger,
                t.text if pwr_confirm else t.danger,
            ),
        ]
        for _, l1, l2, bx, bg_c, bd_c, fg_c in dock_specs:
            canvas.fill_rect(bx, 1146, 162, 106, bg_c)
            canvas.draw_rect_border(bx, 1146, 162, 106, bd_c, thickness=2)
            canvas.draw_text_centered(bx + 81, 1184, l1, fg_c, scale=2)
            canvas.draw_text_centered(bx + 81, 1218, l2, fg_c, scale=2)

    def handle_touch(self, ev: TouchEvent, rpc: RpcClient, now: float | None = None) -> bool:
        """Process a touch event and return True if display needs redraw."""
        if now is None:
            now = time.monotonic()
        if ev.kind not in ("down", "move"):
            return False

        btn = self.hit_test(ev.x, ev.y)
        if btn is None:
            return False

        # Only allow drag/move updates on brightness slider
        if ev.kind == "move" and btn.action != "bl_slider":
            return False

        try:
            if btn.action == "bl_down":
                res = rpc.call({"cmd": "backlight", "action": "down", "step_pct": 15})
                if res.get("ok"):
                    bl = res["backlight"]
                    if self.status_data is not None:
                        self.status_data["backlight"] = bl
                    self.feedback = f"BRIGHTNESS: {bl['pct']}%"
                return True

            if btn.action == "bl_up":
                res = rpc.call({"cmd": "backlight", "action": "up", "step_pct": 15})
                if res.get("ok"):
                    bl = res["backlight"]
                    if self.status_data is not None:
                        self.status_data["backlight"] = bl
                    self.feedback = f"BRIGHTNESS: {bl['pct']}%"
                return True

            if btn.action == "bl_slider":
                val = self.slider_value_for_x(ev.x)
                res = rpc.call({"cmd": "backlight", "action": "set", "value": val})
                if res.get("ok"):
                    bl = res["backlight"]
                    if self.status_data is not None:
                        self.status_data["backlight"] = bl
                    self.feedback = f"BRIGHTNESS: {bl['pct']}%"
                return True

            if btn.action == "tx_signal":
                self.active_flash_key = f"sig:{btn.arg}"
                self.active_flash_until = now + 0.28
                res = rpc.call({"cmd": "tx_signal", "name": btn.arg})
                self.last_tx_data = rpc.call({"cmd": "last_tx"}).get("last_tx")
                self.feedback = f"TX {btn.arg.upper()} -> {res.get('pulse_count', 0)}P"
                return True

            if btn.action == "macro_revert":
                self.active_flash_key = "macro_revert"
                self.active_flash_until = now + 0.28
                res = rpc.call({"cmd": "macro", "action": "revert"})
                self.last_tx_data = rpc.call({"cmd": "last_tx"}).get("last_tx")
                self.feedback = f"MACRO REVERT -> {res.get('pulse_count', 0)}P"
                return True

            if btn.action == "macro_double":
                self.active_flash_key = "macro_double"
                self.active_flash_until = now + 0.28
                res = rpc.call({"cmd": "macro", "action": "double_tap_power"})
                self.last_tx_data = rpc.call({"cmd": "last_tx"}).get("last_tx")
                self.feedback = f"2X POWER -> {res.get('taps', 2)} TAPS"
                return True

            if btn.action == "rf_scan":
                self.active_flash_key = "rf_scan"
                self.active_flash_until = now + 0.28
                res = rpc.call({"cmd": "rf_scan", "auto_load": True}, timeout=6.0)
                room = (res.get("matched_room") or {}).get("room_name") or "NONE"
                aps = res.get("ap_count", 0)
                sigs = rpc.call({"cmd": "list_signals"}).get("signals", [])
                if sigs:
                    self.signals = list(sigs)
                self.feedback = f"RF: {aps} AP | ROOM: {room[:14].upper()}"
                return True

            if btn.action == "screen_off":
                rpc.call({"cmd": "backlight", "action": "toggle"})
                self.feedback = "SCREEN BLANKED"
                return True

            if btn.action == "theme":
                th = self.cycle_theme()
                self.feedback = f"THEME: {th.name}"
                return True

            if btn.action == "ir_test":
                self.active_flash_key = "ir_test"
                self.active_flash_until = now + 0.35
                for _ in range(3):
                    rpc.call(
                        {
                            "cmd": "tx_nec",
                            "address": NEC_IR_TEST_ADDR,
                            "command": NEC_IR_TEST_CMD,
                        }
                    )
                    time.sleep(0.06)
                self.last_tx_data = rpc.call({"cmd": "last_tx"}).get("last_tx")
                self.feedback = "IR TEST: 3x 38KHZ NEC SENT"
                return True

            if btn.action == "power_off":
                if now < self.poweroff_confirm_until:
                    self.feedback = "POWERING OFF..."
                    rpc.call({"cmd": "poweroff"})
                else:
                    self.poweroff_confirm_until = now + 4.0
                    self.feedback = "TAP POWER OFF AGAIN TO SHUTDOWN"
                return True

        except Exception as exc:
            self.feedback = f"ERR: {str(exc)[:28].upper()}"
            return True

        return False


def run_fb_ui(host: str = "127.0.0.1", port: int = 9000) -> int:
    canvas = FramebufferCanvas.open_fb0()
    touch = EvdevTouchReader(screen_w=canvas.width, screen_h=canvas.height)
    ui = ApplianceUI(width=canvas.width, height=canvas.height)
    rpc = RpcClient(host=host, port=port)

    last_poll = 0.0
    last_bl_poll = 0.0
    last_sys_bl: int | None = _read_sysfs_brightness()
    dirty = True

    try:
        while True:
            now = time.monotonic()

            # Poll sysfs brightness frequently so Vol+/Vol- physical presses appear live
            if now - last_bl_poll >= 0.15:
                last_bl_poll = now
                cur_bl = _read_sysfs_brightness()
                if cur_bl is not None and cur_bl != last_sys_bl:
                    last_sys_bl = cur_bl
                    if ui.status_data is not None and "backlight" in ui.status_data:
                        mx = max(1, int(ui.status_data["backlight"].get("max_brightness", 255)))
                        ui.status_data["backlight"]["brightness"] = cur_bl
                        ui.status_data["backlight"]["pct"] = round(cur_bl * 100 / mx)
                        ui.status_data["backlight"]["blanked"] = cur_bl == 0
                    dirty = True

            # Refresh full telemetry from daemon every 1.2s when screen is awake
            is_blanked = last_sys_bl == 0
            if not is_blanked and (now - last_poll >= 1.2):
                last_poll = now
                try:
                    ui.status_data = rpc.call({"cmd": "status"})
                    sigs = rpc.call({"cmd": "list_signals"}).get("signals", [])
                    if sigs:
                        ui.signals = list(sigs)
                    ui.last_tx_data = rpc.call({"cmd": "last_tx"}).get("last_tx")
                    dirty = True
                except Exception as exc:
                    ui.feedback = f"DAEMON OFFLINE: {str(exc)[:20].upper()}"
                    dirty = True

            # Clear temporary visual flash or poweroff confirm timeout
            if ui.active_flash_key is not None and now >= ui.active_flash_until:
                ui.active_flash_key = None
                dirty = True
            if ui.poweroff_confirm_until > 0.0 and now >= ui.poweroff_confirm_until:
                ui.poweroff_confirm_until = 0.0
                dirty = True

            if dirty and not is_blanked:
                ui.render(canvas, now=now)
                canvas.flush()
                dirty = False

            wait_fds = [touch.fd] if touch.fd is not None else []
            if wait_fds:
                select.select(wait_fds, [], [], 0.12)
                events = touch.read_events()
                if not is_blanked:
                    for ev in events:
                        if ui.handle_touch(ev, rpc, now=time.monotonic()):
                            dirty = True
                            last_bl_poll = 0.0
            else:
                time.sleep(0.12)
    except KeyboardInterrupt:
        return 0
    finally:
        touch.close()
        canvas.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 940 Framebuffer Touch UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    return run_fb_ui(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
