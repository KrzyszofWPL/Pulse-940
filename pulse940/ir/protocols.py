"""Multi-protocol Flipper-compatible IR pulse encoders (NEC, Samsung32, SIRC, RC5/RC6, Kaseikyo, Pioneer, RCA)."""

from __future__ import annotations

from pulse940.ir.nec import (
    NEC_CARRIER_HZ,
    NEC_DUTY_CYCLE,
    encode_flipper_nec,
    parse_flipper_hex_bytes,
)

SUPPORTED_PROTOCOLS: tuple[str, ...] = (
    "NEC",
    "NECext",
    "NEC42",
    "NEC42ext",
    "Samsung32",
    "SIRC",
    "SIRC15",
    "SIRC20",
    "RC5",
    "RC5X",
    "RC6",
    "Kaseikyo",
    "Pioneer",
    "RCA",
)


def _u32_le(byte_list: list[int]) -> int:
    val = 0
    for idx, b in enumerate(byte_list[:4]):
        val |= (b & 0xFF) << (8 * idx)
    return val


def _append_bits_lsb(
    val: int, count: int, mark: int, one_space: int, zero_space: int, out: list[int]
) -> None:
    for bit in range(count):
        out.append(mark)
        out.append(one_space if ((val >> bit) & 1) else zero_space)


def _append_bits_msb(
    val: int, count: int, mark: int, one_space: int, zero_space: int, out: list[int]
) -> None:
    for shift in range(count - 1, -1, -1):
        out.append(mark)
        out.append(one_space if ((val >> shift) & 1) else zero_space)


def _coalesce_manchester(segments: list[tuple[bool, int]]) -> list[int]:
    """Merge adjacent same-polarity (is_mark, duration_us) runs into odd LIRC mark..mark array."""
    merged: list[tuple[bool, int]] = []
    for is_mark, dur in segments:
        if dur <= 0:
            continue
        if merged and merged[-1][0] == is_mark:
            merged[-1] = (is_mark, merged[-1][1] + dur)
        else:
            merged.append((is_mark, dur))

    # Strip leading spaces and trailing spaces so array begins & ends with a mark
    while merged and not merged[0][0]:
        merged.pop(0)
    while merged and not merged[-1][0]:
        merged.pop()

    if not merged:
        raise ValueError("Manchester sequence produced no marks.")
    return [dur for _, dur in merged]


def encode_nec42(address: int, command: int, ext: bool = False) -> list[int]:
    pulses = [9000, 4500]
    a = address & 0x3FFFFFF if ext else address & 0x1FFF
    c = command & 0xFFFF if ext else command & 0xFF
    if ext:
        _append_bits_lsb(a, 26, 560, 1690, 560, pulses)
        _append_bits_lsb(c, 16, 560, 1690, 560, pulses)
    else:
        _append_bits_lsb(a, 13, 560, 1690, 560, pulses)
        _append_bits_lsb(a ^ 0x1FFF, 13, 560, 1690, 560, pulses)
        _append_bits_lsb(c, 8, 560, 1690, 560, pulses)
        _append_bits_lsb(c ^ 0xFF, 8, 560, 1690, 560, pulses)
    pulses.append(560)
    return pulses


def encode_samsung32(address: int, command: int) -> list[int]:
    a = address & 0xFF
    c = command & 0xFF
    pulses = [4500, 4500]
    for b in (a, a, c, c ^ 0xFF):
        _append_bits_lsb(b, 8, 550, 1650, 550, pulses)
    pulses.append(550)
    return pulses


def encode_sirc(address: int, command: int, bits: int = 12) -> list[int]:
    cmd7 = command & 0x7F
    if bits == 12:
        addr_bits = 5
        addr_val = address & 0x1F
    elif bits == 15:
        addr_bits = 8
        addr_val = address & 0xFF
    else:
        addr_bits = 13
        addr_val = address & 0x1FFF

    frame: list[int] = [2400, 600]
    for val, count in ((cmd7, 7), (addr_val, addr_bits)):
        for bit in range(count):
            frame.append(1200 if ((val >> bit) & 1) else 600)
            frame.append(600)
    # Remove trailing space so frame ends cleanly on a mark; emit 2 frames for Sony Rx reliability
    frame.pop()
    return frame + [25000] + list(frame)


def encode_rc5(address: int, command: int, ext: bool = False) -> list[int]:
    unit = 889
    addr5 = address & 0x1F
    if ext:
        cmd_bits = 7
        cmd_val = command & 0x7F
        field_bit = 1
    else:
        cmd6 = command & 0x7F
        field_bit = 0 if (cmd6 >= 64) else 1
        cmd_val = cmd6 & 0x3F
        cmd_bits = 6

    bits: list[int] = [1, field_bit, 0]
    for s in range(4, -1, -1):
        bits.append((addr5 >> s) & 1)
    for s in range(cmd_bits - 1, -1, -1):
        bits.append((cmd_val >> s) & 1)

    segs: list[tuple[bool, int]] = []
    for b in bits:
        if b == 1:
            segs.append((False, unit))
            segs.append((True, unit))
        else:
            segs.append((True, unit))
            segs.append((False, unit))
    return _coalesce_manchester(segs)


