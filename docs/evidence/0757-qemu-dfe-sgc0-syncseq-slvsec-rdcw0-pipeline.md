# E0757 — Cross-Body `DrvSGC0` / `SyncSeq` / `DfeTop` / `SYSV` Contract Map, Bare-Metal Stall Diagnosis, and QEMU `dfe-sensor=on` / `sysv-cold=on` Bayer RAW Pipeline

```text
ID: E0757
Date: 2026-11-28
Claim: Maps the 72 MHz CXD90045 / CXD90057 Digital Front-End (DFE) sensor timing, frame sequencer, interrupt routing, SLVS-EC link-lock, and RdcW0 Bayer RAW DMA contracts across ILCE-6400 2.00 (90d515cf...), ILCE-6400A 1.01 (3029a174...), and ZV-E10 2.02/2.03 (bc6cba98..., pinned via qn06142/zv-e10-re@9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7); proves bit-identical dfe_dat.bin (703f457a..., 65,536 B) and dfe_app.bin (8f9a8d3f..., 1,956 B); decodes every DrvSGC0 (0xFC0A1100..0xFC0A1BE0), SyncSeq (0xFC080000..0xFC080114), DfeTop (0xFC0A0000..0xFC0A0208), DFE VIC (0xFC01F000..0xFC01FF00, GIC IRQs 135/136), SLVS-EC (0xFC0F00B0), RdcW0 (0xFC0E1100 & 0xFC0A9200), and SYSV (0xF3402000) register/bitfield; isolates the cold bare-metal DrvSGC0 (0xFC0A1110 = 0x00000001, HVcnt = 0x3FFF3FFF, Fcnt = 0x7C) and SyncSeq (0xFC080030 = 0x00EE466C) stall to unstarted master SYSV (0xF3402000 + SUGI_CLK 0xF3400000 + CLKGEN 0xF280xxxx) and DfeTop / Leo reset gates; replays unmodified ARM Thumb-2 DrvSgc (338 insns Mode-1, 450 insns with NLR LUT, 63/73/73 insns across trig_mode 0/1/2) and SyncSeq_Setup (583 insns) with byte-identical MMIO write traces across ILCE-6400 and ILCE-6400A; and implements opt-in QEMU device models (dfe-sensor=on, sysv-cold=on in cxd90045_dfe.c, cxd90014_vph.c, cxd90014_p2s.c, sony_a6400.c) verified end-to-end from cold-SYSV stall through SYSV unlock, SyncSeq P2S #7 (0x00B3=0x90) release, SLVS-EC lock, VIC vector dispatch, and 14-bit RGGB Bayer RAW DMA readout.
Status: verified-offline (6/6 TestDrvSgc0SyncSeq + Unicorn replays on ILCE-6400 & ILCE-6400A + 6/6 QemuDfeSensorTests + 20/20 combined WSL qtest suite + 36/38 unittest & 112/112 standalone rule-window checks PASS; signal-routing explanation for ZV-E10 bare-metal cold stall marked [INFERENCE] pending physical read-only /dev/mem or bare-metal confirmation)
```

## Input and Tool Identities

| Artifact / Binary / Reference | Size | SHA-256 / Commit |
| --- | ---: | --- |
| `ILCE-6400` 2.00 packaged `av-cam.bin` (`nflasha3_unpacked/av-cam.bin`, `ORIL` rev 4, base `0x635C6000`) | 16,674,456 B | `90d515cfec8b936f1ba9a69dcac462816b880297752a652474d67c4bba39125e` |
| `ILCE-6400A` 1.01 packaged `av-cam.bin` (`nflasha3_unpacked/av-cam.bin`, `ORIL` rev 4, base `0x635C6000`) | 16,822,380 B | `3029a174b393fee9b2624d27737c20bc60e71cbbb33736b2a9363b57184ee03d` |
| `dfe_dat.bin` (`64 KiB` DFE ARM coprocessor image, bit-identical across `ILCE-6400` 2.00, `ILCE-6400A` 1.01, and `ZV-E10` 2.02/2.03) | 65,536 B | `703f457a480eee74d246e6df8b4f14ca83680ed64b016f1c0a96bed7993989d7` |
| `dfe_app.bin` (DFE companion image, bit-identical across `ILCE-6400` 2.00 and `ILCE-6400A` 1.01) | 1,956 B | `8f9a8d3f8e270d959088c2cb4029b8a115be8d72c70cb18932f4772c27c3cd5f` |
| Upstream `qn06142/zv-e10-re` (`2026-11-28`, MIT; `docs/hardware/SENSOR_PIPELINE.md` from `5937639cd3e2c55bb367e0d4ee9ca5d2c8fee330`) | — | `9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7` |
| Upstream `ZV-E10` `2.02` / `2.03` `av-cam.bin` (referenced by `qn06142/zv-e10-re`) | 17,289,388 B | `bc6cba98c1ce5ef9c7692b9701ed855c25958d285a646ef90dc3fdf398f5c4fa` |
| Compiled WSL QEMU `11.1.3` (`qemu-system-arm` with `emulators/qemu-a6400` + `emulators/a6000` overlays; base `005c61c0ef6f6ae615283440b4bb7be51b2c6ddd`) | — | `a7fee3558e2febfb819ba302ccb1243d979d76a6ddae849a8f6948023b9770bb` |
| Local contract mapper, Unicorn harness & state machine ([`src/a6400/sensor/sgc0_syncseq.py`](../../src/a6400/sensor/sgc0_syncseq.py)) | — | Repository working tree (`2026-11-28`) |
| Local QEMU DFE device model ([`emulators/qemu-a6400/qemu-overlay/hw/misc/cxd90045_dfe.c`](../../emulators/qemu-a6400/qemu-overlay/hw/misc/cxd90045_dfe.c)) | — | Repository working tree (`2026-11-28`) |

