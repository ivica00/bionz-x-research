# QEMU `sony-a6400`: findings and next steps

## Viewfinder console (2026-11-16 / E0544)

This section continues the captured-input boot recorded in E0524. It does not
replace that qualification. The hash-checked input set, CMD1 sector negotiation,
Task 43 flag-10 wait, unasserted IRQ 101/102 observation, and dormant Task 65
remain as stated below.

Later records E0525–E0541, on that same machine:

- E0525 puts a ZV-E10 `ulogio.bin` stand-in into the per-run overlay. Appframework
  creates its objects and opens view 2. Without a sub-micro model the power lever
  reads OFF (factor 7).
- E0526–E0527 add SIO, GPIO, ADC, and a replay of one read-only 4 KiB sub-micro
  page. With the timing-event block, the factor-7 power-off stops.
- E0528–E0529: `ObjMic` was waiting on ADF `SetPath(0xA0)`. The ported VPH and
  SYSV frame clock let boot reach `modelCamera ACTIVE`, `EEStart`, and `PIN_OPEN`.
- E0530–E0536: Sugilite P3D/PPF draws the shooting-screen OSD. Console 0 is the
  viewfinder readout, so the a5000 640→853 rear-LCD stretch stays off. The centre
  stays black because VPH `0x144` bit0 stays clear. Leo PLL and MIPI D-PHY init
  complete. The imager never writes SA GO and never kicks P2S.
- E0537–E0541: the eye sensor is not the a5000 hinge pin. The finder predicates
  and the two backup bytes do not switch the image set. Forcing `isOutputEvf` to
  return 0 changes 765 focus-mark pixels and leaves the viewfinder skin in place.
- E0542: `viewUnified2.so` registers `LayoutST_NORMAL_*` beside each
  `LayoutST_NORMAL_EVF_*`. `isOutputEvf` at `0x60f5c4` only changes a coordinate.
- E0543: imager `PowerOn` (`0x6379093C`) is only a function-table slot. The `VD_Pre`
  function at `0x6391E614` returns before CamCore start unless virtual slot
  `+0x28` is non-zero and bit 1 of the flag byte is set. `en144` stays
  `0x3000232`.
- E0544: the mode sequencer index is 12 of 43, on wait `0x8347`. Module 1 is the
  only masked mismatch (`0x0105` requested, `0x0100` reported). The imager word
  `0x0A10` matches on both sides.
- E0549: index 0 is module id 1. The frame publisher copies its report from
  `*(0x6434CA54 + 8) + 0x690`. At `SetContentTarget` that halfword is `0x0100`,
  while indexes 1 and 2 already show `0x0285` and `0x0320`. The selector flag
  at `0x643334E8 + 0x8E` is clear, so the other source is not used.
- E0550: the path selector at `0x643305E4` has already accepted request `0x3003`.
  Its result word at `0x643305D0` is `2`. The mode halfword at
  `0x64351898 + 0x690` is still `0x0100` in that same sample.
- E0551: `0x63988EAA` writes `*(0x645A21E4+0x3EB0)+0x690`. At the stall that
  object is `0x6435CC38` and the halfword is `0x0000`. The copy runs only
  when `*(0x645A21E4+0x623C)+0x1F` is 5; that byte is 0. The sequencer
  index at `0x64330614` is 12. Module 1's halfword is still `0x0100`.
- E0553: a write watchpoint on `0x64351F28` caught `0x63922304` seeding `0x0100`
  and `0x639267BC` storing the value returned by `0x63D3BD56`. That function
  returns the old halfword unless the request is `0x0112`. At the stall the
  request at `0x64350F04` is `0x0105`, and the byte at `0x64354174` that lets
  the store run is 0. The index stays 12. No device model changed.

`tools/a6400_probe.py` waits for a `/proc/snsc_boot_time` checkpoint instead of a
fixed sleep. A `/proc/snsc_boot_time` read can block for more than 30 seconds and
then answer (E0530). That delay is not a hung shell.

