# Sony a6400 Research & Emulator Platform

An open, evidence-backed research project for the **Sony a6400** (`ILCE-6400`, `CXD90045` chip).
We study how the camera's original software and Linux system work by running them safely
in an offline emulator and observing the real camera read-only—plus building low-latency
USB liveview and debugging tools.

**You don't need a camera to contribute.** Roughly 90% of the open work is offline Python.
Every result here is strictly labelled by what was actually shown (offline emulation,
read-only observation, or a real hardware test), and nothing is presented as a percentage
of "full firmware compatibility."

## Three main goals

| Goal | What it means | Current state |
| --- | --- | --- |
| **1 · Offline Firmware Emulator** | Run Sony's original camera software, bootloader, and Linux kernel on a PC with zero risk to hardware | Captured main firmware reaches four QEMU cores, original filesystem mounts, LIRO/application startup and a shell; no complete camera boot or verified emulator GUI frame |
| **2 · Low-Latency Liveview & USB Video (`CinemaHUD`)** | Stream video from the camera with minimal delay over USB | **Android/PTP liveview** works on real hardware. **Native USB webcam (UVC) mode** shows up on the host and disconnects cleanly, but video streaming still stalls during USB setup |
| **3 · Multi-Camera Support** | Share hardware and kernel knowledge across BIONZ X bodies, including the a6400A | Eight sourced profiles: original a6400 is the only verified execution target; the other seven are reference profiles |

## Latest progress

### Reproducible captured QEMU boot inputs ([E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md))

The [QEMU workflow](qemu-a6400/README.md) now rebuilds its inputs from hash-checked
camera dumps without mixing in updater-package libraries or default settings.
Two independent preparations have identical output hashes, and the original
captures remain unchanged. The native eMMC backing layout and CMD1 sector-mode
negotiation are corrected and regression-tested.

Original firmware reaches four cores, LIRO/stella startup and a working shell.
Qualified snapshots identify Task 43 waiting on flag 10 (`0x3373`), with its
IRQ 101/102 pathways enabled but never asserted by the model. Task 65 is dormant,
not a demonstrated sub-micro deadlock. The next step is the actual event-producer
contract, not an injected completion. No camera access or emulator GUI frame is
claimed.

### Continued QEMU machine work ([E0525](docs/evidence/0525-qemu-a6400-ulogio-queue-unblocks-appframework.md)–[E0544](docs/evidence/0544-qemu-a6400-mode-sequencer-waits-at-step-12.md))

The same captured-input boot now continues through Appframework, a replayed
sub-micro link, and a Sugilite console. Graphic console 0 shows the viewfinder
readout (program mark, battery, exposure scale). A disposable overlay that
selects device 1 in the firmware display controller activates the original
rear-LCD layout on console 0
([E0572](docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md)).
An interactive rear panel routes its buttons through the emulated camera
inputs ([E0573](docs/evidence/0573-qemu-a6400-rear-panel-controls.md)).
The panel reports when the live output is blank
([E0574](docs/evidence/0574-qemu-a6400-blank-panel-recovery.md)).
The mod development track now has a measured QEMU process and address
baseline ([E0575](docs/evidence/0575-qemu-mod-loader-v2-compatibility-boundary.md)).
An offline reload request lets the v2 core replace an enabled mod in one
verified step ([E0576](docs/evidence/0576-mod-loader-v2-reload-request.md)).
In QEMU the loader now installs in the original `im.elf`, loads the camera MenuTouchFull build
through per-library relocation and hot-reloads rebuilt mods in the Sony menu
([E0577](docs/evidence/0577-qemu-real-mods-relocation-and-hot-reload.md)).
The YUV live image remains a separate investigation.
Offline firmware analysis and Unicorn verification also map a staged Open Gate (3:2) recording development path
([E0578](docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md)).

### ILCE-7RM4A reference profile ([E0570](docs/evidence/0570-ilce-7rm4a-offline-sourced-profile.md))