See [docs/RELATED_REPOSITORIES.md](../RELATED_REPOSITORIES.md) for the commit-pinned provenance entry and transfer/safety boundaries on `qn06142/zv-e10-re`.

---

## 1. Cross-Body Symbol, Vtable, and Interrupt Correspondence (`[OFFLINE]`)

`Camera::DFECore::DfeCoreSgcBase` stores the DFE MMIO base `0xFC000000` (`mov.w r1, #-0x4000000`) at `[this + 0x04]` (`[this + 0x08] = sgc_id`, `[this + 0x0C] = 0x36`) and loads 2-byte-aligned `+0x0A11xx` / `+0x0A1Bxx` literal offsets in Thumb-2 leaf accessors, which is why 4-byte-aligned literal scans miss `0xFC0A11xx`. [`extract_sgc0_syncseq_contracts`](../../src/a6400/sensor/sgc0_syncseq.py) resolves RTTI, primary Itanium C++ ABI vtables, Thumb-2 `BL`/`B.W` branches, and `SyncSeq_Setup` PIC `.bss` references automatically across `ORIL` v4 images:

| Symbol / Hardware Contract | `ILCE-6400` (`2.00`) | `ILCE-6400A` (`1.01`) | `ZV-E10` (`2.02` / `2.03`) |
| --- | --- | --- | --- |
| `ORIL` rev 4 load base / entry | `0x635C6000` / `0x635C6044` | `0x635C6000` / `0x635C6044` | `0x635C6000` / `0x635C6044` |
| `Camera::DFECore::DfeCoreSgcBase` vtable | `0x644FAC08` | `0x6451D758` | — |
| `Camera::DFECore::DrvSgc` vtable | `0x644FAC60` | `0x6451DC30` | — |
| `DrvSgc::Enable` (`vtable[+0x00]`) | `0x63CD71E8` | `0x63CEE744` | `0x63D267A4` |
| `DrvSgc::Disable` (`vtable[+0x04]`) | `0x63CD727A` | `0x63CEE7D6` | — |
| `DrvSgc::StartOneShot` (`vtable[+0x08]`) | `0x63CD7436` | `0x63CEE992` | `0x63D269F2` |
| `DrvSgc::IsConfigEnabled` (`vtable[+0x14]`, `+0x110C[0]`) | `0x63CD7222` | `0x63CEE77E` | — |
| `DrvSgc::IsRunning` (`vtable[+0x18]`, `+0x1110[16]`) | `0x63CD7236` (`0x6378054C`) | `0x63CEE792` | `0x637A3F60` |
| `DrvSgc::DumpDiagnostics` (`vtable[+0x1C]`) | `0x63780910` | `0x63797D54` | `0x637A4324` |
| `DrvSgc::ConfigureOutputs` | `0x63CD7486` | `0x63CEE9E2` | `0x63D26A42` |
| `DrvSgc::SetTiming` | `0x63CD7510` | `0x63CEEA6C` | `0x63D26ACC` |
| `DrvSgc0Start(this, trig_mode, rpt)` | `0x63780864` | `0x63797CA8` | `0x637A4278` |
| `DrvSgc::SetDrvSgc0Nlr` (`+0x1174`, `+0x1180`, `+0x1B80..1BE0`) | `0x63CD75A0` (`0x63CD734C`) | `0x63CEEAFC` | — |
| `setDrvSgc0Vd0Mask` (`0xFC0A113C[0]`) | `0x6377D918` (`0x63CD764E`) | via `DrvSgc` leaf table | `0x6379CF04` |
| `Leo` reset-bit helper (`0xFC021024` `BIT(6)`/`BIT(7)`) | `0x63CD82AE` | `0x63CEB810` | — |
| `DfeTop` core W1S (`0xFC0A0000`) / W1C (`0xFC0A0004`) | `0x63CD84C6` / `0x63CD84D0` | `0x63CEBA28` / `0x63CEBA32` | — |
| `Camera::Imaging::SyncSequencer` vtable | `0x644FC7C8` (`0x63D208A6`) | `0x6451F308` | — |
| `SyncSeq_Setup` & `.bss` `clk_mhz` global | `0x635C85F0` / `0x641E411C` | `0x635C85F0` / `0x642054B8` | `0x635C8628` (HS `0x635CE230` / `0x638D15C4`) |
| `Camera::DFECore::RdcW0` vtable | `0x644FB0D8` | `0x6451DB28` | — |
| `SLVS-EC` `0xFC0F00B0` literal VA | `0x6377DF88` | `0x6378EE04` | (`0xFC0F00B0` MMIO) |
| `SYSV` `0xF3402100` master-cfg literal VA | `0x639F1488` | `0x63A02734` | (`0xF3402000` MMIO) |
| `DFE` GIC interrupts (`0x645046F8` / `0x64504708`) | `IRQ 135` (`0x6377DDBC` err), `IRQ 136` (`0x6377DC08` `0xFC01FF00` VIC) | `IRQ 135` / `IRQ 136` | `IRQ 135` / `IRQ 136` |

