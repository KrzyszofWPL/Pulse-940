"""Pure-Python 3D wireframe RF/EMF/Geiger tactical scanner for Pulse 940."""

from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from dataclasses import dataclass, field

from pulse940.hal.sensors import SensorSnapshot


@dataclass(frozen=True)
class RfObject3D:
    """3D spatial representation of an observed Wi-Fi AP or BLE device."""

    obj_id: str
    label: str
    kind: str  # "wifi" | "ble"
    brand: str
    rssi_dbm: int
    rssi_delta: int
    azimuth_rad: float
    radius: float  # normalized 0.20 .. 0.95
    height: float  # 0.15 .. 0.65


def project_3d(
    x: float,
    y: float,
    z: float,
    yaw_rad: float,
    pitch_rad: float,
    cx: int,
    cy: int,
    scale: float,
) -> tuple[int, int] | None:
    """Project world (x=right, y=up, z=forward) into screen (px, py) with perspective."""
    cy_v = math.cos(yaw_rad)
    sy_v = math.sin(yaw_rad)
    cp_v = math.cos(pitch_rad)
    sp_v = math.sin(pitch_rad)

    # Yaw rotation around Y
    xr = x * cy_v - z * sy_v
    zr = x * sy_v + z * cy_v
    # Pitch rotation around X
    yr = y * cp_v - zr * sp_v
    zf = y * sp_v + zr * cp_v

    cam_z = zf + 2.65
    if cam_z <= 0.25:
        return None
    persp = scale / cam_z
    return (round(cx + xr * persp), round(cy - yr * persp))


def _mac_azimuth(mac: str) -> float:
    digest = hashlib.blake2s(mac.lower().encode("ascii", errors="ignore"), digest_size=2).digest()
    val = int.from_bytes(digest, "big")
    return (val / 65536.0) * (2.0 * math.pi)


