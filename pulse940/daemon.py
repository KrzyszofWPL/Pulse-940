"""Main Pulse 940 background daemon."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from pulse940.hal.lirc import create_lirc_device
from pulse940.hal.power import PowerMonitor
from pulse940.net.bridge import BridgeServer, CommandDispatcher

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
    args = parser.parse_args(argv)

    lirc = create_lirc_device(force_mock=args.mock)
    power = PowerMonitor()
    dispatcher = CommandDispatcher(lirc=lirc, power=power)
    server = BridgeServer(dispatcher=dispatcher, host=args.host, port=args.port)

    def _shutdown(_signum: int, _frame: object) -> None:
        log.info("Shutting down Pulse 940 daemon...")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "Pulse 940 daemon ready | lirc=%s | listen=%s:%d",
        lirc.backend_name,
        args.host,
        server.port,
    )
    server.start(background=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