---

## 2. Complete Register & Bit-Field Contracts (`[OFFLINE]`)

### 2.1 `DrvSGC0` (`0xFC0A1100..0xFC0A1BE0`) — Complete Bit-Field & Gate Table

Disassembly of `DrvSgc` leaf accessors (`0x63780510..0x63780978`), `ConfigureOutputs` (`0x63CD7486`), `SetTiming` (`0x63CD7510`), `DrvSgc0Start` (`0x63780864`), and `SetDrvSgc0Nlr` (`0x63CD75A0`) resolves every register and bitfield in `DrvSGC0`, answering Question #2 of `SENSOR_PIPELINE.md`:

| MMIO Address | Name | Access | Mode-1 / Cold Value | Bitfields & Verified Role |
| --- | --- | :---: | --- | --- |
| `0xFC0A1100` | `SGC0_EN` | RW | `0x00000001` | `[0] EN`: Local block enable after `Leo` `0xFC021024` `BIT(6)` clear (`0x0005B7C3 -> 0x0005B783`) and `DfeTop` `0xFC0A0000` `BIT(8)` W1S. |
| `0xFC0A1108` | `SGC0_INT_TRIG` | RW | `0x00000000` | `[0] INT_TRIG`: Immediate internal software self-trigger (`1` **only** in `trig_mode = 0`; `0` in `trig_mode = 1` and `trig_mode = 2`). |
| `0xFC0A110C` | `SGC0_CFG_EN` | RW | `0x00000001` | `[0] CFG_EN`: Output timing configuration commit flag read by `DrvSgc::IsConfigEnabled`. |
| `0xFC0A1110` | `SGC0_START_STATUS` | RW / RO | `0x00010001` (live) / `0x00000001` (stalled) | `[0] START_REQ` (RW): start request latched by `DrvSgc0Start`. `[16] HW_ACTIVE` (RO, `DrvSgc::IsRunning`): `1` when 72 MHz counters actively run; stays `0` while waiting on `VD0` or `EXT_SYNC`. |
| `0xFC0A1114` | `SGC0_HVCNT_LATCH` | WO | `0x00000001` | `[0] LATCH`: Write `1` to snapshot live `(V_CNT, H_CNT)` into `0xFC0A1118`. |
| `0xFC0A1118` | `SGC0_HVCNT_VAL` | RO | `0x3FFF3FFF` (cold) | `[13:0] H_CNT`, `[29:16] V_CNT`: Latched 72 MHz horizontal tick and vertical line counters (`0x3FFF3FFF` idle sentinel before first tick or after reset). |
| `0xFC0A111C` | `SGC0_FCNT_LATCH` | RW | `0x00000000` | `[0] LATCH`: Pulsed `1 -> 0` to latch the 8-bit frame counter into `0xFC0A1128`. |
| `0xFC0A1120` | `SGC0_HV_CYCLE` | RW | `0x026A0793` | `[13:0] H_MINUS_1` (`1939 = 0x793`), `[29:16] V_MINUS_1` (`618 = 0x26A`): Zero-based `1940 x 619` Mode-1 terminal counts. |
| `0xFC0A1124` | `SGC0_V_SUB101` | RW | `0x00000206` | `[15:0] V_MINUS_101`: Pre-end vertical line threshold `V - 0x65` (`619 - 101 = 518 = 0x206`). |
| `0xFC0A1128` | `SGC0_FCNT_VAL` | RO | `0x0000007C` (cold) | `[7:0] FCNT` (`[0] FCNT_LSB` logged by `DumpDiagnostics`): Latched 8-bit frame counter (`0x7C` cold/idle sentinel). |
| `0xFC0A112C` | `SGC0_RPT` | RW | `0x00000001` | `[0] RPT`: Continuous repeat (`1` = repeating LiveView, `0` = single-shot still that auto-clears `+0x1110` after 1 frame). |
| `0xFC0A1130` | `SGC0_EXT_SYNC` | RW | `0x00000000` | `[16] EXT_SYNC`: Sync source MUX (`0` = primary `SYSV` / `SyncSeq` `VD0` in `trig_mode = 1`; `1` = alternate external `EXT_SYNC` in `trig_mode = 2`). |
| `0xFC0A113C` | `SGC0_VD0_MASK` | RW | `0x00000000` | `[0] VD0_MASK` (`setDrvSgc0Vd0Mask`): When `1`, suppresses outgoing `VD0`/`XVS` sync pulses (`DFE_IRQ_SGC0_VD0`, `SLVS-EC` lock, and downstream `RdcW0` frame triggers) while internal `Fcnt` still increments. |
| `0xFC0A1140` | `SGC0_OUT_SEL` | RW | `0x00040007` | `[2:0] VD_SEL` (`7` when `v_pulse > 0` else `6`), `[18:16] HD_SEL` (`4`). |
| `0xFC0A114C` | `SGC0_VD_WIDTH` | RW | `0x00000004` | `[13:0] VD_PULSE_WIDTH`: Vertical sync pulse width (`4` in Mode-1). |
| `0xFC0A1150` | `SGC0_HD_EDGES` | RW | `0x00250005` | `[13:0] HD_START` (`SensHDTim + 2 = 5`), `[29:16] HD_END` (`5 + 32 = 0x25`). |
| `0xFC0A1174` | `SGC0_NLR_WINDOW` | RW | `0x00000000` | `[13:0] NLR_START`, `[29:16] NLR_END`: Non-linear readout line window programmed by `SetDrvSgc0Nlr`. |
| `0xFC0A1180` | `SGC0_NLR_LUT_EN` | RW | `0x00000000` | `[0] NLR_LUT_EN`: Enables multi-slope NLR LUT bank at `0xFC0A1B80..0xFC0A1BE0`. |
| `0xFC0A1194` | `SGC0_HD0_COMMIT` | RW | `0x00010000` | `[16] HD0_OUT_EN`: `HD0`/`XHS` output pad enable committed at end of `ConfigureOutputs`. |
| `0xFC0A1198` | `SGC0_VD0_COMMIT` | RW | `0x00010000` | `[16] VD0_OUT_EN`: `VD0`/`XVS` output pad enable committed at end of `ConfigureOutputs`. |
| `0xFC0A119C` | `SGC0_SYNC_OPT` | RW | `0x00000000` | `[16] SEC_SYNC`: Secondary sync gate cleared to `0` (`0x63D201D6(0)`) right before `SyncSeq` / `DrvSGC0` start. |
| `0xFC0A11A8` | `SGC0_NLR_CTRL` | RW | `0x026B0001` | `[0] NLR_EN` (`1`), `[29:16] V_ALT` (`nlr_delta + V - 1 = 619 = 0x26B`). |
| `0xFC0A11AC` | `SGC0_NLR_FRAC` | RW | `0x00110061` | `[7:0] NLR_DEN` (`97 = 0x61`), `[23:16] NLR_NUM` (`17 = 0x11`): Fractional line cadence `(619 + 17/97) * 1940 = 1,201,200` (`0x00125430`) ticks @ `72 MHz` (`16,683.33 us` = `59.94 Hz`). |
| `0xFC0A1B80..1B8C` | `SGC0_NLR_HDR0..3` | RW | `0x00000000` | 4-word NLR LUT header (`base`, `[12:0]/[28:16]` bounds, `step`, and `[1:0]/[17:16]/[20]` mode flags) written by `SetDrvSgc0Nlr`. |
| `0xFC0A1B90..1BE0` | `SGC0_NLR_LUT_00..20` | RW | `0x00000000` | 21-word multi-slope NLR lookup table payload (20 packed `u16` pairs + terminal word; `0xFC0A1B80..0xFC0A1BE0` total bank) written by `DrvSgc::SetNlrLut` (`0x63CD734C`). |