- E0557: the third writer of `0x64351F28` is `0x63923A1A` in `VD_Sequence`
  (`0x63923874`). Only the HS root `0x639242D8` posts `VD_Sequence`, and only
  in HS slot 0. The HS root is on timing-event source 30 (ch5). The frame
  start `0x63924A38`, which resets the slot, is on source 29 (ch6). The evsel
  model fires both in one pulse, so the root always reads slot 3 and
  `VD_Sequence` never runs. The byte at `0x64354174` is set by `int_HS` once
  per frame; the 0 in E0553 was a mid-frame sample. With the opt-in
  `EVSEL_LATE_US=500` (evsel `late-src=30 late-us=500`), `VD_Sequence` runs
  every frame and module 1 reports `0x0118`. The sequencer passes step 12 and
  waits at step 32, `WAIT{3,4,5,6,10,12}`, on module 3: `0x032E` requested,
  `0x0326` reported. VPH ch51 (YUV) is programmed: `lb=0x400`, 570 lines,
  two alternating bases. The 500 µs phase is an assumption.

- E0561: module 3 accepts `0x32E` only after a counter passes 6. The counter
  advances only on frames with a new, valid PQD Ydet (luminance statistics)
  record. The producer `0x63BEE670` (`PQD_YdetRegReadManager.cpp`) never
  runs, so the consumer reads a zero record every frame.

- [E0563](../docs/evidence/0563-qemu-ydet-adapter-and-opd-comparison.md):
  `0x63D3DD22` calls the Ydet adapter with a handle and four bytes of input
  metadata. The a6400 shares the a5000 OPD base and bit-1 start instruction,
  with seven firmware IRQ records and different IRQ values. The tested OPD
  start sites and Ydet producer stayed idle at step 32. A passive replay
  counted 509 single-window descriptor lookups and 556 consumer visits.

- [E0564](../docs/evidence/0564-qemu-ydet-window-configuration-reaches-camcore.md):
  the window descriptor is valid and writes `0xF3015848–0xF3015854`.
  The getter pair belongs to the third argument of the window setup;
  its first base is `0xF3000000`, yielding candidate result address
  `0xF3015700`. The result publisher still receives no calls.
  A predicate in the SuperSlow manager checks `0x640D39BC` and returns zero.

- [E0565](../docs/evidence/0565-qemu-superslow-field-and-pqd-came-dispatch.md):
  `0x640D39BC` holds a SuperSlow resource pointer. Its constructor clears
  it, and normal `VD_Pre` continues after the resource check. The normal
  result path is the CamCore job callback at `0x639023AC`, which submits
  `EC::PQD_CamE` and reaches the Ydet producer via `0x63D3DD22`. A passive
  trace saw 206 submissions with this callback forwarded to the EE
  controller at `0x63CBCA06`, and no callback execution.

- [E0566](../docs/evidence/0566-qemu-ee-callback-stays-pending.md):
  EE accepts the submissions and writes `0x639023AD` to its pending slot
  at `+0x84`. The observed boot never ran the promotion function
  `0x63CBC586` or the completion load at `0x63CBBF6E`. The registered
  event owner is `CamPigInterruption23` at base `0xF3000000`. Startup
  programs paths 2 and 3, but the unmodelled enable register at `+0x50`
  reads zero between the two writes.

- [E0567](../docs/evidence/0567-qemu-campig-enable-register-readback.md):
  the opt-in word at `0xF3000050` lets original firmware retain both enable
  bits and write `0x0C000000`. Paired replays still wait at step 32 with
  zero activation and result-publication hits.

- [E0568](../docs/evidence/0568-qemu-campig-dispatch-order-and-input-sync.md):
  firmware starts input sync at `0xF3120210` and registers IRQ 36 event A.
  The IRQ 37 table orders completion before promotion for coalesced status.
  No frame callback ran. The 36 MHz input clock is inferred from the
  programmed geometry and nominal frame period.

- [E0569](../docs/evidence/0569-qemu-sgc1-event-a.md):
  the opt-in input timer delivered 231 events to the original IRQ 35
  handler and callback. The earlier IRQ 36 trial instead invoked the
  `0xF3000000` handler. The callback's front-end write branch remained idle.

