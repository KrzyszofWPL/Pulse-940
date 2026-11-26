# Pulse 940

A minimal, dedicated bare-metal Linux appliance for **ARM64 smartphones / Linux-capable mobile hardware** — built for advanced infrared (IR) transmission, remote Bluetooth RFCOMM control, and passive RF room-context detection.

**Zero Android. Zero APK. Zero heavy GUI.**

---

## Why Pure Linux?

Android imposes artificial limits that interfere with low-level hardware control:
- `ConsumerIrManager` frame buffer size restrictions
- Background process throttling (Doze Mode)
- Wi-Fi scan rate throttling
- Unnecessary graphical and background service overhead

Pulse 940 runs directly on a stripped **Alpine Linux / postmarketOS (`console`)** userspace with direct kernel-level access to `/dev/lirc0`, `evdev`, `BlueZ`, `iw`, and `/sys/class/`.

---

## Core Architecture

1. **Hardware Abstraction Layer (HAL)**
   - **Target Hardware (ARM64 Linux):** Native Linux LIRC (`/dev/lirc0`), `BlueZ` RFCOMM, `iw` wireless scanning, `evdev` hardware buttons, and `/sys/class/power_supply/` telemetry.
   - **QEMU Dev Environment (Windows):** `MockLircDevice` (microsecond timing array validator) + TCP bridge (`port 9000`) simulating remote Master–Slave control.

2. **Master–Slave Remote Control**
   - The device acts as a stationary **Slave** listening for JSON commands over **Bluetooth RFCOMM (`BlueZ`)**, even with the display blanked (Stealth mode).
   - Controlled remotely from a primary mobile device (**Master**) or a desktop CLI client during development.

3. **IR Engine & Macro System**
   - Direct Linux LIRC transmission without Android frame-length limits.
   - **Flipper-IRDB `.ir`** support: `type: raw` (projectors, long HVAC frames) and `type: parsed` with a custom **NEC / NECext (38 kHz)** encoder.
   - Stealth & automation modes:
     - Discrete input switching (`USB`, `Video`, `HDMI2`, `LAN`) + safe `REVERT` (`HDMI1`)
     - `Freeze` and `AV_Mute`
     - `Auto Double-Tap Power` confirmation
     - `Scheduled TX` (delayed execution)
     - `Loop / Keep-Alive` (countering Auto Source Search)
     - `Probe & Lock` (fast brand/protocol discovery)

4. **Passive Room Context Detection**
   - Passive **Wi-Fi BSSID + RSSI fingerprinting** (via `iw`, without associating to any network) to automatically identify rooms and load matching device profiles.
   - Passive SSID / BLE brand radar.

5. **Boot-to-Tool Monochrome TUI**
   - Automatic boot (`tty1` autologin via OpenRC) directly into a full-screen monochrome TUI (black background, amber/green).
   - Live power telemetry (`/sys/class/power_supply/`), real-time PWM waveform visualizer from microsecond arrays, and live CLI event logs.
   - Physical button navigation via `evdev` (`Volume Up/Down`, `Power`) with instant screen blanking (Stealth mode) while the background daemon remains active.

---

## Local QEMU Development Environment (Windows)

The repository currently includes the **QEMU ARM64 (`aarch64`)** virtual machine launcher simulating the target environment (`cortex-a53`, 4 vCPU, 1 GB RAM, stripped Alpine Linux 3.24):

- **Interactive console:**
  ```powershell
  .\vm\start-vm.ps1
  ```
- **Headless mode (SSH on `localhost:2222`, TCP bridge on `localhost:9000`):**
  ```powershell
  .\vm\start-vm.ps1 -Headless
  ssh -p 2222 root@localhost
  ```

*(Large local VM images `.iso` / `.qcow2` / firmware files are excluded via `.gitignore`.)*

---

## Roadmap

- [x] QEMU `aarch64` + stripped Alpine Linux 3.24 base environment
- [ ] Hardware Abstraction Layer (`MockLircDevice` + TCP bridge)
- [ ] Flipper-IRDB `.ir` parser & NEC/NECext 38 kHz encoder
- [ ] Background daemon + JSON protocol (TCP / RFCOMM)
- [ ] Passive Wi-Fi BSSID/RSSI fingerprinting & room profiles
- [ ] Monochrome `tty1` TUI + `evdev` controls & live PWM visualizer
- [ ] Deployment to target ARM64 mobile hardware (`lk2nd` / mainline Linux + postmarketOS)
