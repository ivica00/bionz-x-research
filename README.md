# Sony BIONZ X Universal Emulator, Modding & Custom OS Platform

An open, evidence-backed reverse-engineering, emulation (**QEMU** + **Unicorn**), and runtime modding platform for Sony **BIONZ X (`CXD90045`)** cameras—starting with **`ILCE-6400`** and scaling across 13 mapped camera bodies.

**You don't need a camera to contribute.** Roughly 90% of the work is offline Python & C. Every claim below is strictly labelled by what was actually demonstrated (offline emulation, read-only observation, or physical RAM-only run), and nothing is presented as a percentage of "full firmware compatibility."

> **[📦 Install & Use Camera Mods (Mod Loader v2, Touch Menu, ARRI Look)](mods/README.md)** · **[🖥️ Run QEMU Emulator (`sony-a6400`)](qemu-a6400/README.md)** · **[🔬 Develop & Hot-Reload Mods in QEMU](qemu-a6400/MOD_DEVELOPMENT.md)** · **[📷 Add a BIONZ X Camera](docs/ADDING_A_BIONZX_CAMERA.md)** · **[🎥 UVC / PTP Liveview](docs/CONTRIBUTING_UVC_PTP.md)**

---

## Project Goals

| Goal | What it means | Current milestone |
| :--- | :--- | :--- |
| **1 · Emulate Every BIONZ X Model** | Run original bootloaders, Linux kernels (`ARM-CXD900X0`), and `im.elf` UI/ISP stacks offline in **QEMU** and **Unicorn** across the `CXD90045` family | **13 bodies / 9 kernels mapped** ([`PLATFORM_MODEL.md`](docs/PLATFORM_MODEL.md)); **8 registered profiles** (`ILCE-6400` verified execution target + 7 sourced reference profiles, [E0570](docs/evidence/0570-ilce-7rm4a-offline-sourced-profile.md)) |
| **2 · Universal Modding Across Models** | Position-independent (`SMOD v2`) runtime mods with per-library relocation (`--reloc-layout`), hot-reloadable in QEMU and RAM-only on physical cameras | **Mod Loader v2 (`MLCR`)** runs in QEMU ([E0577](docs/evidence/0577-qemu-real-mods-relocation-and-hot-reload.md)) and `ILCE-6400` RAM ([E0555](docs/evidence/0555-arrivederci-menu-exit-recompute-and-cc8-refill.md)); ships **MTF2 Touch** ([E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md)), **Arrivederci ARRI Look** ([E0556](docs/evidence/0556-arrivederci-classic-fitted-matrix.md)), and **Open Gate 3:2** feasibility ([E0578](docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md)) |
| **3 · Custom OS & Open Camera Stack** | Replace closed camera bottlenecks with open, verified kernel/userspace modules—native composite USB webcam (UVC+PTP), persistent WinUSB debugging, custom OSD/ISP pipelines, and an open replacement OS layer | **USB Debugger v2** ([E0401](docs/evidence/0401-usb-debugger-v2-ring-recover-and-mode-boundary.md)), **29.8 FPS PTP Liveview** ([E0349](docs/evidence/0349-p1-foreign-get-jpeg-succeeds-in-pc-remote.md)), **Composite UVC+PTP enumeration** & EP0 pre-arm bridge ([E0476](docs/evidence/0476-status-prearm-fix-build-audit-unicorn.md)) |

---

## Emulation Architecture & Progress: QEMU vs. Unicorn

This project uses two complementary emulators:

- **QEMU ([`qemu-a6400/`](qemu-a6400/README.md)) — Full-System Board (`sony-a6400`):** Out-of-tree QEMU v10.1.0 machine emulating 4× Cortex-A5 SMP cores, native eMMC partitions, 40 kernel modules, `av-cam.bin` (LIRO RTOS), `im.elf`, Sugilite P3D/PPF OSD, and an interactive browser rear panel (`rear_panel.py`) with live mod hot-reload.
- **Unicorn (`src/a6400/`) — Deterministic Contract & Instruction Engine:** Fast Python + Unicorn harness split into **L2** (`UnicornMachine` kernel boot & MMU/TLB), **L3** (`InterpreterProbe` user-space syscall/IPC/thread engine), and **isolated ARM/Thumb unit harnesses** for verifying mods and kernel patches in CI without booting a full VM.