**Parked live-image checkpoint:** with `EVSEL_LATE_US=500`,
`CAMPIG_ENABLE_READBACK=1`, and `SGC1_EVENT_A=1`, the sequencer
waits at step 32 (`0x032E` requested, `0x0326` reported).
Resume at the IRQ 35 callback `0x6375BBF0`: its front-end write branch
at `0x6375BC46`, CamPig IRQ 36 completion, and the Ydet producer
`0x63BEE670` all stayed idle. The ch51 buffer contents remain unverified.

**Rear-monitor milestone:** [E0572](../docs/evidence/0572-qemu-a6400-rear-lcd-layout-on-panel.md)
captures the original `LayoutST_NORMAL_WITH_INFO_LR` on QEMU console 0
after an opt-in overlay selects device 1 in the firmware display controller.
Stock boot still selects the viewfinder. The next boundary is the
`InputService::getInputEventStatus` key/value contract that selects the
same device through the original input path.

[E0573](../docs/evidence/0573-qemu-a6400-rear-panel-controls.md) adds an
interactive panel around the VPH output. The opt-in keys drive ADC, GPIO,
sub-micro and TPU paths. The original firmware opens MENU and responds to
directional and center input. The manual Finder/Monitor setting is now
reachable for a stock boot verification.

[E0574](../docs/evidence/0574-qemu-a6400-blank-panel-recovery.md) confirmed
the panel was receiving real black frames in a long-running instance.
A fresh instance recovered the original menu. In a separate replay,
boot-time blanks had zero ch53 base and both OSD buffers were empty.
The late blank still needs a same-run register and buffer capture.

[E0575](../docs/evidence/0575-qemu-mod-loader-v2-compatibility-boundary.md)
qualifies the Mod Loader v2 target mismatch. QEMU runs `im.elf` as PID 144
in the observed boot, with Sony library bases different from the current
physical guard. Its unchanged ARM check returns `-5 identity/maps` in
the guest. See [the mod development workflow](MOD_DEVELOPMENT.md).

## Captured-input qualification (2026-11-16 / E0524)

The reproducible input builder now uses all 18 captured images: two 4 MiB
hardware boot areas and the 16 nonempty SDM user partitions. It retains the
captured kernel, initrd, av-cam, libraries and settings together. Two independent
preparations produce identical output hashes; the original captures remain
unchanged. The native eMMC user area begins at backing offset 8 MiB.

The 2 GiB model also needed a real protocol correction: honor the original
`CMD1 0x40000080` sector-mode request. Before the fix, rootfs sector `0x52002`
was read as a byte offset; afterward it reads `0x0A400400`. Both sector-addressed
and byte-addressed constructed-disk regressions pass, without inflating capacity.

Original firmware reaches four CPUs, mounts its captured filesystems, runs LIRO
`entry_pre` / `entry_main`, initializes DMP/stella and answers shell commands.
This is not a complete camera boot or a verified GUI frame.

Stopped, hash-qualified task/IRQ observations refine the older next-step list:

- Task 65 is dormant with no queued activation. Its observed entry target has a
  finite initialization body; the helper sleep alone is not a demonstrated
  sub-micro deadlock.
- Task 43 waits on ITRON flag 10, mask `0x3373`, mode 1, timeout -1.
  Original registered IRQ 101/102 handlers contain paths setting bits 1/2.
  Native traces show both IRQs enabled, with zero input assertions during the
  bounded run.
- Task 1 waits on OSAL semaphore `0x00BF04EE` at av-cam `0x6361BB48`.
  Its lifecycle purpose and status as a global blocker remain unproven.

**Next:** recover the actual register/status/timing producer contract for the
enabled IRQ 101/102 paths. Do not inject a periodic interrupt or signal a flag
merely to advance the firmware. These are qualified observations of separate
waits, not proof of one global deadlock.

Commands, identities, failed receipts and limitations:
[E0524](../docs/evidence/0524-reproducible-captured-qemu-boot-and-liro-waits.md).

