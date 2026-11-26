"""Master CLI client for controlling Pulse 940 over the TCP bridge."""

from __future__ import annotations

import argparse
import json
import socket


def send_command(host: str, port: int, payload: dict) -> dict:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8").strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 940 Master CLI Client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("status")
    sub.add_parser("last-tx")
    sub.add_parser("list-signals")

    rf_p = sub.add_parser("rf-scan")
    rf_p.add_argument("--auto-load", action="store_true", help="Auto-load matched room .ir")

    tx_p = sub.add_parser("tx-raw")
    tx_p.add_argument("--carrier", type=int, default=38000)
    tx_p.add_argument("--duty", type=int, default=33)
    tx_p.add_argument("pulses", nargs="+", type=int, help="Odd-length us array")

    sig_p = sub.add_parser("tx-signal")
    sig_p.add_argument("name", help="Signal name from loaded .ir profile")

    nec_p = sub.add_parser("tx-nec")
    nec_p.add_argument("address", help="Hex address (e.g. 0x04)")
    nec_p.add_argument("code", help="Hex command (e.g. 0x08)")
    nec_p.add_argument("--ext", action="store_true", help="Use 16-bit NECext")

    mac_p = sub.add_parser("macro")
    mac_p.add_argument(
        "action",
        choices=[
            "revert",
            "freeze",
            "av_mute",
            "double_tap_power",
            "schedule",
            "cancel_schedule",
            "loop_start",
            "loop_stop",
        ],
    )
    mac_p.add_argument("--signal", default="USB")
    mac_p.add_argument("--delay", type=float, default=2.0)
    mac_p.add_argument("--interval", type=float, default=2.0)

    args = parser.parse_args(argv)

    if args.command == "ping":
        req = {"cmd": "ping"}
    elif args.command == "status":
        req = {"cmd": "status"}
    elif args.command == "last-tx":
        req = {"cmd": "last_tx"}
    elif args.command == "list-signals":
        req = {"cmd": "list_signals"}
    elif args.command == "rf-scan":
        req = {"cmd": "rf_scan", "auto_load": args.auto_load}
    elif args.command == "tx-raw":
        req = {
            "cmd": "tx_raw",
            "carrier": args.carrier,
            "duty_cycle": args.duty,
            "pulses": args.pulses,
        }
    elif args.command == "tx-signal":
        req = {"cmd": "tx_signal", "name": args.name}
    elif args.command == "tx-nec":
        req = {
            "cmd": "tx_nec",
            "address": int(args.address, 0),
            "command": int(args.code, 0),
            "ext": args.ext,
        }
    else:
        req = {
            "cmd": "macro",
            "action": args.action,
            "signal": args.signal,
            "delay_s": args.delay,
            "interval_s": args.interval,
        }

    resp = send_command(args.host, args.port, req)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