### Compact Stage Matrix

Legend: `🟢 Verified` · `🟡 Partial / Opt-in` · `⚪ Open Boundary` · `⛔ Not Supported`

| Stage / Subsystem | QEMU (`sony-a6400` Full Board) | Unicorn (`L2` Kernel / `L3` Userspace) | Key Evidence & Docs |
| :--- | :--- | :--- | :--- |
| **CPU, SMP & Boot** | 🟢 4× Cortex-A5 SMP, `kemco` handoff, CXD4115 timers | 🟢 L2 boots `vmlinux.bin` (`0xDA9`), MMU/TLB, reaches `ld-2.11.2.so` & BusyBox `ash` | [E0522](docs/evidence/0522-qemu-a6400-smp-four-cores-and-cxd4115-timers.md), [E0510](docs/evidence/0510-nflasha1-busybox-ash-ttyam0-shell.md), [E0560](docs/evidence/0560-unicorn-nonsecure-af-exception-return.md) |
| **Storage & Mounts** | 🟢 18 captured partitions, CMD1 sector mode, qcow2 overlay | 🟢 Read-only ext2 (`nflasha7`/`15`), FAT12 `/setting` (`nflasha2`), 88 devnodes | [E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md), [E0307](docs/evidence/0307-nflasha2-setting-namespace-uipc-unregister-and-idle-state.md) |
| **RTOS (`av-cam` / LIRO)** | 🟡 40 `.ko` loaded, `entry_main`, `VD_Sequence` reaches step 32 (`EVSEL_LATE_US=500`) | 🟢 Static & slice execution of CC 3D-LUT, EC Gamma, DMM & Open Gate 3:2 tables | [E0557](docs/evidence/0557-qemu-a6400-hs-root-order-passes-step-12.md), [E0505](docs/evidence/0505-av-cam-cc-3dlut-and-ec-gamma-pipeline.md), [E0578](docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md) |
| **UI & Display (`im.elf`)** | 🟢 Sugilite P3D/PPF renders EVF OSD & Rear-LCD Menu (`640×480`) | 🟡 L3 spawns 88+ threads (`Appframework`, `gui.so`); stops at `stella` heap boundary | [E0536](docs/evidence/0536-qemu-a6400-viewfinder-readout-and-eye-sensor-command.md), [E0572](docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md), [E0314](docs/evidence/0314-gpes-bar-irq-contract-engine-model.md) |
| **Inputs & Touch** | 🟢 Web Rear Panel (`ADC`, `GPIO`, sub-micro, wheel, `MENU`); ⛔ **No touch panel** | 🟢 Isolated harnesses for touch press/release (`MTF2`) & menu vtables | [E0573](docs/evidence/0573-qemu-a6400-rear-panel-controls.md), [E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md) |
| **Mod Loader & Mods** | 🟢 `Mod Loader v2` installs in guest `im.elf`, relocates & hot-reloads `.mod` files | 🟢 10-scenario `MLCR` core + `SMOD v2` relocation & trampoline checks | [E0577](docs/evidence/0577-qemu-real-mods-relocation-and-hot-reload.md), [E0576](docs/evidence/0576-mod-loader-v2-reload-request.md) |
| **Live Sensor / YUV** | ⚪ VPH ch51 programmed; parked at step 32 (`PQD_Ydet` / `CamPig` IRQ 35→36) | ⚪ Sensor feeder unwired; DMM `0x0167046a` JPEG queue contract decoded | [E0561](docs/evidence/0561-qemu-a6400-module3-waits-on-ydet-statistics.md), [E0569](docs/evidence/0569-qemu-sgc1-event-a.md), [E0349](docs/evidence/0349-p1-foreign-get-jpeg-succeeds-in-pc-remote.md) |
| **USB, Wi-Fi/BT, HDMI, Lens** | ⛔ **Not supported** (no DWC3 gadget, `BCM4339`, HDMI out, lens, or audio) | 🟡 Offline USB/EP0 descriptor & callback contracts only; ⛔ no radio/HDMI/lens | [E0337](docs/evidence/0337-emulator-uvc-ptp-preparation-and-0a4002-broadcast.md), [E0406](docs/evidence/0406-bcm4339-wifi-bt-tuning-surface-readonly.md), [E0476](docs/evidence/0476-status-prearm-fix-build-audit-unicorn.md) |

