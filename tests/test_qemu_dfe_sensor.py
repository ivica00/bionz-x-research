"""Offline QEMU qtest/QMP and static hygiene verification for CXD90045 DFE sensor pipeline.

Covers:
- Patch/overlay registration of ``cxd90045_dfe.c`` and ``run.sh`` ``DFE_SENSOR`` / ``SYSV_COLD`` opt-ins.
- Legacy ``sony-a6400`` baseline preservation when ``dfe-sensor`` and ``sysv-cold`` are off.
- Exact bare-metal ``DrvSGC0`` + ``SyncSeq`` + ``P2S`` #7 + ``SLVS-EC`` cold-``SYSV`` stall reproduction.
- Full ``SYSV`` master V-clock unlock through ``SyncSeq``, ``DrvSGC0``, ``P2S`` Ch 1, ``SLVS-EC``,
  ``DfeTop`` / ``0xFC01FF00`` VIC interrupts, and ``RdcW0`` + ``DFE AIF RdcW0`` 14-bit RGGB Bayer DMA.
- ``DrvSGC0`` trigger modes, one-shot ``rpt=0``, ``Vd0Mask`` suppression, and ``DfeTop`` W1C / ``Leo`` reset hazards.
"""
from __future__ import annotations

import re
import struct
import unittest

from project_paths import PROJECT_ROOT
from src.a6400.sensor.imx284_p2s import IMX284_CHIP_ID, P2sBurst, encode_p2s_frame
from tests.test_qemu_emmc import NATIVE_AVAILABLE
from tests.test_qemu_media import qtest_machine, temporary_media


DFE_CORE_SGC0 = 1 << 8
DFE_CORE_AIF = 1 << 15
DFE_CORE_SLVSEC = 1 << 16
DFE_CORE_RDCW0 = 1 << 19
DFE_CORE_BRIDGE = 1 << 24

DFE_IRQ_SGC0_VD0 = 1 << 1
DFE_IRQ_SYNCSEQ_0 = 1 << 17
DFE_IRQ_SYNCSEQ_1 = 1 << 18
DFE_IRQ_RDCW0 = 1 << 25


def _imx284_col3_wake_payload() -> bytes:
    """IMX284 Column-3 (1996x570 11-bit LiveView) setup frame with 0x00B3 = 0x80 sync-reset."""
    return encode_p2s_frame(
        [
            P2sBurst(IMX284_CHIP_ID, 0x0000, bytes([0x2C, 0x40, 0x10])),
            P2sBurst(IMX284_CHIP_ID, 0x0007, bytes([0x20, 0x01])),
            P2sBurst(IMX284_CHIP_ID, 0x000A, bytes([0x6A, 0x00, 0x6C, 0x04])),
            P2sBurst(IMX284_CHIP_ID, 0x000F, bytes([0x18])),
            P2sBurst(IMX284_CHIP_ID, 0x0017, bytes([0x04])),
            P2sBurst(IMX284_CHIP_ID, 0x00B3, bytes([0x80])),
            P2sBurst(IMX284_CHIP_ID, 0x0420, bytes([0x99, 0x00])),
            P2sBurst(IMX284_CHIP_ID, 0x0803, bytes([0x11])),
            P2sBurst(IMX284_CHIP_ID, 0x0924, bytes([0x30])),
        ]
    )


def _imx284_stream_on_payload() -> bytes:
    """7th frame-synchronous P2S burst asserting IMX284 SLVS-EC streaming (0x00B3 = 0x90)."""
    return encode_p2s_frame(
        [
            P2sBurst(IMX284_CHIP_ID, 0x0832, bytes([0x03])),
            P2sBurst(IMX284_CHIP_ID, 0x00B3, bytes([0x90])),
        ]
    )


def _load_p2s_ch1_slots(io, payload: bytes) -> None:
    for slot_idx, byte_val in enumerate(payload):
        io.write(0xFC088340 + 4 * slot_idx, byte_val)
    io.write(0xFC08830C, len(payload))