class Scanner3DState:
    """Maintains 3D tactical objects, sonar sweep beam, EMF & Geiger histories."""

    def __init__(self) -> None:
        self.submode = "rf3d"  # "rf3d" | "emf" | "geiger"
        self.imu_mode = True
        self.sweep_angle = 0.0
        self._last_tick = time.monotonic()
        self.objects: list[RfObject3D] = []
        self._prev_rssi: dict[str, int] = {}
        self.last_ping_id: str | None = None
        self.shadow_alert_until: float = 0.0
        self.shadow_source: str = ""
        self.emf_history: deque[float] = deque(maxlen=64)
        self.geiger_target_idx: int = 0
        self.geiger_history: deque[int] = deque(maxlen=64)

    def update_rf(self, rf_data: dict | None, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        if not rf_data:
            return
        objs: list[RfObject3D] = []
        max_shadow = 0
        shadow_name = ""

        for ap in (rf_data.get("aps") or [])[:12]:
            mac = str(ap.get("bssid", "")).lower()
            rssi = int(ap.get("rssi_dbm", -85))
            prev = self._prev_rssi.get(mac, rssi)
            delta = rssi - prev
            self._prev_rssi[mac] = rssi
            if abs(delta) >= 8 and abs(delta) > max_shadow:
                max_shadow = abs(delta)
                shadow_name = str(ap.get("ssid") or mac[:8])

            norm = max(0.0, min(1.0, (rssi + 95) / 60.0))  # 1.0 strong -> 0.0 weak
            radius = 0.92 - norm * 0.72
            height = 0.14 + norm * 0.45
            lbl = str(ap.get("ssid") or mac[-8:])[:12]
            objs.append(
                RfObject3D(
                    obj_id=mac,
                    label=lbl,
                    kind="wifi",
                    brand="",
                    rssi_dbm=rssi,
                    rssi_delta=delta,
                    azimuth_rad=_mac_azimuth(mac),
                    radius=radius,
                    height=height,
                )
            )

        for dev in (rf_data.get("ble_devices") or [])[:8]:
            mac = str(dev.get("mac", "")).lower()
            rssi = int(dev.get("rssi_dbm", -85))
            prev = self._prev_rssi.get(mac, rssi)
            delta = rssi - prev
            self._prev_rssi[mac] = rssi
            norm = max(0.0, min(1.0, (rssi + 95) / 60.0))
            radius = 0.92 - norm * 0.72
            height = 0.12 + norm * 0.36
            lbl = str(dev.get("brand") or dev.get("name") or mac[-8:])[:12]
            objs.append(
                RfObject3D(
                    obj_id=mac,
                    label=lbl,
                    kind="ble",
                    brand=str(dev.get("brand") or ""),
                    rssi_dbm=rssi,
                    rssi_delta=delta,
                    azimuth_rad=_mac_azimuth(mac),
                    radius=radius,
                    height=height,
                )
            )

        objs.sort(key=lambda o: o.rssi_dbm, reverse=True)
        self.objects = objs
        if max_shadow >= 8:
            self.shadow_alert_until = now + 2.5
            self.shadow_source = shadow_name[:14]
        if self.objects:
            tgt = self.objects[self.geiger_target_idx % len(self.objects)]
            self.geiger_history.append(tgt.rssi_dbm)

    def cycle_geiger_target(self) -> RfObject3D | None:
        if not self.objects:
            return None
        self.geiger_target_idx = (self.geiger_target_idx + 1) % len(self.objects)
        self.geiger_history.clear()
        tgt = self.objects[self.geiger_target_idx]
        self.geiger_history.append(tgt.rssi_dbm)
        return tgt


def _draw_compass_tape(
    canvas,
    vx: int,
    vy: int,
    vw: int,
    heading_deg: float,
    t,
) -> None:
    canvas.fill_rect(vx + 8, vy + 8, vw - 16, 30, t.bg)
    canvas.draw_rect_border(vx + 8, vy + 8, vw - 16, 30, t.border, thickness=1)
    cx = vx + vw // 2
    labels = [
        (0, "N"),
        (45, "NE"),
        (90, "E"),
        (135, "SE"),
        (180, "S"),
        (225, "SW"),
        (270, "W"),
        (315, "NW"),
    ]
    px_per_deg = (vw - 40) / 140.0
    for deg, name in labels:
        diff = ((deg - heading_deg + 180.0) % 360.0) - 180.0
        if abs(diff) <= 64.0:
            tx = round(cx + diff * px_per_deg)
            col = t.danger if name == "N" else t.accent
            canvas.draw_text_centered(tx, vy + 23, name, col, scale=2)
    # Center needle
    canvas.fill_rect(cx - 1, vy + 8, 3, 30, t.text)


def render_scanner_3d(
    canvas,
    state: Scanner3DState,
    snap: SensorSnapshot,
    t,
    now: float | None = None,
) -> dict:
    """Render the 3D wireframe tactical scanner viewport at (24, 294, 672, 460)."""
    if now is None:
        now = time.monotonic()

    dt = max(0.01, min(0.25, now - state._last_tick))
    state._last_tick = now
    state.sweep_angle = (state.sweep_angle + dt * 2.1) % (2.0 * math.pi)
    state.emf_history.append(snap.mag_total_ut)

    vx, vy, vw, vh = 24, 294, 672, 460
    cx = vx + vw // 2
    cy = vy + vh // 2 + 24

    canvas.fill_rect(vx, vy, vw, vh, t.bg)
    canvas.draw_rect_border(vx, vy, vw, vh, t.border, thickness=2)
    # Tactical corner brackets
    for bx, by, dx, dy in (
        (vx, vy, 1, 1),
        (vx + vw - 1, vy, -1, 1),
        (vx, vy + vh - 1, 1, -1),
        (vx + vw - 1, vy + vh - 1, -1, -1),
    ):
        canvas.fill_rect(bx if dx > 0 else bx - 22, by if dy > 0 else by - 2, 22, 3, t.accent)
        canvas.fill_rect(bx if dx > 0 else bx - 2, by if dy > 0 else by - 22, 3, 22, t.accent)

    _draw_compass_tape(canvas, vx, vy, vw, snap.heading_deg, t)

    if state.imu_mode:
        yaw = math.radians(snap.heading_deg)
        pitch = math.radians(max(12.0, min(58.0, 22.0 + abs(snap.pitch_deg) * 0.55)))
    else:
        yaw = (now * 0.55) % (2.0 * math.pi)
        pitch = math.radians(28.0)

    events = {
        "sonar_hit": False,
        "emf_alert": snap.mag_total_ut >= 75.0,
        "prox_alert": snap.prox_blocked,
        "geiger_interval_s": None,
    }

    if state.submode == "rf3d":
        _render_rf3d(canvas, state, yaw, pitch, cx, cy, t, now, events)
    elif state.submode == "emf":
        _render_emf3d(canvas, state, snap, yaw, pitch, cx, cy, vx, vy, vw, vh, t, events)
    else:
        _render_geiger(canvas, state, vx, vy, vw, vh, t, events)

    # Beam / Shadow alert overlays
    if snap.prox_blocked:
        canvas.fill_rect(vx + 64, vy + 46, vw - 128, 34, t.danger)
        canvas.draw_rect_border(vx + 64, vy + 46, vw - 128, 34, t.text, thickness=2)
        canvas.draw_text_centered(cx, vy + 63, "PROX BEAM BLOCKED!", t.text, scale=2)
    elif now < state.shadow_alert_until:
        canvas.fill_rect(vx + 64, vy + 46, vw - 128, 34, t.warn)
        canvas.draw_text_centered(
            cx, vy + 63, f"RF SHADOW: {state.shadow_source}", t.bg, scale=2
        )

    return events


def _render_rf3d(canvas, state: Scanner3DState, yaw: float, pitch: float, cx: int, cy: int, t, now: float, events: dict) -> None:
    scale = 410.0
    grid_col = t.border
    # 3D floor grid (-1.0 .. 1.0)
    for g in (-0.8, -0.4, 0.0, 0.4, 0.8):
        p1 = project_3d(g, 0.0, -0.8, yaw, pitch, cx, cy, scale)
        p2 = project_3d(g, 0.0, 0.8, yaw, pitch, cx, cy, scale)
        if p1 and p2:
            canvas.draw_line(p1[0], p1[1], p2[0], p2[1], grid_col, 1)
        p3 = project_3d(-0.8, 0.0, g, yaw, pitch, cx, cy, scale)
        p4 = project_3d(0.8, 0.0, g, yaw, pitch, cx, cy, scale)
        if p3 and p4:
            canvas.draw_line(p3[0], p3[1], p4[0], p4[1], grid_col, 1)

    # 3D range rings (r=0.45, 0.90)
    segs = 20
    for rad in (0.45, 0.90):
        pts = []
        for k in range(segs + 1):
            a = (k / segs) * 2.0 * math.pi
            pts.append(project_3d(math.sin(a) * rad, 0.0, math.cos(a) * rad, yaw, pitch, cx, cy, scale))
        for k in range(segs):
            if pts[k] and pts[k + 1]:
                canvas.draw_line(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], t.accent_dim, 1)

    # Rotating 3D sonar sweep beam
    origin = project_3d(0.0, 0.02, 0.0, yaw, pitch, cx, cy, scale)
    tip = project_3d(
        math.sin(state.sweep_angle) * 0.95, 0.02, math.cos(state.sweep_angle) * 0.95, yaw, pitch, cx, cy, scale
    )
    if origin and tip:
        canvas.draw_line(origin[0], origin[1], tip[0], tip[1], t.ok, 2)

    if not state.objects:
        canvas.draw_text_centered(cx, cy - 20, "TAP RESCAN RF FOR 3D TARGETS", t.text_dim, scale=2)
        return

    # Plot RF objects as 3D wireframe pyramids (Wi-Fi) or diamonds (BLE)
    for obj in state.objects:
        ox = math.sin(obj.azimuth_rad) * obj.radius
        oz = math.cos(obj.azimuth_rad) * obj.radius
        ang_diff = abs(((obj.azimuth_rad - state.sweep_angle + math.pi) % (2.0 * math.pi)) - math.pi)
        pinged = ang_diff < 0.24
        if pinged and state.last_ping_id != obj.obj_id:
            state.last_ping_id = obj.obj_id
            events["sonar_hit"] = True

        shadowed = abs(obj.rssi_delta) >= 8
        col = t.danger if shadowed else (t.text if pinged else (t.accent if obj.kind == "wifi" else t.ok))

        base_sz = 0.075
        apex = project_3d(ox, obj.height, oz, yaw, pitch, cx, cy, scale)
        base_c = project_3d(ox, 0.0, oz, yaw, pitch, cx, cy, scale)
        corners = [
            project_3d(ox - base_sz, 0.0, oz - base_sz, yaw, pitch, cx, cy, scale),
            project_3d(ox + base_sz, 0.0, oz - base_sz, yaw, pitch, cx, cy, scale),
            project_3d(ox + base_sz, 0.0, oz + base_sz, yaw, pitch, cx, cy, scale),
            project_3d(ox - base_sz, 0.0, oz + base_sz, yaw, pitch, cx, cy, scale),
        ]
        if apex and all(corners):
            for i in range(4):
                c1, c2 = corners[i], corners[(i + 1) % 4]
                canvas.draw_line(c1[0], c1[1], c2[0], c2[1], col, 2 if pinged else 1)
                canvas.draw_line(c1[0], c1[1], apex[0], apex[1], col, 2 if pinged else 1)
        if base_c and origin and pinged:
            canvas.draw_line(origin[0], origin[1], base_c[0], base_c[1], t.warn, 1)

        if apex and (pinged or obj.rssi_dbm >= -62):
            canvas.draw_text(
                min(540, max(34, apex[0] - 32)),
                max(345, apex[1] - 18),
                f"{obj.label[:9]} {obj.rssi_dbm}",
                t.text if pinged else col,
                scale=1,
            )