### What Is Not Supported Yet

- **Live camera sensor, shutter & recording (QEMU & Unicorn):** No live YUV/RAW sensor stream, AF/AE loop, still capture, or movie recording to SD (QEMU stops at mode-sequencer step 32; see [QEMU checkpoint](qemu-a6400/STATUS.md)).
- **Touch-panel hardware in QEMU:** The `sony-a6400` board has no emulated touch controller, so touch mods (`MTF2`) cannot be exercised via clicks in QEMU ([`MOD_DEVELOPMENT.md`](qemu-a6400/MOD_DEVELOPMENT.md)).
- **USB Gadget (UVC / PTP / MSC) in QEMU:** Live USB host-to-camera enumeration and streaming are not emulated in QEMU; USB/UVC contracts are verified via Unicorn and physical hardware runs.
- **Peripherals & Radios (QEMU & Unicorn):** Wi-Fi / Bluetooth (`BCM4339`), HDMI output, E-mount lens communication, audio I/O, and hardware watchdog (`0xF2402000`) are unmodelled.
- **Full Unicorn L3 GUI & Non-a6400 Boot:** `InterpreterProbe` (Unicorn L3) does not yet produce a GUI frame (`stella` heap & GPES barriers), and only `ILCE-6400` is a verified execution target (other `CXD90045` bodies are reference profiles).

---

## Universal Modding & Available Mods

**[Mod Loader v2 (`MLCR`)](docs/MOD_LOADER_V2_DESIGN.md)** installs a tiny permanent RAM shim in `im.elf` (`viewUnified2.so` hooks) and loads a position-independent core (`core.bin`) plus relocatable **`SMOD v2`** `.mod` packages—requiring **zero flash writes** and restoring stock behavior on reboot. Using `--reloc-layout`, a single `.mod` binary shifts its library addresses at load time to run across both QEMU and physical camera memory layouts ([E0577](docs/evidence/0577-qemu-real-mods-relocation-and-hot-reload.md)).

| Mod | What it does | Status |
| :--- | :--- | :--- |
| **[MenuTouchFull (`MTF2`)](mods/menu_touch_full/README.md)** | Full-menu touch navigation: tabs, page dots, swipe paging, row select/open, popup scroll & apply | Verified on `ILCE-6400` ([E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md)); `SMOD v2` usable & hot-reloads in QEMU |
| **[Arrivederci](mods/arri_isp/arrivederci/README.md)** | ARRI LogC3 Classic tone curve + fitted Q10 color matrix for S-Log3 / S-Gamut3.Cine | Usable in Modset v2 ([E0555](docs/evidence/0555-arrivederci-menu-exit-recompute-and-cc8-refill.md), [E0556](docs/evidence/0556-arrivederci-classic-fitted-matrix.md)); turn OFF before power-off |
| **ModsTabLook** | Renders the custom `MODS` tab icon artwork (`E0522`) | Auto-enabled in QEMU mod-dev mode ([E0522](docs/evidence/0522-mods-tab-icon-and-uxd-image-container.md)) |
| **Open Gate (3:2)** | Staged `IMX284` 3:2 (`1620×1080` / `3240×2160`) recording path via `DMM` & `ZIMA DVENC` | Offline feasibility mapped ([E0578](docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md)); untested on hardware |

👉 **[Read `mods/README.md` for step-by-step installation (QEMU & physical `ILCE-6400`)](mods/README.md)**

