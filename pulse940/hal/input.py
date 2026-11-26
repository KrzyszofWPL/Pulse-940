"""Input abstraction for hardware buttons (evdev) with terminal fallback."""

from __future__ import annotations

from enum import Enum


class ButtonEvent(str, Enum):
    """Normalized physical or keyboard navigation events."""

    UP = "UP"
    DOWN = "DOWN"
    POWER = "POWER"
    SELECT = "SELECT"
    BACK = "BACK"


KEYMAP_TERMINAL: dict[str, ButtonEvent] = {
    "k": ButtonEvent.UP,
    "w": ButtonEvent.UP,
    "j": ButtonEvent.DOWN,
    "s": ButtonEvent.DOWN,
    "\r": ButtonEvent.SELECT,
    "\n": ButtonEvent.SELECT,
    " ": ButtonEvent.SELECT,
    "p": ButtonEvent.POWER,
    "b": ButtonEvent.BACK,
    "\x1b": ButtonEvent.BACK,
}


def map_terminal_key(ch: str) -> ButtonEvent | None:
    """Map a raw terminal character to a normalized ButtonEvent."""
    return KEYMAP_TERMINAL.get(ch)
