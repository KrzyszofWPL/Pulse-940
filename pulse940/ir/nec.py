"""NEC and NECext 38 kHz infrared protocol encoder."""

from __future__ import annotations

NEC_CARRIER_HZ = 38000
NEC_DUTY_CYCLE = 33

NEC_HDR_MARK = 9000
NEC_HDR_SPACE = 4500
NEC_BIT_MARK = 560
NEC_ONE_SPACE = 1690
NEC_ZERO_SPACE = 560


def parse_flipper_hex_bytes(hex_str: str) -> list[int]:
    """Parse Flipper-style space-separated hex bytes (e.g. '04 00 00 00')."""
    parts = hex_str.strip().split()
    if not parts:
        raise ValueError("Empty hex byte string.")
    return [int(p, 16) & 0xFF for p in parts]


def _encode_byte_lsb(byte_val: int, out: list[int]) -> None:
    for bit in range(8):
        out.append(NEC_BIT_MARK)
        if (byte_val >> bit) & 1:
            out.append(NEC_ONE_SPACE)
        else:
            out.append(NEC_ZERO_SPACE)


def encode_nec_bytes(b0: int, b1: int, b2: int, b3: int) -> list[int]:
    """Encode 4 bytes into a 67-element LIRC microsecond pulse array (38 kHz)."""
    pulses = [NEC_HDR_MARK, NEC_HDR_SPACE]
    for b in (b0, b1, b2, b3):
        _encode_byte_lsb(b & 0xFF, pulses)
    pulses.append(NEC_BIT_MARK)
    return pulses


def encode_nec(address: int, command: int) -> list[int]:
    """Encode standard 8-bit NEC (addr, ~addr, cmd, ~cmd)."""
    a = address & 0xFF
    c = command & 0xFF
    return encode_nec_bytes(a, a ^ 0xFF, c, c ^ 0xFF)


def encode_necext(address: int, command: int) -> list[int]:
    """Encode extended 16-bit NECext (addr_lo, addr_hi, cmd_lo, cmd_hi)."""
    a_lo = address & 0xFF
    a_hi = (address >> 8) & 0xFF
    c_lo = command & 0xFF
    c_hi = (command >> 8) & 0xFF
    return encode_nec_bytes(a_lo, a_hi, c_lo, c_hi)


def encode_flipper_nec(protocol: str, address_hex: str, command_hex: str) -> list[int]:
    """Encode Flipper-IRDB NEC/NECext address+command hex strings to pulses."""
    proto = protocol.strip().upper()
    addr_bytes = parse_flipper_hex_bytes(address_hex)
    cmd_bytes = parse_flipper_hex_bytes(command_hex)

    if proto == "NEC":
        return encode_nec(addr_bytes[0], cmd_bytes[0])
    if proto == "NECEXT":
        a_lo = addr_bytes[0]
        a_hi = addr_bytes[1] if len(addr_bytes) > 1 else 0
        c_lo = cmd_bytes[0]
        c_hi = cmd_bytes[1] if len(cmd_bytes) > 1 else 0
        return encode_nec_bytes(a_lo, a_hi, c_lo, c_hi)

    raise ValueError(f"Unsupported NEC protocol variant: {protocol!r}")