## Historical E0521/E0522 snapshot

Snapshot: 2026-11-15, after E0521 and E0522. Nothing below has been run on the real camera.
Labels: [C] confirmed by a run receipt or disassembly, [I] inference, [A] assumption.

## Where the boot stands

`./run.sh` (WSL2) boots the main `nflasha3` firmware through these stages:

1. Kernel at `0x00108000` with the kemco block at `0x00103000`; 4 Cortex-A5 cores online [C].
2. `init` mounts every eMMC partition through a fresh qcow2 overlay (`/`, `/usr`, `/setting`, `/system`, `/log`, …) [C].
3. 40 kernel modules load, including `backup`, `dmm`, `upm`, `mcmn_drv`, `liro`, `dma330` and `hdmi` [C].
4. av-cam (LIRO µITRON) runs `entry_pre` / `entry_main`, and its tasks bind to CPUs 0–3 [C].
5. `stella`, `DMP_2D_Initialize : 0`, the `im.elf` monitor buffer, and a BusyBox `ash` on `ttyAM0` [C].

A shell prompt is not a working camera application. No `liro-*` interrupt has fired yet [C].

## Findings

The bootloader hand-off is not executed; it is reproduced. Each item below was recovered from the
a6400 binaries, then cross-checked against `Oein/a5000-emulator` `docs/BOOT.md`.

| # | Finding | Evidence | How it is modelled |
| --- | --- | --- | --- |
| 1 | The kernel runs at TEXT_OFFSET `0x00108000` and reads `"kemco"+cmdline` from `r2 = 0x00103000`, not ATAGS/DTB | `boot/loadaddr.txt`; `cmdline override at 60103005` [C] | `sony_a6400_load_boot_contract()` |
| 2 | `backup.ko Backup_load=0` expects `Backup.bin` to be preloaded at the physical address in its header word 0 (`0x02E80000`). Without it, `-EIO` and ~20 dependent modules fail | `backupfile_init` @`0x4614`, `Data_check` @`0x3e04` [C] | `-device loader` in `run.sh` |
| 3 | The main boot needs `/setting/kemco.txt`. `data/nflasha3-kemco.bin` is the updater cmdline, whose `memrsv` leaves av-cam's RAM unreserved | E0521 §0.3 [C] | `run.sh` reads it from `setting.tar` (console token swapped) |
| 4 | `liro.ko adrs=035c6000` expects `av-cam.bin` preloaded there (`LIRO` magic at +4) | `Magic unmatched` before the fix, `end entry_main` after [C] | `-device loader` in `run.sh` |
| 5 | Secondary cores: `cxd900x0_smp_init` writes the physical `cxd900x0_secondary_entry` to SRAM `0xFE005010 + 4*(cpu-1)` and issues `SEV`, then the standard pen-release handshake follows | `0x6011ed40..0x6011ee98`, `boot_secondary` @`0x603c08ac` [C] | Secondary WFE stub at `0x1200` |
| 6 | The kernel never writes `GICD_ITARGETSR`. Its `irq_select_affinity` only reads the target byte back | `gic_dist_init` @`0x604951c8`, `0x6011feb0` [C] | CPU0 stub at `0x1000` presets the targets |
| 7 | Per-CPU timers: clockevents ch0/2/3/4 → IRQ 153/155/156/157; hwtimer0..3 = ch5..8 → IRQ 158..161 | tables `0x604d8d80`, `0x603c9354` [C]; hwtimerN = CPU N [I] | Target bytes; `cxd4115_timer.c` |
| 8 | The timer block follows the CXD4115 protocol (clocksource ch1 `0x1031`, periodic `0x1102`, one-shot `0x112`) | kernel timer code [C] | `cxd4115_timer.c` adapted from a5000 (GPL-2.0) |
| 9 | Without `-icount`, ~15 µs hwtimer deadlines expire before they are armed (3,254 warnings per run) | E0522 §0.6 [C] | `run.sh` default `-icount shift=0,sleep=on` [A] |
| 10 | The inputs come from two firmware versions: the kernel is a 2021 build, while rootfs and `/usr` are 2019 (`38111_03.2019052701`) | E0521 §1 [C] | Not fixed; modules load because vermagic matches |