---

## Multi-Camera Support (`CXD90045` Platform)

Registered profiles live in [`src/a6400/targets/`](src/a6400/targets/catalog.py) and share the cross-body `CXD90045` model ([`PLATFORM_MODEL.md`](docs/PLATFORM_MODEL.md)). Guide: **[Adding a BIONZ X camera](docs/ADDING_A_BIONZX_CAMERA.md)**.

| Body | Kernel Group | Status | Notes / Evidence |
| :--- | :--- | :--- | :--- |
| **`ILCE-6400`** | `a6400` / `a6600` source | **Verified Execution Target** | Full QEMU & Unicorn L2/L3 execution, physical RAM-only mods |
| **`ILCE-6400A`** | `1.01` packaged | **Sourced Reference** | Native `GetModelInfo` (`0032858730`), `libJiritsu.so` `GET_FILE` ([E0506](docs/evidence/0506-a6400a-offline-qualification-and-debugger-boundary.md), [E0554](docs/evidence/0554-a6400a-v101-jiritsu-command-dispatch.md)) |
| **`ILCE-6600`** | `a6400` / `a6600` source | **Sourced Reference** | Identical 14,401 kernel function symbols; `0x413b` command delta |
| **`ZV-E10`**, **`ILCE-7C`** | Shared build | **Sourced Reference** | Identical 14,460 kernel function symbols; `ZV-E10` UVC stack cataloged ([E0321](docs/evidence/0321-zve10-uvc-enablement-manifest.md)) |
| **`ILCE-6100`**, **`ILCE-7M3`**, **`ILCE-7RM4A`** | Shared symbol list | **Sourced Reference** | `ILCE-7RM4A` 1.13 gadget-core symbols decoded ([E0570](docs/evidence/0570-ilce-7rm4a-offline-sourced-profile.md)) |

---

## Live Status Telemetry

Recorded offline suite: **1992 tests** (latest evidence: [E0578](docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md)). Earlier narrative highlights are preserved in [docs/archives/readme-latest-progress-2026-11-18.md](docs/archives/readme-latest-progress-2026-11-18.md) and [docs/archives/readme-latest-progress-2026-11-15.md](docs/archives/readme-latest-progress-2026-11-15.md).

![Research status: demonstrated results and remaining boundaries](docs/images/research-status.svg?v=e5948c71e8d8)

![Daily automated test suite growth: 1992 tests across 31 days](docs/images/test-growth.svg?v=9bd7ed84b026)

---

## How to Help (No Camera Needed)

**Top 3 open tasks right now:**

1. **Switch to the rear LCD through stock firmware input (QEMU):** The original LCD layout renders on console 0 with a diagnostic overlay ([E0572](docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md)). Find the `InputService::getInputEventStatus` key/value that selects it without the overlay, or unblock `CamPig` IRQ 35→36 / `PQD_Ydet` at mode-sequencer step 32 ([E0569](docs/evidence/0569-qemu-sgc1-event-a.md), [QEMU checkpoint](qemu-a6400/STATUS.md)).
2. **Decode the USB control callback packet format (Unicorn / UVC):** Determine the remaining message layout so an incoming USB configuration request reaches the firmware's USB state machine ([`usb_sync_callback.py`](src/a6400/execution/usb_sync_callback.py), [E0332](docs/evidence/0332-sync-callback-delivery-first-invocation.md)).
3. **Unblock the GPU completion signal (Unicorn L3):** Resolve the cross-thread wait condition in the emulated graphics driver so the display path advances on its own ([`guest_gles.py`](src/a6400/execution/guest_gles.py)).

More guides: [Contributing to UVC / PTP](docs/CONTRIBUTING_UVC_PTP.md) · [UVC/PTP progress log](docs/UVC_PTP_STATUS.md) · [USB Debugger v2](docs/NATIVE_USB_DEBUGGER.md).

---

## Quick Start

Requires Python 3.10+ and [`requirements.txt`](requirements.txt) (no camera needed):

```powershell
# Windows (PowerShell)
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m unittest discover -s tests -q
```

