# Sony a6400 QEMU Machine (`sony-a6400`)

Out-of-tree QEMU v10.1.0 overlay and patch for the Sony ILCE-6400 (`CXD90045` / `ARM-CXD900X0`), modeled after the clean `qemu-overlay/` + `patches/` pattern used by `Oein/a5000-emulator`.

## Prerequisites (WSL2 Ubuntu)

Install the QEMU build dependencies in WSL2 Ubuntu:

```bash
sudo apt update
sudo apt install -y build-essential git meson ninja-build pkg-config \
  libglib2.0-dev libpixman-1-dev libfdt-dev zlib1g-dev python3-venv \
  python3-tomli libsdl2-dev
```

## Setup & Build

From the `qemu-a6400/` directory inside WSL2:

```bash
./setup-qemu.sh
cd qemu/build
../configure --target-list=arm-softmmu --enable-sdl --disable-werror
ninja qemu-system-arm qemu-img
```

The patch also honors eMMC CMD1 sector-addressing requests below 2 GiB. Without
that negotiation, the original driver requests sector `0x52002` but QEMU reads
byte address `0x52002` instead of `0x0A400400`, so a valid captured ext2 rootfs
cannot mount. This is a device-protocol correction, not a firmware patch or a
reason to inflate the disk to 4 GiB. Optional native regressions use constructed
disks and paused CPUs, requiring neither firmware nor camera:

```powershell
wsl.exe --exec python3 -m unittest tests.test_qemu_emmc -v
```

Run this command from the repository root. The ordinary Windows suite skips
these two native tests when the Linux QEMU build is unavailable.

## Milestone 1: Initial Bring-Up

Verify the machine registers cleanly:

```bash
./qemu/build/qemu-system-arm -M help | grep sony-a6400
```

## Interactive rear panel

On this Windows workstation, double-click `a6400_QEMU.bat` on the
Desktop. It starts the emulator and opens the panel in the default
browser at `http://127.0.0.1:8641/`. With prepared camera inputs and a
layout-aware loader build present it starts in mod-development mode, and
the page toolbar loads, watches and hot-reloads mods and pauses, resumes
or restarts the emulator (see [MOD_DEVELOPMENT.md](MOD_DEVELOPMENT.md)). Running it while the panel is
already open reuses that instance. The LCD starts displaying when
firmware publishes its first frame.

Rebuild QEMU with the current board overlay. From the repository root in WSL,
start a new run with a fresh log path:

```bash
EVSEL_LATE_US=500 python3 qemu-a6400/tools/rear_panel.py --log a6400-panel.log
```