### 2.2 `SyncSeq` (`0xFC080000..0xFC080114`), `DfeTop` (`0xFC0A0000..0xFC0A0208`), `DFE` VIC/Mailbox, `SLVS-EC`, `RdcW0`, and `SYSV`

| Block & Span | Key Registers & Bitfields | Verified Contract |
| --- | --- | --- |
| **`SyncSeq`** (`0xFC080000..0xFC080114`) | `+0x014` `SOFT_PULSE` (`1->0` / write `1`), `+0x024` `MODE_CTRL = 0`, `+0x028` `PERIOD_CAP = 0x00FFFFFC`, `+0x030` `LIVE_INTERVAL` (`0x00EE466C` cold -> `0x00125430` live), `+0x038` `FRAME_CYCLES = 0x00125430`, `+0x03C/054/06C/084/09C` slot delays (`0x10`), `+0x040/058/070/088/0B8/0C8/0D8/0E8` slot enables (`0`), `+0x0F4` `EXT_TRIG_TIMER = 0x00012AC8` (`76,488` ticks = `1,201,200 - 72 * 15,621 us`), `+0x0F8/0FC` multi-frame exposure (`0`), `+0x104` / `+0x110` W1C IRQ status, `+0x108` / `+0x114` IRQ mask/enable, `+0x10C` `PREARM_GATE` (`3 -> 2` pre-arm) | Latches master `SYSV` `VD` period into `+0x030` and fires `+0x0F4` (`1,062.33 us` into the `1V` period) to raise `DFE_IRQ_SYNCSEQ_0`/`1` (`BIT(17)`/`BIT(18)`, VIC ch `17`/`18`) and pulse `P2S` Ch 1 `frame-sync`, releasing queued `run_mode = 0` burst #7 (`IMX284` `0x00B3 = 0x90`). |
| **`DfeTop`** (`0xFC0A0000..0xFC0A0208`) | `+0x000` `CORE_W1S` / `+0x004` `CORE_W1C` (`BIT(7)=RDC`, `BIT(8)=DrvSGC0`, `BIT(9)=DrvSGC1`, `BIT(15)=AIF`, `BIT(16)=SLVS-EC`, `BIT(19)=RdcW0`, `BIT(24)=Bridge`), `+0x010..+0x030` 3-bank sub-IRQ status/W1C/mask, `+0x108` `BRIDGE_GATE` (`[0]=1`), `+0x150=0x02000000` / `+0x178=0x00020000` preview crossbar, `+0x154=0x40000000` / `+0x180=0x00010000` RAW `RdcW0` crossbar, `+0x200` `IRQ_EN` / `+0x204` `IRQ_W1C` / `+0x208` `IRQ_STAT` (`BIT(1)=SGC0_VD0`, `BIT(17/18)=SyncSeq`, `BIT(25)=RdcW0`) | W1S/W1C clock/power/bridge gating and sub-IRQ aggregation into `GIC IRQ 136`. Writing `1` to `BIT(8)` of `+0x004` (`CORE_W1C`) immediately stops `DrvSGC0`, drops `+0x1110[16]`, and resets `HVcnt` to `0x3FFF3FFF`. |
| **`DFE` Doorbell, Mailbox, Timers & VIC** (`0xFC01E000..0xFC01FFFF`, `0xFC030000`, `0xFC098000`) | Doorbell `0xFC01E018` `BIT(27)`; VIC enable/clear `0xFC01F010/014/01C`, vector table `0xFC01F100 + 4*ch` (`ch 17, 18, 23, 24, 26`), active vector read/ack `0xFC01FF00` (`0xFF` idle); Mailbox `0xFC0300F4` (`0xFF` ready), `0xFC0300F8` (`fw_pa >> 8` / status `0xFF00` run, `0xFE00` idle), `0xFC0300FC` (`BIT(0)` kick, `BIT(16)` abort, `BIT(20)` stop, `BIT(28)` run); Timer gates `0xFC098808 = 0x003C0006` (W1S / `+0x80C` W1C), `0xFC098908 = 0x00020000` | Stages `64 KiB` `dfe_dat.bin` (`703f457a...`), latches firmware header, asserts timer ready gates, and dispatches prioritized vector IDs (`[0x11, 0x12, 0x17, 0x1A]`) to `av-cam` `0x6377DC08` on `GIC IRQ 136`. |
| **`SLVS-EC RX`** (`0xFC0F0000..0xFC0F0FFF`) | `+0x050` PHY/lane gate (`[0]`), `+0x0B0` / `+0x1B0` `LINK_LOCK` (`[0]` RO) | Reports `1` iff (`DfeTop` `BIT(16)` or `+0x050[0]`), `DrvSGC0` is actively ticking (`0xFC0A1110[16] == 1` with unmasked `VD0`/`HD0` outputs in `DrvSgc0SyncSeqModel`), and `P2S` Ch 1 has placed `IMX284` into active `SLVS-EC` streaming (`0x00B3 == 0x90`, standby cleared). |
| **`RdcW0`** (`0xFC0E1100..0xFC0E115C` & `0xFC0A9200..0xFC0A922C`) | **Path A (standalone `0xFC0E1100`):** `+0x1100[0]` `EN`, `+0x110C` bit-depth/repeat (`12`/`14`/`16`-bit, `BIT(8)` continuous), `+0x1110` `START` (`[0]` arm, `[8]` repeat, `[16]` active), `+0x1120..+0x1134` geometry (`W`, `H`), `+0x1150` `DST_PA`, `+0x1154` `STRIDE_BYTES`, `+0x1158` `FRAME_CNT`, `+0x115C` `END_PA`. **Path B (`DFE AIF` `0xFC0A9200`):** `+0x200[0]` `ARM`, `+0x204` `BIT(30)` abort, `+0x210` `END_PA`, `+0x220` `DST_PA`, `+0x224` `LINE_BYTES`, `+0x228` `LINE_GAP`, `+0x22C` `H - 1`. | On each unmasked `DrvSGC0` `VD0` frame while `SLVS-EC` `LINK_LOCK == 1`, writes deterministic 14-bit (`0x0001..0x3FFF`) RGGB Bayer RAW frames into validated guest DRAM (`0x00000000..0x3FFFFFFF` or `0x80000000..0xBF7FFFFF`, `<= 32 MiB`) and asserts `DFE_IRQ_RDCW0` (`BIT(25)`, VIC ch `26`). |
| **`SYSV`** (`0xF3400000` & `0xF3402000..0xF3402100`) | `SUGI_CLK` `0xF3400000[0]`; `SYSV` `+0x010` `ENABLE` (`[1:0]`), `+0x014` `H_PERIOD = 0x794` (`1940`), `+0x018` `V_PERIOD = 0x26B` (`619`), `+0x01C` `SYNC_MODE = 1`, `+0x020` `OUT_EN = 1`, `+0x024` `STATUS` (RO), `+0x028` `IRQ_CTRL` (`GIC IRQ 98`), `+0x02C` / `+0x030` `PHASE_OFFSET0/1 = 0x8000000A`, `+0x094/+0x098` extended timing, `+0x100` `MASTER_CFG` | Master engine V-clock generator ([E0529](./0529-qemu-a6400-adf-decode-sysv-frame-clock-unblocks-objmic.md)) driving both `EVSEL` (`0xF3C30000` `ext-vd` source 29) and `DFE SyncSeq` / `DrvSGC0` (`trig_mode = 1`) at `1,201,200` ticks (`16,683.33 us`). |

