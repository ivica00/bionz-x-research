# External ILCE-6000 emulator receipt

Date: 2026-11-27. Contributor: `voxivoid`. Scope: the `sony-a6000` QEMU machine for the
ILCE-6000, which uses the **CXD90014** SoC rather than the CXD90045. This record is
**not an upstream a6400 milestone**. It does not change the a6400 status snapshot or its
test count.

Source: [voxivoid/a6000-qemu](https://github.com/voxivoid/a6000-qemu), commit `62cf3bc`
(QEMU 10.1.0). That project is a fork of
[Oein/a5000-emulator](https://github.com/Oein/a5000-emulator), base commit `6ad36bd`.
This repository's port is `emulators/a6000/qemu-overlay/hw/arm/sony_a6000.c`, built on the
pinned QEMU 11.1.3. Scripts and instructions are in
[emulators/a6000/README.md](../../../emulators/a6000/README.md).

## Inputs

All inputs stayed on the contributor's computer, and none are in this repository:

- Sony's public updater `Update_ILCE6000V321.exe`, firmware **3.21**, unpacked with fwtool.
- The contributor's own camera NAND dump, provisioned into an emulated NAND with Sony's
  own NAND stack (`provision.sh`).

| Input | SHA-256 |
|---|---|
| `Update_ILCE6000V321.exe` | `0c3e61b03daafa469f4dfcb04decf0983088e609d7c32a29f356dab57ce45d57` |
| `FirmwareData_ILCE6000V321.dat` (cut from the `.exe`) | `29aef21ab9b5c2e98388974ac9749c2cbfb1815273b444e62f2e41398aa8892f` |
| `vmlinux.bin` (updater, `nflasha3`) | `1ad249a2f231785e8d036fb8be2bbb90ca7bcbb00fd24f4a87647a92acd55053` |
| Emulated NAND (owner's dump) | not published: it holds the owner's device data |

Builds: QEMU 11.1.3 (`005c61c0ef6f6ae615283440b4bb7be51b2c6ddd`) staged by
`emulators/a6000/setup-qemu.sh` from this branch and configured with
`--target-list=arm-softmmu --enable-sdl --disable-werror`; the source build is
voxivoid/a6000-qemu `62cf3bc` on QEMU 10.1.0.

## Commands

From `emulators/a6000/`, each run on a fresh copy of the same emulated NAND
(`A6K_NAND=<copy>`):

```sh
./run.sh -display none -monitor unix:<sock>,server,nowait            # boot, then HMP:
#   sendkey ret 300 / sendkey m 300 / sendkey down 300 ... ; screendump <file>.ppm
./run.sh -display none -monitor tcp:127.0.0.1:<p>,server,nowait \
  -chardev socket,id=nfc,host=127.0.0.1,port=<n>,server=on,wait=off \
  -M usb-socket=127.0.0.1:<u>,nfc=nfc,hdmi=on,lens=ekit \
  -netdev user,id=w0 -global ar6003-sdio.netdev=w0 \
  -global 'ar6003-sdio.aps=ssid=TestAP,psk=testpass123'
#   SD: A6K_SD=<blank SDHC from tools/mksd.py>; guest shell (A6K_SHELL=<port>,
#       tools/a6kshell.py): grep mmc /proc/partitions; dmesg | grep mmc
#   USB: Sony-PMCA-RE with tools/pmca-socket/: pmca-console info
#   NFC: write "tap 1000" to the nfc chardev; then ifconfig -a, lsmod, dmesg | grep ath
#   HDMI: screendump <file>.ppm vph 1   (console 1)
```

Screendumps were compared by MD5 between the two builds at each step.

## Identity

| Claim | Value | Basis |
|---|---|---|
| SoC | `CXD90014` | The updater's `config.yaml` states `crypterName: CXD90014`. Its 3.21 `vmlinux.bin` contains `arch/arm/mach-cxd90014/` paths and `cxd90014_*` symbols, such as `cxd90014_pm_init`. |
| Firmware version | `3.21` | The updater that was booted (`Update_ILCE6000V321.exe`). |
| Kernel version | `3.0.27_nl-rt106` | `uname -r` in the guest root shell (`tools/a6kshell.py`) during an emulated boot of that updater's kernel. |

## What was observed

Every item below was run twice: on the QEMU 10.1.0 build from the source repository, and
on this repository's QEMU 11.1.3 build. Both runs used a fresh copy of the same emulated
NAND.

| Item | Result |
|---|---|
| Two-stage boot, first-boot setup dialog, shooting screen, MENU | Screendumps are byte-identical between the two builds at each step, and over three runs each. |
| HDMI console 1 at 1080p | Byte-identical between the two builds. |
| SD card | `mmca10: p1` detected in 6 of 6 runs. |
| USB | Sony-PMCA-RE `info` over the emulator's USB socket reports firmware 3.21, in 6 of 6 runs. |
| NFC | A virtual phone `tap` reads the PlayMemories NDEF record and starts Smart Remote Embedded. A second `tap` in the same session times out on both builds. With three emulators running in parallel on one host, one first `tap` also timed out; two in parallel did not. |
| Wi-Fi | After the NFC tap, the stock `ath6kl_sdio` loads on the AR6003 SDIO model (`ar6003 hw 2.1.1 sdio fw 3.4.0.189`) and `ath0`/`ath1` come up. 3 of 3 runs on each build. |
| Guest time | Without `-icount`, 1 of 6 parallel runs reached the UI. With `-icount shift=0`, 16 of 16 did (8 with `sleep=off`, 8 with `sleep=on`). `run.sh` now passes `-icount`. |

The tag answer contains the camera's Wi-Fi Direct SSID and password, so no NFC log is
included here.

## Changes to shared models

These properties are added by the ILCE-6000 patch
(`emulators/a6000/patches/`), so `qemu-a6400/` itself is unchanged. They are
opt-in, and their defaults keep the a6400 behaviour. The native qtests show no new
failures against a baseline run on the unchanged tree.

- `cxd90014-vph` `scan-without-arm`: composes every frame, as the a5000 model did. With
  the per-frame arm rule (E0639), the ILCE-6000 panel could turn black after the UI had
  been shown. In one run, a screendump showed the setup dialog at 200 s and a black panel
  at 260 s. The firmware does not re-arm the OSD channel for a static screen.
- `cxd90014-gpio` `pulse-us`: pin IRQ pulse width. `sony-a6000` uses 1 ms.
- `cxd90014-adc`: a per-channel idle level for the ILCE-6000 mode-dial ladders.

## Boundaries

- At the date of this record the A6000 booted only with the user's own NAND dump.
  This boundary no longer holds since 2026-11-28; see the update below.
- Not shown: still capture (no ISP or live-view model), lens behaviour, EVF and eye
  sensor, control dial, power-cycle resume screen, PMCA app install.
- The emulated Wi-Fi module is an AR6003 model. It is not established that the ILCE-6000
  carries an AR6003.
- These are emulator observations. Nothing was run on the camera for this record.

## Update 2026-11-28: NAND without a camera dump

`emulators/a6000/tools/mknand_synth.py` builds the NAND image from the updater alone: its
partition table and images, the factory-default `Backup.bin` template from `0110_backup/`,
and `DmmConfig.bin` and `ulogio.bin` rebuilt from tables in the factory warm-boot image.
`nflasha4` is written erased; the firmware writes its own records there.

Run in a copy of `emulators/a6000/` without `dumps/`, `state/`, `inputs.local.json` or
`data/`: `tools/mkdata.py` → `mknand_synth.py` → `provision.sh` → `run.sh` (and
`tools/camui.py --launch`). The firmware reached the shooting screen and the menus, and
the kernel log had no oops. The NAND file's SHA-256 did not change.

Still missing compared with a real camera: the per-unit calibration (protected
`Backup.bin` props, `/setting/psd.bin`), the updater core (`nflasha1`), and owner data.
[emulators/a6000/README.md](../../../emulators/a6000/README.md#what-a-synthetic-nand-lacks)
lists each item and what it is for. On the synthetic NAND the shooting screen has shown
a SteadyShot warning icon whose cause is not known.

Static findings in the 3.21 updater (offline: disassembly and file comparison, no camera run):

- `/setting/psd.bin` (phase-detect AF calibration) is written only by the factory adjust
  command `A_PSD_DATA_WRITE` (`adjstctl.elf`: OSAL ID `0x000301D7`, block `0x0404`, code
  `0x63`; `A_PSD_DATA_READ` is code `0x62`). av-cam's code-`0x63` handler (`0x4378947C`)
  checks the `PF` magic and that the XOR of all words is 0, then sends message type 8 to
  `CameraOnDemandSaver.so`, which writes the file. av-cam reads it at lens attach
  ("[ZPD]: load dra data."). Normal camera use never writes it.
- Calibration lives in `Backup.bin` properties with attribute bit `0x02` (protected).
  Updates keep them (`ud_keep_backup -w` → `Backup_rewrite(BK_PROTECT_ADJ)`). In the
  updater template they are unset (0x00/0xFF). Groups: image adjustment (subsystems
  `0x0003`, `0x005f`, `0x0060`, `0x00bc`, `0x0132`), USB serial `0x00e70031`
  (`UsbAdjCmd::usb_serial_set`), Wi-Fi Direct credentials `0x013e0016`–`0x013e0018`
  (generated by `NsP2PGenerateBackUpCredential`), WLAN channel range `0x0147*`
  (`libWlanAdjust`).
- `nflasha4` (MPR Flash Backup, `libmpr.so`) is erased by `endupdate.sh` (`ud_lerase`)
  after every update.

Emulator observation: after a power-off, a wake through the camui power ring or
`system_wakeup` does a hot resume (DDR kept), but the panel stays black.

## Update 2026-11-28: control dial, eye sensor, pin interrupts, DFE

Contributor: `Kagander`. Source: [Kagander/a6k-emulator](https://github.com/Kagander/a6k-emulator),
a fork of voxivoid/a6000-qemu (`3a039c5`, QEMU 10.1.0) where these changes were first made
and used. This update ports four of them to this repository's QEMU 11.1.3 tree. They touch
only `emulators/a6000/`; no shared `cxd90014_*` model and nothing under
`emulators/qemu-a6400/` changes.

| Change | Where |
|---|---|
| Top control dial: TPU channel 1 (`0xF2404114`) is a second up/down counter. Host keys `.` / `,`. One wheel key press is now 4 counts of channel 0. | `cxd90014_jog.c`, `sony_a6000.c` |
| Eye sensor P1.6 idles high ("no eye") through the board's `reset-high` list. | `sony_a6000.c` |
| The boot stub sets every GPIO pin interrupt (GIC 121..135, 137..143, 144) edge-triggered. | `sony_a6000.c` |
| DFE interrupt source controller on GIC 136 (which is no longer wired to p0.15), sync-generator start bits, and two more `cxd90014-p2s` instances for the DFE P2S units. | new `cxd90014_dfe.c`, `sony_a6000.c` |

### Inputs and builds

Same updater as above (`vmlinux.bin` SHA-256 `1ad249a2…5053`, checked). The emulated NAND
is one provisioned from the contributor's own camera dump; it is not published. It still
shows the first-boot "Set Area/Date/Time" dialog.

Three builds of QEMU 11.1.3 (`005c61c0ef6f6ae615283440b4bb7be51b2c6ddd`), staged by
`emulators/a6000/setup-qemu.sh` on macOS (Apple Silicon) and configured with
`--target-list=arm-softmmu --disable-docs --disable-werror --disable-gtk --disable-sdl`:
**base** (this repository before the change), **all** (this change) and **no-eye** (this
change without the `1.6` entry in `reset-high`).

### Commands

Each build ran once, all three in parallel, each on its own copy of the NAND with the
machine options of `run.sh` (`nand-snapshot=on`, `-icount shift=0,sleep=on`), plus
`-display none -nic none -trace gic_acknowledge_irq -trace 'cxd_dfe_*'` and a TCG plugin
that prints av-cam's own debug log (hooks at `0x435ab89a` and `0x4390273c`; the plugin is
in the contributor's repository, `a6000-qemu/tools/avlog.c`, not here). Keys were sent over
QMP `send-key` with a 300 ms hold and the LCD read with `screendump`:

```text
wait 80 s; ret; wait 10 s; s (held 3 s); wait 10 s; m; wait 12 s; dot; wait 10 s;
bracket_right; wait 10 s; m; wait 12 s; pgup; wait 15 s; pgup; wait 15 s
```

### What was observed

| Item | base | all |
|---|---|---|
| Boot, setup dialog, area screen, MENU page 1 | same screendump MD5 in all three builds | |
| `dot` (top dial) in MENU | no change | page 1 → page 2 |
| `bracket_right` (wheel) in MENU | no change | cursor moves down one item |
| Acknowledges of GIC 133 (link strobe p0.12) over the run | 1,248,547 | 3,984 |
| av-cam log lines "Last frame is not started" | 7,003 | 5 |
| av-cam log lines "wai_flag error" | 1,157 | 0 |
| Acknowledges of GIC 136 (DFE) | 0 | 599 |
| Shooting screen after the half press | the reduced layout without the exposure graphic | the monitor layout with the exposure graphic |

The **no-eye** build matched **all** in every row except the last, where it matched
**base**. The layout difference therefore comes from the eye-sensor level alone. Which
display the firmware calls that reduced layout was not read from the firmware.

In an earlier run of **base** and **all** only (same options, shorter waits) GIC 133 was
acknowledged 666,725 and 2,118 times.

### Static findings (av-cam 3.21, VA = file offset + `0x43541000`)

- `JogTpu` reads a 16-bit counter at `+0x14` of each TPU channel of `0xF2404000`
  (`439917D2`); channel 0 is the rear wheel, channel 1 (`+0x114`) the top dial.
- The eye sensor is GPIO P1.6, active low: `4359251A` sends key `0x708B` = 1 when the pin
  reads low.
- The DFE ISR `436CF279` is registered on GIC 136 in the IRQ table at `0x43FD5E30`. It
  reads the pending source from `0xFC01FF00`, dispatches sources 0..13 and writes the same
  register at the end. Sources 0 and 1 are the P2S units DFE0/DFE1 (`0xFC088000`,
  `0xFC089000`). The sync-generator objects poll bit 0 of `+0x10` in their register blocks
  at `0xFC0A1100…` and `0xFC099100` and log "Last frame is not started" while it reads 1.

### Boundaries

- One run per build, on macOS only; no Linux build was made. The firmware-free tests of
  `tests/test_qemu_a6000_machine.py` passed against the **all** build (16 run, 1 skipped
  for missing dosfstools).
- Seen only on QEMU 10.1.0 in the contributor's fork, not repeated here: in S mode the
  wheel and the dial each move the shutter speed one third stop per press; in M mode the
  wheel sets the shutter speed and the dial the aperture; the dial steps through Playback
  pictures. The 4-counts-per-wheel-step rule comes from those S-mode runs.
- Model assumptions, marked `hypothesis` in the source: the sync-generator start bit clears
  at the next 33.367 ms frame boundary; the real bootloader's interrupt configuration is
  not known. DFE sources 2..13 are not driven.
- The shared GPIO model still raises the line on both edges of a pin and holds it for
  1 ms; a second edge of the same pin inside that time is not a new interrupt.
- No still capture, EVF, lens or movie behaviour is claimed. A synthetic NAND was not
  tested with this change.
- These are emulator observations. Nothing was run on a camera for this update.
