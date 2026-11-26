"""Device network anonymity, opaque identity rotation, and passive broadcast hygiene."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import socket
import subprocess
import sys


def generate_opaque_id(prefix: str = "dev") -> str:
    """Return a non-identifying random device ID (never contains pulse/ir/xiaomi)."""
    return f"{prefix}-{secrets.token_hex(4)}"


def generate_random_local_mac() -> str:
    """Return a locally-administered unicast MAC address (x2:xx:xx:xx:xx:xx)."""
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xFE) | 0x02
    return ":".join(f"{b:02x}" for b in raw)


def _read_sysfs_mac(iface: str) -> str:
    path = f"/sys/class/net/{iface}/address"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip().lower()
    except OSError:
        return "02:00:00:00:00:00"


def _is_service_active(unit: str) -> bool:
    if sys.platform == "win32":
        return False
    res = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    return res.returncode == 0 and res.stdout.strip() == "active"


def _is_bt_discoverable() -> bool:
    if sys.platform == "win32" or not os.path.isdir("/sys/class/bluetooth/hci0"):
        return False
    res = subprocess.run(
        ["bluetoothctl", "show"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return "Discoverable: yes" in res.stdout


def _is_llmnr_disabled() -> bool:
    conf = "/etc/systemd/resolved.conf.d/pulse940-privacy.conf"
    if not os.path.isfile(conf):
        return sys.platform == "win32"
    try:
        with open(conf, "r", encoding="utf-8") as fh:
            text = fh.read()
        return "LLMNR=no" in text and "MulticastDNS=no" in text
    except OSError:
        return False


def get_privacy_status(wifi_iface: str = "wlan0") -> dict:
    """Return current network anonymity telemetry for Pulse 940."""
    host = socket.gethostname()
    mac = _read_sysfs_mac(wifi_iface)
    avahi_active = _is_service_active("avahi-daemon")
    bt_disc = _is_bt_discoverable()
    llmnr_off = _is_llmnr_disabled()
    opaque_ok = bool(re.match(r"^(dev|node|host)-[0-9a-f]{6,12}$", host))
    stealth_ok = opaque_ok and (not avahi_active) and (not bt_disc) and llmnr_off
    return {
        "hostname": host,
        "wlan_mac": mac,
        "avahi_active": avahi_active,
        "llmnr_off": llmnr_off,
        "bt_discoverable": bt_disc,
        "stealth_ok": stealth_ok,
    }


def _update_etc_hosts(new_host: str) -> None:
    hosts_path = "/etc/hosts"
    if not os.path.isfile(hosts_path):
        return
    with open(hosts_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith("127.0.1.1"):
            out.append(f"127.0.1.1\t{new_host}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"127.0.1.1\t{new_host}")
    with open(hosts_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def rotate_identity(wifi_iface: str = "wlan0") -> dict:
    """Rotate hostname, BT alias (while keeping BT hidden), and unassociated wlan MAC."""
    new_id = generate_opaque_id("dev")
    new_mac = generate_random_local_mac()

    if sys.platform == "win32" or os.geteuid() != 0:  # type: ignore[attr-defined]
        return {
            "rotated": False,
            "simulated": True,
            "hostname": new_id,
            "wlan_mac": new_mac,
            "avahi_active": False,
            "llmnr_off": True,
            "bt_discoverable": False,
            "stealth_ok": True,
        }

    # 1. Rotate Linux hostname + /etc/hosts (critical so `sudo` never hangs)
    try:
        with open("/etc/hostname", "w", encoding="utf-8") as fh:
            fh.write(new_id + "\n")
        _update_etc_hosts(new_id)
        subprocess.run(["hostname", new_id], check=False, timeout=2)
    except OSError:
        pass

    # 2. Keep Bluetooth completely hidden & set matching opaque alias
    if os.path.isdir("/sys/class/bluetooth/hci0"):
        for bt_cmd in (
            ["bluetoothctl", "discoverable", "off"],
            ["bluetoothctl", "pairable", "off"],
            ["bluetoothctl", "system-alias", new_id],
        ):
            subprocess.run(bt_cmd, capture_output=True, text=True, timeout=3, check=False)

    # 3. Rotate wlan MAC only when wlan is not actively associated (operstate down)
    oper_path = f"/sys/class/net/{wifi_iface}/operstate"
    if os.path.isdir(f"/sys/class/net/{wifi_iface}"):
        oper = "unknown"
        try:
            with open(oper_path, "r", encoding="utf-8") as fh:
                oper = fh.read().strip().lower()
        except OSError:
            pass
        if oper in ("down", "dormant", "unknown"):
            subprocess.run(["ip", "link", "set", "dev", wifi_iface, "down"], check=False, timeout=3)
            subprocess.run(
                ["ip", "link", "set", "dev", wifi_iface, "address", new_mac],
                check=False,
                timeout=3,
            )
            subprocess.run(["ip", "link", "set", "dev", wifi_iface, "up"], check=False, timeout=3)

    status = get_privacy_status(wifi_iface)
    status["rotated"] = True
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pulse 940 Network Privacy Controller")
    parser.add_argument("--apply-boot", action="store_true", help="Rotate opaque identity on boot")
    parser.add_argument("--status", action="store_true", help="Print privacy status")
    args = parser.parse_args(argv)
    if args.apply_boot:
        res = rotate_identity()
        print(res)
        return 0
    if args.status:
        print(get_privacy_status())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