---

## 3. Root-Cause Analysis of Cold Bare-Metal `DrvSGC0` & `SyncSeq` Stall (`[OFFLINE]` vs `[INFERENCE]`)

Answering Question #1 from `qn06142/zv-e10-re` `docs/hardware/SENSOR_PIPELINE.md` requires strictly separating `[OFFLINE]` binary proofs from `[INFERENCE]` hardware signal-routing conclusions:

1. **`[OFFLINE]` Verified Firmware Behavior (`ILCE-6400`, `ILCE-6400A`, `ZV-E10` `av-cam.bin`):**
   - `DrvSgc0Start` (`0x63780864` / `0x63797CA8` / `0x637A4278`) implements a 3-way branch on `trig_mode`:
     - `trig_mode == 0`: writes `0xFC0A1108[0] = 1` (`INT_TRIG`) and `0xFC0A1110[0] = 1` (self-starts immediately without touching `0xFC0A1130[16]`).
     - `trig_mode == 1` (stock Mode-1 LiveView and `StartOneShot`): writes `0xFC0A1130[16] = 0`, `0xFC0A1108[0] = 0`, and `0xFC0A1110[0] = 1`.
     - `trig_mode == 2`: writes `0xFC0A1130[16] = 1`, `0xFC0A1108[0] = 0`, and `0xFC0A1110[0] = 1`.
   - In the stock `SyncTask` HS sequence (`0x638A77C0..0x638A7A60`), `av-cam.bin` unmasks `VD0` (`0xFC0A113C[0] = 0`), clears secondary sync (`0xFC0A119C[16] = 0`), runs `SyncSeq_Setup` (`0x635C85F0`: `+0x028 = 0x00FFFFFC`, `+0x038 = 0x00125430`, `+0x0F4 = 0x00012AC8`), programs `DrvSgc::SetTiming` (`1940 x 619 + 17/97 = 0x00125430` ticks), and arms `DrvSgc0Start(..., trig_mode=1, rpt)` while `SYSV` (`0xF3402000` + `SUGI_CLK` `0xF3400000` + `CLKGEN` `0xF280xxxx`) is already running at `1,201,200` ticks (`16,683.33 us`).
   - `DfeTop` requires `0xFC0A0000` W1S (`BIT(8)` `DrvSGC0`, `BIT(24)` `Bridge`) paired with `0xFC0A0108[0] = 1` and `Leo` `0xFC021024` `BIT(6) == 0`; any subsequent write of `BIT(8)` to `0xFC0A0004` (`CORE_W1C`) or `BIT(6)` to `0xFC021024` resets `DrvSGC0`.

