"""Bare-metal /dev/fb0 capacitive touch 3-tab laboratory UI for Pulse 940."""

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

UNIVERSAL_CATEGORIES: list[tuple[str, str]] = [
    ("projector", "PROJ"),
    ("tv", "TV"),
    ("audio", "AUDIO"),
    ("ac", "AC"),
]

CATEGORY_ACTION_BUTTONS: dict[str, list[tuple[str, str]]] = {
    "projector": [("Power", "POWER"), ("Mute", "MUTE"), ("Vol_up", "VOL+"), ("Vol_dn", "VOL-")],
    "tv": [("Power", "POWER"), ("Mute", "MUTE"), ("Vol_up", "VOL+"), ("Vol_dn", "VOL-")],
    "audio": [("Power", "POWER"), ("Mute", "MUTE"), ("Vol_up", "VOL+"), ("Vol_dn", "VOL-")],
    "ac": [("Off", "OFF"), ("Cool_hi", "COOL"), ("Heat_hi", "HEAT"), ("Dh", "DRY")],
}


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

    def call(self, payload: dict, timeout: float = 2.0) -> dict:
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
    """3-Tab Flipper-class layout, hit-testing, state, and framebuffer renderer."""

    def __init__(self, width: int = 720, height: int = 1280) -> None:
        self.width = width
        self.height = height
        self.theme_idx = 0
        self.tab = "universal"  # "universal" | "remote" | "radar"
        self.category = "projector"
        self.action = "Power"

        self.status_data: dict | None = None
        self.last_tx_data: dict | None = None
        self.signals: list[str] = []
        self.rf_data: dict | None = None
        self.priority_brands: list[str] = []
        self.feedback: str = "READY // SELECT UNIVERSAL OR RADAR"
        self.active_flash_key: str | None = None
        self.active_flash_until: float = 0.0
        self.poweroff_confirm_until: float = 0.0
        self.slider_rect: tuple[int, int, int, int] = (140, 144, 440, 48)

    @property
    def theme(self) -> Theme:
        return THEMES[self.theme_idx]

    def cycle_theme(self) -> Theme:
        self.theme_idx = (self.theme_idx + 1) % len(THEMES)
        return self.theme

    def _is_sweeping(self) -> bool:
        sw = (self.status_data or {}).get("sweep") or {}
        return bool(sw.get("active", False))

    def _header_and_dock_buttons(self) -> list[RectButton]:
        return [
            RectButton("bl_down", "", 36, 140, 88, 56),
            RectButton("bl_slider", "", 140, 138, 440, 62),
            RectButton("bl_up", "", 596, 140, 88, 56),
            # 3 top tabs
            RectButton("tab", "universal", 24, 222, 214, 58),
            RectButton("tab", "remote", 253, 222, 214, 58),
            RectButton("tab", "radar", 482, 222, 214, 58),
            # 4 bottom dock buttons
            RectButton("screen_off", "", 24, 1146, 162, 106),
            RectButton("theme", "", 194, 1146, 162, 106),
            RectButton(
                "sweep_stop_or_test",
                "",
                364,
                1146,
                162,
                106,
            ),
            RectButton("power_off", "", 534, 1146, 162, 106),
        ]

    def _tab_buttons(self) -> list[RectButton]:
        btns: list[RectButton] = []
        if self.tab == "universal":
            for idx, (cat_key, _) in enumerate(UNIVERSAL_CATEGORIES):
                btns.append(RectButton("cat", cat_key, 24 + idx * 171, 294, 158, 82))
            acts = CATEGORY_ACTION_BUTTONS.get(
                self.category, CATEGORY_ACTION_BUTTONS["projector"]
            )
            for idx, (act_key, _) in enumerate(acts):
                btns.append(RectButton("u_act", act_key, 24 + idx * 171, 390, 158, 80))

            btns.extend(
                [
                    RectButton("sweep", "quick", 24, 486, 672, 72),
                    RectButton("sweep", "full", 24, 570, 672, 72),
                    RectButton("sweep", "deep", 24, 654, 672, 72),
                ]
            )
            if self._is_sweeping():
                btns.append(RectButton("sweep_lock", "", 24, 742, 672, 108))
            return btns

        if self.tab == "remote":
            cols = 2
            rows = 4
            gap = 14
            card_w = (self.width - 48 - gap) // cols
            card_h = 102
            for idx, sig in enumerate(self.signals[: cols * rows]):
                r = idx // cols
                c = idx % cols
                btns.append(
                    RectButton(
                        "tx_signal",
                        sig,
                        24 + c * (card_w + gap),
                        486 + r * (card_h + gap),
                        card_w,
                        card_h,
                    )
                )
            btns.extend(
                [
                    RectButton("macro_revert", "", 24, 954, 214, 92),
                    RectButton("macro_double", "", 253, 954, 214, 92),
                    RectButton("ir_test", "", 482, 954, 214, 92),
                ]
            )
            return btns

        # radar tab
        btns.append(RectButton("rf_scan", "", 24, 294, 672, 86))
        brands = ((self.rf_data or {}).get("brands") or [])[:4]
        for idx, b in enumerate(brands):
            col = idx % 2
            row = idx // 2
            btns.append(
                RectButton("pick_brand", str(b), 40 + col * 324, 466 + row * 68, 308, 56)
            )
        btns.append(RectButton("save_room", "", 470, 728, 210, 72))
        btns.append(RectButton("privacy_rotate", "", 470, 954, 210, 74))
        return btns

    def all_buttons(self) -> list[RectButton]:
        return self._tab_buttons() + self._header_and_dock_buttons()

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
        self, canvas: FramebufferCanvas, x: int, y: int, pct: int, t: Theme
    ) -> None:
        fill_col = t.ok if pct >= 35 else (t.warn if pct >= 15 else t.danger)
        canvas.draw_rect_border(x, y, 48, 24, t.border, thickness=2)
        canvas.fill_rect(x + 48, y + 6, 4, 12, t.border)
        inner_w = max(0, min(40, round(40 * max(0, min(100, pct)) / 100)))
        if inner_w > 0:
            canvas.fill_rect(x + 4, y + 4, inner_w, 16, fill_col)

    def _draw_waveform_box(
        self, canvas: FramebufferCanvas, x: int, y: int, w: int, h: int, t: Theme
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
        canvas.draw_text(x + 16, y + 12, header, t.accent, scale=2)

        gx, gy, gw, gh = x + 16, y + 42, w - 32, h - 56
        canvas.fill_rect(gx, gy, gw, gh, t.bg)
        canvas.draw_rect_border(gx, gy, gw, gh, t.border, thickness=1)
        for frac in (0.25, 0.5, 0.75):
            canvas.fill_rect(gx + 1, gy + int(gh * frac), gw - 2, 1, t.panel)

        pulses = (tx or {}).get("pulses_us") or []
        if not pulses:
            canvas.draw_text_centered(
                gx + gw // 2, gy + gh // 2, "NO RECENT TX", t.text_dim, scale=2
            )
            return

        hi_y = gy + 14
        lo_y = gy + gh - 18
        total = max(1, sum(pulses))
        cur_t = 0
        prev_y = lo_y
        prev_px = gx + 4
        plot_w = gw - 8
        for idx, dur in enumerate(pulses):
            level_y = hi_y if idx % 2 == 0 else lo_y
            if idx > 0 and level_y != prev_y:
                top = min(prev_y, level_y)
                canvas.fill_rect(prev_px, top, 2, abs(level_y - prev_y) + 2, t.accent)
            cur_t += dur
            end_px = gx + 4 + min(plot_w, round((cur_t / total) * plot_w))
            seg_w = max(2, end_px - prev_px)
            canvas.fill_rect(prev_px, level_y, seg_w, 3, t.accent)
            if idx % 2 == 0:
                canvas.fill_rect(prev_px, level_y + 3, seg_w, lo_y - level_y - 3, t.accent_dim)
            prev_px = max(prev_px, end_px)
            prev_y = level_y

    def _render_universal_tab(self, canvas: FramebufferCanvas, t: Theme, now: float) -> None:
        # 1. Category strip
        for idx, (cat_key, label) in enumerate(UNIVERSAL_CATEGORIES):
            bx = 24 + idx * 171
            selected = self.category == cat_key
            canvas.fill_rect(bx, 294, 158, 82, t.accent if selected else t.panel)
            canvas.draw_rect_border(
                bx, 294, 158, 82, t.accent if selected else t.border, thickness=2
            )
            canvas.draw_text_centered(
                bx + 79, 335, label, t.bg if selected else t.text, scale=2
            )

        # 2. Action strip
        acts = CATEGORY_ACTION_BUTTONS.get(
            self.category, CATEGORY_ACTION_BUTTONS["projector"]
        )
        for idx, (act_key, label) in enumerate(acts):
            bx = 24 + idx * 171
            selected = self.action.lower() == act_key.lower()
            canvas.fill_rect(bx, 390, 158, 80, t.panel_alt if selected else t.panel)
            canvas.draw_rect_border(
                bx, 390, 158, 80, t.accent if selected else t.border, thickness=2
            )
            canvas.draw_text_centered(
                bx + 79, 430, label, t.accent if selected else t.text, scale=2
            )

        # 3. 3-Mode Universal Sweep buttons (3-4s / 10-16s / 40-55s)
        cats_meta = ((self.status_data or {}).get("universal_categories") or {}).get(
            self.category, {}
        )
        act_cnt = ((cats_meta.get("actions") or {}).get(self.action)) or 0
        quick_cnt = min(22, act_cnt) if act_cnt else 22
        full_est = f"~{max(4, round(act_cnt * 0.16))}S ({act_cnt} CODES)" if act_cnt else "ALL BRANDS"

        sw_meta = (self.status_data or {}).get("sweep") or {}
        active_mode = sw_meta.get("mode", "") if sw_meta.get("active") else ""
        modes = [
            ("quick", "QUICK SWEEP", f"3-4S  | TOP {quick_cnt} BRANDS", 486),
            ("full", "FULL SWEEP", f"10-16S+ | {full_est}", 570),
            ("deep", "DEEP NEC 256", "40-55S | EXHAUSTIVE 0x00-0xFF", 654),
        ]
        for mkey, title, sub, by in modes:
            is_running = active_mode == mkey
            bg = t.accent if is_running else t.panel_alt
            bd = t.accent if is_running else t.border
            fg = t.bg if is_running else t.accent
            fg_sub = t.bg if is_running else t.text_dim
            canvas.fill_rect(24, by, 672, 72, bg)
            canvas.draw_rect_border(24, by, 672, 72, bd, thickness=2)
            canvas.draw_text(44, by + 16, title, fg, scale=2)
            canvas.draw_text(44, by + 44, sub, fg_sub, scale=2)

        # 4. Live Sweep Monitor OR Oscilloscope
        if sw_meta.get("active"):
            canvas.fill_rect(24, 742, 672, 108, t.danger)
            canvas.draw_rect_border(24, 742, 672, 108, t.text, thickness=3)
            canvas.draw_text_centered(360, 782, "STOP & LOCK SIGNAL", t.text, scale=3)
            canvas.draw_text_centered(
                360, 824, "TAP IMMEDIATELY WHEN DEVICE REACTS", t.text, scale=1
            )

            idx_n = int(sw_meta.get("index", 0))
            tot_n = max(1, int(sw_meta.get("total", 1)))
            proto = str(sw_meta.get("current_protocol", "")).upper()
            lbl = str(sw_meta.get("current_label", ""))[:18].upper()
            canvas.fill_rect(24, 862, 672, 54, t.panel)
            canvas.draw_rect_border(24, 862, 672, 54, t.accent, thickness=2)
            fill_w = max(4, round(664 * min(idx_n, tot_n) / tot_n))
            canvas.fill_rect(28, 866, fill_w, 46, t.accent_dim)
            canvas.draw_text(
                40, 881, f"{idx_n}/{tot_n}  {proto}  {lbl}", t.text, scale=2
            )
        else:
            self._draw_waveform_box(canvas, 24, 742, 672, 174, t)

        # 5. Radar Priority & Locked Signal Summary Card
        canvas.fill_rect(24, 930, 672, 116, t.panel)
        canvas.draw_rect_border(24, 930, 672, 116, t.border, thickness=2)
        prio = ", ".join(self.priority_brands[:4]).upper() or "NONE (TAP RADAR)"
        locked = sw_meta.get("locked_signal")
        if locked:
            lock_txt = f"LOCKED: {locked.get('protocol','')} {locked.get('label','')}"[:34].upper()
            lock_col = t.ok
        elif not sw_meta.get("active") and sw_meta.get("total", 0) > 0 and sw_meta.get("index", 0) > 0:
            skip_n = int(sw_meta.get("skipped", 0))
            skip_s = f" SKIP:{skip_n}" if skip_n > 0 else ""
            lock_txt = f"LAST SWEEP: {sw_meta.get('index')}/{sw_meta.get('total')}{skip_s}"
            lock_col = t.ok if skip_n == 0 else t.warn
        else:
            lock_txt = "TARGET LOCK: NONE"
            lock_col = t.text_dim
        canvas.draw_text(40, 948, f"PRIORITY BRANDS: {prio}"[:35], t.accent, scale=2)
        canvas.draw_text(40, 986, lock_txt, lock_col, scale=2)

    def _render_remote_tab(self, canvas: FramebufferCanvas, t: Theme, now: float) -> None:
        self._draw_waveform_box(canvas, 24, 294, 672, 176, t)
        gap = 14
        card_w = (self.width - 48 - gap) // 2
        card_h = 102
        items = self.signals[:8]
        if not items:
            canvas.fill_rect(24, 486, 672, 450, t.panel)
            canvas.draw_rect_border(24, 486, 672, 450, t.border, thickness=2)
            canvas.draw_text_centered(360, 710, "NO PROFILE SIGNALS", t.text_dim, scale=2)
        else:
            for idx, sig in enumerate(items):
                r = idx // 2
                c = idx % 2
                bx = 24 + c * (card_w + gap)
                by = 486 + r * (card_h + gap)
                flashing = self.active_flash_key == f"sig:{sig}" and now < self.active_flash_until
                canvas.fill_rect(bx, by, card_w, card_h, t.accent if flashing else t.panel)
                canvas.draw_rect_border(
                    bx, by, card_w, card_h, t.accent if flashing else t.border, thickness=2
                )
                canvas.draw_text_centered(
                    bx + card_w // 2,
                    by + 42,
                    sig.upper().replace("_", " ")[:15],
                    t.bg if flashing else t.text,
                    scale=2,
                )
                canvas.draw_text_centered(
                    bx + card_w // 2,
                    by + 80,
                    "TAP TO TX",
                    t.bg if flashing else t.accent_dim,
                    scale=1,
                )

        macros = [
            ("macro_revert", "REVERT", "HDMI 1", 24),
            ("macro_double", "2X PWR", "SHUTDOWN", 253),
            ("ir_test", "IR TEST", "38KHZ BURST", 482),
        ]
        for action, title, sub, bx in macros:
            flashing = self.active_flash_key == action and now < self.active_flash_until
            canvas.fill_rect(bx, 954, 214, 92, t.accent if flashing else t.panel_alt)
            canvas.draw_rect_border(bx, 954, 214, 92, t.accent_dim, thickness=2)
            canvas.draw_text_centered(bx + 107, 988, title, t.bg if flashing else t.accent, scale=2)
            canvas.draw_text_centered(
                bx + 107, 1022, sub, t.bg if flashing else t.text_dim, scale=1
            )

    def _render_radar_tab(self, canvas: FramebufferCanvas, t: Theme, now: float) -> None:
        # 1. Scan button
        flashing = self.active_flash_key == "rf_scan" and now < self.active_flash_until
        canvas.fill_rect(24, 294, 672, 86, t.accent if flashing else t.panel_alt)
        canvas.draw_rect_border(24, 294, 672, 86, t.accent, thickness=2)
        canvas.draw_text_centered(
            360, 337, "PASSIVE WIFI + BLE SCAN", t.bg if flashing else t.accent, scale=3
        )

        # 2. Detected Brands box
        canvas.fill_rect(24, 396, 672, 214, t.panel)
        canvas.draw_rect_border(24, 396, 672, 214, t.border, thickness=2)
        rf = self.rf_data or {}
        ap_n = rf.get("ap_count", 0)
        ble_n = rf.get("ble_count", 0)
        canvas.draw_text(
            40, 414, f"RADAR: {ap_n} WIFI AP | {ble_n} BLE DEVICES", t.accent, scale=2
        )
        brands = (rf.get("brands") or [])[:4]
        if not brands:
            canvas.draw_text_centered(
                360, 524, "TAP SCAN TO DETECT NEARBY AV BRANDS", t.text_dim, scale=2
            )
        else:
            for idx, b in enumerate(brands):
                col = idx % 2
                row = idx // 2
                bx, by = 40 + col * 324, 466 + row * 68
                prio = b in self.priority_brands
                canvas.fill_rect(bx, by, 308, 56, t.accent if prio else t.panel_alt)
                canvas.draw_rect_border(bx, by, 308, 56, t.accent_dim, thickness=2)
                canvas.draw_text_centered(
                    bx + 154,
                    by + 28,
                    f"{'* ' if prio else ''}{str(b).upper()[:14]}",
                    t.bg if prio else t.text,
                    scale=2,
                )

        # 3. Room Match + SAVE ROOM
        canvas.fill_rect(24, 624, 672, 192, t.panel)
        canvas.draw_rect_border(24, 624, 672, 192, t.border, thickness=2)
        room = ((rf.get("matched_room") or {}).get("room_name")) or "UNMATCHED"
        aps = rf.get("aps") or []
        top_ssid = (aps[0].get("ssid") or aps[0].get("bssid", ""))[:22] if aps else "NO SCAN YET"
        canvas.draw_text(40, 642, f"ROOM: {str(room).upper()[:24]}", t.ok if room != "UNMATCHED" else t.text, scale=2)
        canvas.draw_text(40, 682, f"STRONGEST: {str(top_ssid).upper()}", t.text_dim, scale=2)
        canvas.fill_rect(470, 728, 210, 72, t.panel_alt)
        canvas.draw_rect_border(470, 728, 210, 72, t.accent_dim, thickness=2)
        canvas.draw_text_centered(575, 764, "SAVE ROOM", t.accent, scale=2)

        # 4. Privacy Shield Card
        priv = (self.status_data or {}).get("privacy") or {}
        host_id = str(priv.get("hostname", "UNKNOWN")).upper()[:16]
        wmac = str(priv.get("wlan_mac", "--:--")).upper()
        stealth_ok = bool(priv.get("stealth_ok", False))
        mdns_txt = "OFF" if not priv.get("avahi_active", True) else "ON!"
        bt_txt = "HIDDEN" if not priv.get("bt_discoverable", True) else "VISIBLE!"
        canvas.fill_rect(24, 830, 672, 216, t.panel)
        canvas.draw_rect_border(
            24, 830, 672, 216, t.ok if stealth_ok else t.warn, thickness=2
        )
        canvas.draw_text(
            40,
            848,
            f"PRIVACY SHIELD: {'ACTIVE' if stealth_ok else 'HARDENING'}",
            t.ok if stealth_ok else t.warn,
            scale=2,
        )
        canvas.draw_text(40, 886, f"NET ID: {host_id}", t.text, scale=2)
        canvas.draw_text(40, 920, f"MAC: {wmac}", t.text_dim, scale=2)
        canvas.draw_text(40, 962, f"MDNS:{mdns_txt}  BT:{bt_txt}", t.text_dim, scale=2)
        canvas.fill_rect(470, 954, 210, 74, t.panel_alt)
        canvas.draw_rect_border(470, 954, 210, 74, t.accent, thickness=2)
        canvas.draw_text_centered(575, 991, "ROTATE ID", t.accent, scale=2)

    def render(self, canvas: FramebufferCanvas, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        t = self.theme
        canvas.clear(t.bg)

        status = self.status_data or {}
        pwr = status.get("power", {})
        bl = status.get("backlight", {})
        priv = status.get("privacy", {})
        backend = str(status.get("lirc_backend", "OFFLINE")).upper()
        pct = int(pwr.get("capacity_pct", 0))
        mv = int(pwr.get("voltage_mv", 0))
        temp = float(pwr.get("temp_c", 0.0))
        bl_pct = int(bl.get("pct", 50))
        bl_blanked = bool(bl.get("blanked", False))
        net_id = str(priv.get("hostname", "")).upper()[:12]

        # 1. Header Bar (y=12..100)
        canvas.fill_rect(24, 12, self.width - 48, 88, t.panel)
        canvas.draw_rect_border(24, 12, self.width - 48, 88, t.border, thickness=2)
        canvas.draw_text(40, 22, "PULSE 940", t.accent, scale=3)
        lirc_ok = "NATIVE" in backend
        sub = f"{'IR HW' if lirc_ok else backend[:8]} | {net_id or 'LAB'}"
        canvas.draw_text(40, 64, sub, t.ok if lirc_ok else t.warn, scale=2)

        self._draw_battery_icon(canvas, 464, 24, pct, t)
        canvas.draw_text(528, 25, f"{pct:3d}%", t.text, scale=3)
        canvas.draw_text(464, 64, f"{mv}mV {temp:.1f}C", t.text_dim, scale=2)

        # 2. Brightness Control Card (y=112..210)
        canvas.fill_rect(24, 112, self.width - 48, 98, t.panel)
        canvas.draw_rect_border(24, 112, self.width - 48, 98, t.border, thickness=2)
        canvas.draw_text(40, 120, f"BRIGHTNESS: {bl_pct}%", t.text_dim, scale=1)

        canvas.fill_rect(36, 140, 88, 56, t.panel_alt)
        canvas.draw_rect_border(36, 140, 88, 56, t.accent_dim, thickness=2)
        canvas.draw_text_centered(80, 168, "-", t.accent, scale=4)

        sx, sy, sw, sh = self.slider_rect
        canvas.fill_rect(sx, sy, sw, sh, t.bg)
        canvas.draw_rect_border(sx, sy, sw, sh, t.border, thickness=2)
        fill_pct = 0 if bl_blanked else max(0, min(100, bl_pct))
        fill_w = max(0, round((sw - 8) * fill_pct / 100))
        if fill_w > 0:
            canvas.fill_rect(sx + 4, sy + 4, fill_w, sh - 8, t.accent)

        canvas.fill_rect(596, 140, 88, 56, t.panel_alt)
        canvas.draw_rect_border(596, 140, 88, 56, t.accent_dim, thickness=2)
        canvas.draw_text_centered(640, 168, "+", t.accent, scale=4)

        # 3. Top Navigation Tabs (y=222..280)
        tab_specs = [("universal", "UNIVERSAL", 24), ("remote", "REMOTE", 253), ("radar", "RADAR", 482)]
        for tkey, tlabel, tx in tab_specs:
            active = self.tab == tkey
            canvas.fill_rect(tx, 222, 214, 58, t.accent if active else t.panel)
            canvas.draw_rect_border(tx, 222, 214, 58, t.accent if active else t.border, thickness=2)
            canvas.draw_text_centered(
                tx + 107, 251, tlabel, t.bg if active else t.text, scale=2
            )

        # 4. Active Tab Content
        if self.tab == "universal":
            self._render_universal_tab(canvas, t, now)
        elif self.tab == "remote":
            self._render_remote_tab(canvas, t, now)
        else:
            self._render_radar_tab(canvas, t, now)

        # 5. Status Bar (y=1062..1130)
        canvas.fill_rect(24, 1062, 672, 68, t.panel)
        canvas.draw_rect_border(24, 1062, 672, 68, t.border, thickness=2)
        canvas.draw_text(40, 1088, self.feedback[:36], t.text, scale=2)

        # 6. Bottom Control Dock (y=1146..1252)
        sweeping = self._is_sweeping()
        pwr_confirm = now < self.poweroff_confirm_until
        dock_specs = [
            ("SCREEN", "OFF", 24, t.panel, t.border, t.text),
            ("THEME", t.name[:8], 194, t.panel, t.border, t.accent),
            (
                "STOP" if sweeping else "IR LED",
                "SWEEP" if sweeping else "TEST TX",
                364,
                t.danger if sweeping else t.panel_alt,
                t.danger if sweeping else t.accent_dim,
                t.text if sweeping else t.ok,
            ),
            (
                "CONFIRM" if pwr_confirm else "POWER",
                "TAP NOW" if pwr_confirm else "OFF",
                534,
                t.danger if pwr_confirm else t.panel,
                t.danger,
                t.text if pwr_confirm else t.danger,
            ),
        ]
        for l1, l2, bx, bg_c, bd_c, fg_c in dock_specs:
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
        if ev.kind == "move" and btn.action != "bl_slider":
            return False

        try:
            if btn.action == "bl_down":
                res = rpc.call({"cmd": "backlight", "action": "down", "step_pct": 15})
                if res.get("ok"):
                    if self.status_data is not None:
                        self.status_data["backlight"] = res["backlight"]
                    self.feedback = f"BRIGHTNESS: {res['backlight']['pct']}%"
                return True

            if btn.action == "bl_up":
                res = rpc.call({"cmd": "backlight", "action": "up", "step_pct": 15})
                if res.get("ok"):
                    if self.status_data is not None:
                        self.status_data["backlight"] = res["backlight"]
                    self.feedback = f"BRIGHTNESS: {res['backlight']['pct']}%"
                return True

            if btn.action == "bl_slider":
                val = self.slider_value_for_x(ev.x)
                res = rpc.call({"cmd": "backlight", "action": "set", "value": val})
                if res.get("ok"):
                    if self.status_data is not None:
                        self.status_data["backlight"] = res["backlight"]
                    self.feedback = f"BRIGHTNESS: {res['backlight']['pct']}%"
                return True

            if btn.action == "tab":
                self.tab = btn.arg
                self.feedback = f"MODE: {btn.arg.upper()}"
                return True

            if btn.action == "cat":
                self.category = btn.arg
                acts = CATEGORY_ACTION_BUTTONS.get(btn.arg, [("Power", "POWER")])
                self.action = acts[0][0]
                self.feedback = f"TARGET: {btn.arg.upper()} / {self.action.upper()}"
                return True

            if btn.action == "u_act":
                self.action = btn.arg
                self.feedback = f"ACTION: {self.category.upper()} {btn.arg.upper()}"
                return True

            if btn.action == "sweep":
                res = rpc.call(
                    {
                        "cmd": "sweep_start",
                        "mode": btn.arg,
                        "category": self.category,
                        "action": self.action,
                        "priority_brands": self.priority_brands,
                    }
                )
                if self.status_data is not None and "sweep" in res:
                    self.status_data["sweep"] = res["sweep"]
                self.feedback = f"SWEEP {btn.arg.upper()}: {self.category.upper()} {self.action.upper()}"
                return True

            if btn.action == "sweep_lock":
                res = rpc.call({"cmd": "sweep_lock"})
                if self.status_data is not None and "sweep" in res:
                    self.status_data["sweep"] = res["sweep"]
                locked = (res.get("sweep") or {}).get("locked_signal") or {}
                self.feedback = f"LOCKED: {locked.get('protocol','?')} {locked.get('label','')}"[:36].upper()
                return True

            if btn.action == "sweep_stop_or_test":
                if self._is_sweeping():
                    res = rpc.call({"cmd": "sweep_stop"})
                    if self.status_data is not None and "sweep" in res:
                        self.status_data["sweep"] = res["sweep"]
                    self.feedback = "SWEEP STOPPED"
                else:
                    return self._run_ir_test(rpc, now)
                return True

            if btn.action == "ir_test":
                return self._run_ir_test(rpc, now)

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
                res = rpc.call(
                    {"cmd": "rf_scan", "auto_load": True, "include_ble": True}, timeout=9.0
                )
                self.rf_data = res
                brands = res.get("brands") or []
                if brands:
                    self.priority_brands = list(brands)
                room = (res.get("matched_room") or {}).get("room_name") or "NONE"
                self.feedback = (
                    f"RADAR: {res.get('ap_count', 0)} AP/{res.get('ble_count', 0)} BLE | {room[:12].upper()}"
                )
                return True

            if btn.action == "pick_brand":
                if btn.arg in self.priority_brands:
                    self.priority_brands = [b for b in self.priority_brands if b != btn.arg]
                else:
                    self.priority_brands = [btn.arg] + [
                        b for b in self.priority_brands if b != btn.arg
                    ]
                self.feedback = f"PRIORITY: {', '.join(self.priority_brands[:3]).upper() or 'NONE'}"
                return True

            if btn.action == "save_room":
                room_id = f"Room_{int(time.time()) % 1000}"
                res = rpc.call({"cmd": "save_room", "name": room_id}, timeout=5.0)
                if res.get("ok"):
                    self.feedback = f"SAVED FINGERPRINT: {room_id.upper()}"
                return True

            if btn.action == "privacy_rotate":
                res = rpc.call({"cmd": "privacy_rotate"}, timeout=5.0)
                if res.get("ok") and self.status_data is not None:
                    self.status_data["privacy"] = res["privacy"]
                    self.feedback = f"NEW ID: {str(res['privacy'].get('hostname','')).upper()}"
                return True

            if btn.action == "screen_off":
                rpc.call({"cmd": "backlight", "action": "toggle"})
                self.feedback = "SCREEN BLANKED"
                return True

            if btn.action == "theme":
                th = self.cycle_theme()
                self.feedback = f"THEME: {th.name}"
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

    def _run_ir_test(self, rpc: RpcClient, now: float) -> bool:
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

            is_blanked = last_sys_bl == 0
            poll_interval = 0.25 if ui._is_sweeping() else 1.2
            if not is_blanked and (now - last_poll >= poll_interval):
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
                select.select(wait_fds, [], [], 0.10)
                events = touch.read_events()
                if not is_blanked:
                    for ev in events:
                        if ui.handle_touch(ev, rpc, now=time.monotonic()):
                            dirty = True
                            last_bl_poll = 0.0
                            last_poll = 0.0
            else:
                time.sleep(0.10)
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
