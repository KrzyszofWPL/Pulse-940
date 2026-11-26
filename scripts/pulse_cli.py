"""Master CLI client for controlling Pulse 940 over the TCP bridge."""

from __future__ import annotations

import argparse
import json
import socket
import sys


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

    tx_p = sub.add_parser("tx-raw")
    tx_p.add_argument("--carrier", type=int, default=38000)
    tx_p.add_argument("--duty", type=int, default=33)
    tx_p.add_argument("pulses", nargs="+", type=int, help="Odd-length us array")

    args = parser.parse_args(argv)

    if args.command == "ping":
        req = {"cmd": "ping"}
    elif args.command == "status":
        req = {"cmd": "status"}
    elif args.command == "last-tx":
        req = {"cmd": "last_tx"}
    else:
        req = {
            "cmd": "tx_raw",
            "carrier": args.carrier,
            "duty_cycle": args.duty,
            "pulses": args.pulses,
        }

    resp = send_command(args.host, args.port, req)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