2. **`[INFERENCE]` Hardware Signal-Routing Root Cause of Upstream Cold-Boot Stall:**
   - Upstream `cxd90057_pipeline.h` and `stock-reg-diff-03` only inspected and programmed `0xFC0xxxxx` DFE pages, omitting `SYSV` (`0xF3402000`), `SUGI_CLK` (`0xF3400000`), and `CLKGEN` (`0xF280xxxx`).
   - After a cold camera reboot (`cold-warm-exp-05..35`), `SYSV` is stopped, so no master `VD` edge enters `SyncSeq`: `0xFC080030` remains at its cold-reset value `0x00EE466C` instead of latching `0x00125430`, `SyncSeq` `+0x0F4` (`0x00012AC8`) never fires to release `P2S` Ch 1 `run_mode = 0` burst #7 (`0x00B3 = 0x90`), `DrvSGC0` in `trig_mode = 1` stays armed-waiting (`0xFC0A1110 = 0x00000001`, `HVcnt = 0x3FFF3FFF`, `Fcnt = 0x7C`), and `SLVS-EC` `0xFC0F00B0` remains `0`.
   - In upstream `senser-bayer-test-06..14`, a prior partial stock `EE` start (`test-05`) had already started `SYSV` (`0xF3402000`), which persisted across warm Senser runs and supplied the master `VD` pulses needed by both `SyncSeq` and `DrvSGC0` (`trig_mode = 1`). Standalone bare-metal bring-up must either start `SYSV` (`0xF3400000` + `0xF3402000`) alongside `DfeTop` `BIT(24)` + `0xFC0A0108[0] = 1` or use `trig_mode = 0` (`0xFC0A1108[0] = 1`) + immediate `P2S` (`run_mode = 2`) / `SyncSeq` `+0x014` soft-pulse.

