"""Main Pulse 940 background daemon."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading

from pulse940.hal.backlight import BacklightController
from pulse940.hal.buttons import EvdevButtonMonitor
from pulse940.hal.lirc import create_lirc_device
from pulse940.hal.power import PowerMonitor
from pulse940.ir.macros import IrMacroEngine
from pulse940.ir.universal import UniversalLibrary, UniversalSweepEngine
from pulse940.net.bridge import BridgeServer, CommandDispatcher
from pulse940.rf.bluetooth import BleScanner
from pulse940.rf.rooms import find_default_rooms_file, load_room_profiles
from pulse940.rf.wifi import WifiScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pulse940.daemon")


def _trigger_poweroff(backlight: BacklightController) -> None:
    if sys.platform == "win32":
        log.info("Ignoring poweroff request on Windows")
        return
    log.warning("Executing system poweroff...")
    try:
        backlight.set_brightness(0)
    except Exception:
        pass
    for cmd in (["systemctl", "poweroff"], ["poweroff"]):
        try:
            subprocess.Popen(cmd)
            return
        except FileNotFoundError:
            continue


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
    backlight = BacklightController()
    macros = IrMacroEngine(lirc=lirc)
    if args.profile and os.path.isfile(args.profile):
        loaded = macros.load_profile(args.profile)
        log.info("Loaded IR profile %s (%d signals)", args.profile, len(loaded))

    wifi = WifiScanner(force_mock=args.mock)
    ble = BleScanner(force_mock=args.mock)
    universal = UniversalSweepEngine(
        lirc=lirc,
        library=UniversalLibrary(),
        on_tx=lambda res: setattr(macros, "last_tx", res),
    )
    rooms_file = find_default_rooms_file()
    rooms = load_room_profiles(rooms_file) if rooms_file else []
    if rooms:
        log.info("Loaded %d room fingerprint(s) from %s", len(rooms), rooms_file)

    dispatcher = CommandDispatcher(
        lirc=lirc,
        power=power,
        macros=macros,
        wifi=wifi,
        ble=ble,
        rooms=rooms,
        backlight=backlight,
        universal=universal,
        on_poweroff=lambda: _trigger_poweroff(backlight),
    )
    server = BridgeServer(dispatcher=dispatcher, host=args.host, port=args.port)

    def _handle_vol_up() -> None:
        if backlight.get_state().blanked:
            return
        st = backlight.step_up(15)
        log.info("Vol+ -> brightness=%d (%d%%)", st.brightness, st.pct)

    def _handle_vol_down() -> None:
        if backlight.get_state().blanked:
            return
        st = backlight.step_down(15, min_brightness=8)
        log.info("Vol- -> brightness=%d (%d%%)", st.brightness, st.pct)

    def _handle_power_short() -> None:
        st = backlight.toggle_blank()
        log.info("Power short -> blanked=%s brightness=%d", st.blanked, st.brightness)

    def _handle_power_long() -> None:
        log.warning("Power long press detected -> initiating system poweroff")
        _trigger_poweroff(backlight)

    buttons = EvdevButtonMonitor(
        on_vol_up=_handle_vol_up,
        on_vol_down=_handle_vol_down,
        on_power_short=_handle_power_short,
        on_power_long=_handle_power_long,
    )

    stop_event = threading.Event()

    def _shutdown(_signum: int, _frame: object) -> None:
        log.info("Shutting down Pulse 940 daemon...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "Pulse 940 daemon ready | lirc=%s | backlight=%s | listen=%s:%d",
        lirc.backend_name,
        backlight.get_state().backend,
        args.host,
        server.port,
    )
    buttons.start()
    server.start(background=True)
    stop_event.wait()
    buttons.stop()
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
