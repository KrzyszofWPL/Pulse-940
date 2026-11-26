"""Deploy Pulse 940 daemon to the running QEMU Alpine Linux VM over SSH/SFTP."""

from __future__ import annotations

import os
import posixpath
import sys

import paramiko

HOST = "127.0.0.1"
PORT = 2222
USER = "root"
PASSWORD = "pulse"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_PKG = os.path.join(REPO_ROOT, "pulse940")
LOCAL_PROFILES = os.path.join(REPO_ROOT, "profiles")
REMOTE_ROOT = "/opt/pulse940"

OPENRC_SCRIPT = """#!/sbin/openrc-run

name="pulse940"
description="Pulse 940 IR & RF Daemon"
command="/usr/bin/python3"
command_args="-m pulse940.daemon --port 9000"
directory="/opt/pulse940"
command_background=true
pidfile="/run/pulse940.pid"
output_log="/var/log/pulse940.log"
error_log="/var/log/pulse940.err"

depend() {
    need net
    after sshd
}
"""


def sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def upload_dir(sftp: paramiko.SFTPClient, local_dir: str, remote_dir: str) -> None:
    sftp_mkdir_p(sftp, remote_dir)
    for item in os.listdir(local_dir):
        if item == "__pycache__" or item.endswith(".pyc"):
            continue
        lpath = os.path.join(local_dir, item)
        rpath = posixpath.join(remote_dir, item)
        if os.path.isdir(lpath):
            upload_dir(sftp, lpath, rpath)
        else:
            sftp.put(lpath, rpath)


def run_ssh(client: paramiko.SSHClient, cmd: str) -> str:
    _, stdout, stderr = client.exec_command(cmd)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if rc != 0:
        raise RuntimeError(f"Command failed ({rc}): {cmd}\n{err}")
    return out


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {USER}@{HOST}:{PORT}...")
    client.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=10)

    try:
        sftp = client.open_sftp()
        print("Uploading pulse940/ to /opt/pulse940/pulse940...")
        upload_dir(sftp, LOCAL_PKG, posixpath.join(REMOTE_ROOT, "pulse940"))
        if os.path.isdir(LOCAL_PROFILES):
            print("Uploading profiles/ to /opt/pulse940/profiles...")
            upload_dir(sftp, LOCAL_PROFILES, posixpath.join(REMOTE_ROOT, "profiles"))

        print("Installing OpenRC service /etc/init.d/pulse940...")
        with sftp.file("/etc/init.d/pulse940", "w") as fh:
            fh.write(OPENRC_SCRIPT)
        sftp.chmod("/etc/init.d/pulse940", 0o755)
        sftp.close()

        print("Fixing autologin on tty1 and ttyAMA0 in /etc/inittab...")
        inittab_fix = (
            "sed -i '/^tty1::/d; /^ttyAMA0::/d' /etc/inittab && "
            "printf 'tty1::respawn:/sbin/getty -n -l /bin/sh 38400 tty1\\n"
            "ttyAMA0::respawn:/sbin/getty -n -l /bin/sh 115200 ttyAMA0 vt100\\n' >> /etc/inittab && "
            "kill -HUP 1"
        )
        run_ssh(client, inittab_fix)

        print("Enabling and starting pulse940 OpenRC service...")
        run_ssh(client, "rc-update add pulse940 default >/dev/null 2>&1 || true")
        run_ssh(client, "rc-service pulse940 stop >/dev/null 2>&1 || pkill -9 -f '[p]ulse940.daemon' >/dev/null 2>&1 || true")
        run_ssh(client, "rc-service pulse940 zap >/dev/null 2>&1 || true")
        run_ssh(client, "rc-service pulse940 start")
        status = run_ssh(client, "rc-service pulse940 status")
        print(status.strip())
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