---

## 4. Bounded Unicorn Thumb-2 Replays & QEMU `dfe-sensor=on` / `sysv-cold=on` Receipts

### 4.1 Unmodified Firmware Thumb-2 Replays (`src/a6400/sensor/sgc0_syncseq.py`)

Original Thumb-2 machine code from unpacked `ILCE-6400` 2.00 (`90d515cf...`) and `ILCE-6400A` 1.01 (`3029a174...`) `av-cam.bin` was executed in bounded Unicorn harnesses (`max_insns <= 2000`, strict return-stub PC check) and fed directly into `DrvSgc0SyncSeqModel`:

- `unicorn_replay_drv_sgc0_mode1` (`Enable` + `ConfigureOutputs` + `SetTiming` + `DrvSgc0Start(trig_mode=1, rpt=1)`): **`338` instructions** on `ILCE-6400` 2.00 and **`338` instructions** on `ILCE-6400A` 1.01 (`450` instructions when `SetDrvSgc0Nlr` is included), producing **100% identical `(MMIO_addr, value)` write sequences** (`0xFC021024=0x0005B783`, `0xFC0A0000=0x00000100`, `0xFC0A1100=1`, `0xFC0A1140=0x00040007`, `0xFC0A114C=4`, `0xFC0A1150=0x00250005`, `0xFC0A110C=1`, `0xFC0A1198=0x00010000`, `0xFC0A1194=0x00010000`, `0xFC0A1120=0x026A0793`, `0xFC0A11A8=0x026B0001`, `0xFC0A11AC=0x00110061`, `0xFC0A1124=0x00000206`, `0xFC0A112C=1`, `0xFC0A1130=0`, `0xFC0A1108=0`, `0xFC0A1110=1`).
- `unicorn_replay_drv_sgc0_start_modes`: **`63` instructions** (`trig_mode = 0`), **`73` instructions** (`trig_mode = 1`), and **`73` instructions** (`trig_mode = 2`) across both bodies with identical MMIO traces.
- `unicorn_replay_syncseq_setup` (`Mode-1`, `59.94 Hz`, `readout_us = 15,621`, `72 MHz`): **`583` instructions** on both `ILCE-6400` 2.00 and `ILCE-6400A` 1.01, writing `0xFC080028 = 0x00FFFFFC`, `0xFC080038 = 0x00125430`, and `0xFC0800F4 = 0x00012AC8`.

### 4.2 Exact Verification Commands and Results

```powershell
# 1. Windows Python unit + Unicorn original-code replay + static QEMU hygiene tests
.\venv\Scripts\python.exe -m unittest tests/test_sgc0_syncseq.py tests/test_qemu_dfe_sensor.py -v
# -> Ran 12 tests in 1.140s: OK (8 PASS: 6/6 test_sgc0_syncseq + 2/2 static hygiene; 4 native WSL qtests skipped on Windows host)
```