The a7R IVA row of the E0477 platform map is now a registered reference profile,
sourced from that body's own packaged 1.13 updater. Seven gadget-core symbols
come from its own `vmlinux.bin`. Its kernel addresses are not the a6400's, and
`probe_interpreter` does not reuse the a6400 inventory or the ILCE-7C manifest
for it. No camera was accessed.

Two RAM-only mods run on a live ILCE-6400 (`im.elf`): a guarded loader that enables `.mod`
payloads from the SD card, and the MTF2 full-menu touch mod. Both live only in RAM — no flash
writes and no boot-time persistence; a camera restart restores stock behaviour.

### RAM-only Mod Loader ([E0523](docs/evidence/0523-mod-loader-ok-routing-diagnostics.md))

A reversible 7th `Mods` tab in the camera menu plus an SD-card `.mod` loader, housed in
`/libmprctrl.so` RX padding with a writable anchor and three hooks in
`viewUnified2.so`, installed through the Mass Storage debugger. It scans `/mnt/mmca1/mods`
(and `/media/mmca1/mods`, `/mnt/sd/mods`, `/tmp_bt/mods`) for SMOD v1 `.mod` files, lists up
to 12 of them as ON/OFF rows, and toggles a row only after verifying the original hook bytes:
it copies the payload and its state block into RAM and writes Thumb trampolines over the
three hooks; toggling off restores the original bytes. The loader copies a per-mod
state-init blob instead of zero-filling state, and flushes every CPU
(`sched_setaffinity` + `cacheflush`) because the camera runs a 4-core SMP kernel. It is
fail-closed: an invalid mod is shown as `ERR` and nothing is patched.
Sources: [`mod_loader.c`](tools/native_camera/mod_loader.c),
[`native_mod_loader_guard.c`](tools/native_camera/native_mod_loader_guard.c),
[`pack_mod.py`](tools/research/pack_mod.py).

The Mod Loader includes [the Mods tab artwork](docs/evidence/0522-mods-tab-icon-and-uxd-image-container.md).
The camera's menu selects inactive, active, and tab-bar focus images through its existing
resource properties. Each image has 192-byte rows clipped to the stock tab rectangle.

### MTF2 full-menu touch mod ([E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md))

A RAM-only touch layer for the main menu, housed in `/libObj.so` RX padding. Two hooks on
the press/release handlers in `/libObj.so` feed the camera's own event queue, so taps and
swipes drive the original state machine: tap tabs and page dots, swipe horizontally to page,
first tap selects a row and a second tap opens it, and in a popup a tap selects and applies
the option without closing, a vertical swipe scrolls, and an outside tap closes only the
popup. Verified on a physical ILCE-6400 with all 175 `im.elf` threads resumed; not handled:
My Menu (tab 6), the injected Mods tab (7), and tab-bar focus.
Sources: [`menu_touch_full.c`](tools/native_camera/menu_touch_full.c),
[`native_menu_touch_full_guard.c`](tools/native_camera/native_menu_touch_full_guard.c),
verified snapshot in [`mods/menu_touch_full/`](mods/menu_touch_full/README.md).

### Arrivederci look mod