def _render_emf3d(canvas, state: Scanner3DState, snap: SensorSnapshot, yaw: float, pitch: float, cx: int, cy: int, vx: int, vy: int, vw: int, vh: int, t, events: dict) -> None:
    scale = 380.0
    o = project_3d(0.0, 0.0, 0.0, yaw, pitch, cx, cy - 30, scale)
    ax_pt = project_3d(0.75, 0.0, 0.0, yaw, pitch, cx, cy - 30, scale)
    ay_pt = project_3d(0.0, 0.75, 0.0, yaw, pitch, cx, cy - 30, scale)
    az_pt = project_3d(0.0, 0.0, 0.75, yaw, pitch, cx, cy - 30, scale)
    if o:
        if ax_pt:
            canvas.draw_line(o[0], o[1], ax_pt[0], ax_pt[1], t.border, 2)
            canvas.draw_text(ax_pt[0], ax_pt[1], "X", t.text_dim, scale=1)
        if ay_pt:
            canvas.draw_line(o[0], o[1], ay_pt[0], ay_pt[1], t.border, 2)
            canvas.draw_text(ay_pt[0], ay_pt[1], "Z", t.text_dim, scale=1)
        if az_pt:
            canvas.draw_line(o[0], o[1], az_pt[0], az_pt[1], t.border, 2)
            canvas.draw_text(az_pt[0], az_pt[1], "Y", t.text_dim, scale=1)

        # Scaled magnetic field vector in 3D
        mag_norm = max(0.15, min(1.0, snap.mag_total_ut / 110.0))
        inv = 1.0 / max(1.0, snap.mag_total_ut)
        tip = project_3d(
            snap.mag_x_ut * inv * mag_norm * 0.9,
            snap.mag_z_ut * inv * mag_norm * 0.9,
            snap.mag_y_ut * inv * mag_norm * 0.9,
            yaw,
            pitch,
            cx,
            cy - 30,
            scale,
        )
        v_col = t.danger if snap.mag_total_ut >= 75.0 else t.accent
        if tip:
            canvas.draw_line(o[0], o[1], tip[0], tip[1], v_col, 3)
            canvas.fill_rect(tip[0] - 4, tip[1] - 4, 8, 8, t.text)

    # Bottom EMF waveform chart
    gx, gy, gw, gh = vx + 20, vy + vh - 115, vw - 40, 96
    canvas.fill_rect(gx, gy, gw, gh, t.panel)
    canvas.draw_rect_border(gx, gy, gw, gh, t.border, thickness=1)
    canvas.draw_text(gx + 10, gy + 8, f"EMF FIELD: {snap.mag_total_ut:.1f} uT", t.danger if snap.mag_total_ut >= 75.0 else t.accent, scale=2)
    pts = list(state.emf_history)
    if len(pts) >= 2:
        max_u = max(90.0, max(pts))
        step_x = (gw - 20) / max(1, len(pts) - 1)
        for i in range(len(pts) - 1):
            x1 = round(gx + 10 + i * step_x)
            x2 = round(gx + 10 + (i + 1) * step_x)
            y1 = round(gy + gh - 8 - (pts[i] / max_u) * (gh - 38))
            y2 = round(gy + gh - 8 - (pts[i + 1] / max_u) * (gh - 38))
            canvas.draw_line(x1, y1, x2, y2, t.ok, 2)


