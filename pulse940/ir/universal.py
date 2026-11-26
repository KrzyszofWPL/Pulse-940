"""Flipper-style Universal IR library loader and 3-mode thermally safe Sweep Engine."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

log = logging.getLogger("pulse940.ir.universal")

from pulse940.hal.lirc import LircDevice, TxResult
from pulse940.ir.flipper import IrSignal, load_ir_signals
from pulse940.ir.nec import NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_nec, encode_necext

DEFAULT_CATEGORIES: tuple[str, ...] = ("projector", "tv", "audio", "ac")

CATEGORY_ACTIONS: dict[str, tuple[str, ...]] = {
    "projector": ("Power", "Mute", "Vol_up", "Vol_dn"),
    "tv": ("Power", "Mute", "Vol_up", "Vol_dn"),
    "audio": ("Power", "Mute", "Vol_up", "Vol_dn"),
    "ac": ("Off", "Cool_hi", "Heat_hi", "Dh"),
}

# Vendor -> protocol affinity for ordering universal signals when RF radar spots a brand
BRAND_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "sony": ("SIRC", "SIRC15", "SIRC20"),
    "samsung": ("SAMSUNG32",),
    "lg": ("NEC", "NECEXT"),
    "epson": ("NEC", "NECEXT", "KASEIKYO"),
    "benq": ("NEC", "NECEXT"),
    "optoma": ("NEC", "NECEXT"),
    "nec": ("NEC", "NECEXT"),
    "panasonic": ("KASEIKYO",),
    "philips": ("RC5", "RC5X", "RC6"),
    "pioneer": ("PIONEER",),
}


@dataclass(frozen=True)
class SweepStatus:
    active: bool
    mode: str
    category: str
    action: str
    index: int
    total: int
    current_label: str
    current_protocol: str
    locked_signal: dict | None
    last_error: str | None
    skipped: int = 0


class UniversalLibrary:
    """Loads & deduplicates Flipper Universal Remote .ir asset files."""

    def __init__(self, base_dir: str = "profiles/universal") -> None:
        self.base_dir = base_dir
        self._cache: dict[str, list[IrSignal]] = {}

    def _load_category(self, category: str) -> list[IrSignal]:
        cat = category.lower()
        if cat not in self._cache:
            path = os.path.join(self.base_dir, f"{cat}.ir")
            if os.path.isfile(path):
                self._cache[cat] = load_ir_signals(path, skip_unsupported=True)
            else:
                self._cache[cat] = []
        return self._cache[cat]

    def available_categories(self) -> dict[str, dict[str, int]]:
        info: dict[str, dict[str, int]] = {}
        for cat in DEFAULT_CATEGORIES:
            sigs = self._load_category(cat)
            actions = {}
            for act in CATEGORY_ACTIONS[cat]:
                actions[act] = len(self.get_signals(cat, act))
            info[cat] = {"total_signals": len(sigs), "actions": actions}  # type: ignore[dict-item]
        return info

    def get_signals(
        self,
        category: str,
        action: str,
        priority_brands: list[str] | None = None,
    ) -> list[IrSignal]:
        sigs = self._load_category(category)
        target = action.strip().lower()
        deduped: list[IrSignal] = []
        seen_waves: set[tuple[int, tuple[int, ...]]] = set()

        for sig in sigs:
            if sig.name.strip().lower() != target:
                continue
            key = (sig.carrier_hz, sig.pulses_us)
            if key in seen_waves:
                continue
            seen_waves.add(key)
            deduped.append(sig)

        if not priority_brands or not deduped:
            return deduped

        low_brands = [b.lower() for b in priority_brands]
        prio_protos: set[str] = set()
        for b in low_brands:
            prio_protos.update(BRAND_PROTOCOLS.get(b, ()))

        def _score(sig: IrSignal) -> int:
            lbl = sig.label.lower()
            proto = (sig.protocol or "").upper()
            if any(b in lbl for b in low_brands):
                return 0
            if proto and proto in prio_protos:
                return 1
            return 2

        return sorted(deduped, key=_score)


class UniversalSweepEngine:
    """Runs thermally safe Quick (3-4s), Full (10-16s+), and Deep NEC 256 (40-55s) sweeps."""

    MIN_COOLDOWN_MS = 80

    def __init__(
        self,
        lirc: LircDevice,
        library: UniversalLibrary | None = None,
        on_tx: Callable[[TxResult], None] | None = None,
    ) -> None:
        self.lirc = lirc
        self.library = library or UniversalLibrary()
        self._on_tx = on_tx
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._active = False
        self._mode = "quick"
        self._category = "projector"
        self._action = "Power"
        self._index = 0
        self._total = 0
        self._current_label = ""
        self._current_protocol = ""
        self._last_signal: IrSignal | None = None
        self._locked_signal: dict | None = None
        self._last_error: str | None = None
        self._skipped: int = 0

    def list_categories(self) -> dict[str, dict[str, int]]:
        return self.library.available_categories()

    def get_status(self) -> dict:
        with self._lock:
            return asdict(
                SweepStatus(
                    active=self._active,
                    mode=self._mode,
                    category=self._category,
                    action=self._action,
                    index=self._index,
                    total=self._total,
                    current_label=self._current_label,
                    current_protocol=self._current_protocol,
                    locked_signal=self._locked_signal,
                    last_error=self._last_error,
                    skipped=self._skipped,
                )
            )

    def _build_deep_nec_signals(self, nec_address: int) -> list[IrSignal]:
        is_ext = nec_address > 0xFF
        addr_hex = f"{nec_address & 0xFF:02X} {(nec_address >> 8) & 0xFF:02X}"
        out: list[IrSignal] = []
        for cmd in range(256):
            pulses = (
                encode_necext(nec_address, cmd)
                if is_ext
                else encode_nec(nec_address, cmd)
            )
            out.append(
                IrSignal(
                    name=f"NEC_{cmd:02X}",
                    signal_type="parsed",
                    carrier_hz=NEC_CARRIER_HZ,
                    duty_cycle=NEC_DUTY_CYCLE,
                    pulses_us=tuple(pulses),
                    protocol="NECext" if is_ext else "NEC",
                    label=f"ADDR 0x{nec_address:X} CMD 0x{cmd:02X}",
                    address_hex=addr_hex,
                    command_hex=f"{cmd:02X} 00",
                )
            )
        return out

    def start_sweep(
        self,
        mode: str = "quick",
        category: str = "projector",
        action: str = "Power",
        priority_brands: list[str] | None = None,
        nec_address: int = 0x04,
        cooldown_ms: int = 100,
    ) -> dict:
        self.stop_sweep()
        m = mode.strip().lower()
        cat = category.strip().lower()
        act = action.strip() or "Power"
        delay_s = max(self.MIN_COOLDOWN_MS, int(cooldown_ms)) / 1000.0

        if m == "deep":
            queue = self._build_deep_nec_signals(int(nec_address))
        else:
            queue = self.library.get_signals(cat, act, priority_brands=priority_brands)
            if m == "quick":
                queue = queue[:22]

        if not queue:
            raise ValueError(f"No universal signals found for {cat}/{act} (mode={m}).")

        self._stop_event.clear()
        with self._lock:
            self._active = True
            self._mode = m
            self._category = cat
            self._action = act
            self._index = 0
            self._total = len(queue)
            self._current_label = queue[0].label or queue[0].name
            self._current_protocol = queue[0].protocol or queue[0].signal_type
            self._last_error = None
            self._skipped = 0

        def _worker() -> None:
            try:
                for idx, sig in enumerate(queue, start=1):
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        self._index = idx
                        self._current_label = sig.label or sig.name
                        self._current_protocol = sig.protocol or sig.signal_type
                        self._last_signal = sig

                    try:
                        res = self.lirc.send_raw(
                            list(sig.pulses_us),
                            carrier_hz=sig.carrier_hz,
                            duty_cycle=min(40, sig.duty_cycle),
                        )
                        if self._on_tx is not None:
                            self._on_tx(res)
                    except Exception as exc:
                        log.warning(
                            "Sweep signal %d/%d (%s) skipped: %s",
                            idx,
                            len(queue),
                            sig.label or sig.name,
                            exc,
                        )
                        with self._lock:
                            self._skipped += 1
                            self._last_error = str(exc)

                    if self._stop_event.wait(delay_s):
                        break
            finally:
                with self._lock:
                    self._active = False

        t = threading.Thread(target=_worker, daemon=True)
        self._thread = t
        t.start()
        return self.get_status()

    def stop_sweep(self) -> dict:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._active = False
        return self.get_status()

    def lock_current(self) -> dict:
        self.stop_sweep()
        with self._lock:
            sig = self._last_signal
            if sig is not None:
                self._locked_signal = {
                    "name": sig.name,
                    "protocol": sig.protocol or sig.signal_type,
                    "label": sig.label,
                    "carrier_hz": sig.carrier_hz,
                    "duty_cycle": sig.duty_cycle,
                    "address_hex": sig.address_hex,
                    "command_hex": sig.command_hex,
                    "pulses_us": list(sig.pulses_us),
                }
        return self.get_status()
