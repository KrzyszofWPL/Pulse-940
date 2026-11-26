"""Line-delimited JSON TCP bridge server for Master-Slave control."""

from __future__ import annotations

import json
import socketserver
import threading
import time
from dataclasses import asdict
from typing import Any, Callable

from pulse940.hal.backlight import BacklightController
from pulse940.hal.lirc import LircDevice, TxResult
from pulse940.hal.power import PowerMonitor
from pulse940.ir.macros import IrMacroEngine
from pulse940.ir.nec import NEC_CARRIER_HZ, NEC_DUTY_CYCLE, encode_nec, encode_necext
from pulse940.rf.rooms import RoomProfile, match_room
from pulse940.rf.wifi import WifiScanner, detect_brands


class CommandDispatcher:
    """Handles incoming JSON commands from Master (TCP or RFCOMM)."""

    def __init__(
        self,
        lirc: LircDevice,
        power: PowerMonitor,
        macros: IrMacroEngine | None = None,
        wifi: WifiScanner | None = None,
        rooms: list[RoomProfile] | None = None,
        backlight: BacklightController | None = None,
        on_poweroff: Callable[[], None] | None = None,
    ) -> None:
        self.lirc = lirc
        self.power = power
        self.macros = macros or IrMacroEngine(lirc=lirc)
        self.wifi = wifi or WifiScanner()
        self.rooms = rooms or []
        self.backlight = backlight or BacklightController()
        self._on_poweroff = on_poweroff
        self.started_at = time.time()
        self.last_tx: TxResult | None = None
        self._lock = threading.Lock()

    def _record_tx(self, res: TxResult) -> dict[str, Any]:
        self.last_tx = res
        return {
            "ok": res.ok,
            "backend": res.backend,
            "carrier_hz": res.carrier_hz,
            "duty_cycle": res.duty_cycle,
            "pulse_count": res.pulse_count,
            "total_duration_us": res.total_duration_us,
        }

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pong": True, "ts": time.time()}

        if cmd == "status":
            pwr = self.power.read()
            last = self.macros.last_tx or self.last_tx
            return {
                "ok": True,
                "lirc_backend": self.lirc.backend_name,
                "uptime_s": round(time.time() - self.started_at, 2),
                "power": asdict(pwr),
                "backlight": asdict(self.backlight.get_state()),
                "profile": self.macros.profile_name,
                "signal_count": len(self.macros.signals),
                "loop_signal": self.macros.loop_signal,
                "scheduled_signal": self.macros.scheduled_signal,
                "has_last_tx": last is not None,
            }

        if cmd == "last_tx":
            last = self.macros.last_tx or self.last_tx
            if last is None:
                return {"ok": True, "last_tx": None}
            return {"ok": True, "last_tx": asdict(last)}

        if cmd == "tx_raw":
            pulses = payload.get("pulses")
            carrier = payload.get("carrier", NEC_CARRIER_HZ)
            duty = payload.get("duty_cycle", NEC_DUTY_CYCLE)
            with self._lock:
                res = self.lirc.send_raw(pulses, carrier_hz=carrier, duty_cycle=duty)
                self.macros.last_tx = res
            return self._record_tx(res)

        if cmd == "tx_nec":
            addr = int(payload["address"])
            cmd_code = int(payload["command"])
            ext = bool(payload.get("ext", False))
            pulses = encode_necext(addr, cmd_code) if ext else encode_nec(addr, cmd_code)
            with self._lock:
                res = self.lirc.send_raw(
                    pulses, carrier_hz=NEC_CARRIER_HZ, duty_cycle=NEC_DUTY_CYCLE
                )
                self.macros.last_tx = res
            return self._record_tx(res)

        if cmd == "load_ir":
            path = str(payload["path"])
            names = self.macros.load_profile(path)
            return {"ok": True, "profile": path, "signals": names}

        if cmd == "list_signals":
            return {
                "ok": True,
                "profile": self.macros.profile_name,
                "signals": list(self.macros.signals.keys()),
            }

        if cmd == "tx_signal":
            res = self.macros.send_signal(str(payload["name"]))
            return self._record_tx(res)

        if cmd == "macro":
            action = str(payload.get("action", "")).lower()
            if action == "revert":
                res = self.macros.revert(str(payload.get("default_source", "HDMI1")))
                return self._record_tx(res)
            if action == "freeze":
                return self._record_tx(self.macros.freeze())
            if action == "av_mute":
                return self._record_tx(self.macros.av_mute())
            if action == "double_tap_power":
                delay_ms = int(payload.get("delay_ms", 500))
                results = self.macros.double_tap_power(delay_ms=delay_ms)
                return {
                    "ok": True,
                    "taps": len(results),
                    "last": self._record_tx(results[-1]),
                }
            if action == "schedule":
                sig = str(payload["signal"])
                delay_s = float(payload.get("delay_s", 5.0))
                self.macros.schedule_tx(sig, delay_s)
                return {"ok": True, "scheduled": sig, "delay_s": delay_s}
            if action == "cancel_schedule":
                return {"ok": True, "cancelled": self.macros.cancel_scheduled()}
            if action == "loop_start":
                sig = str(payload["signal"])
                interval_s = float(payload.get("interval_s", 2.0))
                self.macros.start_loop(sig, interval_s)
                return {"ok": True, "loop_signal": sig, "interval_s": interval_s}
            if action == "loop_stop":
                return {"ok": True, "stopped": self.macros.stop_loop()}
            return {"ok": False, "error": f"Unknown macro action: {action!r}"}

        if cmd == "rf_scan":
            source, aps = self.wifi.scan()
            brands = detect_brands([ap.ssid for ap in aps if ap.ssid])
            match = match_room(aps, self.rooms)
            auto_loaded = False
            if match is not None and bool(payload.get("auto_load", False)):
                self.macros.load_profile(match.ir_profile)
                auto_loaded = True
            return {
                "ok": True,
                "source": source,
                "ap_count": len(aps),
                "aps": [asdict(ap) for ap in aps],
                "brands": brands,
                "matched_room": asdict(match) if match else None,
                "auto_loaded": auto_loaded,
            }

        if cmd == "backlight":
            action = str(payload.get("action", "get")).lower()
            step_pct = int(payload.get("step_pct", 10))
            if action == "get":
                st = self.backlight.get_state()
            elif action == "up":
                st = self.backlight.step_up(step_pct=step_pct)
            elif action == "down":
                st = self.backlight.step_down(step_pct=step_pct)
            elif action == "toggle":
                st = self.backlight.toggle_blank()
            elif action == "set":
                st = self.backlight.set_brightness(int(payload["value"]))
            else:
                return {"ok": False, "error": f"Unknown backlight action: {action!r}"}
            return {"ok": True, "backlight": asdict(st)}

        if cmd == "poweroff":
            if self._on_poweroff is not None:
                t = threading.Timer(0.2, self._on_poweroff)
                t.daemon = True
                t.start()
            return {"ok": True, "powering_off": True}

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
        self.dispatcher.macros.stop_loop()
        self.dispatcher.macros.cancel_scheduled()
        self._server.shutdown()
        self._server.server_close()
