"""Flipper-IRDB (.ir) file parser supporting raw and multi-protocol parsed signals."""

from __future__ import annotations

from dataclasses import dataclass

from pulse940.ir.nec import NEC_CARRIER_HZ
from pulse940.ir.protocols import encode_flipper_protocol


@dataclass(frozen=True)
class IrSignal:
    """Normalized IR signal ready for LIRC transmission."""

    name: str
    signal_type: str  # "raw" or "parsed"
    carrier_hz: int
    duty_cycle: int
    pulses_us: tuple[int, ...]
    protocol: str | None = None
    label: str = ""
    address_hex: str = ""
    command_hex: str = ""


def _normalize_raw_pulses(raw_tokens: list[str]) -> tuple[int, ...]:
    pulses = [int(tok) for tok in raw_tokens if int(tok) > 0]
    if not pulses:
        raise ValueError("Raw signal contains no positive pulse timings.")
    # LIRC pulse mode requires an odd-length array ending with a mark.
    if len(pulses) % 2 == 0:
        pulses.pop()
    return tuple(pulses)


def parse_ir_signals(content: str, skip_unsupported: bool = True) -> list[IrSignal]:
    """Parse Flipper Zero .ir file content into a list preserving duplicate names."""
    signals: list[IrSignal] = []
    current: dict[str, str] = {}
    pending_label = ""
    current_label = ""

    def _flush() -> None:
        nonlocal current_label
        if not current or "name" not in current or "type" not in current:
            return
        name = current["name"].strip()
        sig_type = current["type"].strip().lower()
        try:
            if sig_type == "raw":
                freq = int(float(current.get("frequency", str(NEC_CARRIER_HZ))))
                if not (15000 <= freq <= 80000):
                    freq = NEC_CARRIER_HZ
                duty_raw = float(current.get("duty_cycle", "0.33"))
                duty_pct = max(
                    1,
                    min(40, round(duty_raw * 100) if duty_raw <= 1.0 else round(duty_raw)),
                )
                pulses = _normalize_raw_pulses(current.get("data", "").split())
                signals.append(
                    IrSignal(
                        name=name,
                        signal_type="raw",
                        carrier_hz=freq,
                        duty_cycle=duty_pct,
                        pulses_us=pulses,
                        label=current_label,
                    )
                )
            elif sig_type == "parsed":
                proto = current.get("protocol", "").strip()
                addr = current.get("address", "")
                cmd = current.get("command", "")
                freq, duty_pct, pulses_list = encode_flipper_protocol(proto, addr, cmd)
                signals.append(
                    IrSignal(
                        name=name,
                        signal_type="parsed",
                        carrier_hz=freq,
                        duty_cycle=min(40, duty_pct),
                        pulses_us=tuple(pulses_list),
                        protocol=proto,
                        label=current_label,
                        address_hex=addr,
                        command_hex=cmd,
                    )
                )
        except Exception:
            if not skip_unsupported:
                raise

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            _flush()
            current = {}
            comment = line.lstrip("#").strip()
            if comment and not comment.lower().startswith(("filetype:", "version:")):
                pending_label = comment
            current_label = pending_label
            continue
        if ":" not in line:
            continue
        if not current:
            current_label = pending_label
        key, val = line.split(":", 1)
        current[key.strip().lower()] = val.strip()

    _flush()
    return signals


def parse_ir_text(content: str) -> dict[str, IrSignal]:
    """Parse Flipper Zero .ir file content into a name->IrSignal mapping."""
    mapping: dict[str, IrSignal] = {}
    for sig in parse_ir_signals(content, skip_unsupported=False):
        mapping[sig.name] = sig
    return mapping


def load_ir_signals(path: str, skip_unsupported: bool = True) -> list[IrSignal]:
    """Load and parse all Flipper .ir signals from disk (keeping duplicate names)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_ir_signals(fh.read(), skip_unsupported=skip_unsupported)


def load_ir_file(path: str) -> dict[str, IrSignal]:
    """Load and parse a Flipper .ir file from disk."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_ir_text(fh.read())
