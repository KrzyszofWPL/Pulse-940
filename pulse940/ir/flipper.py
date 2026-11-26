"""Flipper-IRDB (.ir) file parser supporting both raw and parsed signals."""

from __future__ import annotations

from dataclasses import dataclass

from pulse940.ir.nec import NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_flipper_nec


@dataclass(frozen=True)
class IrSignal:
    """Normalized IR signal ready for LIRC transmission."""

    name: str
    signal_type: str  # "raw" or "parsed"
    carrier_hz: int
    duty_cycle: int
    pulses_us: tuple[int, ...]
    protocol: str | None = None


def _normalize_raw_pulses(raw_tokens: list[str]) -> tuple[int, ...]:
    pulses = [int(tok) for tok in raw_tokens if int(tok) > 0]
    if not pulses:
        raise ValueError("Raw signal contains no positive pulse timings.")
    # LIRC pulse mode requires an odd-length array ending with a mark.
    if len(pulses) % 2 == 0:
        pulses.pop()
    return tuple(pulses)


def parse_ir_text(content: str) -> dict[str, IrSignal]:
    """Parse Flipper Zero .ir file content into a name->IrSignal mapping."""
    signals: dict[str, IrSignal] = {}
    current: dict[str, str] = {}

    def _flush() -> None:
        if not current or "name" not in current or "type" not in current:
            return
        name = current["name"].strip()
        sig_type = current["type"].strip().lower()
        if sig_type == "raw":
            freq = int(float(current.get("frequency", str(NEC_CARRIER_HZ))))
            duty_raw = float(current.get("duty_cycle", "0.33"))
            duty_pct = max(1, min(99, round(duty_raw * 100) if duty_raw <= 1.0 else round(duty_raw)))
            pulses = _normalize_raw_pulses(current.get("data", "").split())
            signals[name] = IrSignal(
                name=name,
                signal_type="raw",
                carrier_hz=freq,
                duty_cycle=duty_pct,
                pulses_us=pulses,
            )
        elif sig_type == "parsed":
            proto = current.get("protocol", "").strip()
            addr = current.get("address", "")
            cmd = current.get("command", "")
            pulses = tuple(encode_flipper_nec(proto, addr, cmd))
            signals[name] = IrSignal(
                name=name,
                signal_type="parsed",
                carrier_hz=NEC_CARRIER_HZ,
                duty_cycle=NEC_DUTY_CYCLE,
                pulses_us=pulses,
                protocol=proto,
            )

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            _flush()
            current = {}
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        current[key.strip().lower()] = val.strip()

    _flush()
    return signals


def load_ir_file(path: str) -> dict[str, IrSignal]:
    """Load and parse a Flipper .ir file from disk."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_ir_text(fh.read())