def _render_geiger(canvas, state: Scanner3DState, vx: int, vy: int, vw: int, vh: int, t, events: dict) -> None:
    cx = vx + vw // 2
    if not state.objects:
        canvas.draw_text_centered(cx, vy + vh // 2, "TAP RESCAN RF TO LOCK TARGET", t.text_dim, scale=2)
        return
    tgt = state.objects[state.geiger_target_idx % len(state.objects)]
    strength = max(0.05, min(1.0, (tgt.rssi_dbm + 95) / 60.0))
    events["geiger_interval_s"] = max(0.18, 1.3 - strength * 1.1)

    canvas.draw_text_centered(cx, vy + 96, f"LOCK: {tgt.label} ({tgt.kind.upper()})", t.accent, scale=2)
    canvas.draw_text_centered(cx, vy + 150, f"{tgt.rssi_dbm} dBm", t.text, scale=4)
    canvas.draw_text_centered(cx, vy + 198, "TAP VIEWPORT TO SWITCH TARGET", t.text_dim, scale=1)

    # Proximity meter bar
    bx, by, bw, bh = vx + 40, vy + 224, vw - 80, 44
    canvas.fill_rect(bx, by, bw, bh, t.panel)
    canvas.draw_rect_border(bx, by, bw, bh, t.border, thickness=2)
    fill_col = t.danger if strength > 0.72 else (t.warn if strength > 0.42 else t.ok)
    canvas.fill_rect(bx + 4, by + 4, max(8, round((bw - 8) * strength)), bh - 8, fill_col)

    # Scrolling history plot
    gx, gy, gw, gh = vx + 40, vy + 290, vw - 80, 145
    canvas.fill_rect(gx, gy, gw, gh, t.panel)
    canvas.draw_rect_border(gx, gy, gw, gh, t.border, thickness=1)
    hist = list(state.geiger_history)
    if len(hist) >= 2:
        step_x = (gw - 16) / max(1, len(hist) - 1)
        for i in range(len(hist) - 1):
            x1 = round(gx + 8 + i * step_x)
            x2 = round(gx + 8 + (i + 1) * step_x)
            n1 = max(0.0, min(1.0, (hist[i] + 95) / 60.0))
            n2 = max(0.0, min(1.0, (hist[i + 1] + 95) / 60.0))
            y1 = round(gy + gh - 10 - n1 * (gh - 20))
            y2 = round(gy + gh - 10 - n2 * (gh - 20))
            canvas.draw_line(x1, y1, x2, y2, fill_col, 2)
