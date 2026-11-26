"""Linux LIRC (/dev/lirc0) and QEMU MockLircDevice abstraction."""

from __future__ import annotations

import array
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

# Linux LIRC ioctl constants (from <linux/lirc.h>)
_IOC_WRITE = 1
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS


def _iow(ioc_type: str, nr: int, size: int) -> int:
    return (
        (_IOC_WRITE << _IOC_DIRSHIFT)
        | (ord(ioc_type) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


LIRC_MODE_PULSE = 0x00000002
LIRC_SET_SEND_MODE = _iow("i", 0x11, 4)
LIRC_SET_SEND_CARRIER = _iow("i", 0x13, 4)
LIRC_SET_SEND_DUTY_CYCLE = _iow("i", 0x15, 4)


@dataclass(frozen=True)
class TxResult:
    """Result of a single IR transmission."""

    ok: bool
    backend: str
    carrier_hz: int
    duty_cycle: int
    pulse_count: int
    total_duration_us: int
    timestamp: float = field(default_factory=time.time)
    pulses_us: tuple[int, ...] = field(default_factory=tuple)
    error: str | None = None


def validate_pulses(
    pulses_us: list[int] | tuple[int, ...],
    carrier_hz: int,
    duty_cycle: int,
) -> list[int]:
    """Validate LIRC pulse/space timing array and carrier parameters.

    Linux LIRC raw pulse mode requires an odd-length list of positive
    integers (mark, space, mark, ..., mark) ending with a mark.
    """
    if not pulses_us:
        raise ValueError("Pulse array must not be empty.")
    if not (15000 <= carrier_hz <= 80000):
        raise ValueError(f"Carrier frequency out of range (15-80 kHz): {carrier_hz} Hz")
    if not (1 <= duty_cycle <= 99):
        raise ValueError(f"Duty cycle must be 1..99%: {duty_cycle}%")

    cleaned: list[int] = []
    for idx, val in enumerate(pulses_us):
        if isinstance(val, bool) or not isinstance(val, int):
            raise TypeError(f"Pulse at index {idx} must be int, got {type(val).__name__}")
        if val <= 0:
            raise ValueError(f"Pulse at index {idx} must be > 0 us, got {val}")
        cleaned.append(val)

    if len(cleaned) % 2 == 0:
        raise ValueError(
            f"LIRC pulse array must have an odd length (mark..mark), got {len(cleaned)}"
        )
    return cleaned


class LircDevice(ABC):
    """Abstract base class for IR transmission backends."""

    backend_name: str = "unknown"

    @abstractmethod
    def send_raw(
        self,
        pulses_us: list[int],
        carrier_hz: int = 38000,
        duty_cycle: int = 33,
    ) -> TxResult:
        """Transmit raw microsecond mark/space timings."""


class NativeLircDevice(LircDevice):
    """Direct Linux kernel LIRC transmitter via /dev/lirc0."""

    backend_name = "native_lirc"

    def __init__(self, device_path: str = "/dev/lirc0") -> None:
        self.device_path = device_path

    def send_raw(
        self,
        pulses_us: list[int],
        carrier_hz: int = 38000,
        duty_cycle: int = 33,
    ) -> TxResult:
        # Safety cap: never drive the IR LED near 100% continuous DC.
        safe_duty = max(1, min(45, int(duty_cycle)))
        cleaned = validate_pulses(pulses_us, carrier_hz, safe_duty)
        import fcntl
        import struct

        fd = os.open(self.device_path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, LIRC_SET_SEND_MODE, struct.pack("I", LIRC_MODE_PULSE))
            fcntl.ioctl(fd, LIRC_SET_SEND_CARRIER, struct.pack("I", carrier_hz))
            fcntl.ioctl(fd, LIRC_SET_SEND_DUTY_CYCLE, struct.pack("I", safe_duty))
            buf = array.array("I", cleaned)
            os.write(fd, buf.tobytes())
        finally:
            os.close(fd)

        return TxResult(
            ok=True,
            backend=self.backend_name,
            carrier_hz=carrier_hz,
            duty_cycle=safe_duty,
            pulse_count=len(cleaned),
            total_duration_us=sum(cleaned),
            pulses_us=tuple(cleaned),
        )


class MockLircDevice(LircDevice):
    """QEMU/Windows mock LIRC device that strictly validates microsecond arrays."""

    backend_name = "mock_lirc"

    def __init__(self, on_tx: Callable[[TxResult], None] | None = None) -> None:
        self.last_tx: TxResult | None = None
        self.tx_count: int = 0
        self._on_tx = on_tx

    def send_raw(
        self,
        pulses_us: list[int],
        carrier_hz: int = 38000,
        duty_cycle: int = 33,
    ) -> TxResult:
        cleaned = validate_pulses(pulses_us, carrier_hz, duty_cycle)
        result = TxResult(
            ok=True,
            backend=self.backend_name,
            carrier_hz=carrier_hz,
            duty_cycle=duty_cycle,
            pulse_count=len(cleaned),
            total_duration_us=sum(cleaned),
            pulses_us=tuple(cleaned),
        )
        self.last_tx = result
        self.tx_count += 1
        if self._on_tx is not None:
            self._on_tx(result)
        return result


def create_lirc_device(
    device_path: str = "/dev/lirc0",
    force_mock: bool = False,
    on_tx: Callable[[TxResult], None] | None = None,
) -> LircDevice:
    """Return NativeLircDevice if /dev/lirc0 is present, otherwise MockLircDevice."""
    if not force_mock and os.path.exists(device_path):
        return NativeLircDevice(device_path=device_path)
    return MockLircDevice(on_tx=on_tx)
