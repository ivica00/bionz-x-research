"""``DrvSGC0``, ``SyncSeq``, ``DfeTop``, and ``SYSV`` contract mapper and model.

Maps Sony BIONZ X (``CXD90045`` / ``CXD90057`` + ``IMX284``) 72 MHz sensor timing
and synchronization blocks from unpacked ``av-cam.bin`` ``ORIL`` rev 4 images
across ``ILCE-6400`` 2.00 and ``ILCE-6400A`` 1.01, cross-referenced against
``qn06142/zv-e10-re`` (commit ``9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7``,
``docs/hardware/SENSOR_PIPELINE.md``).

Includes:
* Verified register/bitfield catalogs (``DRV_SGC0_REGISTERS``,
  ``SYNCSEQ_REGISTERS``, ``DFETOP_REGISTERS``, ``SYSV_REGISTERS``).
* Automatic RTTI, vtable, and Thumb-2 pattern extraction
  (``extract_sgc0_syncseq_contracts``).
* Bounded Unicorn ARM Thumb-2 original-instruction replay helpers
  (``unicorn_replay_drv_sgc0_mode1``, ``unicorn_replay_drv_sgc0_start_modes``,
  ``unicorn_replay_syncseq_setup``).
* Deterministic hardware state-machine model (``DrvSgc0SyncSeqModel``) coupling
  ``SYSV`` -> ``SyncSeq`` -> ``DrvSGC0`` -> ``P2S`` Ch 1 -> ``SLVS-EC``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct

from .imx284_p2s import (
    IMX284_CHIP_ID,
    Imx284VirtualSensor,
    P2sBurst,
    decode_p2s_frame,
    encode_p2s_frame,
)

ORIL_BASE = 0x635C6000
ORIL_MAGIC = b"ORIL"

DFE_MMIO_BASE = 0xFC000000
DFE_MMIO_END = 0xFC200000
LEO_CRG_BASE = 0xFC021000
LEO_CRG_RESET = 0xFC021024
DFE_DOORBELL = 0xFC01E018
DFE_MBOX_CMD = 0xFC0300F4
DFE_MBOX_STAT = 0xFC0300F8
DFE_MBOX_KICK = 0xFC0300FC
DFE_TIMER_GATE0 = 0xFC098808
DFE_TIMER_GATE1 = 0xFC098908

SYNCSEQ_BASE = 0xFC080000
P2S_CH1_BASE = 0xFC088000
DFETOP_BASE = 0xFC0A0000
DRV_SGC0_BASE = 0xFC0A1100
SLVSEC_RX_BASE = 0xFC0F0000
SYSV_CRG_BASE = 0xF3400000
SYSV_BASE = 0xF3402000

MODE1_TIMING_HZ = 72_000_000
MODE1_H_TOTAL = 1940
MODE1_V_TOTAL = 619
MODE1_NLR_DELTA = 1
MODE1_NLR_DEN = 97
MODE1_NLR_NUM = 17
MODE1_FRAME_TICKS = 0x00125430  # 1,201,200 ticks @ 72 MHz = (619 + 17/97) * 1940
MODE1_READOUT_US = 15621
MODE1_EXT_TRIG_TICKS = 0x00012AC8  # 76,488 ticks = 1,201,200 - 72 * 15,621

COLD_LEO_RESET = 0x0005B7C3  # BIT(6) held high at cold boot before DrvSgc::Enable
COLD_HVCNT_IDLE = 0x3FFF3FFF
COLD_FCNT_IDLE = 0x7C
COLD_SYNCSEQ_INTERVAL = 0x00EE466C

ILCE6400_AVCAM_SHA256 = (
    "90d515cfec8b936f1ba9a69dcac462816b880297752a652474d67c4bba39125e"
)
ILCE6400A_AVCAM_SHA256 = (
    "3029a174b393fee9b2624d27737c20bc60e71cbbb33736b2a9363b57184ee03d"
)
ZVE10_AVCAM_SHA256 = (
    "bc6cba98c1ce5ef9c7692b9701ed855c25958d285a646ef90dc3fdf398f5c4fa"
)
DFE_DAT_SHA256 = (
    "703f457a480eee74d246e6df8b4f14ca83680ed64b016f1c0a96bed7993989d7"
)
DFE_APP_SHA256 = (
    "8f9a8d3f8e270d959088c2cb4029b8a115be8d72c70cb18932f4772c27c3cd5f"
)


class Sgc0SyncSeqError(ValueError):
    """Raised when an ORIL image, Unicorn replay, or MMIO operation fails validation."""


@dataclass(frozen=True)
class BitFieldSpec:
    """Verified bit-field contract within a 32-bit hardware register."""

    name: str
    lsb: int
    width: int
    access: str
    role: str

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb

    def extract(self, reg_val: int) -> int:
        return (reg_val >> self.lsb) & ((1 << self.width) - 1)


@dataclass(frozen=True)
class RegisterSpec:
    """Verified 32-bit MMIO register contract with ZV-E10 cross-reference."""

    addr: int
    name: str
    block: str
    access: str
    mode1_or_reset: int | None
    role: str
    fields: tuple[BitFieldSpec, ...]
    zve10_ref: str


def _build_drv_sgc0_registers() -> dict[int, RegisterSpec]:
    regs: list[RegisterSpec] = [
        RegisterSpec(
            addr=0xFC0A1100,
            name="SGC0_EN",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000001,
            role="Local DrvSGC0 block enable after Leo reset bit 6 clear and DfeTop BIT(8) W1S.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Local block enable"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d267a4)",
        ),
        RegisterSpec(
            addr=0xFC0A1108,
            name="SGC0_INT_TRIG",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Internal software self-trigger (1 in trig_mode=0, 0 in trig_mode=1/2).",
            fields=(BitFieldSpec("INT_TRIG", 0, 1, "RW", "Immediate software self-trigger"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4278)",
        ),
        RegisterSpec(
            addr=0xFC0A110C,
            name="SGC0_CFG_EN",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000001,
            role="Output configuration commit flag read by DrvSgc::IsConfigEnabled.",
            fields=(BitFieldSpec("CFG_EN", 0, 1, "RW", "Output timing configuration enabled"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A1110,
            name="SGC0_START_STATUS",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00010001,
            role="Start request (bit 0) and read-only hardware running status (bit 16).",
            fields=(
                BitFieldSpec("START_REQ", 0, 1, "RW", "Start request latched by DrvSgc0Start"),
                BitFieldSpec("HW_ACTIVE", 16, 1, "RO", "1 when SGC0 72 MHz counters actively run"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4278, 0x637a3f60)",
        ),
        RegisterSpec(
            addr=0xFC0A1114,
            name="SGC0_HVCNT_LATCH",
            block="DrvSGC0",
            access="WO",
            mode1_or_reset=0x00000001,
            role="Write 1 to snapshot live H/V counters into 0xFC0A1118.",
            fields=(BitFieldSpec("LATCH", 0, 1, "WO", "Snapshot HVcnt into +0x18"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a24)",
        ),
        RegisterSpec(
            addr=0xFC0A1118,
            name="SGC0_HVCNT_VAL",
            block="DrvSGC0",
            access="RO",
            mode1_or_reset=COLD_HVCNT_IDLE,
            role="Latched HV counter readback (0x3FFF3FFF when cold/stalled).",
            fields=(
                BitFieldSpec("H_CNT", 0, 14, "RO", "Latched horizontal tick counter"),
                BitFieldSpec("V_CNT", 16, 14, "RO", "Latched vertical line counter"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4324)",
        ),
        RegisterSpec(
            addr=0xFC0A111C,
            name="SGC0_FCNT_LATCH",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Pulsed 1 -> 0 to latch 8-bit frame counter into 0xFC0A1128.",
            fields=(BitFieldSpec("LATCH", 0, 1, "RW", "Frame count latch pulse"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d269fa)",
        ),
        RegisterSpec(
            addr=0xFC0A1120,
            name="SGC0_HV_CYCLE",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x026A0793,
            role="Zero-based terminal counts ((V - 1) << 16) | (H - 1); Mode-1 = 1940 x 619.",
            fields=(
                BitFieldSpec("H_MINUS_1", 0, 14, "RW", "Horizontal period H - 1 (1939 in Mode-1)"),
                BitFieldSpec("V_MINUS_1", 16, 14, "RW", "Vertical period V - 1 (618 in Mode-1)"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26acc)",
        ),
        RegisterSpec(
            addr=0xFC0A1124,
            name="SGC0_V_SUB101",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000206,
            role="Pre-end vertical line threshold V - 101 (518 = 0x206 in Mode-1).",
            fields=(BitFieldSpec("V_MINUS_101", 0, 16, "RW", "V - 0x65 line threshold"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26acc)",
        ),
        RegisterSpec(
            addr=0xFC0A1128,
            name="SGC0_FCNT_VAL",
            block="DrvSGC0",
            access="RO",
            mode1_or_reset=COLD_FCNT_IDLE,
            role="Latched 8-bit frame counter (0x7C when cold/stalled).",
            fields=(
                BitFieldSpec("FCNT_LSB", 0, 1, "RO", "Frame parity bit logged by DumpDiagnostics"),
                BitFieldSpec("FCNT", 0, 8, "RO", "8-bit frame counter"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4324)",
        ),
        RegisterSpec(
            addr=0xFC0A112C,
            name="SGC0_RPT",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000001,
            role="Continuous repeat mode (1 = repeating LiveView, 0 = one-shot still).",
            fields=(BitFieldSpec("RPT", 0, 1, "RW", "Repeat frame generation"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4278)",
        ),
        RegisterSpec(
            addr=0xFC0A1130,
            name="SGC0_EXT_SYNC",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="External sync selector (0 = SYSV/SyncSeq VD0 in trig_mode=1, 1 = EXT_SYNC in trig_mode=2).",
            fields=(BitFieldSpec("EXT_SYNC", 16, 1, "RW", "Select alternate external sync input"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x637a4278)",
        ),
        RegisterSpec(
            addr=0xFC0A113C,
            name="SGC0_VD0_MASK",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Masks outgoing VD0/XVS pulses when 1 (setDrvSgc0Vd0Mask).",
            fields=(BitFieldSpec("VD0_MASK", 0, 1, "RW", "1 = mask outgoing VD0 sync"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x6379cf04)",
        ),
        RegisterSpec(
            addr=0xFC0A1140,
            name="SGC0_OUT_SEL",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00040007,
            role="VD/HD output signal mux selectors (7 when v_pulse > 0 else 6; HD = 4).",
            fields=(
                BitFieldSpec("VD_SEL", 0, 3, "RW", "VD output mux (7 or 6)"),
                BitFieldSpec("HD_SEL", 16, 3, "RW", "HD output mux (4)"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A114C,
            name="SGC0_VD_WIDTH",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000004,
            role="Vertical sync pulse width in lines/ticks (4 in Mode-1).",
            fields=(BitFieldSpec("VD_PULSE_WIDTH", 0, 14, "RW", "VD pulse width"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A1150,
            name="SGC0_HD_EDGES",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00250005,
            role="Horizontal sync pulse start (SensHDTim + 2 = 5) and end (5 + 32 = 0x25).",
            fields=(
                BitFieldSpec("HD_START", 0, 14, "RW", "HD assertion edge"),
                BitFieldSpec("HD_END", 16, 14, "RW", "HD deassertion edge"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A1174,
            name="SGC0_NLR_WINDOW",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Non-linear readout (NLR) start/end line window programmed by SetDrvSgc0Nlr.",
            fields=(
                BitFieldSpec("NLR_START", 0, 14, "RW", "NLR window start line"),
                BitFieldSpec("NLR_END", 16, 14, "RW", "NLR window end line"),
            ),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
        RegisterSpec(
            addr=0xFC0A1180,
            name="SGC0_NLR_LUT_EN",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Enables multi-slope NLR LUT table at 0xFC0A1B80..0xFC0A1BE0.",
            fields=(BitFieldSpec("NLR_LUT_EN", 0, 1, "RW", "Enable NLR LUT"),),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
        RegisterSpec(
            addr=0xFC0A1194,
            name="SGC0_HD0_COMMIT",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00010000,
            role="HD0/XHS output pad enable committed at end of ConfigureOutputs.",
            fields=(BitFieldSpec("HD0_OUT_EN", 16, 1, "RW", "Enable HD0 output pad"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A1198,
            name="SGC0_VD0_COMMIT",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00010000,
            role="VD0/XVS output pad enable committed at end of ConfigureOutputs.",
            fields=(BitFieldSpec("VD0_OUT_EN", 16, 1, "RW", "Enable VD0 output pad"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26a42)",
        ),
        RegisterSpec(
            addr=0xFC0A119C,
            name="SGC0_SYNC_OPT",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Secondary sync option cleared prior to SyncSeq/DrvSGC0 start.",
            fields=(BitFieldSpec("SEC_SYNC", 16, 1, "RW", "Secondary sync gate"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x6379cedc)",
        ),
        RegisterSpec(
            addr=0xFC0A11A8,
            name="SGC0_NLR_CTRL",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x026B0001,
            role="Fractional-line NLR enable and alternate V terminal count (nlr_delta + V - 1).",
            fields=(
                BitFieldSpec("NLR_EN", 0, 1, "RW", "Enable fractional NLR line cadence"),
                BitFieldSpec("V_ALT", 16, 14, "RW", "Alternate V terminal count (619 = 0x26B)"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26acc)",
        ),
        RegisterSpec(
            addr=0xFC0A11AC,
            name="SGC0_NLR_FRAC",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00110061,
            role="Fractional-line NLR rational numerator/denominator (17/97 in Mode-1 -> 1,201,200 ticks).",
            fields=(
                BitFieldSpec("NLR_DEN", 0, 8, "RW", "Fractional denominator (97 = 0x61)"),
                BitFieldSpec("NLR_NUM", 16, 8, "RW", "Fractional numerator (17 = 0x11)"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d26acc)",
        ),
        RegisterSpec(
            addr=0xFC0A1B80,
            name="SGC0_NLR_HDR0",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="NLR LUT header base parameter programmed by SetDrvSgc0Nlr.",
            fields=(BitFieldSpec("VAL", 0, 16, "RW", "NLR base value"),),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
        RegisterSpec(
            addr=0xFC0A1B84,
            name="SGC0_NLR_HDR1",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="NLR LUT slope bound pair programmed by SetDrvSgc0Nlr.",
            fields=(
                BitFieldSpec("LO", 0, 13, "RW", "Low 13-bit bound"),
                BitFieldSpec("HI", 16, 13, "RW", "High 13-bit bound"),
            ),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
        RegisterSpec(
            addr=0xFC0A1B88,
            name="SGC0_NLR_HDR2",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="NLR LUT step count programmed by SetDrvSgc0Nlr.",
            fields=(BitFieldSpec("VAL", 0, 16, "RW", "NLR step parameter"),),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
        RegisterSpec(
            addr=0xFC0A1B8C,
            name="SGC0_NLR_HDR3",
            block="DrvSGC0",
            access="RW",
            mode1_or_reset=0x00000000,
            role="NLR LUT mode/shift flags programmed by SetDrvSgc0Nlr.",
            fields=(
                BitFieldSpec("MODE_A", 0, 2, "RW", "Low mode select"),
                BitFieldSpec("MODE_B", 16, 2, "RW", "High mode select"),
                BitFieldSpec("EN_20", 20, 1, "RW", "Extended LUT flag"),
            ),
            zve10_ref="av-cam.bin SetDrvSgc0Nlr (0x63cd75a0)",
        ),
    ]
    # 21-word LUT payload region 0xFC0A1B90..0xFC0A1BE0 (20 packed u16 pairs + terminal word)
    for idx, addr in enumerate(range(0xFC0A1B90, 0xFC0A1BE4, 4)):
        regs.append(
            RegisterSpec(
                addr=addr,
                name=f"SGC0_NLR_LUT_{idx:02d}",
                block="DrvSGC0",
                access="RW",
                mode1_or_reset=0x00000000,
                role=f"NLR multi-slope lookup table word {idx} (0xFC0A1B80..0xFC0A1BE0 bank).",
                fields=(
                    BitFieldSpec("ENTRY_LO", 0, 16, "RW", "Low 16-bit LUT entry"),
                    BitFieldSpec("ENTRY_HI", 16, 16, "RW", "High 16-bit LUT entry"),
                ),
                zve10_ref="av-cam.bin DrvSgc::SetNlrLut (0x63cd734c)",
            )
        )
    return {r.addr: r for r in regs}


DRV_SGC0_REGISTERS: dict[int, RegisterSpec] = _build_drv_sgc0_registers()

SYNCSEQ_REGISTERS: dict[int, RegisterSpec] = {
    r.addr: r
    for r in (
        RegisterSpec(
            addr=0xFC080014,
            name="SYNCSEQ_SOFT_PULSE",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Software sync pulse toggle (1 -> 0 edge triggers manual SyncSeq event).",
            fields=(BitFieldSpec("SOFT_PULSE", 0, 1, "RW", "Software sync pulse"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x63d25de4)",
        ),
        RegisterSpec(
            addr=0xFC080024,
            name="SYNCSEQ_MODE_CTRL",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Cleared to 0 during SyncSeq_Setup.",
            fields=(BitFieldSpec("MODE", 0, 32, "RW", "SyncSeq mode word"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x635c8628)",
        ),
        RegisterSpec(
            addr=0xFC080028,
            name="SYNCSEQ_PERIOD_CAP",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00FFFFFC,
            role="24-bit free-run / reload ceiling (0x00FFFFFC in Mode-1, 0x40 on soft pulse).",
            fields=(BitFieldSpec("PERIOD_CAP", 0, 24, "RW", "Reload ceiling ticks"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7 (0x635c8628)",
        ),
        RegisterSpec(
            addr=0xFC080030,
            name="SYNCSEQ_LIVE_INTERVAL",
            block="SyncSeq",
            access="RO",
            mode1_or_reset=COLD_SYNCSEQ_INTERVAL,
            role="Latched 72 MHz master VD interval (cold 0x00EE466C, active Mode-1 ~0x00125430).",
            fields=(BitFieldSpec("INTERVAL_TICKS", 0, 24, "RO", "72 MHz ticks per SYSV VD"),),
            zve10_ref="SENSOR_PIPELINE.md s1, s2.7",
        ),
        RegisterSpec(
            addr=0xFC080038,
            name="SYNCSEQ_FRAME_CYCLES",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=MODE1_FRAME_TICKS,
            role="Programmed sensor frame ticks (0x00125430 when sensor period < exposure period, else 0).",
            fields=(BitFieldSpec("FRAME_TICKS", 0, 24, "RW", "72 MHz frame ticks"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0 -> 0x63cda7b6)",
        ),
        RegisterSpec(
            addr=0xFC08003C,
            name="SYNCSEQ_SLOT0_DELAY",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000010,
            role="Event slot 0 delay threshold (0x10 default ticks).",
            fields=(BitFieldSpec("DELAY", 0, 24, "RW", "72 MHz delay ticks"),),
            zve10_ref="av-cam.bin 0x63787104",
        ),
        RegisterSpec(
            addr=0xFC080040,
            name="SYNCSEQ_SLOT0_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 0 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 0 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC080054,
            name="SYNCSEQ_SLOT1_DELAY",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000010,
            role="Event slot 1 delay threshold (0x10 default ticks).",
            fields=(BitFieldSpec("DELAY", 0, 24, "RW", "72 MHz delay ticks"),),
            zve10_ref="av-cam.bin 0x63787104",
        ),
        RegisterSpec(
            addr=0xFC080058,
            name="SYNCSEQ_SLOT1_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 1 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 1 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC08006C,
            name="SYNCSEQ_SLOT2_DELAY",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000010,
            role="Event slot 2 delay threshold (0x10 default ticks, or line-scaled).",
            fields=(BitFieldSpec("DELAY", 0, 24, "RW", "72 MHz delay ticks"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC080070,
            name="SYNCSEQ_SLOT2_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 2 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 2 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC080084,
            name="SYNCSEQ_SLOT3_DELAY",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000010,
            role="Event slot 3 delay threshold (0x10 default ticks, or line-scaled).",
            fields=(BitFieldSpec("DELAY", 0, 24, "RW", "72 MHz delay ticks"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC080088,
            name="SYNCSEQ_SLOT3_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 3 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 3 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC08009C,
            name="SYNCSEQ_SLOT4_DELAY",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000010,
            role="Event slot 4 delay threshold (0x10 default ticks, or line-scaled).",
            fields=(BitFieldSpec("DELAY", 0, 24, "RW", "72 MHz delay ticks"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800B8,
            name="SYNCSEQ_SLOT5_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 5 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 5 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800C8,
            name="SYNCSEQ_SLOT6_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 6 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 6 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800D8,
            name="SYNCSEQ_SLOT7_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 7 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 7 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800E8,
            name="SYNCSEQ_SLOT8_EN",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Event slot 8 enable flag.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Slot 8 enable"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800F4,
            name="SYNCSEQ_EXT_TRIG_TIMER",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=MODE1_EXT_TRIG_TICKS,
            role="Frame-sync trigger timer releasing P2S Ch 1 run-mode-0 burst (0x00012AC8 in Mode-1).",
            fields=(BitFieldSpec("TRIG_TICKS", 0, 24, "RW", "72 MHz trigger offset ticks"),),
            zve10_ref="SENSOR_PIPELINE.md s1, s2.7 (0x635c8628)",
        ),
        RegisterSpec(
            addr=0xFC0800F8,
            name="SYNCSEQ_EXP_FRAMES",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Multi-frame exposure span count.",
            fields=(BitFieldSpec("FRAMES", 0, 16, "RW", "Multi-frame span"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC0800FC,
            name="SYNCSEQ_EXP_QUOT",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Multi-frame exposure quotient count.",
            fields=(BitFieldSpec("QUOT", 0, 16, "RW", "Multi-frame quotient"),),
            zve10_ref="av-cam.bin SyncSeq_Setup (0x635c85f0)",
        ),
        RegisterSpec(
            addr=0xFC080104,
            name="SYNCSEQ_IRQ_STAT0",
            block="SyncSeq",
            access="W1C",
            mode1_or_reset=0x00000000,
            role="SyncSeq interrupt status bank 0 (write-1-to-clear).",
            fields=(BitFieldSpec("STAT", 0, 32, "W1C", "Pending slot IRQ bits"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7",
        ),
        RegisterSpec(
            addr=0xFC080108,
            name="SYNCSEQ_IRQ_MASK0",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000000,
            role="SyncSeq interrupt enable/mask bank 0.",
            fields=(BitFieldSpec("MASK", 0, 32, "RW", "Slot IRQ mask"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7",
        ),
        RegisterSpec(
            addr=0xFC08010C,
            name="SYNCSEQ_PREARM_GATE",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000002,
            role="Pre-arm gate (bit 0 cleared 3 -> 2 during Phase 1 sensor pre-arm).",
            fields=(BitFieldSpec("HOLD", 0, 1, "RW", "Cleared before SyncSeq run"),),
            zve10_ref="SENSOR_PIPELINE.md s1 Phase 1",
        ),
        RegisterSpec(
            addr=0xFC080110,
            name="SYNCSEQ_IRQ_STAT1",
            block="SyncSeq",
            access="W1C",
            mode1_or_reset=0x00000000,
            role="SyncSeq frame/ext-trig interrupt status bank 1 (write-1-to-clear).",
            fields=(BitFieldSpec("STAT", 0, 32, "W1C", "Frame/trigger IRQ status"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7",
        ),
        RegisterSpec(
            addr=0xFC080114,
            name="SYNCSEQ_IRQ_EN1",
            block="SyncSeq",
            access="RW",
            mode1_or_reset=0x00000001,
            role="SyncSeq frame interrupt enable bank 1.",
            fields=(BitFieldSpec("EN", 0, 1, "RW", "Enable SyncSeq frame IRQ"),),
            zve10_ref="SENSOR_PIPELINE.md s2.7",
        ),
    )
}

DFETOP_REGISTERS: dict[int, RegisterSpec] = {
    r.addr: r
    for r in (
        RegisterSpec(
            addr=0xFC0A0000,
            name="DFETOP_CORE_W1S",
            block="DfeTop",
            access="W1S",
            mode1_or_reset=0x00000160,
            role="Write-1-to-set DFE core block enables (BIT(8)=DrvSGC0, BIT(9)=DrvSGC1, BIT(24)=Bridge).",
            fields=(
                BitFieldSpec("IFMT", 1, 1, "W1S", "Input formatter enable"),
                BitFieldSpec("SIF", 4, 1, "W1S", "Sensor interface enable"),
                BitFieldSpec("SYNCSEQ", 5, 1, "W1S", "SyncSeq domain enable"),
                BitFieldSpec("PIN", 6, 1, "W1S", "DFE pad/pin mux enable"),
                BitFieldSpec("RDC", 7, 1, "W1S", "RDC RAW writer enable"),
                BitFieldSpec("DRVSGC0", 8, 1, "W1S", "DrvSGC0 timing core enable"),
                BitFieldSpec("DRVSGC1", 9, 1, "W1S", "DrvSGC1 timing core enable"),
                BitFieldSpec("AIF", 15, 1, "W1S", "AIF bridge enable"),
                BitFieldSpec("BRIDGE", 24, 1, "W1S", "DFE top bridge enable"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s1, s2.9; av-cam.bin 0x637822ac",
        ),
        RegisterSpec(
            addr=0xFC0A0004,
            name="DFETOP_CORE_W1C",
            block="DfeTop",
            access="W1C",
            mode1_or_reset=0x00000000,
            role="Write-1-to-clear DFE core block enables (clearing BIT(8) disables/resets DrvSGC0).",
            fields=(
                BitFieldSpec("DRVSGC0", 8, 1, "W1C", "Disable DrvSGC0 core"),
                BitFieldSpec("DRVSGC1", 9, 1, "W1C", "Disable DrvSGC1 core"),
                BitFieldSpec("BRIDGE", 24, 1, "W1C", "Disable DFE top bridge"),
            ),
            zve10_ref="SENSOR_PIPELINE.md s2.9; av-cam.bin 0x637822bc",
        ),
        RegisterSpec(
            addr=0xFC0A0014,
            name="DFETOP_IRQ_STAT_LO",
            block="DfeTop",
            access="W1C",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt status low word.",
            fields=(BitFieldSpec("STAT", 0, 32, "W1C", "Interrupt status low"),),
            zve10_ref="av-cam.bin 0x63cd83bc / 0x63782240",
        ),
        RegisterSpec(
            addr=0xFC0A0018,
            name="DFETOP_IRQ_EN_LO",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt mask low word.",
            fields=(BitFieldSpec("EN", 0, 32, "RW", "Interrupt enable low"),),
            zve10_ref="av-cam.bin 0x6378224c..0x63782258",
        ),
        RegisterSpec(
            addr=0xFC0A0020,
            name="DFETOP_IRQ_STAT_MID",
            block="DfeTop",
            access="W1C",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt status middle word.",
            fields=(BitFieldSpec("STAT", 0, 32, "W1C", "Interrupt status mid"),),
            zve10_ref="av-cam.bin 0x63cd83c4 / 0x63782264",
        ),
        RegisterSpec(
            addr=0xFC0A0024,
            name="DFETOP_IRQ_EN_MID",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt mask middle word.",
            fields=(BitFieldSpec("EN", 0, 32, "RW", "Interrupt enable mid"),),
            zve10_ref="av-cam.bin 0x63782270..0x63782288",
        ),
        RegisterSpec(
            addr=0xFC0A002C,
            name="DFETOP_IRQ_STAT_HI",
            block="DfeTop",
            access="RO",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt status high word.",
            fields=(BitFieldSpec("STAT", 0, 32, "RO", "Interrupt status high"),),
            zve10_ref="av-cam.bin 0x63cd83cc",
        ),
        RegisterSpec(
            addr=0xFC0A0030,
            name="DFETOP_IRQ_EN_HI",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000000,
            role="DfeTop sub-block interrupt mask high word.",
            fields=(BitFieldSpec("EN", 0, 32, "RW", "Interrupt enable high"),),
            zve10_ref="av-cam.bin 0x63782294..0x637822a0",
        ),
        RegisterSpec(
            addr=0xFC0A0108,
            name="DFETOP_BRIDGE_GATE",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000001,
            role="DFE bridge local enable bit paired with 0xFC0A0000 BIT(24).",
            fields=(BitFieldSpec("BRIDGE_EN", 0, 1, "RW", "Enable DFE bridge"),),
            zve10_ref="av-cam.bin 0x63784360..0x6378437a / 0x63cd83d4",
        ),
        RegisterSpec(
            addr=0xFC0A0150,
            name="DFETOP_XBAR_PREVIEW0",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x02000000,
            role="Mode-1 LiveView preview crossbar route selector 0.",
            fields=(BitFieldSpec("ROUTE", 0, 32, "RW", "Crossbar mux word"),),
            zve10_ref="SENSOR_PIPELINE.md s1 Phase 4, s2.9",
        ),
        RegisterSpec(
            addr=0xFC0A0154,
            name="DFETOP_XBAR_RAW0",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x40000000,
            role="Standalone Mode-1 RAW RdcW0 crossbar selector (res=0x2f).",
            fields=(BitFieldSpec("ROUTE", 0, 32, "RW", "RAW crossbar mux"),),
            zve10_ref="SENSOR_PIPELINE.md s2.9",
        ),
        RegisterSpec(
            addr=0xFC0A0178,
            name="DFETOP_XBAR_PREVIEW1",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00020000,
            role="Mode-1 LiveView preview crossbar route selector 1.",
            fields=(BitFieldSpec("ROUTE", 0, 32, "RW", "Crossbar mux word"),),
            zve10_ref="SENSOR_PIPELINE.md s1 Phase 4, s2.9",
        ),
        RegisterSpec(
            addr=0xFC0A0180,
            name="DFETOP_XBAR_RAW1",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00010000,
            role="Standalone Mode-1 RAW RdcW0 output crossbar commit.",
            fields=(BitFieldSpec("ROUTE", 0, 32, "RW", "RAW crossbar commit"),),
            zve10_ref="SENSOR_PIPELINE.md s2.9",
        ),
        RegisterSpec(
            addr=0xFC0A0200,
            name="DFETOP_TOP_EN0",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000011,
            role="DfeTop global pipeline enable/IRQ gate 0.",
            fields=(BitFieldSpec("EN", 0, 32, "RW", "Pipeline gate 0"),),
            zve10_ref="SENSOR_PIPELINE.md s1 Phase 4, s2.9",
        ),
        RegisterSpec(
            addr=0xFC0A0204,
            name="DFETOP_TOP_EN1",
            block="DfeTop",
            access="RW",
            mode1_or_reset=0x00000011,
            role="DfeTop global pipeline enable/IRQ gate 1.",
            fields=(BitFieldSpec("EN", 0, 32, "RW", "Pipeline gate 1"),),
            zve10_ref="SENSOR_PIPELINE.md s1 Phase 4, s2.9",
        ),
    )
}

SYSV_REGISTERS: dict[int, RegisterSpec] = {
    r.addr: r
    for r in (
        RegisterSpec(
            addr=0xF3400000,
            name="SYSV_CRG_CTRL",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000001,
            role="Sugilite/SYSV domain clock gate controlling master V-clock root.",
            fields=(BitFieldSpec("CLK_EN", 0, 1, "RW", "Enable SYSV root clock"),),
            zve10_ref="av-cam.bin 0x639f1576..0x639f15a2",
        ),
        RegisterSpec(
            addr=0xF3402010,
            name="SYSV_ENABLE",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000001,
            role="Master SYSV V-clock generator enable (gates VD pulses to SyncSeq and DrvSGC0 trig_mode=1).",
            fields=(BitFieldSpec("MODE_EN", 0, 2, "RW", "Non-zero starts SYSV VD generation"),),
            zve10_ref="av-cam.bin 0x639f1532 / 0x63d6705c",
        ),
        RegisterSpec(
            addr=0xF3402014,
            name="SYSV_H_PERIOD",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000794,
            role="SYSV horizontal period in master clock ticks.",
            fields=(BitFieldSpec("H_PERIOD", 0, 12, "RW", "12-bit H period"),),
            zve10_ref="av-cam.bin 0x639f1516 / 0x63d67076",
        ),
        RegisterSpec(
            addr=0xF3402018,
            name="SYSV_V_PERIOD",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x0000026B,
            role="SYSV vertical period in lines.",
            fields=(BitFieldSpec("V_PERIOD", 0, 12, "RW", "12-bit V period"),),
            zve10_ref="av-cam.bin 0x639f14fa / 0x63d67090",
        ),
        RegisterSpec(
            addr=0xF340201C,
            name="SYSV_SYNC_MODE",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000001,
            role="SYSV master/slave timing mode selector.",
            fields=(BitFieldSpec("SYNC_MODE", 0, 3, "RW", "3-bit sync mode"),),
            zve10_ref="av-cam.bin 0x639f14de / 0x63d670aa",
        ),
        RegisterSpec(
            addr=0xF3402020,
            name="SYSV_OUT_EN",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000001,
            role="SYSV master VD output enable to EVSEL ext-vd and DFE SyncSeq.",
            fields=(BitFieldSpec("OUT_EN", 0, 1, "RW", "Enable master VD output"),),
            zve10_ref="av-cam.bin 0x639f14c6 / 0x63d670c4",
        ),
        RegisterSpec(
            addr=0xF3402024,
            name="SYSV_STATUS",
            block="SYSV",
            access="RO",
            mode1_or_reset=0x00000000,
            role="SYSV active running status bit.",
            fields=(BitFieldSpec("ACTIVE", 0, 1, "RO", "1 when SYSV generator active"),),
            zve10_ref="av-cam.bin 0x63d670e8",
        ),
        RegisterSpec(
            addr=0xF3402028,
            name="SYSV_IRQ_CTRL",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000000,
            role="SYSV vertical blanking interrupt control/clear (GIC IRQ 98).",
            fields=(BitFieldSpec("IRQ_CTRL", 0, 32, "RW", "SYSV IRQ mask/ack"),),
            zve10_ref="av-cam.bin 0x639f15aa..0x639f1606",
        ),
        RegisterSpec(
            addr=0xF340202C,
            name="SYSV_PHASE_OFFSET0",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x8000000A,
            role="Signed master VD phase offset channel 0.",
            fields=(
                BitFieldSpec("MAG", 0, 21, "RW", "Phase offset magnitude"),
                BitFieldSpec("NEG", 28, 1, "RW", "Sign bit (1 = negative)"),
                BitFieldSpec("EN", 31, 1, "RW", "Enable phase offset 0"),
            ),
            zve10_ref="av-cam.bin 0x639f14aa / 0x63d670f8",
        ),
        RegisterSpec(
            addr=0xF3402030,
            name="SYSV_PHASE_OFFSET1",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x8000000A,
            role="Signed master VD phase offset channel 1 (DFE SyncSeq VD feed).",
            fields=(
                BitFieldSpec("MAG", 0, 21, "RW", "Phase offset magnitude"),
                BitFieldSpec("NEG", 28, 1, "RW", "Sign bit (1 = negative)"),
                BitFieldSpec("EN", 31, 1, "RW", "Enable phase offset 1"),
            ),
            zve10_ref="av-cam.bin 0x639f148e / 0x63d67150",
        ),
        RegisterSpec(
            addr=0xF3402094,
            name="SYSV_TIMING_EXT0",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Extended SYSV sub-line timing word 0.",
            fields=(BitFieldSpec("EXT0", 0, 32, "RW", "Extended timing 0"),),
            zve10_ref="av-cam.bin 0x639f160a / 0x63d67514",
        ),
        RegisterSpec(
            addr=0xF3402098,
            name="SYSV_TIMING_EXT1",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000000,
            role="Extended SYSV sub-line timing word 1.",
            fields=(BitFieldSpec("EXT1", 0, 32, "RW", "Extended timing 1"),),
            zve10_ref="av-cam.bin 0x639f1622 / 0x63d6750c",
        ),
        RegisterSpec(
            addr=0xF3402100,
            name="SYSV_MASTER_CFG",
            block="SYSV",
            access="RW",
            mode1_or_reset=0x00000000,
            role="SYSV master timing table configuration block.",
            fields=(BitFieldSpec("CFG", 0, 32, "RW", "Master configuration"),),
            zve10_ref="av-cam.bin 0x639f1470 / 0x63d671aa",
        ),
    )
}


@dataclass(frozen=True)
class ZvE10Correspondence:
    """Pinned upstream ZV-E10 reference addresses from qn06142/zv-e10-re."""

    repo: str
    commit: str
    doc_path: str
    avcam_sha256: str
    dfe_dat_sha256_prefix: str
    dfe_dat_embedded_va: int
    drv_sgc_enable: int
    drv_sgc_start_one_shot: int
    drv_sgc_configure_outputs: int
    drv_sgc_set_timing: int
    drv_sgc0_start: int
    drv_sgc_is_running: int
    drv_sgc_dump_diagnostics: int
    set_drv_sgc0_vd0_mask: int
    syncseq_setup: int
    sync_task_hs_entry: int
    sync_task_hs_worker: int


ZVE10_CORRESPONDENCE = ZvE10Correspondence(
    repo="qn06142/zv-e10-re",
    commit="9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7",
    doc_path="docs/hardware/SENSOR_PIPELINE.md",
    avcam_sha256=ZVE10_AVCAM_SHA256,
    dfe_dat_sha256_prefix="703f457a480eee74",
    dfe_dat_embedded_va=0x64560700,
    drv_sgc_enable=0x63D267A4,
    drv_sgc_start_one_shot=0x63D269F2,
    drv_sgc_configure_outputs=0x63D26A42,
    drv_sgc_set_timing=0x63D26ACC,
    drv_sgc0_start=0x637A4278,
    drv_sgc_is_running=0x637A3F60,
    drv_sgc_dump_diagnostics=0x637A4324,
    set_drv_sgc0_vd0_mask=0x6379CF04,
    syncseq_setup=0x635C8628,
    sync_task_hs_entry=0x635CE230,
    sync_task_hs_worker=0x638D15C4,
)


@dataclass(frozen=True)
class DrvSgcMethods:
    """Discovered Thumb-2 method entrypoints (bit 0 cleared) of Camera::DFECore::DrvSgc."""

    enable: int
    disable: int
    start_one_shot: int
    is_config_enabled: int
    is_running: int
    dump_diagnostics: int
    configure_outputs: int
    set_timing: int
    drv_sgc0_start: int
    set_drv_sgc0_nlr: int


@dataclass(frozen=True)
class Sgc0SyncSeqContracts:
    """Extracted vtables, method addresses, and firmware hashes for an ORIL av-cam.bin."""

    avcam_size: int
    avcam_sha256: str
    oril_version: int
    oril_entry: int
    dfe_core_sgc_base_vtable: int
    drv_sgc_vtable: int
    sync_sequencer_vtable: int
    rdc_w0_vtable: int | None
    drv_sgc_methods: DrvSgcMethods
    syncseq_setup: int
    syncseq_clk_mhz_va: int
    leo_set_reset_bit: int
    dfetop_set_core_bit: int
    dfetop_clear_core_bit: int
    slvsec_lock_literal_va: int | None
    sysv_master_cfg_literal_va: int | None
    dfe_dat_sha256: str | None
    dfe_app_sha256: str | None
    zve10_correspondence: ZvE10Correspondence


def _decode_thumb32_branch(hw1: int, hw2: int, pc_va: int) -> tuple[str | None, int]:
    """Decode unconditional Thumb-2 32-bit BL or B.W target address."""
    if (hw1 & 0xF800) != 0xF000 or (hw2 & 0x8000) == 0:
        return None, 0
    op_hi = hw2 & 0xD000
    if op_hi == 0xD000:
        kind = "BL"
    elif op_hi == 0x9000:
        kind = "B.W"
    else:
        return None, 0
    s = (hw1 >> 10) & 1
    imm10 = hw1 & 0x3FF
    j1 = (hw2 >> 13) & 1
    j2 = (hw2 >> 11) & 1
    imm11 = hw2 & 0x7FF
    i1 = (~(j1 ^ s)) & 1
    i2 = (~(j2 ^ s)) & 1
    imm24 = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:
        imm24 -= 1 << 25
    return kind, (pc_va + 4 + imm24) & 0xFFFFFFFF


def _find_primary_vtable(data: bytes, mangled_rtti: bytes) -> int:
    """Locate the primary Itanium C++ ABI vtable (offset-to-top == 0) for an RTTI name."""
    candidates: list[int] = []
    for m in re.finditer(re.escape(mangled_rtti), data):
        str_va = ORIL_BASE + m.start()
        ti_pat = struct.pack("<I", str_va)
        for ti_m in re.finditer(re.escape(ti_pat), data):
            if ti_m.start() < 4:
                continue
            ti_va = ORIL_BASE + ti_m.start() - 4
            vt_pat = struct.pack("<I", ti_va)
            for vt_m in re.finditer(re.escape(vt_pat), data):
                if vt_m.start() < 4 or vt_m.start() + 8 > len(data):
                    continue
                offset_to_top = struct.unpack_from("<i", data, vt_m.start() - 4)[0]
                first_fn = struct.unpack_from("<I", data, vt_m.start() + 4)[0]
                if offset_to_top == 0 and (first_fn & 1) == 1 and ORIL_BASE <= first_fn < ORIL_BASE + len(data):
                    candidates.append(ORIL_BASE + vt_m.start() + 4)
    if not candidates:
        raise Sgc0SyncSeqError(f"Primary vtable not found for RTTI {mangled_rtti!r}")
    return candidates[0]


def _scan_bls_in_range(data: bytes, start_va: int, length: int) -> list[int]:
    """Extract ordered Thumb-2 BL call targets within a bounded code window."""
    targets: list[int] = []
    base_off = start_va - ORIL_BASE
    end_off = min(len(data) - 4, base_off + length)
    off = base_off
    while off <= end_off:
        hw1, hw2 = struct.unpack_from("<HH", data, off)
        kind, dest = _decode_thumb32_branch(hw1, hw2, ORIL_BASE + off)
        if kind == "BL":
            targets.append(dest)
            off += 4
            continue
        # Advance by Thumb instruction width (32-bit if top 5 bits in 0b11101..0b11111)
        off += 4 if (hw1 & 0xF800) in (0xE800, 0xF000, 0xF800) else 2
    return targets


def _find_drv_sgc_nonvirtuals(data: bytes, start_one_shot_va: int) -> tuple[int, int, int, int]:
    """Discover DrvSgc0Start, ConfigureOutputs, SetTiming, and SetDrvSgc0Nlr from StartOneShot."""
    sos_off = start_one_shot_va - ORIL_BASE
    if sos_off < 0 or sos_off + 8 > len(data):
        raise Sgc0SyncSeqError(f"StartOneShot VA out of bounds: {start_one_shot_va:#x}")

    hw0, hw1, bw1, bw2 = struct.unpack_from("<4H", data, sos_off)
    if hw0 != 0x2101 or hw1 != 0x2200:
        raise Sgc0SyncSeqError(
            f"Unexpected StartOneShot prologue at {start_one_shot_va:#x}: {hw0:#06x} {hw1:#06x}"
        )
    kind, drv_sgc0_start = _decode_thumb32_branch(bw1, bw2, start_one_shot_va + 4)
    if kind != "B.W":
        raise Sgc0SyncSeqError(f"Expected tail B.W in StartOneShot at {start_one_shot_va + 4:#x}")

    win_end = min(len(data) - 16, sos_off + 0x300)
    # ConfigureOutputs: push.w {r4, r5, r6, r7, r8, lr} (0xE92D 0x41F0) + mov r4, r0 (0x4604) + ldr r7, [sp, #0x18] (0x9F06)
    cfg_pat = struct.pack("<4H", 0xE92D, 0x41F0, 0x4604, 0x9F06)
    cfg_idx = data.find(cfg_pat, sos_off, win_end)
    if cfg_idx < 0:
        raise Sgc0SyncSeqError("DrvSgc::ConfigureOutputs signature not found near StartOneShot")
    configure_outputs = ORIL_BASE + cfg_idx

    # SetTiming: push.w {r4, r5, r6, r7, r8, sb, sl, lr} (0xE92D 0x47F0) ... sub.w r1, r4, #0x65 (0xF1A4 0x0165)
    tim_pro = struct.pack("<2H", 0xE92D, 0x47F0)
    sub_101 = struct.pack("<2H", 0xF1A4, 0x0165)
    tim_idx = data.find(tim_pro, cfg_idx + 4, win_end)
    if tim_idx < 0 or data.find(sub_101, tim_idx, min(len(data), tim_idx + 0xB0)) < 0:
        raise Sgc0SyncSeqError("DrvSgc::SetTiming signature not found after ConfigureOutputs")
    set_timing = ORIL_BASE + tim_idx

    # SetDrvSgc0Nlr: push {r4, r5, r6, lr} (0xB570), mov r4, r1 (0x460C), adds r5, r0, #4 (0x1D05)
    nlr_pat = struct.pack("<3H", 0xB570, 0x460C, 0x1D05)
    nlr_idx = data.find(nlr_pat, tim_idx + 4, win_end)
    if nlr_idx < 0:
        raise Sgc0SyncSeqError("DrvSgc::SetDrvSgc0Nlr signature not found after SetTiming")
    set_drv_sgc0_nlr = ORIL_BASE + nlr_idx

    return drv_sgc0_start, configure_outputs, set_timing, set_drv_sgc0_nlr


def _find_syncseq_setup(data: bytes) -> tuple[int, int]:
    """Discover SyncSeq_Setup and its runtime sensor-clock MHz .bss pointer."""
    # Literal pool anchor: double 1000.0 (0x00000000, 0x408F4000) followed by 0x00FFFFFC
    lit_anchor = struct.pack("<III", 0x00000000, 0x408F4000, 0x00FFFFFC)
    lit_idx = data.find(lit_anchor)
    if lit_idx < 0:
        raise Sgc0SyncSeqError("SyncSeq_Setup literal pool anchor (1000.0, 0x00FFFFFC) not found")

    # Walk back up to 0x300 bytes to push.w {r0, r4, r5, r6, r7, r8, sb, sl, fp, lr} (0xE92D 0x4FF1)
    prologue = struct.pack("<2H", 0xE92D, 0x4FF1)
    search_start = max(0, lit_idx - 0x300)
    fn_idx = data.rfind(prologue, search_start, lit_idx)
    if fn_idx < 0:
        raise Sgc0SyncSeqError("SyncSeq_Setup prologue not found before literal pool")

    syncseq_setup = ORIL_BASE + fn_idx
    # Decode ldr r6, [pc, #imm] at +0x0C and add r6, pc (0x447E) in the first 0x30 bytes
    ldr_hw = struct.unpack_from("<H", data, fn_idx + 0x0C)[0]
    add_r6_pc = data.find(struct.pack("<H", 0x447E), fn_idx, fn_idx + 0x30)
    if (ldr_hw & 0xFF00) != 0x4E00 or add_r6_pc < 0:
        raise Sgc0SyncSeqError("Unexpected SyncSeq_Setup clock-global PIC sequence")
    ldr_pc_aligned = ((syncseq_setup + 0x0C + 4) & ~0x3)
    lit_va = ldr_pc_aligned + ((ldr_hw & 0xFF) << 2)
    rel_off = struct.unpack_from("<I", data, lit_va - ORIL_BASE)[0]
    add_pc = ORIL_BASE + add_r6_pc + 4
    clk_mhz_va = (rel_off + add_pc) & 0xFFFFFFFF
    return syncseq_setup, clk_mhz_va


def _find_literal_va(data: bytes, u32_val: int) -> int | None:
    idx = data.find(struct.pack("<I", u32_val))
    return (ORIL_BASE + idx) if idx >= 0 else None


def extract_sgc0_syncseq_contracts(
    avcam_path: Path | str | bytes,
    *,
    dfe_dat_path: Path | str | None = None,
    dfe_app_path: Path | str | None = None,
) -> Sgc0SyncSeqContracts:
    """Extract DrvSGC0, SyncSeq, DfeTop, and SYSV contracts from an ORIL av-cam.bin."""
    sibling_dat_sha: str | None = None
    sibling_app_sha: str | None = None

    if isinstance(avcam_path, (bytes, bytearray, memoryview)):
        data = bytes(avcam_path)
    else:
        p = Path(avcam_path)
        data = p.read_bytes()
        dat_candidate = Path(dfe_dat_path) if dfe_dat_path is not None else p.with_name("dfe_dat.bin")
        app_candidate = Path(dfe_app_path) if dfe_app_path is not None else p.with_name("dfe_app.bin")
        if dat_candidate.is_file():
            sibling_dat_sha = hashlib.sha256(dat_candidate.read_bytes()).hexdigest()
        if app_candidate.is_file():
            sibling_app_sha = hashlib.sha256(app_candidate.read_bytes()).hexdigest()

    if len(data) < 0x100 or data[4:8] != ORIL_MAGIC:
        raise Sgc0SyncSeqError("Input is not a valid ORIL av-cam.bin image")

    oril_version, oril_entry = struct.unpack_from("<II", data, 8)
    if oril_version != 4:
        raise Sgc0SyncSeqError(f"Unsupported ORIL version {oril_version} (expected 4)")

    sgc_base_vt = _find_primary_vtable(data, b"N6Camera7DFECore14DfeCoreSgcBaseE\x00")
    drv_sgc_vt = _find_primary_vtable(data, b"N6Camera7DFECore6DrvSgcE\x00")
    syncseq_vt = _find_primary_vtable(data, b"N6Camera7Imaging13SyncSequencerE\x00")
    try:
        rdc_w0_vt: int | None = _find_primary_vtable(data, b"N6Camera7DFECore5RdcW0E\x00")
    except Sgc0SyncSeqError:
        rdc_w0_vt = None

    vt_words = struct.unpack_from("<8I", data, drv_sgc_vt - ORIL_BASE)
    enable_va = vt_words[0] & ~1
    disable_va = vt_words[1] & ~1
    start_one_shot_va = vt_words[2] & ~1
    is_cfg_en_va = vt_words[5] & ~1
    is_running_va = vt_words[6] & ~1
    dump_diag_va = vt_words[7] & ~1

    drv_sgc0_start, configure_outputs, set_timing, set_drv_sgc0_nlr = _find_drv_sgc_nonvirtuals(
        data, start_one_shot_va
    )
    syncseq_setup, syncseq_clk_mhz_va = _find_syncseq_setup(data)

    enable_bls = _scan_bls_in_range(data, enable_va, 0x38)
    if len(enable_bls) < 3:
        raise Sgc0SyncSeqError(f"Failed to decode Leo/DfeTop BLs in DrvSgc::Enable at {enable_va:#x}")
    leo_set_reset_bit = enable_bls[0]
    dfetop_set_core_bit = enable_bls[2]

    disable_bls = _scan_bls_in_range(data, disable_va, 0x68)
    if not disable_bls:
        raise Sgc0SyncSeqError(f"Failed to decode DfeTop BLs in DrvSgc::Disable at {disable_va:#x}")
    dfetop_clear_core_bit = disable_bls[-1]

    return Sgc0SyncSeqContracts(
        avcam_size=len(data),
        avcam_sha256=hashlib.sha256(data).hexdigest(),
        oril_version=oril_version,
        oril_entry=oril_entry,
        dfe_core_sgc_base_vtable=sgc_base_vt,
        drv_sgc_vtable=drv_sgc_vt,
        sync_sequencer_vtable=syncseq_vt,
        rdc_w0_vtable=rdc_w0_vt,
        drv_sgc_methods=DrvSgcMethods(
            enable=enable_va,
            disable=disable_va,
            start_one_shot=start_one_shot_va,
            is_config_enabled=is_cfg_en_va,
            is_running=is_running_va,
            dump_diagnostics=dump_diag_va,
            configure_outputs=configure_outputs,
            set_timing=set_timing,
            drv_sgc0_start=drv_sgc0_start,
            set_drv_sgc0_nlr=set_drv_sgc0_nlr,
        ),
        syncseq_setup=syncseq_setup,
        syncseq_clk_mhz_va=syncseq_clk_mhz_va,
        leo_set_reset_bit=leo_set_reset_bit,
        dfetop_set_core_bit=dfetop_set_core_bit,
        dfetop_clear_core_bit=dfetop_clear_core_bit,
        slvsec_lock_literal_va=_find_literal_va(data, 0xFC0F00B0),
        sysv_master_cfg_literal_va=_find_literal_va(data, 0xF3402100),
        dfe_dat_sha256=sibling_dat_sha,
        dfe_app_sha256=sibling_app_sha,
        zve10_correspondence=ZVE10_CORRESPONDENCE,
    )


@dataclass(frozen=True)
class MmioAccess:
    """Single MMIO read or write recorded during Unicorn Thumb-2 execution."""

    op: str  # "R" or "W"
    addr: int
    size: int
    value: int
    pc: int


@dataclass(frozen=True)
class UnicornReplayResult:
    """Bounded Unicorn execution receipt and ordered MMIO trace."""

    trace: tuple[MmioAccess, ...]
    final_regs: dict[int, int]
    insns_executed: int

    @property
    def writes(self) -> tuple[MmioAccess, ...]:
        return tuple(a for a in self.trace if a.op == "W")

    @property
    def reads(self) -> tuple[MmioAccess, ...]:
        return tuple(a for a in self.trace if a.op == "R")


class _AvcamUnicornHarness:
    """Internal bounded Unicorn ARMv7 Thumb-2 harness for unmodified ORIL calls."""

    SCRATCH_BASE = 0x10000000
    SCRATCH_SIZE = 0x10000
    RET_STUB_VA = SCRATCH_BASE + 0x100
    NOOP_STUB_VA = SCRATCH_BASE + 0x110
    THIS_VA = SCRATCH_BASE + 0x200
    MODE_STRUCT_VA = SCRATCH_BASE + 0x400
    REQ_STRUCT_VA = SCRATCH_BASE + 0x500
    STACK_TOP = SCRATCH_BASE + 0x8000
    BSS_PAD = 0x400000
    KAPI_START = 0x635C60D8
    KAPI_END = 0x635C6420

    def __init__(self, avcam_bytes: bytes, contracts: Sgc0SyncSeqContracts) -> None:
        from unicorn import (
            UC_ARCH_ARM,
            UC_HOOK_CODE,
            UC_HOOK_MEM_READ,
            UC_HOOK_MEM_WRITE,
            UC_MEM_WRITE,
            UC_MODE_THUMB,
            UC_PROT_EXEC,
            UC_PROT_READ,
            UC_PROT_WRITE,
            Uc,
        )
        from unicorn.arm_const import (
            UC_ARM_REG_C1_C0_2,
            UC_ARM_REG_FPEXC,
            UC_ARM_REG_LR,
            UC_ARM_REG_PC,
            UC_ARM_REG_R0,
            UC_ARM_REG_R1,
            UC_ARM_REG_R2,
            UC_ARM_REG_R3,
            UC_ARM_REG_SP,
        )

        self._reg_consts = (
            UC_ARM_REG_R0,
            UC_ARM_REG_R1,
            UC_ARM_REG_R2,
            UC_ARM_REG_R3,
            UC_ARM_REG_SP,
            UC_ARM_REG_LR,
            UC_ARM_REG_PC,
        )
        self.contracts = contracts
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)

        # Enable VFP CP10/CP11 for SyncSeq_Setup double-precision interval math.
        cpacr = self.uc.reg_read(UC_ARM_REG_C1_C0_2)
        self.uc.reg_write(UC_ARM_REG_C1_C0_2, cpacr | (0xF << 20))
        self.uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)

        mapped_oril = (len(avcam_bytes) + self.BSS_PAD + 0xFFF) & ~0xFFF
        self.uc.mem_map(ORIL_BASE, mapped_oril, UC_PROT_READ | UC_PROT_WRITE | UC_PROT_EXEC)
        self.uc.mem_write(ORIL_BASE, avcam_bytes)

        self.uc.mem_map(
            self.SCRATCH_BASE,
            self.SCRATCH_SIZE,
            UC_PROT_READ | UC_PROT_WRITE | UC_PROT_EXEC,
        )
        # Thumb stub: movs r0, #0; bx lr
        self.uc.mem_write(self.NOOP_STUB_VA, struct.pack("<HH", 0x2000, 0x4770))

        # Populate uninitialized liro.ko Kernel API Table slots (spinlock/log) without touching .text.
        for va in range(self.KAPI_START, min(self.KAPI_END, ORIL_BASE + len(avcam_bytes)), 4):
            if struct.unpack_from("<I", avcam_bytes, va - ORIL_BASE)[0] == 0:
                self.uc.mem_write(va, struct.pack("<I", self.NOOP_STUB_VA | 1))

        self.uc.mem_map(
            DFE_MMIO_BASE,
            DFE_MMIO_END - DFE_MMIO_BASE,
            UC_PROT_READ | UC_PROT_WRITE,
        )

        self.trace: list[MmioAccess] = []
        self.touched_addrs: set[int] = set()
        self.insns_executed = 0

        def _on_code(_uc: object, address: int, _size: int, _user_data: object) -> None:
            if address != self.RET_STUB_VA:
                self.insns_executed += 1

        def _on_mmio(
            uc_obj: Uc,
            access: int,
            address: int,
            size: int,
            value: int,
            _user_data: object,
        ) -> None:
            is_write = access == UC_MEM_WRITE
            op = "W" if is_write else "R"
            mask = (1 << (size * 8)) - 1
            val = (value & mask) if is_write else int.from_bytes(uc_obj.mem_read(address, size), "little")
            pc = uc_obj.reg_read(UC_ARM_REG_PC)
            self.trace.append(MmioAccess(op=op, addr=address, size=size, value=val, pc=pc))
            self.touched_addrs.add(address & ~0x3)

        self.uc.hook_add(UC_HOOK_CODE, _on_code)
        self.uc.hook_add(
            UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE,
            _on_mmio,
            begin=DFE_MMIO_BASE,
            end=DFE_MMIO_END - 1,
        )
        self.reset_mmio()

    def reset_mmio(self) -> None:
        self.uc.mem_write(DFE_MMIO_BASE, b"\x00" * (DFE_MMIO_END - DFE_MMIO_BASE))
        self.uc.mem_write(LEO_CRG_RESET, struct.pack("<I", COLD_LEO_RESET))
        self.trace.clear()
        self.touched_addrs.clear()
        self.insns_executed = 0

    def init_drv_sgc_object(self, *, sgc_id: int = 0) -> None:
        # [this+0]=vtable, [this+4]=0xFC000000, [this+8]=sgc_id, [this+9]=started(0), [this+0xC]=0x36
        payload = struct.pack(
            "<IIBBHI",
            self.contracts.drv_sgc_vtable,
            DFE_MMIO_BASE,
            sgc_id & 0xFF,
            0,
            0,
            0x36,
        )
        self.uc.mem_write(self.THIS_VA, payload)

    def call_thumb(
        self,
        fn_va: int,
        r0: int = 0,
        r1: int = 0,
        r2: int = 0,
        r3: int = 0,
        stack_words: tuple[int, ...] = (),
        *,
        max_insns: int = 2000,
    ) -> None:
        reg_r0, reg_r1, reg_r2, reg_r3, reg_sp, reg_lr, reg_pc = self._reg_consts
        remaining = max_insns - self.insns_executed
        if remaining <= 0:
            raise Sgc0SyncSeqError(f"Instruction budget ({max_insns}) exhausted before calling {fn_va:#x}")

        for idx, word in enumerate(stack_words):
            self.uc.mem_write(self.STACK_TOP + idx * 4, struct.pack("<I", word & 0xFFFFFFFF))
        self.uc.reg_write(reg_sp, self.STACK_TOP)
        self.uc.reg_write(reg_r0, r0 & 0xFFFFFFFF)
        self.uc.reg_write(reg_r1, r1 & 0xFFFFFFFF)
        self.uc.reg_write(reg_r2, r2 & 0xFFFFFFFF)
        self.uc.reg_write(reg_r3, r3 & 0xFFFFFFFF)
        self.uc.reg_write(reg_lr, self.RET_STUB_VA | 1)

        self.uc.emu_start(fn_va | 1, self.RET_STUB_VA, timeout=200_000, count=remaining)
        end_pc = self.uc.reg_read(reg_pc)
        if end_pc != self.RET_STUB_VA or self.insns_executed > max_insns:
            raise Sgc0SyncSeqError(
                f"Function {fn_va:#x} did not return within {max_insns} insns "
                f"(end_pc={end_pc:#x}, insns={self.insns_executed})"
            )

    def snapshot_result(self) -> UnicornReplayResult:
        final_regs: dict[int, int] = {}
        for addr in sorted(self.touched_addrs):
            final_regs[addr] = struct.unpack_from("<I", self.uc.mem_read(addr, 4), 0)[0]
        return UnicornReplayResult(
            trace=tuple(self.trace),
            final_regs=final_regs,
            insns_executed=self.insns_executed,
        )


def _load_avcam_and_contracts(
    avcam_source: Path | str | bytes,
    contracts: Sgc0SyncSeqContracts | None,
) -> tuple[bytes, Sgc0SyncSeqContracts]:
    data = (
        bytes(avcam_source)
        if isinstance(avcam_source, (bytes, bytearray, memoryview))
        else Path(avcam_source).read_bytes()
    )
    resolved = contracts if contracts is not None else extract_sgc0_syncseq_contracts(avcam_source)
    return data, resolved


def unicorn_replay_drv_sgc0_mode1(
    avcam_source: Path | str | bytes,
    *,
    contracts: Sgc0SyncSeqContracts | None = None,
    trig_mode: int = 1,
    rpt: int = 1,
    nlr_header: tuple[int, ...] | None = None,
    max_insns: int = 2000,
) -> UnicornReplayResult:
    """Replay unmodified DrvSgc Enable + ConfigureOutputs + SetTiming (+ SetDrvSgc0Nlr) + DrvSgc0Start."""
    if max_insns <= 0 or max_insns > 2000:
        raise Sgc0SyncSeqError(f"max_insns must be in 1..2000, got {max_insns}")
    data, resolved = _load_avcam_and_contracts(avcam_source, contracts)
    harness = _AvcamUnicornHarness(data, resolved)
    harness.init_drv_sgc_object(sgc_id=0)
    m = resolved.drv_sgc_methods

    harness.call_thumb(m.enable, harness.THIS_VA, max_insns=max_insns)
    harness.call_thumb(
        m.configure_outputs,
        harness.THIS_VA,
        0,
        3,
        32,
        stack_words=(4,),
        max_insns=max_insns,
    )
    harness.call_thumb(
        m.set_timing,
        harness.THIS_VA,
        MODE1_V_TOTAL,
        MODE1_H_TOTAL,
        MODE1_NLR_DELTA,
        stack_words=(MODE1_NLR_DEN, MODE1_NLR_NUM),
        max_insns=max_insns,
    )
    if nlr_header is not None:
        if len(nlr_header) != 10:
            raise Sgc0SyncSeqError(f"nlr_header requires 10 u16 fields, got {len(nlr_header)}")
        nlr_va = harness.SCRATCH_BASE + 0x600
        harness.uc.mem_write(nlr_va, struct.pack("<10H", *(w & 0xFFFF for w in nlr_header)))
        harness.call_thumb(m.set_drv_sgc0_nlr, harness.THIS_VA, nlr_va, max_insns=max_insns)
    harness.call_thumb(
        m.drv_sgc0_start,
        harness.THIS_VA,
        trig_mode,
        rpt,
        max_insns=max_insns,
    )
    return harness.snapshot_result()


def unicorn_replay_drv_sgc0_start_modes(
    avcam_source: Path | str | bytes,
    *,
    contracts: Sgc0SyncSeqContracts | None = None,
    rpt: int = 0,
    max_insns: int = 2000,
) -> dict[int, UnicornReplayResult]:
    """Replay unmodified DrvSgc0Start across all 3 trigger modes (0, 1, 2)."""
    if max_insns <= 0 or max_insns > 2000:
        raise Sgc0SyncSeqError(f"max_insns must be in 1..2000, got {max_insns}")
    data, resolved = _load_avcam_and_contracts(avcam_source, contracts)
    harness = _AvcamUnicornHarness(data, resolved)
    results: dict[int, UnicornReplayResult] = {}

    for trig_mode in (0, 1, 2):
        harness.reset_mmio()
        harness.init_drv_sgc_object(sgc_id=0)
        harness.call_thumb(
            resolved.drv_sgc_methods.drv_sgc0_start,
            harness.THIS_VA,
            trig_mode,
            rpt,
            max_insns=max_insns,
        )
        results[trig_mode] = harness.snapshot_result()
    return results


def unicorn_replay_syncseq_setup(
    avcam_source: Path | str | bytes,
    *,
    contracts: Sgc0SyncSeqContracts | None = None,
    is_preview: bool = True,
    frame_ms: float = 1000.0 / 59.94,
    readout_us: int = MODE1_READOUT_US,
    clk_mhz: int = 72,
    max_insns: int = 2000,
) -> UnicornReplayResult:
    """Replay unmodified SyncSeq_Setup for Mode-1 (1940 x 619 + 17/97 @ 72 MHz)."""
    if max_insns <= 0 or max_insns > 2000:
        raise Sgc0SyncSeqError(f"max_insns must be in 1..2000, got {max_insns}")
    data, resolved = _load_avcam_and_contracts(avcam_source, contracts)
    harness = _AvcamUnicornHarness(data, resolved)
    harness.uc.mem_write(resolved.syncseq_clk_mhz_va, struct.pack("<I", clk_mhz))

    mode_buf = bytearray(0xC0)
    struct.pack_into("<H", mode_buf, 0x18, 0)
    struct.pack_into("<II", mode_buf, 0x38, MODE1_V_TOTAL, MODE1_H_TOTAL)
    mode_buf[0x40] = MODE1_NLR_DELTA & 0xFF
    mode_buf[0x41] = MODE1_NLR_DEN & 0xFF
    mode_buf[0x42] = MODE1_NLR_NUM & 0xFF
    struct.pack_into("<H", mode_buf, 0xA4, 1)
    harness.uc.mem_write(harness.MODE_STRUCT_VA, bytes(mode_buf))

    req_buf = bytearray(0x20)
    struct.pack_into(
        "<IIII",
        req_buf,
        0x00,
        0,
        1 if is_preview else 0,
        0,
        readout_us,
    )
    struct.pack_into("<d", req_buf, 0x10, frame_ms)
    harness.uc.mem_write(harness.REQ_STRUCT_VA, bytes(req_buf))

    harness.call_thumb(
        resolved.syncseq_setup,
        harness.MODE_STRUCT_VA,
        harness.REQ_STRUCT_VA,
        max_insns=max_insns,
    )
    return harness.snapshot_result()


class DrvSgc0SyncSeqModel:
    """Deterministic hardware model coupling SYSV -> SyncSeq -> DrvSGC0 -> P2S Ch 1 -> SLVS-EC."""

    P2S_DATA_START = 0x340
    P2S_DATA_END = 0x740
    P2S_DATA_SLOTS = (P2S_DATA_END - P2S_DATA_START) // 4

    def __init__(self, *, sysv_cold: bool = True) -> None:
        self.sysv_cold = sysv_cold
        self.sensor = Imx284VirtualSensor()
        self.regs: dict[int, int] = {}
        self._dfetop_core_mask = 0
        self._sgc0_hw_active = False
        self._sgc0_ever_ticked = False
        self._sgc0_tick_in_frame = 0
        self._sgc0_h_cnt = 0
        self._sgc0_v_cnt = 0
        self._sgc0_fcnt = 0
        self._latched_hvcnt = COLD_HVCNT_IDLE
        self._latched_fcnt = COLD_FCNT_IDLE
        self._fcnt_latch_prev = 0
        self._syncseq_soft_prev = 0
        self._syncseq_elapsed_ticks = 0
        self._syncseq_ext_trig_fired = False
        self._p2s_busy = False
        self._p2s_status = 0
        self.completed_p2s_bursts = 0
        self.reset()

    def reset(self) -> None:
        """Reset all coupled blocks to cold boot or active-SYSV baseline."""
        self.regs.clear()
        self.sensor.reset()
        self.regs[LEO_CRG_RESET] = COLD_LEO_RESET
        self.regs[DFE_MBOX_STAT] = 0x0000FE00  # idle 0xFE
        self.regs[0xFC080030] = COLD_SYNCSEQ_INTERVAL
        self.regs[0xFC08010C] = 0x00000003
        self.regs[0xFC0A1118] = COLD_HVCNT_IDLE
        self.regs[0xFC0A1128] = COLD_FCNT_IDLE
        self.regs[0xF3400000] = 0 if self.sysv_cold else 1
        self.regs[0xF3402010] = 0 if self.sysv_cold else 1
        self.regs[0xF3402020] = 0 if self.sysv_cold else 1

        self._dfetop_core_mask = 0
        self._sgc0_hw_active = False
        self._sgc0_ever_ticked = False
        self._sgc0_tick_in_frame = 0
        self._sgc0_h_cnt = 0
        self._sgc0_v_cnt = 0
        self._sgc0_fcnt = 0
        self._latched_hvcnt = COLD_HVCNT_IDLE
        self._latched_fcnt = COLD_FCNT_IDLE
        self._fcnt_latch_prev = 0
        self._syncseq_soft_prev = 0
        self._syncseq_elapsed_ticks = 0
        self._syncseq_ext_trig_fired = False
        self._p2s_busy = False
        self._p2s_status = 0
        self.completed_p2s_bursts = 0

    @property
    def sysv_enabled(self) -> bool:
        crg_ok = (self.regs.get(0xF3400000, 1) & 1) != 0 or (self.regs.get(0xF3402010, 0) & 0x3) != 0
        mode_en = (self.regs.get(0xF3402010, 0) & 0x3) != 0
        out_en = (self.regs.get(0xF3402020, 1) & 0x1) != 0
        return crg_ok and mode_en and out_en

    @property
    def p2s_busy(self) -> bool:
        return self._p2s_busy

    def programmed_sgc0_frame_ticks(self) -> int:
        """Compute exact 72 MHz ticks per DrvSGC0 frame from +0x1120, +0x11A8, and +0x11AC."""
        hv = self.regs.get(0xFC0A1120, 0)
        h_total = (hv & 0x3FFF) + 1
        v_total = ((hv >> 16) & 0x3FFF) + 1
        nlr_ctrl = self.regs.get(0xFC0A11A8, 0)
        nlr_frac = self.regs.get(0xFC0A11AC, 0)
        if (nlr_ctrl & 1) and (nlr_frac & 0xFF) != 0:
            v_alt = (nlr_ctrl >> 16) & 0x3FFF
            nlr_delta = (v_alt + 1) - v_total
            den = nlr_frac & 0xFF
            num = (nlr_frac >> 16) & 0xFF
            return ((v_total * den + nlr_delta * num) * h_total) // den
        return h_total * v_total

    def _sgc0_clock_and_reset_ok(self) -> bool:
        leo_released = (self.regs.get(LEO_CRG_RESET, COLD_LEO_RESET) & (1 << 6)) == 0
        dfetop_en = (self._dfetop_core_mask & (1 << 8)) != 0
        local_en = (self.regs.get(0xFC0A1100, 0) & 1) != 0
        return leo_released and dfetop_en and local_en

    def _reset_sgc0_runtime(self, *, clear_start_req: bool) -> None:
        self._sgc0_hw_active = False
        self._sgc0_ever_ticked = False
        self._sgc0_tick_in_frame = 0
        self._sgc0_h_cnt = 0
        self._sgc0_v_cnt = 0
        self._latched_hvcnt = COLD_HVCNT_IDLE
        self._latched_fcnt = COLD_FCNT_IDLE
        if clear_start_req:
            self.regs[0xFC0A1110] = 0

    def _complete_p2s_transfer(self) -> None:
        raw = bytearray(self.P2S_DATA_SLOTS)
        for idx in range(self.P2S_DATA_SLOTS):
            raw[idx] = self.regs.get(P2S_CH1_BASE + self.P2S_DATA_START + 4 * idx, 0) & 0xFF
        if raw[0] == 0x81:
            decode_p2s_frame(raw)
            self.sensor.apply_frame(raw)
        self._p2s_busy = False
        self._p2s_status |= 1
        self.completed_p2s_bursts += 1

    def _slvsec_link_locked(self) -> bool:
        outputs_committed = (
            (self.regs.get(0xFC0A110C, 0) & 1) != 0
            and (self.regs.get(0xFC0A1194, 0) & (1 << 16)) != 0
            and (self.regs.get(0xFC0A1198, 0) & (1 << 16)) != 0
            and (self.regs.get(0xFC0A113C, 0) & 1) == 0
        )
        exp = self.sensor.exposure_state()
        return self._sgc0_hw_active and outputs_committed and exp.slvs_ec_streaming and not exp.standby

    def read32(self, addr: int) -> int:
        if (addr & 0x3) != 0:
            raise Sgc0SyncSeqError(f"Unaligned 32-bit MMIO read at {addr:#x}")

        # Leo PLL lock bits reflect enabled PLLs on 0xFC021000[0,4,8]
        if addr in (0xFC021050, 0xFC021054, 0xFC021058):
            pll_idx = (addr - 0xFC021050) // 4
            pll_en = (self.regs.get(0xFC021000, 0) >> (pll_idx * 4)) & 1
            return pll_en

        if addr == 0xFC0A0000:
            return self._dfetop_core_mask
        if addr == 0xFC0A0004:
            return 0
        if addr == 0xFC0A1110:
            req = self.regs.get(0xFC0A1110, 0) & 1
            active = (1 << 16) if self._sgc0_hw_active else 0
            return active | req
        if addr == 0xFC0A1118:
            return self._latched_hvcnt
        if addr == 0xFC0A1128:
            return self._latched_fcnt
        if addr in (0xFC088904, 0xFC088F04):
            return self._p2s_status
        if addr in (0xFC088908, 0xFC088F08, 0xFC088F00):
            return 1 if self._p2s_busy else 0
        if addr == 0xFC0F00B0:
            return 1 if self._slvsec_link_locked() else 0
        if addr == 0xF3402024:
            return 1 if self.sysv_enabled else 0
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        if (addr & 0x3) != 0:
            raise Sgc0SyncSeqError(f"Unaligned 32-bit MMIO write at {addr:#x}")
        val = value & 0xFFFFFFFF

        # DFE coprocessor mailbox & doorbell
        if addr == DFE_MBOX_KICK or (addr == DFE_DOORBELL and (val & (1 << 27))):
            self.regs[addr] = val
            self.regs[DFE_MBOX_CMD] = 0xFF
            self.regs[DFE_MBOX_STAT] = 0x0000FF00
            self.regs[DFE_TIMER_GATE0] = 0x003C0006
            self.regs[DFE_TIMER_GATE1] = 0x00020000
            return

        # Leo CRG reset register: asserting BIT(6) holds DrvSGC0 in reset
        if addr == LEO_CRG_RESET:
            self.regs[addr] = val
            if val & (1 << 6):
                self._reset_sgc0_runtime(clear_start_req=True)
            return

        # DfeTop W1S / W1C core gates
        if addr == 0xFC0A0000:
            self._dfetop_core_mask = (self._dfetop_core_mask | val) & 0xFFFFFFFF
            return
        if addr == 0xFC0A0004:
            self._dfetop_core_mask &= ~val
            if val & (1 << 8):
                self._reset_sgc0_runtime(clear_start_req=True)
            return

        # SyncSeq W1C IRQ status and soft pulse
        if addr in (0xFC080104, 0xFC080110):
            self.regs[addr] = self.regs.get(addr, 0) & ~val
            return
        if addr == 0xFC080014:
            prev = self._syncseq_soft_prev
            self._syncseq_soft_prev = val & 1
            self.regs[addr] = val
            if prev == 1 and (val & 1) == 0:
                self._on_sync_pulse(interval_ticks=self.regs.get(0xFC080028, 0x40), from_sysv=True)
            return

        # P2S Ch 1 status W1C and start/busy kick
        if addr in (0xFC088904, 0xFC088F04):
            if val & 1:
                self._p2s_status &= ~1
            return
        if addr in (0xFC088908, 0xFC088F08, 0xFC088F00):
            self.regs[addr] = val
            if (val & 1) and not self._p2s_busy:
                self._p2s_busy = True
                run_mode = self.regs.get(0xFC088E00, self.regs.get(0xFC088800, 2)) & 0x3
                if run_mode == 2:
                    self._complete_p2s_transfer()
            return

        # DrvSGC0 block enable, latches, and start request
        if addr == 0xFC0A1100:
            self.regs[addr] = val
            if (val & 1) == 0:
                self._sgc0_hw_active = False
            return
        if addr == 0xFC0A1114:
            self.regs[addr] = val
            if val & 1:
                self._latched_hvcnt = (
                    ((self._sgc0_v_cnt & 0x3FFF) << 16) | (self._sgc0_h_cnt & 0x3FFF)
                    if self._sgc0_ever_ticked
                    else COLD_HVCNT_IDLE
                )
            return
        if addr == 0xFC0A111C:
            prev_latch = self._fcnt_latch_prev
            now_latch = val & 1
            self._fcnt_latch_prev = now_latch
            self.regs[addr] = val
            if now_latch == 1 or (prev_latch == 1 and now_latch == 0):
                self._latched_fcnt = (self._sgc0_fcnt & 0xFF) if self._sgc0_ever_ticked else COLD_FCNT_IDLE
            return
        if addr == 0xFC0A1110:
            req = val & 1
            self.regs[addr] = req
            if req == 0:
                self._sgc0_hw_active = False
            elif self._sgc0_clock_and_reset_ok():
                int_trig = self.regs.get(0xFC0A1108, 0) & 1
                if int_trig == 1:
                    self._activate_sgc0()
            return

        self.regs[addr] = val

    def _activate_sgc0(self) -> None:
        if not self._sgc0_hw_active:
            self._sgc0_hw_active = True
            self._sgc0_ever_ticked = True
            self._sgc0_tick_in_frame = 0
            self._sgc0_h_cnt = 0
            self._sgc0_v_cnt = 0

    def _on_sync_pulse(self, *, interval_ticks: int, from_sysv: bool) -> None:
        """Deliver a master SYSV VD0 or external sync edge into SyncSeq and DrvSGC0."""
        if from_sysv:
            self.regs[0xFC080030] = interval_ticks & 0x00FFFFFF
            self._syncseq_elapsed_ticks = 0
            self._syncseq_ext_trig_fired = False
            self.regs[0xFC080110] = self.regs.get(0xFC080110, 0) | 1
            # If EXT_TRIG_TIMER == 0, fire frame-sync P2S immediately on VD edge
            if self.regs.get(0xFC0800F4, 0) == 0 and self._p2s_busy:
                run_mode = self.regs.get(0xFC088E00, 0) & 0x3
                if run_mode == 0:
                    self._complete_p2s_transfer()
                    self._syncseq_ext_trig_fired = True

        if (self.regs.get(0xFC0A1110, 0) & 1) and self._sgc0_clock_and_reset_ok():
            int_trig = self.regs.get(0xFC0A1108, 0) & 1
            ext_sync = (self.regs.get(0xFC0A1130, 0) >> 16) & 1
            if int_trig == 0:
                if (from_sysv and ext_sync == 0) or ((not from_sysv) and ext_sync == 1):
                    self._activate_sgc0()

    def pulse_ext_sync(self) -> None:
        """Deliver an alternate EXT_SYNC (VD1) edge for DrvSGC0 trig_mode == 2."""
        self._on_sync_pulse(interval_ticks=MODE1_FRAME_TICKS, from_sysv=False)

    def advance_ticks(self, ticks_72mhz: int) -> None:
        """Advance 72 MHz timing domain by ``ticks_72mhz`` without forcing a new SYSV edge."""
        if ticks_72mhz <= 0:
            return

        # Advance SyncSeq EXT_TRIG_TIMER if SYSV has delivered at least one VD reload
        if self.regs.get(0xFC080030, COLD_SYNCSEQ_INTERVAL) != COLD_SYNCSEQ_INTERVAL:
            prev_elapsed = self._syncseq_elapsed_ticks
            self._syncseq_elapsed_ticks += ticks_72mhz
            ext_trig = self.regs.get(0xFC0800F4, 0)
            if (
                not self._syncseq_ext_trig_fired
                and ext_trig > 0
                and prev_elapsed < ext_trig <= self._syncseq_elapsed_ticks
            ):
                self._syncseq_ext_trig_fired = True
                self.regs[0xFC080104] = self.regs.get(0xFC080104, 0) | 1
                if self._p2s_busy and (self.regs.get(0xFC088E00, 0) & 0x3) == 0:
                    self._complete_p2s_transfer()

        if not self._sgc0_hw_active or not self._sgc0_clock_and_reset_ok():
            return

        frame_ticks = max(1, self.programmed_sgc0_frame_ticks())
        hv = self.regs.get(0xFC0A1120, 0)
        h_total = max(1, (hv & 0x3FFF) + 1)
        v_total = max(1, ((hv >> 16) & 0x3FFF) + 1)
        rpt = self.regs.get(0xFC0A112C, 0) & 1

        total = self._sgc0_tick_in_frame + ticks_72mhz
        frames_done, rem = divmod(total, frame_ticks)
        if frames_done > 0:
            if rpt == 0:
                self._sgc0_fcnt = (self._sgc0_fcnt + 1) & 0xFF
                self._sgc0_tick_in_frame = 0
                self._sgc0_h_cnt = h_total - 1
                self._sgc0_v_cnt = v_total - 1
                self._sgc0_hw_active = False
                self.regs[0xFC0A1110] = 0
                return
            self._sgc0_fcnt = (self._sgc0_fcnt + frames_done) & 0xFF

        self._sgc0_tick_in_frame = rem
        self._sgc0_v_cnt = min(v_total - 1, rem // h_total)
        self._sgc0_h_cnt = rem % h_total

    def sysv_tick(self, *, frame_ticks: int = MODE1_FRAME_TICKS) -> bool:
        """Run one master SYSV frame period if SYSV is enabled; returns True if ticked."""
        if not self.sysv_enabled:
            return False
        self._on_sync_pulse(interval_ticks=frame_ticks, from_sysv=True)
        self.advance_ticks(frame_ticks)
        return True

    def enable_sysv(self, *, h_period: int = MODE1_H_TOTAL, v_period: int = MODE1_V_TOTAL) -> None:
        """Program and start the master SYSV V-clock generator at 0xF3402000."""
        self.write32(0xF3400000, 1)
        self.write32(0xF3402014, h_period)
        self.write32(0xF3402018, v_period)
        self.write32(0xF3402030, 0x8000000A)
        self.write32(0xF3402020, 1)
        self.write32(0xF3402010, 1)

    def latch_hvcnt(self) -> int:
        """Trigger 0xFC0A1114 latch and read 0xFC0A1118 (matching DrvSgc diagnostic sequence)."""
        self.write32(0xFC0A1114, 1)
        return self.read32(0xFC0A1118)

    def latch_fcnt(self) -> int:
        """Pulse 0xFC0A111C (1 -> 0) and read 0xFC0A1128 (matching DrvSgc diagnostic sequence)."""
        self.write32(0xFC0A111C, 1)
        self.write32(0xFC0A111C, 0)
        return self.read32(0xFC0A1128) & 0xFF

    def queue_p2s_frame(self, bursts: tuple[P2sBurst, ...] | list[P2sBurst] | bytes, *, run_mode: int) -> None:
        """Load P2S Ch 1 slot registers, set run mode (2=immediate, 0=frame-sync), and kick."""
        raw = bytes(bursts) if isinstance(bursts, (bytes, bytearray, memoryview)) else encode_p2s_frame(bursts)
        for idx, b in enumerate(raw):
            self.write32(P2S_CH1_BASE + self.P2S_DATA_START + 4 * idx, b)
        self.write32(0xFC088E00, run_mode)
        self.write32(0xFC088F04, 1)
        self.write32(0xFC088F08, 1)

    def apply_replay(self, replay: UnicornReplayResult) -> None:
        """Feed an ordered Unicorn MMIO write trace directly into the state machine."""
        for access in replay.writes:
            self.write32(access.addr, access.value)


def make_imx284_mode1_wake_burst() -> bytes:
    """Construct P2S Mode-1 wake/prep burst (clears IMX284 standby, SLVS-EC sync-reset 0x80)."""
    return encode_p2s_frame(
        (
            P2sBurst(IMX284_CHIP_ID, 0x0000, bytes([0x1C, 0x41, 0x10])),
            P2sBurst(IMX284_CHIP_ID, 0x000A, bytes([0x52, 0x01, 0x72, 0x04])),
            P2sBurst(IMX284_CHIP_ID, 0x000F, bytes([0x18])),
            P2sBurst(IMX284_CHIP_ID, 0x0017, bytes([0x04])),
            P2sBurst(IMX284_CHIP_ID, 0x00B3, bytes([0x80])),
            P2sBurst(IMX284_CHIP_ID, 0x0420, bytes([0x99, 0x00])),
            P2sBurst(IMX284_CHIP_ID, 0x0803, bytes([0x00])),
            P2sBurst(IMX284_CHIP_ID, 0x0924, bytes([0x30])),
        )
    )


def make_imx284_mode1_stream_burst() -> bytes:
    """Construct P2S burst #7 (frame-sync run_mode=0 setting IMX284 0x00B3 = 0x90 SLVS-EC stream)."""
    return encode_p2s_frame(
        (
            P2sBurst(IMX284_CHIP_ID, 0x0832, bytes([0x03])),
            P2sBurst(IMX284_CHIP_ID, 0x00B3, bytes([0x90])),
        )
    )