Open [the local rear panel](http://127.0.0.1:8640/). The image is QEMU's
VPH output. The buttons drive the emulated ADC ladders, GPIO lines or
sub-micro link. Hover over the control wheel and scroll to rotate it, or
use `[` and `]`. Press Ctrl+C in the launch terminal to stop the local run.

The panel enables the [E0572](../docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md)
LCD selection on the disposable overlay. Use `--stock` to exercise the
original display selection. `A6400_QEMU_DIR` can point at an alternate
QEMU build. This workflow uses the emulator's eMMC copy.

For the guarded ARM mod workflow (camera-captured inputs, Mod Loader v2 install and mod hot reload
with `--inputs`, `--loader` and `--mod`), see [Developing camera mods in QEMU](MOD_DEVELOPMENT.md).
Prepared inputs read from `/mnt/c` slow every guest disk access. The panel's `--inputs` copies them
to the Linux filesystem once (boot to the menu 63 s → 35 s, E0577).

## Milestone 2: Main firmware boot (E0521)

### Rebuild inputs from a local camera capture

The preferred input set uses the captured SDM partition table, every nonempty
partition image, and the kernel, initrd, av-cam and settings extracted from those
same images. It does not substitute update-package libraries or default settings.
Original capture files are opened read-only; generated files remain git-ignored.
Capture/output overlap is refused before creating any directories or receipts.
The native QEMU eMMC backing layout is `nflashaB0` (4 MiB), `nflashaB1`
(4 MiB), then the user area. The captured SDM header therefore belongs at
backing-file offset 8 MiB, and each user partition at `8 MiB + SDM.start`.
Manifest schema 2 records this translation; placing the SDM header at file
offset zero makes the guest reject the partition table.

Create the ignored `qemu-a6400/inputs.local.json` once for this workstation:

```json
{
  "schema_version": 1,
  "dump_dir": "C:\\private\\a6400-nflasha-readonly",
  "manifest_sha256": "<verified SHA-256 of manifest.json>",
  "checksums_sha256": "<verified SHA-256 of SHA256SUMS.txt>"
}
```

Check these initial identities against the acquisition receipts. Do not refresh
hashes to silence a mismatch. The tool uses the capture's complete per-device
`manifest.json` and the existing checksum list; it does not rewrite either.

From PowerShell in the repository root:

```powershell
$bootInputs = 'artifacts\qemu-a6400\inputs-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
.\venv\Scripts\python.exe -m tools.research.qemu_boot_inputs prepare --report-dir $bootInputs --allow-unacquired-zero-fill
.\venv\Scripts\python.exe -m tools.research.qemu_boot_inputs verify --report-dir $bootInputs --sources
$linuxInputs = (wsl.exe --exec wslpath -a -u (Resolve-Path $bootInputs).Path).Trim()
wsl.exe --exec env "A6400_QEMU_DATA_DIR=$linuxInputs" TIMEOUT=110 bash qemu-a6400/run.sh
```

`--allow-unacquired-zero-fill` explicitly permits zero-filled unacquired disk
ranges, including the tail required by QEMU's power-of-two card-size restriction.
The manifest distinguishes captured capacity, emulated capacity and every such
range. This is a reconstructed emulator disk, **not** a complete physical-disk
dump. Writable source filesystems were also not acquired atomically.

Each preparation requires a new output directory and produces a deterministic
`boot-manifest.json`, disk image and preload files. Repeating preparation with the
same sources, tool bytes and policy produces identical output hashes. Before
booting, `run.sh` checks all prepared hashes, uses the recorded serial command
line and creates a new run directory. `RUN_DIR` may select another new directory.
The raw base image is never attached writable: the guest uses a fresh qcow2 overlay.
An exit caused by `TIMEOUT` is a bounded stop, not proof of successful boot.

### Bounded task and interrupt observations

The standard-library observer runs inside WSL, captures real serial command exit
statuses, stops all emulated CPUs for snapshots and verifies QMP shutdown:

```powershell
wsl.exe --exec python3 -u -m tools.research.qemu_boot_probe --inputs $linuxInputs --report-dir artifacts/qemu-a6400/NEW-PROBE --seconds 120 --wait-for "Created HeapAllocator:" --guest-command "cat /proc/interrupts" --liro-task 1 --liro-task 43 --liro-task 65 --liro-irq 101 --liro-irq 102
```

Serial input is paced and acknowledged one command at a time; commands are not
retried automatically. Active console I/O stays on Linux storage and is
hash-verified when archived, avoiding concurrent cloud-backed DrvFS reads.

The optional LIRO decoder is deliberately restricted to the captured E0524
kernel/usr hashes. It binds the original ELF offsets to this run's module
section addresses, archives each bounded RAM read and does not unwind runnable
tasks. Other builds must establish their own layout; do not replace hashes to
make them pass.

IRQ observations use native GIC enable/disable/input traces, not debugger reads
of GIC MMIO. A debug read reset the emulator connection; the native SMP GIC path
requires a current CPU and is not safe in that QMP context. No IRQ, event flag
or semaphore is signalled by the observer.
See [E0524](../docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md)
for the verified wait and the remaining producer boundary.

### Historical input layout

Without `A6400_QEMU_DATA_DIR`, the legacy launcher expects the locally prepared
`qemu-a6400/data/` layout and its original `AVCAM` override behavior:

```bash
./run.sh            # TIMEOUT=80 ./run.sh for a bounded run
```

`run.sh` reproduces the Sony bootloader's hand-off: kernel at `0x00108000`, initrd at `0x00700000`,
`"kemco"+cmdline` at `0x00103000` (machine code), the main-boot `/setting/kemco.txt` cmdline (console
swapped to `ttyAM0`), `Backup.bin` preloaded at its header word-0 address (`0x02E80000`) and
`av-cam.bin` at `0x035C6000`. The eMMC image is attached through a fresh qcow2 overlay.

State at E0521: kernel → init → all eMMC partitions mounted → 40 kernel modules → av-cam
`entry_main` → `stella`/`DMP_2D` → BusyBox shell on `ttyAM0`. See
[docs/evidence/0521-qemu-a6400-main-firmware-boots-to-avcam.md](../docs/evidence/0521-qemu-a6400-main-firmware-boots-to-avcam.md).

## Milestone 3: SMP (E0522)

The machine models 4 Cortex-A5 cores (`-smp 1` still works). A CPU0 stub presets GIC SPI targets
and enters the kernel. CPU1–3 wait on the kernel's SRAM mailbox (`0xFE005010`). Timers use the CXD4115
model (adapted from Oein/a5000-emulator), with per-CPU ticks on IRQ 153/155/156/157. `run.sh`
defaults to `-icount shift=0,sleep=on`. See
[docs/evidence/0522-qemu-a6400-smp-four-cores-and-cxd4115-timers.md](../docs/evidence/0522-qemu-a6400-smp-four-cores-and-cxd4115-timers.md).

Findings, known gaps and the ordered next steps: [STATUS.md](STATUS.md).

Next, at the time of E0522: av-cam's hardware dependencies (no `liro-*` IRQ fires yet): sub-micro link, ADC, front end;
then the Sugilite display pipeline.
Before each new blocker, check whether `Oein/a5000-emulator` already solved the CXD90014 equivalent.

## Milestone 4: Captured inputs and qualified waits (E0524)

The sections above this heading describe how to rebuild hash-checked inputs and
how the LIRO observer qualifies Task 43, IRQs 101/102, and Task 65. Keep that
workflow. The device work below uses the same boot.

## Milestone 5: Device models through the viewfinder console (E0525–E0541)

`run.sh` can add `/setting/ulogio.bin` to the per-run overlay (E0525) and can
replay a captured sub-micro page (E0527). Set `ULOGIO=` or `SUBMICRO_PAGE=` empty
to skip either one. The default `ULOGIO` path is a local ZV-E10 stand-in, not an
a6400 capture.

The overlay staging supports `RUN_DIR` paths with spaces. For panel captures,
target VPH head 0 with `screendump /path/panel.ppm vph 0`. VPH head 1 is the
separate HDMI output.

`CAMPIG_ENABLE_READBACK=1` enables the experimental CamPig IRQ enable word
at `0xF3000050` (`-M sony-a6400,campig-enable-readback=on`). It latches the
last value written and resets to zero. The firmware uses a read-modify-write
to enable paths 3 and 2. This option covers the enable word; frame completion
and statistics are still separate research boundaries.

`SGC1_EVENT_A=1` enables the experimental input synchronizer event A
(`-M sony-a6400,sgc1-event-a=on`). It schedules the programmed line compare
on the virtual clock, using a 36 MHz source inferred in E0568. Event A
latches status bit 0 and, when enabled, raises IRQ 35. The experiment
covers the input event and leaves CamPig IRQ 36 and Ydet data unmodelled.

`tools/a6400_probe.py` boots, waits until a checkpoint appears in
`/proc/snsc_boot_time`, runs shell commands, and stops QEMU.

The overlay adds SIO, GPIO, ADC, the timing-event block, VPH/SYSV, a Sugilite
GPU, SA DSP ID responses, and a P2S writer. Five guest seconds after
`SetContentTarget`, graphic console 0 shows the viewfinder readout (E0536).
The YUV layer is still off, and forcing `isOutputEvf` to return 0 does not
select the rear-LCD skin (E0541). See [STATUS.md](STATUS.md).

## USB and sensor diagnostics

These diagnostics run offline against the ILCE-6400 main firmware. Prepare
captured inputs using the workflow above. The E0579 boot set already contains
`ulogio.bin`, so its runs use `ULOGIO=` to skip the launcher's optional copy.

- Rebuild the board overlay and set `P2S_TRACE=1` before running `run.sh`.
  The console records up to 64 accepted starts and their modeled completion
  or reset. Each start includes the raw transfer config, mode, and low bytes
  of the 112 staging words at `0xF3C00340–0xF3C004FC`. The staging words can
  retain bytes from previous transfers. Use the trace to compare guest
  programming with the IMX284 tables in [E0578](../docs/evidence/0578-a6400-open-gate-3-2-recording-feasibility.md).
- Build the DWC3 observer against the QEMU 10.1 plugin API and append
  `-plugin /absolute/path/dwc3_trace.so` to your `QEMU_EXTRA` options.
  It records guest PC, physical register, access direction, width and
  value for matched accesses from the ILCE-6400 kernel. The footer reports
  counts and truncation per core. Preserve the default `-icount` option
  when comparing to the E0579 replay.

```bash
# Run from the project root with A6400_QEMU_DIR set to the QEMU checkout.
gcc -std=c11 -Wall -Wextra -Werror -Wno-unused-parameter -fPIC -shared \
    $(pkg-config --cflags glib-2.0) -I "$A6400_QEMU_DIR/include/qemu" \
    qemu-a6400/tools/dwc3_trace.c -o /tmp/dwc3_trace.so
```

Current boot observations and the next USB and sensor boundaries are recorded
in [E0579](../docs/evidence/0579-qemu-usb-and-sensor-observation.md).

### Virtual USB cable

Launch with `USB_VBUS_TEST=1` and a local `-qmp` socket in `QEMU_EXTRA`.
After the guest has started, send `qmp_capabilities` and set the cable
state. Use `false` to disconnect.

```json
{"execute":"qom-set","arguments":{"path":"/machine","property":"usb-vbus","value":true}}
```

The guest's FUSB handler reads the new status on IRQ 230. The experiment
starts disconnected and resets to disconnected. DWC3 gadget transactions
remain the next boundary. See [E0580](../docs/evidence/0580-qemu-usb-vbus-interrupt.md).
