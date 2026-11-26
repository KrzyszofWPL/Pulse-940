"""Download official Flipper Zero Universal Remote .ir asset bundles."""

from __future__ import annotations

import os
import urllib.request

BASE_URL = (
    "https://raw.githubusercontent.com/flipperdevices/flipperzero-firmware/"
    "dev/applications/main/infrared/resources/infrared/assets"
)
ASSETS = ("projector.ir", "tv.ir", "audio.ir", "ac.ir")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "profiles", "universal")


def fetch_assets(out_dir: str = OUT_DIR) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for name in ASSETS:
        url = f"{BASE_URL}/{name}"
        dest = os.path.join(out_dir, name)
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read()
        with open(dest, "wb") as fh:
            fh.write(data)
        written.append(dest)
        print(f"Downloaded {name} ({len(data)} bytes) -> {dest}")

    sources_md = os.path.join(out_dir, "SOURCES.md")
    with open(sources_md, "w", encoding="utf-8") as fh:
        fh.write(
            "# Universal IR Asset Sources\n\n"
            "The `.ir` universal remote bundles in this directory (`projector.ir`, `tv.ir`,\n"
            "`audio.ir`, `ac.ir`) are downloaded from the official Flipper Zero firmware\n"
            "repository:\n\n"
            f"- Source: `{BASE_URL}/` (`flipperdevices/flipperzero-firmware`)\n"
            "- Upstream License: GNU General Public License v3.0 (GPL-3.0)\n"
        )
    return written


if __name__ == "__main__":
    fetch_assets()
