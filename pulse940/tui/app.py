"""Full-screen monochrome console TUI for Pulse 940."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from pulse940.hal.input import ButtonEvent, map_terminal_key
from pulse940.tui.waveform import render_waveform

AMBER = "\033[33m"
GREEN = "\033[32m"
DIM = "\033[2m"
BOLD = "\033[1m"
REV = "\033[7m"
RESET = "\033[0m"
CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def rpc_call(host: str, port: int, payload: dict, timeout: float = 1.5) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8").strip())


@dataclass
class TuiState:
    """Holds UI cursor, theme, stealth mode, and event log."""

    items: list[str] = field(default_factory=list)
    cursor: int = 0
    amber_theme: bool = True
    stealth_blank: bool = False
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=5))

    def move_up(self) -> None:
        if self.items:
            self.cursor = (self.cursor - 1) % len(self.items)

    def move_down(self) -> None:
        if self.items:
            self.cursor = (self.cursor + 1) % len(self.items)

    def selected(self) -> str | None:
        if not self.items:
            return None
        return self.items[self.cursor]

    def toggle_theme(self) -> None:
        self.amber_theme = not self.amber_theme

    def toggle_stealth(self) -> None:
        self.stealth_blank = not self.stealth_blank

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {msg}")


def render_screen(
    state: TuiState,
    status_data: dict | None,
    last_tx_data: dict | None,
) -> str:
    if state.stealth_blank:
        return CLEAR

    fg = AMBER if state.amber_theme else GREEN
    lines: list[str] = [CLEAR, fg]

    pwr = (status_data or {}).get("power", {})
    bl = (status_data or {}).get("backlight", {})
    backend = (status_data or {}).get("lirc_backend", "offline")
    profile = (status_data or {}).get("profile") or "none"
    v_mv = pwr.get("voltage_mv", 0)
    c_ma = pwr.get("current_ma", 0)
    pct = pwr.get("capacity_pct", 0)
    temp = pwr.get("temp_c", 0.0)
    bl_str = "OFF" if bl.get("blanked") else f"{bl.get('pct', 0)}%"

    lines.append(
        f"{BOLD}=== PULSE 940 // LAB APPLIANCE ==={RESET}{fg}  [{backend}]"
    )
    pwr_parts = []
    if v_mv > 0:
        pwr_parts.append(f"{v_mv}mV")
    if c_ma != 0:
        pwr_parts.append(f"{c_ma}mA")
    if pct > 0 or v_mv > 0:
        pwr_parts.append(f"{pct}%")
    pwr_parts.append(f"{temp:.1f}C")
    lines.append(
        f"PWR: {' | '.join(pwr_parts)}   BL: {bl_str}   PROF: {profile}"
    )
    lines.append("-" * 64)
    lines.append(f"{BOLD}SIGNALS & MACROS:{RESET}{fg}")

    for idx, item in enumerate(state.items):
        if idx == state.cursor:
            lines.append(f"  {REV}> {item:<24}{RESET}{fg}")
        else:
            lines.append(f"    {item}")

    lines.append("-" * 64)
    if last_tx_data:
        freq = last_tx_data.get("carrier_hz", 38000)
        duty = last_tx_data.get("duty_cycle", 33)
        cnt = last_tx_data.get("pulse_count", 0)
        dur_ms = last_tx_data.get("total_duration_us", 0) / 1000.0
        lines.append(
            f"{BOLD}PWM OSCILLOSCOPE:{RESET}{fg} {freq}Hz | {duty}% | {cnt} pulses | {dur_ms:.2f}ms"
        )
        lines.append(render_waveform(last_tx_data.get("pulses_us", []), width=54))
    else:
        lines.append(f"{BOLD}PWM OSCILLOSCOPE:{RESET}{fg} idle")
        lines.append(render_waveform([], width=54))

    lines.append("-" * 64)
    lines.append(f"{BOLD}EVENT LOG:{RESET}{fg}")
    if state.logs:
        for entry in state.logs:
            lines.append(f"  {DIM}{entry}{RESET}{fg}")
    else:
        lines.append(f"  {DIM}(waiting for events){RESET}{fg}")

    lines.append("-" * 64)
    lines.append(
        "HW: [Vol+/Vol-] Brightness  [Power] Screen  [Hold Power 3s] Off"
    )
    lines.append(RESET)
    return "\n".join(lines)


def _read_key_nonblocking(timeout: float = 0.25) -> str | None:
    if sys.platform == "win32":
        import msvcrt

        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                return ch
            time.sleep(0.05)
        return None

    import select

    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        return sys.stdin.read(1)
    return None


def run_tui(host: str = "127.0.0.1", port: int = 9000) -> int:
    state = TuiState()
    state.log("TUI started")

    old_termios = None
    if sys.platform != "win32" and sys.stdin.isatty():
        import termios
        import tty

        old_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    last_poll = 0.0
    status_data: dict | None = None
    last_tx_data: dict | None = None

    try:
        while True:
            now = time.time()
            if now - last_poll >= 0.8:
                last_poll = now
                try:
                    status_data = rpc_call(host, port, {"cmd": "status"})
                    sigs = rpc_call(host, port, {"cmd": "list_signals"}).get(
                        "signals", []
                    )
                    state.items = list(sigs) + [
                        "[MACRO] REVERT (HDMI1)",
                        "[MACRO] DOUBLE-TAP POWER",
                        "[RF] SCAN & AUTO-LOAD",
                    ]
                    if state.cursor >= len(state.items):
                        state.cursor = 0
                    last_tx_data = rpc_call(host, port, {"cmd": "last_tx"}).get(
                        "last_tx"
                    )
                except Exception as exc:
                    state.log(f"Daemon offline: {exc}")

            sys.stdout.write(render_screen(state, status_data, last_tx_data))
            sys.stdout.flush()

            ch = _read_key_nonblocking(0.25)
            if ch is None:
                continue

            if state.stealth_blank:
                state.stealth_blank = False
                state.log("Display woken from Stealth")
                continue

            if ch == "q":
                break
            if ch == "t":
                state.toggle_theme()
                continue
            if ch == "r":
                try:
                    res = rpc_call(host, port, {"cmd": "rf_scan", "auto_load": True})
                    room = (res.get("matched_room") or {}).get("room_name", "none")
                    brands = ",".join(res.get("brands", [])) or "none"
                    state.log(f"RF Scan: room={room} brands={brands}")
                    last_poll = 0.0
                except Exception as exc:
                    state.log(f"RF Scan err: {exc}")
                continue

            ev = map_terminal_key(ch)
            if ev == ButtonEvent.UP:
                state.move_up()
            elif ev == ButtonEvent.DOWN:
                state.move_down()
            elif ev == ButtonEvent.POWER:
                state.toggle_stealth()
            elif ev == ButtonEvent.SELECT:
                sel = state.selected()
                if sel is None:
                    continue
                try:
                    if sel == "[MACRO] REVERT (HDMI1)":
                        res = rpc_call(host, port, {"cmd": "macro", "action": "revert"})
                        state.log(f"TX REVERT -> {res.get('pulse_count')} pulses")
                    elif sel == "[MACRO] DOUBLE-TAP POWER":
                        res = rpc_call(
                            host, port, {"cmd": "macro", "action": "double_tap_power"}
                        )
                        state.log(f"TX DOUBLE-TAP PWR -> {res.get('taps')} taps")
                    elif sel == "[RF] SCAN & AUTO-LOAD":
                        res = rpc_call(
                            host, port, {"cmd": "rf_scan", "auto_load": True}
                        )
                        room = (res.get("matched_room") or {}).get("room_name", "none")
                        state.log(f"RF Match -> {room}")
                    else:
                        res = rpc_call(host, port, {"cmd": "tx_signal", "name": sel})
                        state.log(f"TX {sel} -> {res.get('pulse_count')} pulses")
                    last_poll = 0.0
                except Exception as exc:
                    state.log(f"TX err: {exc}")
    finally:
        if old_termios is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
        sys.stdout.write(SHOW_CURSOR + RESET + CLEAR)
        sys.stdout.flush()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 940 Monochrome TUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    return run_tui(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