## Known gaps and noise (not blocking the shell)

- All SPIs other than the timers target CPU0 [A]. No CXD90045 GICD dump exists yet.
- `osal_ulogio.ko` fails: `/setting/ulogio.bin` is absent [C].
- `sata_drv.ko` and `msen_drv.ko` are not present in `/usr/kmod` [C].
- Compressed `dnflasha*` mounts fail `ENXIO` (`AL_EMMC_BLK_UZ … Information table is invalid`). init falls back to the uncompressed partitions [C].
- The eMMC driver prints `execute_tuning … forbiden for non-HS200 mode` on every access (~900 lines per run) [C].
- `/system` (`nflasha3`) on `emmc.img` is an empty FAT. av-cam and the other boot-set files are only supplied as preloads [C].
- The watchdog at `0xF2402000` is unmodelled, although the main cmdline sets `wdt.mode=1` [C]. Today it is harmless because unmapped accesses are ignored [I].
- `nflasha1: bogus number of reserved sectors`: the updater partition on `emmc.img` is not FAT [C].

## Next possible steps (suggested order)

For each step, check `Oein/a5000-emulator` first for the CXD90014 equivalent.

1. **Find what av-cam waits on.** No `liro-*` IRQ has fired. The a5000 analogue is
   `docs/RE_AVCAM_IRQ_BOOT.md`: the boot checkpoint `prof` words in RAM, plus the "Liro Start"
   task blocked before sub-micro traffic. Locate the a6400 av-cam checkpoint variables and read them
   from the QEMU monitor (`xp`) to find the blocking task and wait object.
2. **Sub-micro link.** On the a5000, this is the SIO 128-byte exchange triggered by a GPIO strobe,
   with frames replayed from a real snapshot (`docs/RE_SUBMICRO_LINK.md`, `a5k_submicro_*`). Recover
   the CXD90045 SIO base/IRQs and the frame format from av-cam.
3. **Bootloader hand-off page.** The a5000 preloads `0x80021000` (boot reason at +0x4C, read by
   `upm.ko`). The a6400 cmdline has `wbimem=0x1000@0x20021000` and `pm.resume=0x20021054`. Check what
   `upm.ko` and av-cam read there [I: likely the same role].
4. **ADC / keys and front-end units.** ADC channels must read as "no key pressed". Front-end and
   sensor-interface IRQs are needed once live view runs (a5000 §1.1 IRQ table).
5. **Watchdog.** Model `0xF2402000` as a CXD4115 watchdog channel (the a5000 model has `watchdog=true`)
   before long runs, so a real expiry is visible instead of silently ignored.
6. **Display.** Sugilite/`dmp_smaph` scan-out to a QEMU console. The a5000 has `dmp_smaph.c`,
   `cxd90014_vph.c` and `a5k_fbview.c`. The OSD format is known from E0298 (640×480 RGBA4444).
7. **Optional real-camera verification (read-only, standing authorization):** one 4 KiB read of
   the GIC distributor page `0xF8001000` would replace the CPU0 target default. It would also confirm
   inference 7.
8. **Housekeeping.** Build `emmc.img` reproducibly from the firmware partition images, with a
   script and hashes. Pick one firmware release for the kernel, rootfs and `/usr`. Silence or rate-limit
   the eMMC tuning warning once its cause is understood. Add `ulogio.bin` if a real copy exists.

## Reproduce

```bash
./setup-qemu.sh && cd "$QEMU_TREE/build" && ninja qemu-system-arm qemu-img   # see README.md
TIMEOUT=110 ./run.sh
```

Records: [E0521](../docs/evidence/0521-qemu-a6400-main-firmware-boots-to-avcam.md),
[E0522](../docs/evidence/0522-qemu-a6400-smp-four-cores-and-cxd4115-timers.md).
Receipts: `artifacts/qemu-a6400/e0521/`, `artifacts/qemu-a6400/e0522/`.
