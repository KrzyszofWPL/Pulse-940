"""Deploy Pulse 940 daemon to QEMU VM or real postmarketOS device over SSH/SFTP."""

from __future__ import annotations

import argparse
import os
import posixpath

import paramiko

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

SYSTEMD_UNIT = """[Unit]
Description=Pulse 940 IR & RF Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/pulse940
ExecStart=/usr/bin/python3 -m pulse940.daemon --port 9000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
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


def run_ssh(client: paramiko.SSHClient, cmd: str, password: str | None = None) -> str:
    _, stdout, stderr = client.exec_command(cmd, get_pty=password is not None)
    if password is not None:
        stdout.channel.sendall((password + "\n").encode("utf-8"))
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if rc != 0:
        raise RuntimeError(f"Command failed ({rc}): {cmd}\n{out}\n{err}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Pulse 940 over SSH/SFTP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="pulse")
    args = parser.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {args.user}@{args.host}:{args.port}...")
    client.connect(
        args.host, port=args.port, username=args.user, password=args.password, timeout=10
    )

    is_root = args.user == "root"
    stage_dir = REMOTE_ROOT if is_root else "/tmp/pulse940_stage"

    try:
        sftp = client.open_sftp()
        print(f"Uploading pulse940/ & profiles/ to {stage_dir}...")
        upload_dir(sftp, LOCAL_PKG, posixpath.join(stage_dir, "pulse940"))
        if os.path.isdir(LOCAL_PROFILES):
            upload_dir(sftp, LOCAL_PROFILES, posixpath.join(stage_dir, "profiles"))

        init_dest = "/tmp/pulse940.initd"
        with sftp.file(init_dest, "w") as fh:
            fh.write(OPENRC_SCRIPT)
        sftp.chmod(init_dest, 0o755)
        with sftp.file("/tmp/pulse940.service", "w") as fh:
            fh.write(SYSTEMD_UNIT)
        sftp.close()

        setup_lines = ["#!/bin/sh", "set -e"]
        if not is_root:
            setup_lines.append(
                f"mkdir -p {REMOTE_ROOT} && cp -r {stage_dir}/* {REMOTE_ROOT}/ && rm -rf {stage_dir}"
            )
        setup_lines.extend(
            [
                "SP=$(python3 -c 'import site; print(site.getsitepackages()[0])')",
                "echo '/opt/pulse940' > \"$SP/pulse940.pth\"",
                "printf '#!/bin/sh\\ncd /opt/pulse940 && exec python3 -m pulse940.tui.app \"$@\"\\n' > /usr/local/bin/pulse940-tui",
                "chmod +x /usr/local/bin/pulse940-tui",
                "printf '#!/bin/sh\\nexport TERM=xterm-256color\\ncd /opt/pulse940 && python3 -m pulse940.tui.app\\nexec /bin/sh -l\\n' > /usr/local/bin/pulse940-login",
                "chmod +x /usr/local/bin/pulse940-login",
                "if command -v systemctl >/dev/null 2>&1; then",
                "  mv /tmp/pulse940.service /etc/systemd/system/pulse940.service",
                "  rm -f /tmp/pulse940.initd",
                "  mkdir -p /etc/systemd/system/getty@tty1.service.d",
                "  printf '[Service]\\nExecStart=\\nExecStart=-/sbin/agetty -o \"-p -- \\\\u\" --noclear --autologin root %%I $TERM\\n' > /etc/systemd/system/getty@tty1.service.d/autologin.conf",
                "  printf 'if [ \"$(tty)\" = \"/dev/tty1\" ] && [ -z \"$SSH_CONNECTION\" ]; then exec /usr/local/bin/pulse940-login; fi\\n' > /root/.profile",
                "  systemctl daemon-reload",
                "  systemctl enable pulse940.service",
                "  systemctl restart pulse940.service",
                "  systemctl is-active pulse940.service",
                "else",
                "  mv /tmp/pulse940.initd /etc/init.d/pulse940 && chmod 755 /etc/init.d/pulse940",
                "  rm -f /tmp/pulse940.service",
                "  sed -i '/^tty1::/d' /etc/inittab",
                "  echo 'tty1::respawn:/sbin/getty -n -l /usr/local/bin/pulse940-login 38400 tty1' >> /etc/inittab",
                "  if grep -q '^ttyAMA0' /etc/inittab; then sed -i '/^ttyAMA0::/d' /etc/inittab && echo 'ttyAMA0::respawn:/sbin/getty -n -l /usr/local/bin/pulse940-login 115200 ttyAMA0 vt100' >> /etc/inittab; fi",
                "  kill -HUP 1",
                "  /sbin/rc-update add pulse940 default >/dev/null 2>&1 || true",
                "  /sbin/rc-service pulse940 stop >/dev/null 2>&1 || pkill -9 -f '[p]ulse940.daemon' >/dev/null 2>&1 || true",
                "  /sbin/rc-service pulse940 zap >/dev/null 2>&1 || true",
                "  /sbin/rc-service pulse940 start",
                "  /sbin/rc-service pulse940 status",
                "fi",
            ]
        )

        sftp = client.open_sftp()
        with sftp.file("/tmp/pulse940_setup.sh", "w") as fh:
            fh.write("\n".join(setup_lines) + "\n")
        sftp.chmod("/tmp/pulse940_setup.sh", 0o755)
        sftp.close()

        if is_root:
            out = run_ssh(client, "sh /tmp/pulse940_setup.sh && rm -f /tmp/pulse940_setup.sh")
        else:
            out = run_ssh(
                client,
                f"echo '{args.password}' | sudo -S sh /tmp/pulse940_setup.sh && rm -f /tmp/pulse940_setup.sh",
                password=args.password,
            )
        print(out.strip())
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
