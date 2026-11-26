"""IR Macro & Stealth execution engine."""

from __future__ import annotations

import threading
import time
from typing import Callable

from pulse940.hal.lirc import LircDevice, TxResult
from pulse940.ir.flipper import IrSignal, load_ir_file


class IrMacroEngine:
    """Executes single IR signals, stealth macros, loops, schedules, and Probe & Lock."""

    def __init__(
        self,
        lirc: LircDevice,
        on_tx: Callable[[TxResult], None] | None = None,
    ) -> None:
        self.lirc = lirc
        self._on_tx = on_tx
        self._lock = threading.Lock()
        self.signals: dict[str, IrSignal] = {}
        self.profile_name: str | None = None
        self.last_tx: TxResult | None = None

        self._loop_stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self.loop_signal: str | None = None

        self._sched_timer: threading.Timer | None = None
        self.scheduled_signal: str | None = None

        self._probe_candidates: list[tuple[str, IrSignal]] = []
        self._probe_index: int = -1
        self.locked_brand: str | None = None

    def load_profile(self, path: str) -> list[str]:
        loaded = load_ir_file(path)
        self.signals = loaded
        self.profile_name = path
        return list(loaded.keys())

    def _get_signal(self, name: str) -> IrSignal:
        if name in self.signals:
            return self.signals[name]
        lower_map = {k.lower(): v for k, v in self.signals.items()}
        if name.lower() in lower_map:
            return lower_map[name.lower()]
        raise KeyError(f"Signal {name!r} not found in loaded profile.")

    def _transmit(self, pulses: list[int], carrier_hz: int, duty_cycle: int) -> TxResult:
        with self._lock:
            res = self.lirc.send_raw(pulses, carrier_hz=carrier_hz, duty_cycle=duty_cycle)
            self.last_tx = res
        if self._on_tx is not None:
            self._on_tx(res)
        return res

    def send_signal(self, name: str) -> TxResult:
        sig = self._get_signal(name)
        return self._transmit(list(sig.pulses_us), sig.carrier_hz, sig.duty_cycle)

    def discrete_source(self, source_name: str) -> TxResult:
        return self.send_signal(source_name)

    def revert(self, default_source: str = "HDMI1") -> TxResult:
        return self.send_signal(default_source)

    def freeze(self) -> TxResult:
        return self.send_signal("Freeze")

    def av_mute(self) -> TxResult:
        return self.send_signal("AV_Mute")

    def double_tap_power(self, delay_ms: int = 500) -> list[TxResult]:
        first = self.send_signal("Power")
        time.sleep(max(0.05, delay_ms / 1000.0))
        second = self.send_signal("Power")
        return [first, second]

    def schedule_tx(self, signal_name: str, delay_s: float) -> None:
        self.cancel_scheduled()
        # Validate signal exists before scheduling
        self._get_signal(signal_name)

        def _fire() -> None:
            self.scheduled_signal = None
            self.send_signal(signal_name)

        self.scheduled_signal = signal_name
        timer = threading.Timer(max(0.05, delay_s), _fire)
        timer.daemon = True
        self._sched_timer = timer
        timer.start()

    def cancel_scheduled(self) -> bool:
        if self._sched_timer is not None:
            self._sched_timer.cancel()
            self._sched_timer = None
            self.scheduled_signal = None
            return True
        return False

    def start_loop(self, signal_name: str, interval_s: float = 2.0) -> None:
        self.stop_loop()
        self._get_signal(signal_name)
        self._loop_stop.clear()
        self.loop_signal = signal_name
        interval = max(0.2, interval_s)

        def _worker() -> None:
            while not self._loop_stop.is_set():
                self.send_signal(signal_name)
                if self._loop_stop.wait(interval):
                    break

        t = threading.Thread(target=_worker, daemon=True)
        self._loop_thread = t
        t.start()

    def stop_loop(self) -> bool:
        was_running = self._loop_thread is not None and self._loop_thread.is_alive()
        self._loop_stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
            self._loop_thread = None
        self.loop_signal = None
        return was_running

    def probe_start(self, candidates: list[tuple[str, IrSignal]]) -> int:
        self._probe_candidates = list(candidates)
        self._probe_index = -1
        self.locked_brand = None
        return len(self._probe_candidates)

    def probe_next(self) -> tuple[str, TxResult]:
        if not self._probe_candidates:
            raise RuntimeError("No probe candidates loaded.")
        self._probe_index = (self._probe_index + 1) % len(self._probe_candidates)
        brand, sig = self._probe_candidates[self._probe_index]
        res = self._transmit(list(sig.pulses_us), sig.carrier_hz, sig.duty_cycle)
        return brand, res

    def probe_lock(self) -> str:
        if not self._probe_candidates or self._probe_index < 0:
            raise RuntimeError("No active probe candidate to lock.")
        brand, _ = self._probe_candidates[self._probe_index]
        self.locked_brand = brand
        return brand