[Arrivederci](mods/arri_isp/arrivederci/README.md) is an SMOD v1 Mods-tab entry for the
same loader. The loader calls its Thumb entry with 1 on ON and 0 on OFF when the
mod's flag bit 1 is set. It was checked on a live ILCE-6400 on 2026-11-16 with
Picture Profile gamma S-Log3 and color mode S-Gamut3.Cine. The tone curve is
reloaded from the read-only image at boot. The color matrix is part of Sony's
backup and can remain until it is toggled back and the camera saves stock.
Rebuild with [`mods/arri_isp/arrivederci/build_wsl.sh`](mods/arri_isp/arrivederci/build_wsl.sh).
On 2026-11-17 the SMOD v2 build ran under Mod Loader v2 in RAM. ON and OFF from the Mods tab
applied after leaving the menu, without a Color Mode round trip
([E0555](docs/evidence/0555-arrivederci-menu-exit-recompute-and-cc8-refill.md)). MenuTouchFull
also has an SMOD v2 build ([README](mods/menu_touch_full/README.md#smod-v2-build-for-mod-loader-v2-2026-11-17)).
Its matrix is now fitted to the official ARRI Classic LUT in the camera's own structure
([E0556](docs/evidence/0556-arrivederci-classic-fitted-matrix.md)). The Mod Loader v2 modset (loader, MenuTouchFull v2, this Arrivederci) is marked
[usable](docs/MOD_LOADER_V2_DESIGN.md#modset-v2-usable-2026-11-17) with a RAM install after each boot. Turn Arrivederci OFF before power-off.
The offline ISP staging component and the LUT converter stay separate:
[`mods/arri_isp/`](mods/arri_isp/README.md),
[`a6400-lut-converter/`](a6400-lut-converter/README.md),
[ISP transform catalog](docs/ISP_TRANSFORM_CATALOG.md).

### Short tutorial — load a mod from the SD card

1. **Get the touch mod:** use the tested [MenuTouchFull.mod](mods/menu_touch_full/MenuTouchFull.mod)
   or follow its [build instructions](mods/menu_touch_full/README.md#rebuild).
2. **Copy it to the SD card:** create a `mods/` folder at the card root and put the `.mod`
   file in it. The loader scans for it on every boot (up to 12 mods).
3. **Install the loader RAM-only:** run `wsl.exe --exec bash tools/native_camera/build_mod_loader_wsl.sh`
   from the repository root. It emits the combined `mod-loader-{check,install,remove}.req`
   files and the ARM guard under `artifacts/hardware/`. Drive the Mass Storage debugger client
   ([guide](docs/NATIVE_USB_DEBUGGER.md)): `push` the guard and the check `.req` to
   `/tmp_bt/native_mod_loader_guard` and `/tmp_bt/mod_loader.req` after a fresh MSC boot.
   Confirm readback for both uploads. Run `chmod 700 /tmp_bt/native_mod_loader_guard` and
   execute the guard. After it reports both `CHECK_OK` and `THEME_OK`, push
   `mod-loader-install.req` to the same request path, confirm readback, and run the guard
   again. Successful installation reports `INSTALLED` and resumes the camera threads.
4. **Toggle and use:** open `MENU` → tab 7 (`Mods`) and press the center button on
   `Menu Touch Full` to switch it on or off. The Mods artwork appears as soon as the
   loader is installed.
5. **Undo:** restart the camera to unload the loader and its mods. The combined remove
   request also restores the patched bytes during the same boot before the loader first
   builds its menu.

Safety: RAM-only, one boot at a time. The guard verifies the exact firmware mapping and
original hook bytes before any write, refuses on any mismatch, and restores what it changed;
do not reuse these addresses on a different firmware body without new evidence. Full details:
[E0523](docs/evidence/0523-mod-loader-ok-routing-diagnostics.md),
[E0520](docs/evidence/0520-mtf2-full-menu-touch-verified-on-a6400.md) and
[mods/menu_touch_full/README.md](mods/menu_touch_full/README.md).

Earlier progress highlights are preserved in [docs/archives/readme-latest-progress-2026-11-15.md](docs/archives/readme-latest-progress-2026-11-15.md).

## Status overview

Recorded suite: 1992 tests. The E0524 captured-input receipt remains the boot
qualification. The separate native eMMC controls pass in WSL. Full receipts are reported in
[E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md).
Each result is bounded by its own dated evidence record; no UVC video frame and no
flash write are claimed. Latest records: [E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md)
(reproducible captured QEMU inputs and qualified event waits), [E0544](docs/evidence/0544-qemu-a6400-mode-sequencer-waits-at-step-12.md)
(the same boot continued to a viewfinder console; in the default model the mode sequencer stays at step 12 because evsel source 30 fires in the same pulse as source 29 and `VD_Sequence` never runs. With the opt-in `EVSEL_LATE_US=500` it reaches step 32 and programs the YUV layer, [E0557](docs/evidence/0557-qemu-a6400-hs-root-order-passes-step-12.md)), [E0523](docs/evidence/0523-mod-loader-ok-routing-diagnostics.md)
(guarded RAM-only Mod Loader plus the MTF2 full-menu touch mod), [E0522](docs/evidence/0522-qemu-a6400-smp-four-cores-and-cxd4115-timers.md)
and [E0521](docs/evidence/0521-qemu-a6400-main-firmware-boots-to-avcam.md) (the out-of-tree QEMU
`sony-a6400` machine boots the main `nflasha3` firmware to a shell and brings up four Cortex-A5
cores; this is QEMU, not the Unicorn emulator).

![Research status: demonstrated results and remaining boundaries](docs/images/research-status.svg?v=e5948c71e8d8)

![Daily automated test suite growth: 1992 tests across 31 days](docs/images/test-growth.svg?v=9bd7ed84b026)

### Subsystem status

| Subsystem | What works today | What's still missing | Evidence |
| --- | --- | --- | --- |
| **Offline firmware & kernel** | QEMU boots the captured main kernel, filesystems and LIRO on four cores with a shell, then continues to Appframework and a viewfinder OSD. The mode sequencer waits at step 12 by default and at step 32 with the opt-in source-30 delay (E0557). The path selector has already accepted `0x3003`. With opt-in SCR.AW/FW and A masks, Unicorn matches all 16 sampled updater return rows and reaches BusyBox | With the opt-in source-30 delay, module 3 reports `0x0326` against `0x032E` at step 32 because no PQD Ydet statistics record arrives (E0561), and that 500 µs phase is an assumption; stock rear-LCD selection still open; Four Thumb loader instructions match, and the initial stack reports VFPD32 in Unicorn and VFPv3D16 in QEMU; full camera boot unproven | [E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md), [E0544](docs/evidence/0544-qemu-a6400-mode-sequencer-waits-at-step-12.md), [E0557](docs/evidence/0557-qemu-a6400-hs-root-order-passes-step-12.md), [E0545](docs/evidence/0545-unicorn-qemu-return-window-cpsr-a.md), [E0548](docs/evidence/0548-head-stub-registers-die-before-v7-setup.md), [E0558](docs/evidence/0558-unicorn-qemu-kernel-entry-async-abort-mask.md), [E0559](docs/evidence/0559-unicorn-qemu-irq-async-abort-mask.md), [E0560](docs/evidence/0560-unicorn-nonsecure-af-exception-return.md), [E0562](docs/evidence/0562-thumb-loader-stack-vfp-capability.md) |
| **Internal messaging & USB state** | Message routing and synchronous kernel-to-user callbacks run to completion offline | Remaining callback packet format is still unknown | [E0327](docs/evidence/0327-mwf-objmsg-wire-format.md), [E0332](docs/evidence/0332-sync-callback-delivery-first-invocation.md) |
| **Offline USB enumeration** | Combined USB webcam + PTP descriptors decoded; configuration and endpoint selection modelled | Delivering an incoming USB configuration end-to-end on the sync lane | [E0337](docs/evidence/0337-emulator-uvc-ptp-preparation-and-0a4002-broadcast.md) |
| **Display & GPU** | Camera screen layout confirmed (640×480); GPU memory/interrupt contracts decoded offline. QEMU renders the viewfinder OSD in stock boot and the original rear-LCD OSD with the diagnostic overlay (E0572) | Stock input selection and the YUV layer | [E0298](docs/evidence/0298-safe-osd-capture-and-sugilite-correction.md), [E0314](docs/evidence/0314-gpes-bar-irq-contract-engine-model.md), [E0536](docs/evidence/0536-qemu-a6400-viewfinder-readout-and-eye-sensor-command.md) |
| **PTP liveview (real camera)** | PC Remote streams 1024×680 sensor JPEGs; custom reader captures 29.8 FPS alongside PTP | Running UVC + PTP together with reliable teardown | [E0349](docs/evidence/0349-p1-foreign-get-jpeg-succeeds-in-pc-remote.md), [E0151](docs/evidence/0151-pc-remote-liveview-and-uvc-verified.md) |
| **Composite USB webcam (real camera)** | Camera appears as both PTP and Video device in Windows, and disconnects cleanly without a battery pull | DirectShow times out during the USB control handshake status stage | [E0350](docs/evidence/0350-trackb-transport-enumerates-but-renderstream-fails.md), [E0398](docs/evidence/0398-runb21-status-stage-and-disconnect-verdict.md) |
| **USB Debugger v2** | Persistent Mass Storage debugger for reading live/warm-reboot kernel logs and USB state | Deploying the always-on WinUSB vendor interface | [E0401](docs/evidence/0401-usb-debugger-v2-ring-recover-and-mode-boundary.md), [E0405ab](docs/evidence/0405ab-device-desc-builder-and-msos2-subset.md) |
| **Test UVC video pattern** | Streams a synthetic 720p MJPEG pattern at ~30 FPS; ZV-E10 USB video stack cataloged as reference | Wiring the live camera sensor feed | [E0143](docs/evidence/0143-uvc-720p-test-transport-and-30fps-verified.md), [E0321](docs/evidence/0321-zve10-uvc-enablement-manifest.md) |
| **Multi-camera profiles** | Eight sourced profiles, including `ilce_6400a` and reference `ilce_7rm4a`. Packaged `libJiritsu.so` dispatches `GET_FILE` on halfword 8 | Only original a6400 is a verified execution target. The A6400A library map is not a sent Senser command, so the RAM bootstrap does not open USB. `ilce_7rm4a` has no process images of its own | [E0506](docs/evidence/0506-a6400a-offline-qualification-and-debugger-boundary.md), [E0554](docs/evidence/0554-a6400a-v101-jiritsu-command-dispatch.md), [E0570](docs/evidence/0570-ilce-7rm4a-offline-sourced-profile.md), [`src/a6400/targets/`](src/a6400/targets/catalog.py) |
| **Automated checks** | 1992 tests in the recorded offline suite (structure, profiles, emulator contracts, doc sync), plus native eMMC controls | See the receipt for skips; bounded contracts are not full compatibility | [tests/](tests/README.md), [E0524](docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md) |

## How to help (no camera needed)

**Top 3 open tasks right now**

1. **Switch to the rear LCD through the stock firmware input** — the
   original LCD layout now renders on console 0 with a diagnostic
   overlay. Find the input that selects the same device without the
   overlay ([E0572](docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md)).
   The live-image investigation is parked at mode-sequencer step 32.
   The IRQ 35 callback runs and its input to the CamPig completion path
   remains unresolved ([E0569](docs/evidence/0569-qemu-sgc1-event-a.md),
   [QEMU checkpoint](qemu-a6400/STATUS.md)).
2. **Decode the USB control callback packet format** — figure out the remaining message
   layout so an incoming USB configuration request reaches the firmware's USB state machine
   ([`usb_sync_callback.py`](src/a6400/execution/usb_sync_callback.py)).
3. **Unblock the GPU completion signal** — resolve the cross-thread wait condition in the
   emulated graphics driver so the display path can advance on its own
   ([`guest_gles.py`](src/a6400/execution/guest_gles.py)).

More guides: [Contributing to UVC / PTP](docs/CONTRIBUTING_UVC_PTP.md) · [Adding a BIONZ X camera](docs/ADDING_A_BIONZX_CAMERA.md) · [UVC/PTP progress log](docs/UVC_PTP_STATUS.md).

## Quick start

Requires Python 3.10+ and dependencies from [`requirements.txt`](requirements.txt) (no camera needed):

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
| --- | --- |
| Full offline test suite | `python -m unittest discover -s tests -q` |
| Structure, profiles & doc sync | `python -m unittest tests.test_repository_layout tests.test_target_profiles tests.test_research_status_sync -v` |
| Status card matches data | `python -m tools.research.render_research_status --check` |
| Windows launchers | [`scripts/`](scripts/) (`launch_cinemahud_ptp.bat`, `launch_cinemahud_android.ps1`, `launch_cinema_mpv.bat`, `start_camera_shell.bat`) |

Run scripts as Python modules from the repo root (`python -m tools.research.<tool>`).
Firmware images, backups, and captured artifacts stay local and are excluded from Git.

## Repository layout

| Folder | What's inside |
| --- | --- |
| `src/a6400/execution/` | Offline ARM emulator and verified OS, USB, and GPU contracts |
| `src/a6400/targets/` | Device profiles and validation for supported Sony cameras |
| `src/a6400/debug/` | USB debugger protocol, log ring buffer, and WinUSB transport |
| `tools/research/` | Firmware analysis scripts, emulator runners, and status-card generator |
| `tools/native_camera/` | C helpers, kernel-module sources, and USB/UVC transport code |
| `probes/` | Safe, read-only ARM probe for other BIONZ X cameras |
| [`a6400-lut-converter/`](a6400-lut-converter/README.md) | Offline ISP table fitting, ARRI presets, and 8-bit 4:2:0 preview |
| [`mods/arri_isp/`](mods/arri_isp/README.md) | Conditional ISP staging component, Arrivederci v1 mod, and read-only camera notes |
| `tests/` | Automated contract, regression, and structure checks |
| `docs/evidence/` | Dated research logs with exact observations and boundaries |
| `docs/` | Architecture notes, workflows, and contributor guides |
| `android/`, `android-client-ref/` | Android liveview client (`CinemaHUD`) |
| `camera-daemon/` | Camera-side composite USB webcam daemon |
| `firmware/`, `backups/`, `artifacts/` | Local firmware dumps and generated outputs (never committed to Git) |

## Rules & safety

- **Honest claims only:** Host-written UI or camera logic is **not** firmware compatibility. Every claim links to a dated evidence record.
- **No flash writes:** Never modify internal camera flash, install custom firmware, or alter boot partitions.
- **Clean-room emulation:** Original firmware owns application logic; unknown OS services stop and report where they stopped instead of faking success.
- **Safe hardware checks:** Read-only camera inspection is allowed; writes, mode switches, and module loads require explicit scope. Bulk `/dev/mem` memory sweeps are banned because one caused a USB drop and reboot ([AGENTS.MD](AGENTS.MD)).
- **No hardcoded boot addresses:** Memory layouts and file descriptors change on every reboot and are never treated as fixed constants.

## Links & attribution

[Related research repositories](docs/RELATED_REPOSITORIES.md): an agent-facing catalog
of Sony emulation, firmware-format, service-interface and cross-generation research,
with starting points and limits on transferring upstream findings.

[Evidence index](docs/evidence/README.md) · [Architecture](docs/ARCHITECTURE.md) · [UVC monitor architecture](docs/ARCHITECTURE_UVC_MONITOR.md) · [Roadmap](docs/ROADMAP.md) · [Live camera / emulator workflow](docs/LIVE_CAMERA_EMULATOR_WORKFLOW.md) · [Contributing](CONTRIBUTING.MD) · [Repository layout](docs/REPOSITORY.md) · [Agent rules](AGENTS.MD) · [Archived README](docs/archives/README-2026-11-09.md)

- **Cross-generation BIONZ X research:** [Oein/a5000-emulator](https://github.com/Oein/a5000-emulator) by Park SungHyun (Oein) — clean-room reference for DMP SMAPH/P3D command-list format and 8×8 Morton tiling (`docs/RE_GPU_P3D_CMD.md`, CC BY 4.0), `GICD_ITARGETSR` bootloader pre-routing (`docs/HARDWARE.md`), and `im.elf` UI bringup gates (`docs/RE_UI_BRINGUP.md`).

Independent research; not affiliated with or endorsed by Sony. Sony, a6400, ILCE-6400, and BIONZ X are trademarks of their respective owners.