def _program_dfe_and_mode1_sgc0(io, *, rpt: int = 1, trig_mode: int = 1) -> None:
    """Program DFE mailbox, Leo CRG, DfeTop, Mode-1 DrvSGC0, and SyncSeq."""
    # Stage dummy dfe_dat.bin ARM branch header in guest DRAM and kick DFE mailbox
    io.write_memory(0x10000000, struct.pack("<I", 0xEA00000E))
    io.write(0xFC0300F8, 0x10000000 >> 8)
    io.write(0xFC0300FC, 1)

    # Release Leo reset and enable DfeTop cores + bridge
    io.write(0xFC021024, 0)
    io.write(
        0xFC0A0000,
        DFE_CORE_SGC0 | DFE_CORE_SLVSEC | DFE_CORE_RDCW0 | DFE_CORE_BRIDGE,
    )
    io.write(0xFC0A0108, 1)

    # Program Mode-1 DrvSGC0 (1940 x 619 + 17/97 = 1,201,200 ticks @ 72 MHz)
    io.write(0xFC0A1100, 1)
    io.write(0xFC0A110C, 1)
    io.write(0xFC0A1120, 0x026A0793)
    io.write(0xFC0A1124, 0x00000206)
    io.write(0xFC0A11A8, 0x026B0001)
    io.write(0xFC0A11AC, 0x00110061)
    io.write(0xFC0A114C, 4)
    io.write(0xFC0A1150, 0x00250005)
    io.write(0xFC0A1198, 1 << 16)
    io.write(0xFC0A1194, 1 << 16)
    io.write(0xFC0A113C, 0)
    io.write(0xFC0A112C, rpt)
    if trig_mode == 0:
        io.write(0xFC0A1130, 0)
        io.write(0xFC0A1108, 1)
    elif trig_mode == 1:
        io.write(0xFC0A1130, 0)
        io.write(0xFC0A1108, 0)
    elif trig_mode == 2:
        io.write(0xFC0A1130, 1 << 16)
        io.write(0xFC0A1108, 0)

    # Program SyncSeq limit and 72 MHz P2S frame-sync trigger offset
    io.write(0xFC080028, 0x00FFFFFC)
    io.write(0xFC0800F4, 0x00012AC8)


class QemuDfeSensorStaticHygieneTests(unittest.TestCase):
    """Always-run static checks for QEMU patch, machine defaults, and run.sh opt-ins."""

    def test_patch_and_overlay_register_cxd90045_dfe(self) -> None:
        dfe_c = PROJECT_ROOT / "emulators/qemu-a6400/qemu-overlay/hw/misc/cxd90045_dfe.c"
        dfe_h = PROJECT_ROOT / "emulators/qemu-a6400/qemu-overlay/include/hw/misc/cxd90045_dfe.h"
        patch_file = PROJECT_ROOT / "emulators/qemu-a6400/patches/qemu-v11.1.3-sony-a6400.patch"

        self.assertTrue(dfe_c.is_file(), f"Missing {dfe_c}")
        self.assertTrue(dfe_h.is_file(), f"Missing {dfe_h}")
        patch_text = patch_file.read_text(encoding="utf-8")
        self.assertIn("../misc/cxd90045_dfe.c", patch_text)
        self.assertIn("CONFIG_SONY_A6400", patch_text)

    def test_machine_defaults_and_run_sh_opt_in_wiring(self) -> None:
        machine_c = (
            PROJECT_ROOT / "emulators/qemu-a6400/qemu-overlay/hw/arm/sony_a6400.c"
        ).read_text(encoding="utf-8")
        dfe_c = (
            PROJECT_ROOT / "emulators/qemu-a6400/qemu-overlay/hw/misc/cxd90045_dfe.c"
        ).read_text(encoding="utf-8")
        run_sh = (PROJECT_ROOT / "emulators/qemu-a6400/run.sh").read_text(encoding="utf-8")

        # Both flags must default off (zero-initialized static bools)
        self.assertIsNotNone(
            re.search(r"static bool sony_a6400_dfe_sensor\s*;", machine_c),
            "sony_a6400_dfe_sensor must default off",
        )
        self.assertIsNotNone(
            re.search(r"static bool sony_a6400_sysv_cold\s*;", machine_c),
            "sony_a6400_sysv_cold must default off",
        )
        for token in ('"dfe-sensor"', '"sysv-cold"', '"cxd90014-p2s-ch1"', "cxd90045_dfe_create"):
            self.assertTrue(token in machine_c, f"Missing {token} in sony_a6400.c")
        self.assertTrue(
            '"cxd90045-dfe"' in dfe_c,
            'Missing QOM child registration "cxd90045-dfe" in cxd90045_dfe.c',
        )

        # run.sh must keep DFE_SENSOR and SYSV_COLD opt-in behind :-0 defaults
        dfe_block = re.search(
            r'if \[\s*"\$\{DFE_SENSOR:-0\}"\s*=\s*1\s*\];\s*then(.*?)fi',
            run_sh,
            re.DOTALL,
        )
        self.assertIsNotNone(dfe_block, "DFE_SENSOR opt-in block missing in run.sh")
        self.assertIn("dfe-sensor=on", dfe_block.group(1))

        sysv_block = re.search(
            r'if \[\s*"\$\{SYSV_COLD:-0\}"\s*=\s*1\s*\];\s*then(.*?)fi',
            run_sh,
            re.DOTALL,
        )
        self.assertIsNotNone(sysv_block, "SYSV_COLD opt-in block missing in run.sh")
        self.assertIn("sysv-cold=on", sysv_block.group(1))


