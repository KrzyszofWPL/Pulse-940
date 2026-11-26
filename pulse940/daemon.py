"""Main Pulse 940 background daemon."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading

from pulse940.hal.lirc import create_lirc_device
from pulse940.hal.power import PowerMonitor
from pulse940.ir.macros import IrMacroEngine
from pulse940.net.bridge import BridgeServer, CommandDispatcher
from pulse940.rf.rooms import find_default_rooms_file, load_room_profiles
from pulse940.rf.wifi import WifiScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pulse940.daemon")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 940 IR/RF Daemon")
    parser.add_argument("--host", default="0.0.0.0", help="TCP bridge bind host")
    parser.add_argument("--port", type=int, default=9000, help="TCP bridge port")
    parser.add_argument("--mock", action="store_true", help="Force MockLircDevice")
    parser.add_argument(
        "--profile",
        default="profiles/demo_projector.ir",
        help="Optional initial .ir profile to load",
    )
    args = parser.parse_args(argv)

    lirc = create_lirc_device(force_mock=args.mock)
    power = PowerMonitor()
    macros = IrMacroEngine(lirc=lirc)
    if args.profile and os.path.isfile(args.profile):
        loaded = macros.load_profile(args.profile)
        log.info("Loaded IR profile %s (%d signals)", args.profile, len(loaded))

    wifi = WifiScanner(force_mock=args.mock)
    rooms_file = find_default_rooms_file()
    rooms = load_room_profiles(rooms_file) if rooms_file else []
    if rooms:
        log.info("Loaded %d room fingerprint(s) from %s", len(rooms), rooms_file)

    dispatcher = CommandDispatcher(
        lirc=lirc, power=power, macros=macros, wifi=wifi, rooms=rooms
    )
    server = BridgeServer(dispatcher=dispatcher, host=args.host, port=args.port)

    stop_event = threading.Event()

    def _shutdown(_signum: int, _frame: object) -> None:
        log.info("Shutting down Pulse 940 daemon...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "Pulse 940 daemon ready | lirc=%s | listen=%s:%d",
        lirc.backend_name,
        args.host,
        server.port,
    )
    server.start(background=True)
    stop_event.wait()
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
