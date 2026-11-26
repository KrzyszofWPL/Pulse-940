"""Room BSSID + RSSI fingerprinting and automatic IR profile matching."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from pulse940.rf.wifi import WifiAp


@dataclass(frozen=True)
class RoomProfile:
    """Fingerprint definition for a single room."""

    name: str
    ir_profile: str
    bssids: dict[str, int]  # bssid (lowercase) -> expected RSSI dBm


@dataclass(frozen=True)
class RoomMatch:
    """Best-matching room for a given Wi-Fi scan."""

    room_name: str
    ir_profile: str
    score: float
    matched_bssids: int


def load_room_profiles(path: str) -> list[RoomProfile]:
    """Load room fingerprint definitions from JSON."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rooms: list[RoomProfile] = []
    for item in raw.get("rooms", []):
        bssids = {k.lower(): int(v) for k, v in item.get("bssids", {}).items()}
        rooms.append(
            RoomProfile(
                name=str(item["name"]),
                ir_profile=str(item["ir_profile"]),
                bssids=bssids,
            )
        )
    return rooms


def match_room(
    aps: list[WifiAp],
    rooms: list[RoomProfile],
    min_score: float = 30.0,
) -> RoomMatch | None:
    """Match observed Wi-Fi APs against known room fingerprints."""
    observed = {ap.bssid.lower(): ap.rssi_dbm for ap in aps}
    best: RoomMatch | None = None

    for room in rooms:
        if not room.bssids:
            continue
        matched = 0
        total_score = 0.0
        for bssid, expected_rssi in room.bssids.items():
            if bssid in observed:
                matched += 1
                diff = abs(observed[bssid] - expected_rssi)
                total_score += max(0.0, 100.0 - diff * 2.0)

        if matched == 0:
            continue
        avg_score = total_score / len(room.bssids)
        if avg_score >= min_score and (best is None or avg_score > best.score):
            best = RoomMatch(
                room_name=room.name,
                ir_profile=room.ir_profile,
                score=round(avg_score, 1),
                matched_bssids=matched,
            )
    return best


def find_default_rooms_file(base_dir: str = "profiles") -> str | None:
    """Prefer rooms.local.json if present, otherwise rooms.example.json."""
    for name in ("rooms.local.json", "rooms.example.json"):
        candidate = os.path.join(base_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None