```bash
# 2. Native WSL QEMU 11.1.3 (sha256 a7fee3558e2febfb819ba302ccb1243d979d76a6ddae849a8f6948023b9770bb) qtest + QMP verification
A6400_QEMU_DIR=/home/administrator/sony-a6400-qemu-11.1.3 \
python3 -m unittest \
  tests/test_qemu_dfe_sensor.py \
  tests/test_qemu_liveview.py \
  tests/test_qemu_clkrst_pmic_uhs_hdmi.py -v
# -> 6/6 PASS in test_qemu_dfe_sensor.py; 19/20 PASS (1 skipped without ZMA1 sidecar) or 20/20 PASS with A6400_ZMA1_STARTUP_SIDECAR set

# 3. Baseline QEMU rule-window & default-off regression checks (emulators/qemu-a6400/tests/)
A6400_QEMU_DIR=/home/administrator/sony-a6400-qemu-11.1.3 \
QEMU_BIN=/home/administrator/sony-a6400-qemu-11.1.3/build/qemu-system-arm \
python3 -m unittest \
  emulators/qemu-a6400/tests/test_qemu_f290.py \
  emulators/qemu-a6400/tests/test_qemu_f291_l0117.py \
  emulators/qemu-a6400/tests/test_qemu_f2a0.py \
  emulators/qemu-a6400/tests/test_qemu_f312_l0119.py \
  emulators/qemu-a6400/tests/test_qemu_f3c0.py \
  emulators/qemu-a6400/tests/test_qemu_l0122_zero_windows.py \
  emulators/qemu-a6400/tests/test_qemu_l0126_f30a0_rf.py \
  emulators/qemu-a6400/tests/test_qemu_l0137_sa4_id.py \
  emulators/qemu-a6400/tests/test_qemu_l0154_dwc3_default.py \
  emulators/qemu-a6400/tests/test_qemu_l0159_watchdog_default.py -q
# -> Ran 38 tests: OK (36 PASS, 2 skipped); standalone f022 (12/12), f292 (18/18), f292c (16/16), f304 (18/18), rcraw (41/41), uart3 (7/7) -> 112/112 PASS
```

See [E0700](./0700-qemu-touchscreen-peripherals-and-multimodel.md) and [E0715](./0715-qemu-f01-playback-capture-uhs-mmio-and-kexec-prereqs.md) for the surrounding 8-body QEMU machine and `P2S` Ch 1 `IMX284` recognizer baselines preserved when `dfe-sensor` and `sysv-cold` remain at their default `off` (`DFE_SENSOR=0`, `SYSV_COLD=0`) setting.

---

## 5. Assumptions, Non-Claims, Repeatability, and Next Boundary

- **Offline / emulator-only execution (zero physical writes):** No physical `ILCE-6400`, `ILCE-6400A`, or `ZV-E10` camera was accessed, no `/dev/mem` reads or writes were executed, and upstream `qn06142/zv-e10-re` USB Senser write scripts / `boot.ko` takeover modules remain excluded under `tools/devmem_policy.py` and standing read-only camera rules.
- **Synthetic Bayer scene vs physical photon readout:** `cxd90045_dfe.c` (`RdcW0` at `0xFC0E1100` and `DFE AIF` `0xFC0A9200`) synthesizes deterministic 12/14/16-bit RGGB Bayer patterns from `LIVEVIEW_SOURCE` (`bars` / external PGM) rather than modelling optical photon shot noise, analog CDS, or executing the `64 KiB` `dfe_dat.bin` coprocessor ARM instructions on a secondary CPU core.
- **Repeatability:** Every contract address, SHA-256 digest, Unicorn Thumb-2 instruction count (`338` / `450` / `63` / `73` / `583`), and QEMU `qtest` transition (`0xFC0A1110: 0x00000001 -> 0x00010001`, `0xFC080030: 0x00EE466C -> 0x00125430`, `0xFC0F00B0: 0 -> 1`, VIC `[0x11, 0x12, 0x17, 0x1A]`) is deterministic and verified in [`tests/test_sgc0_syncseq.py`](../../tests/test_sgc0_syncseq.py) and [`tests/test_qemu_dfe_sensor.py`](../../tests/test_qemu_dfe_sensor.py).
- **Next boundary:** Boot full stock `ILCE-6400` / `ILCE-6400A` / `ZV-E10` `nflasha3` firmware under `-M sony-a6400,dfe-sensor=on` (and optional `sysv-cold=on`) to measure live `av-cam.bin` `DrvSGC0` / `SyncSeq` / `RdcW0` transitions across still capture and mode switches, and feed `RdcW0` Bayer DRAM frames directly into `CamCore` (`0xF3000000..0xF3030000`) / `IDT_CAM` ISP stages.