@unittest.skipUnless(NATIVE_AVAILABLE, "Optional WSL/Linux sony-a6400 QEMU build absent")
class QemuDfeSensorRuntimeTests(unittest.TestCase):
    """QEMU qtest + QMP verification of DFE, DrvSGC0, SyncSeq, P2S Ch 1, SLVS-EC, and RdcW0."""

    def test_default_machine_preserves_legacy_fc0_and_p2s_baseline(self) -> None:
        with temporary_media() as root:
            with qtest_machine(root, "sony-a6400", "-d", "unimp") as (io, _):
                self.assertEqual(io.read(0xFC021050) & 1, 1)
                self.assertEqual(io.read(0xFC02A080) & 1, 1)
                self.assertEqual(io.read(0xFC080030), 0)

                # Without dfe-sensor=on, 0xFC0A0004 behaves as plain read/write regfile
                io.write(0xFC0A0004, 0xA55AA55A)
                self.assertEqual(io.read(0xFC0A0004), 0xA55AA55A)

                # P2S Ch 1 run_mode=0 completes in 100 us without needing SyncSeq
                _load_p2s_ch1_slots(io, _imx284_stream_on_payload())
                io.write(0xFC088E00, 0)
                io.write(0xFC088F08, 1)
                io.step(100_000)
                self.assertEqual(io.read(0xFC088F08) & 1, 0)
                self.assertEqual(io.read(0xFC088F04), 1)

    def test_cold_sysv_reproduces_baremetal_sgc0_and_syncseq_stall(self) -> None:
        with temporary_media() as root:
            machine = "sony-a6400,dfe-sensor=on,sysv-cold=on"
            with qtest_machine(root, machine) as (io, qmp):
                _program_dfe_and_mode1_sgc0(io, rpt=1, trig_mode=1)
                self.assertEqual(io.read(0xFC0300F4), 0xFF)
                self.assertEqual(io.read(0xFC098808), 0x003C0006)
                self.assertEqual(io.read(0xFC098908), 0x00020000)

                # Send 6 software-triggered P2S bursts (mode=2) configuring IMX284 Column 3
                wake_payload = _imx284_col3_wake_payload()
                for _ in range(6):
                    _load_p2s_ch1_slots(io, wake_payload)
                    io.write(0xFC088E00, 2)
                    io.write(0xFC088F08, 1)
                    io.step(100_000)
                    self.assertEqual(io.read(0xFC088F08) & 1, 0)
                    self.assertEqual(io.read(0xFC088F04), 1)
                    io.write(0xFC088F04, 1)
                    self.assertEqual(io.read(0xFC088F04), 0)

                self.assertEqual(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90014-p2s-ch1", "property": "imx284-active-col"},
                    ),
                    3,
                )
                self.assertEqual(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90014-p2s-ch1", "property": "imx284-mode"},
                    ),
                    "liveview_1996x570_11bit",
                )
                self.assertFalse(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90014-p2s-ch1", "property": "imx284-streaming"},
                    )
                )

                # Start DrvSGC0 (trig_mode=1) and queue 7th frame-sync P2S burst (0x00B3=0x90)
                io.write(0xFC0A1110, 1)
                _load_p2s_ch1_slots(io, _imx284_stream_on_payload())
                io.write(0xFC088E00, 0)
                io.write(0xFC088F08, 1)

                # Advance 35 ms WITHOUT starting SYSV (0xF3402000)
                io.step(35_000_000)

                # Exact bare-metal cold-SYSV stall state
                self.assertEqual(io.read(0xFC0A1110), 0x00000001)
                io.write(0xFC0A1114, 1)
                self.assertEqual(io.read(0xFC0A1118), 0x3FFF3FFF)
                io.write(0xFC0A111C, 1)
                self.assertEqual(io.read(0xFC0A1128), 0x7C)
                self.assertEqual(io.read(0xFC080030), 0x00EE466C)
                self.assertEqual(io.read(0xFC088F08) & 1, 1)
                self.assertEqual(io.read(0xFC088F04), 0)
                self.assertEqual(io.read(0xFC0F00B0), 0)
                self.assertFalse(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90045-dfe", "property": "sgc0-active"},
                    )
                )
                self.assertFalse(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90045-dfe", "property": "slvsec-locked"},
                    )
                )

    def test_sysv_start_unlocks_syncseq_sgc0_p2s_slvsec_and_rdcw0_bayer_dma(self) -> None:
        with temporary_media() as root:
            machine = "sony-zve10,dfe-sensor=on,sysv-cold=on,liveview-source=bars"
            with qtest_machine(root, machine) as (io, qmp):
                _program_dfe_and_mode1_sgc0(io, rpt=1, trig_mode=1)

                # Configure DFE VIC vector IDs and enable channels 17, 18, 23, 26
                for ch, vec_id in ((17, 0x11), (18, 0x12), (23, 0x17), (26, 0x1A)):
                    io.write(0xFC01F100 + 4 * ch, vec_id)
                io.write(0xFC01F010, (1 << 17) | (1 << 18) | (1 << 23) | (1 << 26))
                io.write(
                    0xFC0A0200,
                    DFE_IRQ_SGC0_VD0 | DFE_IRQ_SYNCSEQ_0 | DFE_IRQ_SYNCSEQ_1 | DFE_IRQ_RDCW0,
                )
                self.assertEqual(io.read(0xFC01FF00), 0xFF)

                # Send 6 software-triggered bursts + queue 7th frame-synchronous stream burst
                wake_payload = _imx284_col3_wake_payload()
                for _ in range(6):
                    _load_p2s_ch1_slots(io, wake_payload)
                    io.write(0xFC088E00, 2)
                    io.write(0xFC088F08, 1)
                    io.step(100_000)
                    io.write(0xFC088F04, 1)

                io.write(0xFC0A1110, 1)
                _load_p2s_ch1_slots(io, _imx284_stream_on_payload())
                io.write(0xFC088E00, 0)
                io.write(0xFC088F08, 1)

                # Clear destination DRAM buffers and arm BOTH standalone RdcW0 and DFE AIF RdcW0
                io.write_memory(0x12000000, b"\x00" * 1024)
                io.write_memory(0x12010000, b"\x00" * 1024)

                io.write(0xFC0E1100, 1)
                io.write(0xFC0E112C, 32)
                io.write(0xFC0E1134, 16)
                io.write(0xFC0E1150, 0x12000000)
                io.write(0xFC0E1154, 64)
                io.write(0xFC0E110C, 0x10E)
                io.write(0xFC0E1110, 0x101)

                io.write(0xFC0A0000, DFE_CORE_AIF)
                io.write(0xFC0A9220, 0x12010000)
                io.write(0xFC0A9224, 64)
                io.write(0xFC0A9228, 0)
                io.write(0xFC0A922C, 15)
                io.write(0xFC0A9200, 1)

                # Start master SYSV V-clock and advance across 2 V-periods (20 ms + 20 ms)
                io.write(0xF3402010, 1)
                io.write(0xF3402030, 1)
                io.step(20_000_000)
                io.step(20_000_000)

                # SyncSeq & DrvSGC0 unlocked and running at 1,201,200 ticks (60 Hz)
                self.assertEqual(io.read(0xFC080030), 0x00125430)
                self.assertEqual(io.read(0xFC0A1110), 0x00010001)
                io.write(0xFC0A1114, 1)
                self.assertNotEqual(io.read(0xFC0A1118), 0x3FFF3FFF)
                io.write(0xFC0A111C, 1)
                self.assertNotEqual(io.read(0xFC0A1128) & 0xFF, 0x7C)

                # P2S #7 (0x00B3 = 0x90) completed & SLVS-EC locked
                self.assertEqual(io.read(0xFC088F08) & 1, 0)
                self.assertEqual(io.read(0xFC088F04), 1)
                self.assertTrue(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90014-p2s-ch1", "property": "imx284-streaming"},
                    )
                )
                self.assertEqual(io.read(0xFC0F00B0), 1)
                self.assertTrue(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90045-dfe", "property": "slvsec-locked"},
                    )
                )
                self.assertGreaterEqual(
                    qmp.request(
                        "qom-get",
                        {"path": "/machine/cxd90045-dfe", "property": "rdcw0-frames"},
                    ),
                    2,
                )

                # DfeTop sub-IRQ status & 0xFC01FF00 VIC vector dispatch + W1C clear
                expected_irqs = (
                    DFE_IRQ_SGC0_VD0 | DFE_IRQ_SYNCSEQ_0 | DFE_IRQ_SYNCSEQ_1 | DFE_IRQ_RDCW0
                )
                self.assertEqual(io.read(0xFC0A0208) & expected_irqs, expected_irqs)
                self.assertEqual(io.read(0xFC0A0010) & expected_irqs, expected_irqs)

                dispatched: list[int] = []
                for _ in range(4):
                    vec = io.read(0xFC01FF00)
                    dispatched.append(vec)
                    io.write(0xFC01FF00, 1)
                self.assertEqual(dispatched, [0x11, 0x12, 0x17, 0x1A])
                self.assertEqual(io.read(0xFC01FF00), 0xFF)

                io.write(0xFC0A0204, expected_irqs)
                self.assertEqual(io.read(0xFC0A0208) & expected_irqs, 0)
                self.assertEqual(io.read(0xFC0A0010) & expected_irqs, 0)

                # Read back 1024-byte DRAM buffers from standalone RdcW0 and DFE AIF RdcW0
                raw_standalone = io.read_memory(0x12000000, 1024)
                raw_aif = io.read_memory(0x12010000, 1024)
                self.assertEqual(raw_standalone, raw_aif)

                pixels = struct.unpack("<512H", raw_standalone)
                self.assertTrue(all(0 < px <= 0x3FFF for px in pixels))
                # Across 32-px width (8 SMPTE bars x 4 columns), R-phase samples vary spatially
                bar_r_samples = [pixels[4 * bar] for bar in range(8)]
                self.assertEqual(len(set(bar_r_samples)), 8)
                # Within top-left 2x2 RGGB quad (R, Gr, Gb, B), channel gains produce distinct codes
                quad_rggb = (pixels[0], pixels[1], pixels[32], pixels[33])
                self.assertEqual(len(set(quad_rggb)), 4)

    def test_sgc0_trig_modes_vd0_mask_oneshot_and_reset_hazards(self) -> None:
        with temporary_media() as root:
            machine = "sony-a6400,dfe-sensor=on,sysv-cold=on,liveview-source=bars"
            with qtest_machine(root, machine) as (io, _):
                _program_dfe_and_mode1_sgc0(io, rpt=0, trig_mode=0)

                # Make IMX284 stream via immediate P2S burst so SLVS-EC / RdcW0 can lock
                _load_p2s_ch1_slots(io, _imx284_col3_wake_payload())
                io.write(0xFC088E00, 2)
                io.write(0xFC088F08, 1)
                io.step(100_000)
                io.write(0xFC088F04, 1)
                _load_p2s_ch1_slots(io, _imx284_stream_on_payload())
                io.write(0xFC088E00, 2)
                io.write(0xFC088F08, 1)
                io.step(100_000)
                io.write(0xFC088F04, 1)

                # 1. trig_mode == 0 (0xFC0A1108 = 1) activates immediately with SYSV cold/stopped
                io.write(0xFC0A112C, 0)  # single-shot rpt = 0
                io.write(0xFC0A1108, 1)
                io.write(0xFC0A1110, 1)
                self.assertEqual(io.read(0xFC0A1110), 0x00010001)

                # 2. Single-shot rpt = 0 clears 0xFC0A1110 -> 0 after 1 frame (17 ms > 16.683 ms)
                io.step(17_000_000)
                self.assertEqual(io.read(0xFC0A1110), 0x00000000)
                io.write(0xFC0A111C, 1)
                fcnt_after_oneshot = io.read(0xFC0A1128) & 0xFF
                self.assertEqual(fcnt_after_oneshot, (0x7C + 1) & 0xFF)

                # 3. Vd0Mask = 1 suppresses DFE_IRQ_SGC0_VD0 (BIT(1)) and RdcW0 DMA while Fcnt increments
                io.write(0xFC0A0204, 0xFFFFFFFF)
                io.write_memory(0x12000000, b"\x00" * 256)
                io.write(0xFC0E1100, 1)
                io.write(0xFC0E112C, 16)
                io.write(0xFC0E1134, 8)
                io.write(0xFC0E1150, 0x12000000)
                io.write(0xFC0E1154, 32)
                io.write(0xFC0E110C, 0x10E)
                io.write(0xFC0E1110, 0x101)

                io.write(0xFC0A113C, 1)  # mask VD0
                io.write(0xFC0A112C, 1)  # continuous repeat
                io.write(0xFC0A1108, 1)
                io.write(0xFC0A1110, 1)
                self.assertEqual(io.read(0xFC0A1110), 0x00010001)
                io.step(17_000_000)

                io.write(0xFC0A111C, 1)
                self.assertEqual(io.read(0xFC0A1128) & 0xFF, (fcnt_after_oneshot + 1) & 0xFF)
                self.assertEqual(io.read(0xFC0A0208) & (DFE_IRQ_SGC0_VD0 | DFE_IRQ_RDCW0), 0)
                self.assertEqual(io.read_memory(0x12000000, 256), b"\x00" * 256)

                # 4a. DfeTop W1C hazard (0xFC0A0004 = 0xFFFFFFFF) immediately drops 0xFC0A1110[16] & HVcnt
                io.step(3_000_000)
                io.write(0xFC0A1114, 1)
                self.assertNotEqual(io.read(0xFC0A1118), 0x3FFF3FFF)

                io.write(0xFC0A0004, 0xFFFFFFFF)
                self.assertEqual((io.read(0xFC0A1110) >> 16) & 1, 0)
                self.assertEqual(io.read(0xFC0A1118), 0x3FFF3FFF)
                io.write(0xFC0A1114, 1)
                self.assertEqual(io.read(0xFC0A1118), 0x3FFF3FFF)

                # 4b. Restore DfeTop, re-arm, then assert Leo reset BIT(6) -> drops 0xFC0A1110[16] & HVcnt
                io.write(
                    0xFC0A0000,
                    DFE_CORE_SGC0 | DFE_CORE_SLVSEC | DFE_CORE_RDCW0 | DFE_CORE_BRIDGE,
                )
                io.write(0xFC0A1110, 1)
                self.assertEqual(io.read(0xFC0A1110), 0x00010001)
                io.step(3_000_000)
                io.write(0xFC0A1114, 1)
                self.assertNotEqual(io.read(0xFC0A1118), 0x3FFF3FFF)

                io.write(0xFC021024, 1 << 6)
                self.assertEqual((io.read(0xFC0A1110) >> 16) & 1, 0)
                self.assertEqual(io.read(0xFC0A1118), 0x3FFF3FFF)
                io.write(0xFC0A1114, 1)
                self.assertEqual(io.read(0xFC0A1118), 0x3FFF3FFF)


if __name__ == "__main__":
    unittest.main()
