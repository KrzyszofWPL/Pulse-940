"""Line-delimited JSON TCP bridge server for Master-Slave control."""

from __future__ import annotations

import json
import socketserver
import threading
import time
from dataclasses import asdict
from typing import Any

from pulse940.hal.lirc import LircDevice, TxResult
from pulse940.hal.power import PowerMonitor


class CommandDispatcher:
    """Handles incoming JSON commands from Master (TCP or RFCOMM)."""

    def __init__(self, lirc: LircDevice, power: PowerMonitor) -> None:
        self.lirc = lirc
        self.power = power
        self.started_at = time.time()
        self.last_tx: TxResult | None = None
        self._lock = threading.Lock()

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pong": True, "ts": time.time()}

        if cmd == "status":
            pwr = self.power.read()
            return {
                "ok": True,
                "lirc_backend": self.lirc.backend_name,
                "uptime_s": round(time.time() - self.started_at, 2),
                "power": asdict(pwr),
                "has_last_tx": self.last_tx is not None,
            }

        if cmd == "last_tx":
            if self.last_tx is None:
                return {"ok": True, "last_tx": None}
            return {"ok": True, "last_tx": asdict(self.last_tx)}

        if cmd == "tx_raw":
            pulses = payload.get("pulses")
            carrier = payload.get("carrier", 38000)
            duty = payload.get("duty_cycle", 33)
            with self._lock:
                res = self.lirc.send_raw(pulses, carrier_hz=carrier, duty_cycle=duty)
                self.last_tx = res
            return {
                "ok": res.ok,
                "backend": res.backend,
                "carrier_hz": res.carrier_hz,
                "duty_cycle": res.duty_cycle,
                "pulse_count": res.pulse_count,
                "total_duration_us": res.total_duration_us,
            }

        return {"ok": False, "error": f"Unknown command: {cmd!r}"}


class _BridgeHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        dispatcher: CommandDispatcher = self.server.dispatcher  # type: ignore[attr-defined]
        while True:
            raw_line = self.rfile.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("JSON payload must be an object.")
                resp = dispatcher.handle(req)
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}

            out = (json.dumps(resp) + "\n").encode("utf-8")
            self.wfile.write(out)
            self.wfile.flush()


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class BridgeServer:
    """Threaded TCP server exposing CommandDispatcher on host:port."""

    def __init__(
        self,
        dispatcher: CommandDispatcher,
        host: str = "0.0.0.0",
        port: int = 9000,
    ) -> None:
        self.dispatcher = dispatcher
        self._server = _ThreadedTCPServer((host, port), _BridgeHandler)
        self._server.dispatcher = dispatcher  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self, background: bool = False) -> None:
        if background:
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            self._thread.start()
        else:
            self._server.serve_forever()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
