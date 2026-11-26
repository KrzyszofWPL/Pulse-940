"""Linux IIO sensor hub for qcom-smgr accel, mag (compass/EMF), and optical prox."""

from __future__ import annotations

import logging
import math
import os
import select
import struct
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("pulse940.hal.sensors")

DEFAULT_MOUNT_MATRIX = True  # Redmi 4A: x = -raw_y, y = -raw_x, z = raw_z


@dataclass(frozen=True)
class SensorSnapshot:
    """Thread-safe snapshot of orientation, magnetic field (uT), and optical prox."""

    backend: str
    heading_deg: float
    pitch_deg: float
    roll_deg: float
    accel_x: float
    accel_y: float
    accel_z: float
    mag_x_ut: float
    mag_y_ut: float
    mag_z_ut: float
    mag_total_ut: float
    prox_raw: int
    prox_blocked: bool
    timestamp: float


def _read_str(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _read_float(path: str, default: float) -> float:
    try:
        return float(_read_str(path))
    except (OSError, ValueError):
        return default


def _write_str(path: str, value: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)


def _apply_mount_matrix(rx: float, ry: float, rz: float) -> tuple[float, float, float]:
    # qcom-smgr mount matrix on Redmi 4A: [0, -1, 0; -1, 0, 0; 0, 0, 1]
    return (-ry, -rx, rz)


def compute_orientation(
    ax: float, ay: float, az: float, mx: float, my: float, mz: float
) -> tuple[float, float, float]:
    """Compute (heading_deg, pitch_deg, roll_deg) from accel and mag vectors."""
    pitch = math.atan2(ay, math.sqrt(ax * ax + az * az) or 1e-6)
    roll = math.atan2(-ax, az if abs(az) > 1e-4 else (1e-4 if az >= 0 else -1e-4))

    if (mx * mx + my * my + mz * mz) < 1e-6:
        return 0.0, math.degrees(pitch), math.degrees(roll)

    cp = math.cos(pitch)
    sp = math.sin(pitch)
    cr = math.cos(roll)
    sr = math.sin(roll)
    xh = mx * cr + mz * sr
    yh = mx * sr * sp + my * cp - mz * cr * sp
    hdg = math.degrees(math.atan2(-yh, xh)) % 360.0
    return hdg, math.degrees(pitch), math.degrees(roll)


def parse_vec3_samples(chunk: bytes) -> tuple[int, int, int] | None:
    """Return the newest (x, y, z) signed 32-bit sample from an IIO buffer read."""
    if len(chunk) < 12:
        return None
    last_off = ((len(chunk) // 12) - 1) * 12
    return struct.unpack_from("<3i", chunk, last_off)


def parse_prox_samples(chunk: bytes) -> int | None:
    """Return the newest unsigned 32-bit proximity sample from an IIO buffer read."""
    if len(chunk) < 4:
        return None
    last_off = ((len(chunk) // 4) - 1) * 4
    return struct.unpack_from("<I", chunk, last_off)[0]


class SensorHub:
    """Lazy-started background IIO reader with synthetic mock fallback."""

    def __init__(
        self,
        iio_root: str = "/sys/bus/iio/devices",
        force_mock: bool = False,
    ) -> None:
        self.iio_root = iio_root
        self.force_mock = force_mock
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices = self._discover_devices() if not force_mock else {}
        self.backend = "qcom_smgr_iio" if self._devices else "mock_sensors"

        self._fds: dict[int, str] = {}
        self._scales: dict[str, float] = {
            "accel": 0.000015258,
            "mag": 0.000015258,
            "prox": -0.000000762,
        }
        self._prox_offset = 0.050003051

        self._ax, self._ay, self._az = 0.0, 4.2, 8.8
        self._mx, self._my, self._mz = 24.0, 5.0, -38.0
        self._prox_raw = 0
        self._prox_blocked = False
        self._heading = 0.0
        self._pitch = 25.0
        self._roll = 0.0
        self._started = False
        self._mock_t0 = time.monotonic()

    def _discover_devices(self) -> dict[str, str]:
        if sys.platform == "win32" or not os.path.isdir(self.iio_root):
            return {}
        found: dict[str, str] = {}
        for entry in sorted(os.listdir(self.iio_root)):
            if not entry.startswith("iio:device"):
                continue
            sys_dir = os.path.join(self.iio_root, entry)
            try:
                name = _read_str(os.path.join(sys_dir, "name")).lower()
            except OSError:
                continue
            dev_node = os.path.join("/dev", entry)
            if not os.path.exists(dev_node):
                continue
            if "accel" in name:
                found["accel"] = entry
            elif "mag" in name:
                found["mag"] = entry
            elif "prox" in name:
                found["prox"] = entry
        return found if ("accel" in found or "mag" in found) else {}

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self.backend != "qcom_smgr_iio":
                self._started = True
                return

            self._stop_event.clear()
            for role, dev_name in self._devices.items():
                sys_dir = os.path.join(self.iio_root, dev_name)
                try:
                    _write_str(os.path.join(sys_dir, "buffer", "enable"), "0")
                    ts_en = os.path.join(sys_dir, "scan_elements", "in_timestamp_en")
                    if os.path.isfile(ts_en):
                        _write_str(ts_en, "0")
                    if role == "prox":
                        self._scales["prox"] = _read_float(
                            os.path.join(sys_dir, "in_proximity_scale"), -0.000000762
                        )
                        self._prox_offset = _read_float(
                            os.path.join(sys_dir, "in_proximity_offset"), 0.050003051
                        )
                        _write_str(
                            os.path.join(sys_dir, "scan_elements", "in_proximity_en"), "1"
                        )
                    else:
                        prefix = "in_accel" if role == "accel" else "in_magn"
                        self._scales[role] = _read_float(
                            os.path.join(sys_dir, f"{prefix}_scale"), 0.000015258
                        )
                        for axis in ("x", "y", "z"):
                            _write_str(
                                os.path.join(sys_dir, "scan_elements", f"{prefix}_{axis}_en"),
                                "1",
                            )
                    _write_str(os.path.join(sys_dir, "buffer", "length"), "16")
                    _write_str(os.path.join(sys_dir, "buffer", "enable"), "1")
                    fd = os.open(os.path.join("/dev", dev_name), os.O_RDONLY | os.O_NONBLOCK)
                    self._fds[fd] = role
                except OSError as exc:
                    log.warning("Could not start IIO %s (%s): %s", role, dev_name, exc)

            if self._fds:
                self._started = True
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            else:
                self.backend = "mock_sensors"
                self._started = True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            fd_list = list(self._fds.keys())
            if not fd_list:
                break
            try:
                readable, _, _ = select.select(fd_list, [], [], 0.06)
            except (OSError, ValueError):
                break
            for fd in readable:
                role = self._fds.get(fd)
                if role is None:
                    continue
                last_chunk = b""
                while True:
                    try:
                        data = os.read(fd, 256)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    last_chunk = data
                if not last_chunk:
                    continue

                if role == "prox":
                    raw_p = parse_prox_samples(last_chunk)
                    if raw_p is not None:
                        dist_m = raw_p * self._scales["prox"] + self._prox_offset
                        with self._lock:
                            self._prox_raw = raw_p
                            self._prox_blocked = (raw_p > 25000) or (dist_m < 0.03)
                else:
                    vec = parse_vec3_samples(last_chunk)
                    if vec is not None:
                        sc = self._scales[role]
                        vx, vy, vz = _apply_mount_matrix(
                            vec[0] * sc, vec[1] * sc, vec[2] * sc
                        )
                        with self._lock:
                            alpha = 0.35
                            if role == "accel":
                                self._ax += alpha * (vx - self._ax)
                                self._ay += alpha * (vy - self._ay)
                                self._az += alpha * (vz - self._az)
                            else:
                                # qcom-smgr-mag is in Gauss -> convert to microtesla (*100)
                                self._mx += alpha * (vx * 100.0 - self._mx)
                                self._my += alpha * (vy * 100.0 - self._my)
                                self._mz += alpha * (vz * 100.0 - self._mz)
                            hdg, pitch, roll = compute_orientation(
                                self._ax, self._ay, self._az, self._mx, self._my, self._mz
                            )
                            diff = ((hdg - self._heading + 180.0) % 360.0) - 180.0
                            self._heading = (self._heading + 0.35 * diff) % 360.0
                            self._pitch = pitch
                            self._roll = roll

    def read(self) -> SensorSnapshot:
        with self._lock:
            if self.backend != "qcom_smgr_iio":
                t = time.monotonic() - self._mock_t0
                hdg = (t * 24.0) % 360.0
                pitch = 26.0 + 6.0 * math.sin(t * 0.9)
                roll = 4.0 * math.cos(t * 0.7)
                mx = 22.0 * math.cos(math.radians(hdg))
                my = 22.0 * math.sin(math.radians(hdg))
                mz = 40.0 + 3.0 * math.sin(t * 1.3)
                mag_tot = math.sqrt(mx * mx + my * my + mz * mz)
                return SensorSnapshot(
                    backend="mock_sensors",
                    heading_deg=round(hdg, 1),
                    pitch_deg=round(pitch, 1),
                    roll_deg=round(roll, 1),
                    accel_x=0.0,
                    accel_y=round(9.81 * math.sin(math.radians(pitch)), 2),
                    accel_z=round(9.81 * math.cos(math.radians(pitch)), 2),
                    mag_x_ut=round(mx, 1),
                    mag_y_ut=round(my, 1),
                    mag_z_ut=round(mz, 1),
                    mag_total_ut=round(mag_tot, 1),
                    prox_raw=self._prox_raw,
                    prox_blocked=self._prox_blocked,
                    timestamp=time.time(),
                )

            mag_tot = math.sqrt(
                self._mx * self._mx + self._my * self._my + self._mz * self._mz
            )
            return SensorSnapshot(
                backend=self.backend,
                heading_deg=round(self._heading, 1),
                pitch_deg=round(self._pitch, 1),
                roll_deg=round(self._roll, 1),
                accel_x=round(self._ax, 2),
                accel_y=round(self._ay, 2),
                accel_z=round(self._az, 2),
                mag_x_ut=round(self._mx, 1),
                mag_y_ut=round(self._my, 1),
                mag_z_ut=round(self._mz, 1),
                mag_total_ut=round(mag_tot, 1),
                prox_raw=self._prox_raw,
                prox_blocked=self._prox_blocked,
                timestamp=time.time(),
            )

    def stop(self) -> None:
        with self._lock:
            was_started = self._started
            self._started = False
            self._stop_event.set()
            thread = self._thread
            self._thread = None
            fds = list(self._fds.keys())
            self._fds.clear()

        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if was_started and self.backend == "qcom_smgr_iio":
            for dev_name in self._devices.values():
                try:
                    _write_str(os.path.join(self.iio_root, dev_name, "buffer", "enable"), "0")
                except OSError:
                    pass
