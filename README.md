# Sony BIONZ X Mods & Camera Emulator

Reversible mods for Sony **BIONZ X** cameras (a6400, a6400A, a6100, ZV-E10), and emulators
that boot the real camera firmware on a PC. Every claim below links to a dated evidence record.

**[Install mods](#install-in-5-minutes)** · **[What works](#what-works-today)** · **[Is it safe?](#is-it-safe-how-do-i-undo-it)** · **[Help us test](#help-us-test)** · **[Developers](docs/DEVELOPERS.md)** · **[All docs](docs/INDEX.md)**

---

## What works today

Status labels ([definitions](docs/STATUS_LABELS.md)): **Verified on camera** · **Emulator only** (Unicorn/QEMU) · **Not run on a camera yet**.

| Mod | What it does | Status |
| :--- | :--- | :--- |
| [MenuTouchFull](mods/menu_touch_full/README.md) | Tap tabs, swipe pages and use dropdowns in Sony's menu with the touchscreen | **Verified on camera:** `ILCE-6400` ([E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md)), `ZV-E10` by direct RAM injection ([guide](tools/native_camera/zve10/README.md)) |
| [SilentShutterUnlock](mods/silent_shutter_unlock/README.md) | Picture Profile, Picture Effect, Auto HDR/DRO, Multi Frame NR and Auto/SCN while **Silent Shooting** is on | **Verified on camera:** `ILCE-6400` 2.00, RAM-only |
| [Arrivederci](mods/arri_isp/arrivederci/README.md) | ARRI LogC3 Classic tone curve and fitted color matrix for PP S-Log3 / S-Gamut3.Cine | **Verified on camera:** `ILCE-6400` RAM stage ([E0556](docs/evidence/0556-arrivederci-classic-fitted-matrix.md)). **Turn it OFF before power-off** (see below) |
| PhotonBin 6MP | 6 MP low-light 16-bit `.ARW` with high conversion gain | **Verified on camera:** `ILCE-6100` ([E9104](docs/evidence/9104-a6100-photonbin-6mp-port.md)); **Emulator only** on `ILCE-6400A` ([E0680](docs/evidence/0680-photonbin-6mp-and-mlv2-nested-submenus.md)) |
| [Rear LCD 60 fps](docs/evidence/0740-rear-lcd-60fps-high-quality-mod.md) | 59.94 fps live view on the rear LCD with Display Quality High | **Emulator only** ([E0740](docs/evidence/0740-rear-lcd-60fps-high-quality-mod.md)) |
| [WiFiSync](mods/wifisync/README.md) | Background `.JPG`/`.ARW` transfer over 5 GHz Wi-Fi | **Not run on a camera yet:** camera half verified offline ([E0698](docs/evidence/0698-wifisync-camera-side-mod.md)); the 5 GHz AP gate is open |
| Film Simulations | Picture Profile film looks built in [BIONZ Look Lab](docs/BIONZ_LOOK_LAB.md) | **Not run on a camera yet:** looks fitted offline ([E0682](docs/evidence/0682-still-jpeg-film-look-domain-and-recipe-recalibration.md)) |
| [Open Gate 3:2](mods/open_gate/README.md) | Full-sensor 3:2 movie recording | **Emulator only:** Unicorn + QEMU ([E0741](docs/evidence/0741-open-gate-32-mode10-smod-v2.md)). Live RAM tests on `ILCE-6400` restore cleanly but no 3:2 clip is recorded yet ([E0752](docs/evidence/0752-a6400-open-gate-a12-to-a19-dual-pipe-live.md)). Guarded [test kit](mods/opengate_a1/kit/README.md): **not run on a camera yet**; its MP4 hooks need a rebuild first ([E0756](docs/evidence/0756-opengate-a1-libmpr-hook-sites-and-guarded-test-kit.md)) |

More mods, formats and per-build notes: [mods/README.md](mods/README.md).

### Cameras

| Camera | Level |
| :--- | :--- |
| `ILCE-6400` | Mods and SD auto-boot verified on camera; full QEMU + Unicorn emulation |
| `ILCE-6400A` | Mods and SD auto-boot verified in QEMU ([`mods/ilce_6400a/`](mods/ilce_6400a/README.md)) |
| `ILCE-6100` | PhotonBin 6MP and Mod Loader v2 SD boot verified on camera |
| `ZV-E10` | Touch menu by direct RAM injection verified on camera; Mod Loader untested |
| `ILCE-7C` | Experimental `sony-a7c` QEMU machine and `mlv2 --target a7c` port ([E9203](docs/evidence/9203-ilce-7c-qemu-and-mlv2-port.md), [E9204](docs/evidence/9204-ilce-7c-menu-after-liveview-and-focus-bracket.md)) |
| `ILCE-6600` · `ILCE-7M3` · `ILCE-7RM4A` | Reference profiles: symbols and USB IDs mapped offline, waiting for testers |
| `ILCE-6700` (BIONZ XR) | Research only: its kernel boots on a new QEMU machine ([E9202](docs/evidence/9202-a6700-qemu-first-boot.md)) |

Another Sony body? See [Adding a BIONZ X camera](docs/ADDING_A_BIONZX_CAMERA.md).

---

## Install in 5 minutes

Windows, `ILCE-6400` / `ILCE-6400A` / `ILCE-6100`, no coding:

1. Install [Python 3.11+](https://www.python.org/downloads/) with **"Add python.exe to PATH"** (or use a `BionzX-Mod-Manager.exe` release).
2. Double-click [`scripts/launch_mod_manager.bat`](scripts/launch_mod_manager.bat) (macOS/Linux: [`scripts/launch_mod_manager.sh`](scripts/launch_mod_manager.sh)).
3. Click **Download Official Release** when asked.
4. On the camera: `MENU → Setup → USB Connection` → **Mass Storage**. Connect USB and click **Scan Camera**.
5. Switch the mods you want **ON**, click **1-Click Install to Camera**, unplug, and open `MENU → Mods`.

Full guide: [Mod Manager](docs/MOD_MANAGER_GUI.md).

---

## Is it safe? How do I undo it?

- **Nothing is flashed.** Mods live in camera RAM through Mod Loader v2. Internal firmware partitions are never written.
- **Power-off normally returns the camera to stock.** One exception: Sony saves some settings to its own backup at power-off. **Arrivederci's color matrix is one of them**, so turn Arrivederci **OFF** in `MENU → Mods` before you switch the camera off. A battery pull does not undo it. If you forget, follow the [verified recovery](mods/arri_isp/arrivederci/README.md#color-matrix-persistence-reversible-by-design): ON, OFF, a Color Mode round trip, then a normal power-off.
- **Each camera and firmware build gets its own loader and mods.** Never copy `.mod` files between bodies.
- **Read-only first.** Diagnostic tools read; anything that writes says so and asks first. Bulk `/dev/mem` scans that can reboot a camera are banned ([AGENTS.MD](AGENTS.MD)).

---

## Help us test

Most rows above say **Not run on a camera yet** because nobody has run them on that body.
If you have the camera:

1. Run the mod or kit as its README says.
2. Open a **[Physical test report](.github/ISSUE_TEMPLATE/physical-test-report.yml)** issue: camera, firmware version, kit commit, what each step printed, what the screen showed, and the log zip.
3. A failed run is as useful as a working one. Say exactly what happened.

---

## For developers

About 90 % of the work runs offline: a QEMU machine family that boots captured firmware to the real
Sony menu ([`emulators/qemu-a6400/`](emulators/qemu-a6400/README.md)) and a fast Unicorn engine
that checks mods and OS contracts in CI ([`src/a6400/`](src/a6400/README.md)). Start with
[docs/DEVELOPERS.md](docs/DEVELOPERS.md); USB video work is in [UVC / PTP](docs/CONTRIBUTING_UVC_PTP.md).

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -q
```

The last recorded full offline suite had 2091 tests (2026-11-18, [`docs/TOOLCHAIN.md`](docs/TOOLCHAIN.md)); skips are never counted as passes.

![Research status: demonstrated results and remaining boundaries](docs/images/research-status.svg?v=a45ea55b11fb)

[Test suite growth over time](docs/images/test-growth.svg) · [Current technical state](docs/CURRENT_STATE.md) · [Knowledge map](docs/map/README.md)

### Repository map

| Folder | What's inside |
| :--- | :--- |
| [`mods/`](mods/README.md) | Camera mods and install guides |
| [`apps/`](apps/README.md) | BIONZ Look Lab, LUT converter, Android clients, camera daemon |
| [`emulators/`](emulators/README.md) | QEMU machines (`sony-a6400` family, a6700, a6000) |
| `src/a6400/` | Python package: Unicorn engine, camera profiles, Mod Manager logic, USB debugger |
| `tools/` | Research tools (`python -m tools.research.<tool>`), Mod Manager UI, native helpers |
| `tests/` | Offline test suite |
| `scripts/` | Launchers and build scripts (Windows + macOS/Linux) |
| [`research/`](research/README.md) | Preserved prototypes, read-only probes, telemetry captures |
| [`docs/`](docs/INDEX.md) | Guides, [knowledge map](docs/map/README.md), [evidence records](docs/evidence/README.md) |

Layout details: [docs/REPOSITORY.md](docs/REPOSITORY.md).

---

## Credits

- [ma1co/Sony-PMCA-RE](https://github.com/ma1co/Sony-PMCA-RE): the USB service-mode switch the Mod Manager builds on.
- [ma1co/fwtool.py](https://github.com/ma1co/fwtool.py) and [DavidBuchanan314/fwtool.py](https://github.com/DavidBuchanan314/fwtool.py): firmware unpacking.
- [Oein/a5000-emulator](https://github.com/Oein/a5000-emulator) by Park SungHyun (Oein): clean-room reference for DMP SMAPH/P3D GPU commands, interrupt pre-routing and `im.elf` UI bring-up.
- Everyone who sent physical test reports. More upstream work: [docs/RELATED_REPOSITORIES.md](docs/RELATED_REPOSITORIES.md).

Independent research; not affiliated with or endorsed by Sony. Sony, a6400, ILCE-6400 and BIONZ X
are trademarks of their respective owners. [Contributing](CONTRIBUTING.MD) · [Roadmap](docs/ROADMAP.md) · [Agent rules](AGENTS.MD)