```bash
# Linux / macOS
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -q
```

| What to check | Command |
| :--- | :--- |
| Full offline suite (1992 tests) | `python -m unittest discover -s tests -q` |
| Structure, profiles & doc sync | `python -m unittest tests.test_repository_layout tests.test_target_profiles tests.test_research_status_sync -v` |
| Status card matches data | `python -m tools.research.render_research_status --check` |
| Windows launchers | [`scripts/`](scripts/) (`launch_cinemahud_ptp.bat`, `launch_cinemahud_android.ps1`, `launch_cinema_mpv.bat`, `start_camera_shell.bat`) |

Run scripts as Python modules from the repo root (`python -m tools.research.<tool>`). Firmware images, backups, and captured artifacts stay local and are excluded from Git.

---

## Repository Layout & Safety Rules

| Folder | What's inside |
| :--- | :--- |
| [`qemu-a6400/`](qemu-a6400/README.md) | Out-of-tree QEMU v10.1.0 `sony-a6400` machine, browser rear panel & mod-dev hot-reload |
| [`mods/`](mods/README.md) | RAM-only camera mods (`MenuTouchFull`, `Arrivederci`, `ModsTabLook`) & install guide |
| `src/a6400/execution/` | Unicorn L2/L3 ARM emulator and verified OS, USB, and GPU contracts |
| `src/a6400/targets/` | Device profiles and validation for supported Sony `CXD90045` cameras |
| `src/a6400/debug/` | USB debugger protocol, log ring buffer, and WinUSB transport |
| `tools/research/` & `tools/native_camera/` | Firmware analysis scripts, Mod Loader v2 (`mlv2/`), C helpers, and status-card generator |
| `probes/` | Safe, read-only ARM probe for other BIONZ X cameras |
| [`a6400-lut-converter/`](a6400-lut-converter/README.md) | Offline ISP table fitting, ARRI presets, and 8-bit 4:2:0 preview |
| `tests/`, `docs/`, `docs/evidence/` | Automated contract checks, architecture notes, and dated evidence logs |
| `android/`, `camera-daemon/` | Android liveview client (`CinemaHUD`) and camera-side composite USB webcam daemon |
| `firmware/`, `backups/`, `artifacts/` | Local firmware dumps and generated outputs (never committed to Git) |

**Safety & Epistemic Rules:**
- **Honest claims only:** Host-written UI or camera logic is **not** firmware compatibility. Every claim links to a dated evidence record.
- **No flash writes:** Never modify internal camera flash, install custom firmware, or alter boot partitions.
- **Clean-room emulation:** Original firmware owns application logic; unknown OS services stop and report where they stopped instead of faking success.
- **Safe hardware checks:** Read-only camera inspection is allowed; writes, mode switches, and module loads require explicit scope. Bulk `/dev/mem` memory sweeps are banned ([`AGENTS.MD`](AGENTS.MD)).

---

## Links & Attribution

[Documentation index](docs/INDEX.md) · [Current state](docs/CURRENT_STATE.md) · [Evidence index](docs/evidence/README.md) · [Related research repositories](docs/RELATED_REPOSITORIES.md) · [Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Contributing](CONTRIBUTING.MD) · [Repository layout](docs/REPOSITORY.md) · [Agent rules](AGENTS.MD) · [Archived README](docs/archives/README-2026-11-09.md)

- **Cross-generation BIONZ X research:** [Oein/a5000-emulator](https://github.com/Oein/a5000-emulator) by Park SungHyun (Oein) — clean-room reference for DMP SMAPH/P3D command-list format and 8×8 Morton tiling (`docs/RE_GPU_P3D_CMD.md`, CC BY 4.0), `GICD_ITARGETSR` bootloader pre-routing (`docs/HARDWARE.md`), and `im.elf` UI bringup gates (`docs/RE_UI_BRINGUP.md`).

Independent research; not affiliated with or endorsed by Sony. Sony, a6400, ILCE-6400, and BIONZ X are trademarks of their respective owners.