def encode_rc6(address: int, command: int) -> list[int]:
    unit = 444
    segs: list[tuple[bool, int]] = [
        (True, 2666),
        (False, 889),
        # Start bit = 1 (RC6 bit 1 = mark, space)
        (True, unit),
        (False, unit),
    ]
    # Mode 000 (3 bits of 0 = space, mark)
    for _ in range(3):
        segs.append((False, unit))
        segs.append((True, unit))
    # Trailer bit (double-width half-bits, toggle=0)
    segs.append((False, unit * 2))
    segs.append((True, unit * 2))

    addr8 = address & 0xFF
    cmd8 = command & 0xFF
    for byte_val in (addr8, cmd8):
        for s in range(7, -1, -1):
            b = (byte_val >> s) & 1
            if b == 1:
                segs.append((True, unit))
                segs.append((False, unit))
            else:
                segs.append((False, unit))
                segs.append((True, unit))
    return _coalesce_manchester(segs)


def encode_kaseikyo(addr_bytes: list[int], cmd_bytes: list[int]) -> list[int]:
    b0 = addr_bytes[0] if len(addr_bytes) > 0 else 0
    b1 = addr_bytes[1] if len(addr_bytes) > 1 else 0
    b2 = addr_bytes[2] if len(addr_bytes) > 2 else 0
    b3 = addr_bytes[3] if len(addr_bytes) > 3 else 0
    c0 = cmd_bytes[0] if len(cmd_bytes) > 0 else 0
    c1 = cmd_bytes[1] if len(cmd_bytes) > 1 else 0
    parity = (b2 ^ c0 ^ c1) & 0xFF
    payload = (b0, b1, b2, c0 if b3 == 0 else b3, c1 if b3 == 0 else c0, parity)

    pulses = [3456, 1728]
    for byte_val in payload:
        _append_bits_lsb(byte_val, 8, 432, 1296, 432, pulses)
    pulses.append(432)
    return pulses


def encode_pioneer(address: int, command: int) -> list[int]:
    a = address & 0xFF
    c = command & 0xFF
    pulses = [8500, 4225]
    for byte_val in (a, a ^ 0xFF, c, c ^ 0xFF):
        _append_bits_lsb(byte_val, 8, 500, 1500, 500, pulses)
    pulses.append(500)
    return pulses


def encode_rca(address: int, command: int) -> list[int]:
    a4 = address & 0x0F
    c8 = command & 0xFF
    pulses = [4000, 4000]
    _append_bits_msb(a4, 4, 500, 2000, 1000, pulses)
    _append_bits_msb(c8, 8, 500, 2000, 1000, pulses)
    _append_bits_msb(a4 ^ 0x0F, 4, 500, 2000, 1000, pulses)
    _append_bits_msb(c8 ^ 0xFF, 8, 500, 2000, 1000, pulses)
    pulses.append(500)
    return pulses


def encode_flipper_protocol(
    protocol: str, address_hex: str, command_hex: str
) -> tuple[int, int, list[int]]:
    """Encode a Flipper Zero parsed IR entry into (carrier_hz, duty_cycle, pulses_us)."""
    key = protocol.strip().upper()
    addr_bytes = parse_flipper_hex_bytes(address_hex)
    cmd_bytes = parse_flipper_hex_bytes(command_hex)
    addr_val = _u32_le(addr_bytes)
    cmd_val = _u32_le(cmd_bytes)

    if key in ("NEC", "NECEXT"):
        return NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_flipper_nec(key, address_hex, command_hex)
    if key == "NEC42":
        return NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_nec42(addr_val, cmd_val, ext=False)
    if key == "NEC42EXT":
        return NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_nec42(addr_val, cmd_val, ext=True)
    if key == "SAMSUNG32":
        return NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_samsung32(addr_bytes[0], cmd_bytes[0])
    if key == "SIRC":
        return 40000, 33, encode_sirc(addr_val, cmd_val, bits=12)
    if key == "SIRC15":
        return 40000, 33, encode_sirc(addr_val, cmd_val, bits=15)
    if key == "SIRC20":
        return 40000, 33, encode_sirc(addr_val, cmd_val, bits=20)
    if key == "RC5":
        return 36000, 33, encode_rc5(addr_bytes[0], cmd_bytes[0], ext=False)
    if key == "RC5X":
        return 36000, 33, encode_rc5(addr_bytes[0], cmd_bytes[0], ext=True)
    if key == "RC6":
        return 36000, 33, encode_rc6(addr_bytes[0], cmd_bytes[0])
    if key == "KASEIKYO":
        return 37000, 33, encode_kaseikyo(addr_bytes, cmd_bytes)
    if key == "PIONEER":
        return 40000, 33, encode_pioneer(addr_bytes[0], cmd_bytes[0])
    if key == "RCA":
        return NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_rca(addr_bytes[0], cmd_bytes[0])

    raise ValueError(f"Unsupported IR protocol: {protocol!r}")
