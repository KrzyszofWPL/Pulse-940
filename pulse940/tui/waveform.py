"""ASCII/Unicode PWM waveform renderer for microsecond IR pulse arrays."""

from __future__ import annotations


def render_waveform(pulses_us: list[int] | tuple[int, ...], width: int = 56) -> str:
    """Render a 2-row high/low square-wave representation of IR timings."""
    if not pulses_us or width < 8:
        return "  [ no signal transmitted yet ]"

    total = sum(pulses_us)
    if total <= 0:
        return "  [ invalid pulse timings ]"

    # Build time-sampled high/low levels across `width` columns.
    levels: list[bool] = []
    # Precompute cumulative end times
    cum_ends: list[int] = []
    acc = 0
    for dur in pulses_us:
        acc += dur
        cum_ends.append(acc)

    idx = 0
    n = len(pulses_us)
    for col in range(width):
        t = (col + 0.5) * total / width
        while idx < n - 1 and t > cum_ends[idx]:
            idx += 1
        # Even indices are marks (HIGH), odd indices are spaces (LOW)
        levels.append(idx % 2 == 0)

    top = "".join("▀" if hi else " " for hi in levels)
    bot = "".join(" " if hi else "▄" for hi in levels)
    return f"  HI |{top}|\n  LO |{bot}|"
