"""Tests for DrvSGC0, SyncSeq, DfeTop, and SYSV mapping, Unicorn replay, and state machine."""
from __future__ import annotations

import struct
import unittest

from project_paths import PROJECT_ROOT
from src.a6400.sensor import (
    COLD_FCNT_IDLE,
    COLD_HVCNT_IDLE,
    COLD_SYNCSEQ_INTERVAL,
    DFETOP_REGISTERS,
    DRV_SGC0_REGISTERS,
    MODE1_EXT_TRIG_TICKS,
    MODE1_FRAME_TICKS,
    SYNCSEQ_REGISTERS,
    SYSV_REGISTERS,
    ZVE10_CORRESPONDENCE,
    DrvSgc0SyncSeqModel,
    Sgc0SyncSeqError,
    extract_sgc0_syncseq_contracts,
    make_imx284_mode1_stream_burst,
    make_imx284_mode1_wake_burst,
    unicorn_replay_drv_sgc0_mode1,
    unicorn_replay_drv_sgc0_start_modes,
    unicorn_replay_syncseq_setup,
)
from src.a6400.sensor.sgc0_syncseq import (
    DFE_APP_SHA256,
    DFE_DAT_SHA256,
    ILCE6400A_AVCAM_SHA256,
    ILCE6400_AVCAM_SHA256,
    LEO_CRG_RESET,
    ORIL_BASE,
)

A6400_AVCAM = (
    PROJECT_ROOT
    / "firmware/extracted_a6400/firmware.tar_unpacked/0700_part_image/dev/nflasha3_unpacked/av-cam.bin"
)
A6400A_AVCAM = (
    PROJECT_ROOT
    / "firmware/extracted_a6400a_v101/firmware.tar_unpacked/0700_part_image/dev/nflasha3_unpacked/av-cam.bin"
)


def _encode_thumb32_branch(src_va: int, dst_va: int, *, link: bool) -> bytes:
    """Encode a Thumb-2 32-bit unconditional BL or B.W instruction."""
    disp = (dst_va - (src_va + 4)) & 0x01FFFFFE
    if (dst_va - (src_va + 4)) < 0:
        s = 1
    else:
        s = 0
    i1 = (disp >> 23) & 1
    i2 = (disp >> 22) & 1
    imm10 = (disp >> 12) & 0x3FF
    imm11 = (disp >> 1) & 0x7FF
    j1 = (~i1 ^ s) & 1
    j2 = (~i2 ^ s) & 1
    hw1 = 0xF000 | (s << 10) | imm10
    hw2 = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def _make_synthetic_oril_fixture() -> bytes:
    """Construct a minimal self-consistent ORIL v4 binary for CI contract extraction tests."""
    buf = bytearray(0x4000)
    buf[0:4] = struct.pack("<I", 0xEA00000A)
    buf[4:8] = b"ORIL"
    struct.pack_into("<II", buf, 8, 4, ORIL_BASE + 0x44)

    # SyncSeq_Setup @ +0x5F0 with PIC ldr r6,[pc,#0x24] and literal pool (1000.0, 0x00FFFFFC)
    ss_off = 0x5F0
    struct.pack_into("<2H", buf, ss_off, 0xE92D, 0x4FF1)
    # At +0x5FC (pc_aligned = ORIL_BASE + 0x600): ldr r6, [pc, #0x24] -> lit @ +0x624
    struct.pack_into("<H", buf, ss_off + 0x0C, 0x4E09)
    # At +0x60E (pc = ORIL_BASE + 0x612): add r6, pc (0x447E)
    struct.pack_into("<H", buf, ss_off + 0x1E, 0x447E)
    # Literal pool @ +0x618: 1000.0, 0x00FFFFFC, rel_off -> ORIL_BASE + 0x3800
    rel_clk = (ORIL_BASE + 0x3800) - (ORIL_BASE + 0x612)
    struct.pack_into("<IIII", buf, 0x618, 0x00000000, 0x408F4000, 0x00FFFFFC, rel_clk & 0xFFFFFFFF)

    # Leaf targets
    leo_rst_va = ORIL_BASE + 0x900
    dfetop_set_va = ORIL_BASE + 0x920
    dfetop_clr_va = ORIL_BASE + 0x940
    drv_start_va = ORIL_BASE + 0x960
    dump_diag_va = ORIL_BASE + 0x9A0
    syncseq_m0_va = ORIL_BASE + 0x9C0
    base_noop_va = ORIL_BASE + 0x9E0
    for off in (0x900, 0x920, 0x940, 0x960, 0x9A0, 0x9C0, 0x9E0):
        struct.pack_into("<H", buf, off, 0x4770)  # bx lr

    # DrvSgc method block @ +0x1000
    enable_off = 0x1000
    enable_va = ORIL_BASE + enable_off
    buf[enable_off : enable_off + 4] = _encode_thumb32_branch(enable_va, leo_rst_va, link=True)
    buf[enable_off + 4 : enable_off + 8] = _encode_thumb32_branch(enable_va + 4, leo_rst_va, link=True)
    buf[enable_off + 8 : enable_off + 12] = _encode_thumb32_branch(enable_va + 8, dfetop_set_va, link=True)

    is_cfg_va = ORIL_BASE + 0x1040
    is_run_va = ORIL_BASE + 0x1060
    struct.pack_into("<H", buf, 0x1040, 0x4770)
    struct.pack_into("<H", buf, 0x1060, 0x4770)

    disable_off = 0x1080
    disable_va = ORIL_BASE + disable_off
    buf[disable_off : disable_off + 4] = _encode_thumb32_branch(disable_va, dfetop_clr_va, link=True)

    sos_off = 0x1100
    sos_va = ORIL_BASE + sos_off
    struct.pack_into("<2H", buf, sos_off, 0x2101, 0x2200)
    buf[sos_off + 4 : sos_off + 8] = _encode_thumb32_branch(sos_va + 4, drv_start_va, link=False)

    cfg_off = 0x1150
    struct.pack_into("<4H", buf, cfg_off, 0xE92D, 0x41F0, 0x4604, 0x9F06)

    tim_off = 0x11DA
    struct.pack_into("<2H", buf, tim_off, 0xE92D, 0x47F0)
    struct.pack_into("<2H", buf, tim_off + 0x20, 0xF1A4, 0x0165)

    nlr_off = 0x126A
    struct.pack_into("<3H", buf, nlr_off, 0xB570, 0x460C, 0x1D05)

    # Literal anchors for SLVS-EC and SYSV
    struct.pack_into("<II", buf, 0x1400, 0xFC0F00B0, 0xF3402100)

    # RTTI strings, type_info, and vtables in +0x2000..+0x2800
    cursor = 0x2000

    def _emit_class(rtti: bytes, methods: list[int]) -> int:
        nonlocal cursor
        s_off = cursor
        buf[s_off : s_off + len(rtti)] = rtti
        cursor = (cursor + len(rtti) + 3) & ~3
        ti_off = cursor
        struct.pack_into("<II", buf, ti_off, ORIL_BASE + 0x3000, ORIL_BASE + s_off)
        cursor += 8
        vt_hdr_off = cursor
        struct.pack_into("<iI", buf, vt_hdr_off, 0, ORIL_BASE + ti_off)
        vt_off = vt_hdr_off + 8
        for idx, fn in enumerate(methods):
            struct.pack_into("<I", buf, vt_off + idx * 4, fn | 1)
        cursor = vt_off + len(methods) * 4
        return ORIL_BASE + vt_off

    _emit_class(
        b"N6Camera7DFECore14DfeCoreSgcBaseE\x00",
        [base_noop_va] * 8,
    )
    _emit_class(
        b"N6Camera7DFECore6DrvSgcE\x00",
        [
            enable_va,
            disable_va,
            sos_va,
            base_noop_va,
            base_noop_va,
            is_cfg_va,
            is_run_va,
            dump_diag_va,
        ],
    )
    _emit_class(
        b"N6Camera7DFECore5RdcW0E\x00",
        [base_noop_va] * 8,
    )
    _emit_class(
        b"N6Camera7Imaging13SyncSequencerE\x00",
        [syncseq_m0_va],
    )
    return bytes(buf)


class TestRegisterCatalogsAndFixtures(unittest.TestCase):
    def test_register_catalogs_and_zve10_provenance(self) -> None:
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A1120].mode1_or_reset, 0x026A0793)
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A1124].mode1_or_reset, 0x00000206)
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A11A8].mode1_or_reset, 0x026B0001)
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A11AC].mode1_or_reset, 0x00110061)
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A1110].fields[1].extract(0x00010001), 1)
        self.assertEqual(DRV_SGC0_REGISTERS[0xFC0A1110].fields[1].extract(0x00000001), 0)
        for lut_addr in range(0xFC0A1B80, 0xFC0A1BE4, 4):
            self.assertIn(lut_addr, DRV_SGC0_REGISTERS)

        self.assertEqual(SYNCSEQ_REGISTERS[0xFC080030].mode1_or_reset, COLD_SYNCSEQ_INTERVAL)
        self.assertEqual(SYNCSEQ_REGISTERS[0xFC080038].mode1_or_reset, MODE1_FRAME_TICKS)
        self.assertEqual(SYNCSEQ_REGISTERS[0xFC0800F4].mode1_or_reset, MODE1_EXT_TRIG_TICKS)
        self.assertIn(0xFC0A0000, DFETOP_REGISTERS)
        self.assertIn(0xFC0A0004, DFETOP_REGISTERS)
        self.assertIn(0xFC0A0108, DFETOP_REGISTERS)
        self.assertIn(0xF3402010, SYSV_REGISTERS)
        self.assertIn(0xF3402100, SYSV_REGISTERS)

        self.assertEqual(ZVE10_CORRESPONDENCE.commit, "9b5debdd5f33f18166fb347a5245ac0c3bdeb3d7")
        self.assertEqual(ZVE10_CORRESPONDENCE.drv_sgc0_start, 0x637A4278)
        self.assertEqual(ZVE10_CORRESPONDENCE.syncseq_setup, 0x635C8628)

    def test_synthetic_oril_extraction_and_error_guards(self) -> None:
        fixture = _make_synthetic_oril_fixture()
        contracts = extract_sgc0_syncseq_contracts(fixture)
        self.assertEqual(contracts.oril_version, 4)
        self.assertEqual(contracts.syncseq_setup, ORIL_BASE + 0x5F0)
        self.assertEqual(contracts.syncseq_clk_mhz_va, ORIL_BASE + 0x3800)
        self.assertEqual(contracts.drv_sgc_methods.enable, ORIL_BASE + 0x1000)
        self.assertEqual(contracts.drv_sgc_methods.disable, ORIL_BASE + 0x1080)
        self.assertEqual(contracts.drv_sgc_methods.start_one_shot, ORIL_BASE + 0x1100)
        self.assertEqual(contracts.drv_sgc_methods.drv_sgc0_start, ORIL_BASE + 0x960)
        self.assertEqual(contracts.drv_sgc_methods.configure_outputs, ORIL_BASE + 0x1150)
        self.assertEqual(contracts.drv_sgc_methods.set_timing, ORIL_BASE + 0x11DA)
        self.assertEqual(contracts.drv_sgc_methods.set_drv_sgc0_nlr, ORIL_BASE + 0x126A)
        self.assertEqual(contracts.leo_set_reset_bit, ORIL_BASE + 0x900)
        self.assertEqual(contracts.dfetop_set_core_bit, ORIL_BASE + 0x920)
        self.assertEqual(contracts.dfetop_clear_core_bit, ORIL_BASE + 0x940)
        self.assertEqual(contracts.slvsec_lock_literal_va, ORIL_BASE + 0x1400)
        self.assertEqual(contracts.sysv_master_cfg_literal_va, ORIL_BASE + 0x1404)

        with self.assertRaises(Sgc0SyncSeqError):
            extract_sgc0_syncseq_contracts(b"too_short")
        bad_ver = bytearray(fixture)
        struct.pack_into("<I", bad_ver, 8, 3)
        with self.assertRaises(Sgc0SyncSeqError):
            extract_sgc0_syncseq_contracts(bytes(bad_ver))


class TestDrvSgc0SyncSeqModel(unittest.TestCase):
    def _configure_mode1(self, model: DrvSgc0SyncSeqModel, *, rpt: int = 1, trig_mode: int = 1) -> None:
        # Start DFE coprocessor mailbox
        model.write32(0xFC0300FC, 1)
        self.assertEqual(model.read32(0xFC0300F4), 0xFF)
        self.assertEqual(model.read32(0xFC098808), 0x003C0006)
        self.assertEqual(model.read32(0xFC098908), 0x00020000)

        # Six software-triggered P2S bursts (mode=2) wake IMX284 with SLVS-EC sync-reset 0x80
        for _ in range(6):
            model.queue_p2s_frame(make_imx284_mode1_wake_burst(), run_mode=2)
        self.assertEqual(model.completed_p2s_bursts, 6)
        self.assertFalse(model.p2s_busy)

        # SyncSeq Mode-1 setup
        model.write32(0xFC080028, 0x00FFFFFC)
        model.write32(0xFC080038, MODE1_FRAME_TICKS)
        model.write32(0xFC0800F4, MODE1_EXT_TRIG_TICKS)

        # DrvSGC0 Enable + ConfigureOutputs + SetTiming + Vd0Mask(0)
        model.write32(LEO_CRG_RESET, 0x0005B783)  # clear BIT(6)
        model.write32(0xFC0A0000, 1 << 8)  # DfeTop W1S BIT(8)
        model.write32(0xFC0A1100, 1)
        model.write32(0xFC0A1140, 0x00040007)
        model.write32(0xFC0A114C, 4)
        model.write32(0xFC0A1150, 0x00250005)
        model.write32(0xFC0A110C, 1)
        model.write32(0xFC0A1198, 0x00010000)
        model.write32(0xFC0A1194, 0x00010000)
        model.write32(0xFC0A113C, 0)
        model.write32(0xFC0A1120, 0x026A0793)
        model.write32(0xFC0A11A8, 0x026B0001)
        model.write32(0xFC0A11AC, 0x00110061)
        model.write32(0xFC0A1124, 0x00000206)
        self.assertEqual(model.programmed_sgc0_frame_ticks(), MODE1_FRAME_TICKS)

        # Queue P2S burst #7 in frame-sync mode (run_mode=0, 0x00B3 = 0x90)
        model.queue_p2s_frame(make_imx284_mode1_stream_burst(), run_mode=0)

        # DrvSgc0Start(trig_mode, rpt)
        model.write32(0xFC0A112C, rpt)
        if trig_mode == 0:
            model.write32(0xFC0A1108, 1)
        elif trig_mode == 1:
            model.write32(0xFC0A1130, 0)
            model.write32(0xFC0A1108, 0)
        elif trig_mode == 2:
            model.write32(0xFC0A1130, 0x00010000)
            model.write32(0xFC0A1108, 0)
        model.write32(0xFC0A1110, 1)

    def test_cold_senser_stall_and_sysv_active_lock(self) -> None:
        model = DrvSgc0SyncSeqModel(sysv_cold=True)
        self._configure_mode1(model, rpt=1, trig_mode=1)

        # With SYSV off, advancing 72 MHz time leaves SGC0/SyncSeq/P2S#7/SLVS-EC stalled
        self.assertFalse(model.sysv_tick())
        model.advance_ticks(MODE1_FRAME_TICKS * 2)
        self.assertEqual(model.read32(0xFC0A1110), 0x00000001)
        self.assertEqual(model.latch_hvcnt(), COLD_HVCNT_IDLE)
        self.assertEqual(model.latch_fcnt(), COLD_FCNT_IDLE)
        self.assertEqual(model.read32(0xFC080030), COLD_SYNCSEQ_INTERVAL)
        self.assertTrue(model.p2s_busy)
        self.assertEqual(model.read32(0xFC088F08), 1)
        self.assertEqual(model.completed_p2s_bursts, 6)
        self.assertEqual(model.read32(0xFC0F00B0), 0)

        # Starting SYSV unlocks SyncSeq, DrvSGC0, P2S burst #7 (0x00B3=0x90), and SLVS-EC
        model.enable_sysv()
        self.assertTrue(model.sysv_tick())
        self.assertEqual(model.read32(0xFC0A1110), 0x00010001)
        self.assertEqual(model.read32(0xFC080030), MODE1_FRAME_TICKS)
        self.assertFalse(model.p2s_busy)
        self.assertEqual(model.completed_p2s_bursts, 7)
        self.assertEqual(model.sensor.read_u8(0x00B3), 0x90)
        self.assertEqual(model.read32(0xFC0F00B0), 1)
        self.assertEqual(model.latch_fcnt(), 1)

        # Partial-frame tick advances HVcnt off idle sentinel; second SYSV frame increments Fcnt
        model.advance_ticks(1940 * 10 + 25)
        hvcnt = model.latch_hvcnt()
        self.assertNotEqual(hvcnt, COLD_HVCNT_IDLE)
        self.assertEqual(hvcnt, (10 << 16) | 25)
        model.sysv_tick()
        self.assertEqual(model.latch_fcnt(), 2)

        # Masking VD0 drops SLVS-EC lock; unmasking restores it
        model.write32(0xFC0A113C, 1)
        self.assertEqual(model.read32(0xFC0F00B0), 0)
        model.write32(0xFC0A113C, 0)
        self.assertEqual(model.read32(0xFC0F00B0), 1)

        # Reset via DfeTop W1C BIT(8) clears DrvSGC0 active/request and drops SLVS-EC
        model.write32(0xFC0A0004, 1 << 8)
        self.assertEqual(model.read32(0xFC0A1110), 0x00000000)
        self.assertEqual(model.latch_hvcnt(), COLD_HVCNT_IDLE)
        self.assertEqual(model.read32(0xFC0F00B0), 0)

    def test_one_shot_rpt0_and_leo_reset_and_alt_trig_modes(self) -> None:
        # One-shot rpt=0 stops after exactly 1 frame
        model = DrvSgc0SyncSeqModel(sysv_cold=False)
        self._configure_mode1(model, rpt=0, trig_mode=1)
        self.assertTrue(model.sysv_tick())
        self.assertEqual(model.latch_fcnt(), 1)
        self.assertEqual(model.read32(0xFC0A1110), 0x00000000)
        self.assertEqual(model.read32(0xFC0F00B0), 0)

        # trig_mode=0 starts immediately without SYSV; Leo reset BIT(6)=1 resets DrvSGC0
        model.reset()
        model.sysv_cold = True
        model.reset()
        self._configure_mode1(model, rpt=1, trig_mode=0)
        self.assertEqual(model.read32(0xFC0A1110), 0x00010001)
        model.write32(LEO_CRG_RESET, 0x0005B7C3)  # set BIT(6)
        self.assertEqual(model.read32(0xFC0A1110), 0x00000000)

        # trig_mode=2 waits specifically for EXT_SYNC, ignoring SYSV VD0
        model.reset()
        self._configure_mode1(model, rpt=1, trig_mode=2)
        model.enable_sysv()
        model.sysv_tick()
        self.assertEqual(model.read32(0xFC0A1110), 0x00000001)
        model.pulse_ext_sync()
        self.assertEqual(model.read32(0xFC0A1110), 0x00010001)


class TestFirmwareExtractionAndUnicornReplay(unittest.TestCase):
    @unittest.skipUnless(A6400_AVCAM.is_file(), "ILCE-6400 av-cam.bin not present")
    def test_ilce6400_contracts_and_unicorn_replays(self) -> None:
        contracts = extract_sgc0_syncseq_contracts(A6400_AVCAM)
        self.assertEqual(contracts.avcam_sha256, ILCE6400_AVCAM_SHA256)
        self.assertEqual(contracts.dfe_dat_sha256, DFE_DAT_SHA256)
        self.assertEqual(contracts.dfe_app_sha256, DFE_APP_SHA256)
        self.assertEqual(contracts.dfe_core_sgc_base_vtable, 0x644FAC08)
        self.assertEqual(contracts.drv_sgc_vtable, 0x644FAC60)
        self.assertEqual(contracts.sync_sequencer_vtable, 0x644FC7C8)
        self.assertEqual(contracts.rdc_w0_vtable, 0x644FB0D8)

        m = contracts.drv_sgc_methods
        self.assertEqual(m.enable, 0x63CD71E8)
        self.assertEqual(m.disable, 0x63CD727A)
        self.assertEqual(m.start_one_shot, 0x63CD7436)
        self.assertEqual(m.is_config_enabled, 0x63CD7222)
        self.assertEqual(m.is_running, 0x63CD7236)
        self.assertEqual(m.dump_diagnostics, 0x63780910)
        self.assertEqual(m.configure_outputs, 0x63CD7486)
        self.assertEqual(m.set_timing, 0x63CD7510)
        self.assertEqual(m.drv_sgc0_start, 0x63780864)
        self.assertEqual(m.set_drv_sgc0_nlr, 0x63CD75A0)
        self.assertEqual(contracts.syncseq_setup, 0x635C85F0)

        avcam_bytes = A6400_AVCAM.read_bytes()
        mode1 = unicorn_replay_drv_sgc0_mode1(avcam_bytes, contracts=contracts, trig_mode=1, rpt=1)
        self.assertLessEqual(mode1.insns_executed, 2000)
        self.assertEqual(mode1.final_regs[0xFC021024], 0x0005B783)
        self.assertEqual(mode1.final_regs[0xFC0A0000], 0x00000100)
        self.assertEqual(mode1.final_regs[0xFC0A1100], 0x00000001)
        self.assertEqual(mode1.final_regs[0xFC0A1140], 0x00040007)
        self.assertEqual(mode1.final_regs[0xFC0A114C], 0x00000004)
        self.assertEqual(mode1.final_regs[0xFC0A1150], 0x00250005)
        self.assertEqual(mode1.final_regs[0xFC0A110C], 0x00000001)
        self.assertEqual(mode1.final_regs[0xFC0A1198], 0x00010000)
        self.assertEqual(mode1.final_regs[0xFC0A1194], 0x00010000)
        self.assertEqual(mode1.final_regs[0xFC0A1120], 0x026A0793)
        self.assertEqual(mode1.final_regs[0xFC0A11A8], 0x026B0001)
        self.assertEqual(mode1.final_regs[0xFC0A11AC], 0x00110061)
        self.assertEqual(mode1.final_regs[0xFC0A1124], 0x00000206)
        self.assertEqual(mode1.final_regs[0xFC0A112C], 0x00000001)
        self.assertEqual(mode1.final_regs[0xFC0A1110], 0x00000001)

        starts = unicorn_replay_drv_sgc0_start_modes(avcam_bytes, contracts=contracts, rpt=0)
        self.assertEqual(starts[0].final_regs[0xFC0A1108], 1)
        self.assertNotIn(0xFC0A1130, starts[0].final_regs)
        self.assertEqual(starts[0].final_regs[0xFC0A1110], 1)
        self.assertEqual(starts[1].final_regs[0xFC0A1130], 0)
        self.assertEqual(starts[1].final_regs[0xFC0A1108], 0)
        self.assertEqual(starts[1].final_regs[0xFC0A1110], 1)
        self.assertEqual(starts[2].final_regs[0xFC0A1130], 0x00010000)
        self.assertEqual(starts[2].final_regs[0xFC0A1108], 0)
        self.assertEqual(starts[2].final_regs[0xFC0A1110], 1)

        syncseq = unicorn_replay_syncseq_setup(avcam_bytes, contracts=contracts)
        self.assertLessEqual(syncseq.insns_executed, 2000)
        self.assertEqual(syncseq.final_regs[0xFC080028], 0x00FFFFFC)
        self.assertEqual(syncseq.final_regs[0xFC080038], MODE1_FRAME_TICKS)
        self.assertEqual(syncseq.final_regs[0xFC0800F4], MODE1_EXT_TRIG_TICKS)
        self.assertEqual(syncseq.final_regs[0xFC08006C], 0x10)
        self.assertEqual(syncseq.final_regs[0xFC080084], 0x10)
        self.assertEqual(syncseq.final_regs[0xFC08009C], 0x10)

        # Feed real Unicorn MMIO traces directly into DrvSgc0SyncSeqModel
        model = DrvSgc0SyncSeqModel(sysv_cold=True)
        model.queue_p2s_frame(make_imx284_mode1_wake_burst(), run_mode=2)
        model.apply_replay(syncseq)
        model.apply_replay(mode1)
        model.queue_p2s_frame(make_imx284_mode1_stream_burst(), run_mode=0)
        self.assertEqual(model.read32(0xFC0A1110), 0x00000001)
        self.assertEqual(model.read32(0xFC0F00B0), 0)
        model.enable_sysv()
        model.sysv_tick()
        self.assertEqual(model.read32(0xFC0A1110), 0x00010001)
        self.assertEqual(model.read32(0xFC0F00B0), 1)

        # Verify SetDrvSgc0Nlr programming and strict instruction cap guard
        nlr_replay = unicorn_replay_drv_sgc0_mode1(
            avcam_bytes,
            contracts=contracts,
            nlr_header=(1, 0x0010, 0x0200, 0x0040, 0x0100, 0x0080, 0x0008, 1, 2, 3),
        )
        self.assertEqual(nlr_replay.final_regs[0xFC0A1180], 1)
        self.assertEqual(nlr_replay.final_regs[0xFC0A1174], 0x02000010)
        self.assertEqual(nlr_replay.final_regs[0xFC0A1B80], 0x00000040)
        self.assertEqual(nlr_replay.final_regs[0xFC0A1B84], 0x01000080)
        self.assertEqual(nlr_replay.final_regs[0xFC0A1B88], 0x00000008)
        self.assertEqual(nlr_replay.final_regs[0xFC0A1B8C], 0x00120003)
        with self.assertRaises(Sgc0SyncSeqError):
            unicorn_replay_drv_sgc0_mode1(avcam_bytes, contracts=contracts, max_insns=5)

    @unittest.skipUnless(A6400A_AVCAM.is_file(), "ILCE-6400A av-cam.bin not present")
    def test_ilce6400a_contracts_and_cross_body_mmio_parity(self) -> None:
        contracts_a = extract_sgc0_syncseq_contracts(A6400A_AVCAM)
        self.assertEqual(contracts_a.avcam_sha256, ILCE6400A_AVCAM_SHA256)
        self.assertEqual(contracts_a.dfe_dat_sha256, DFE_DAT_SHA256)
        self.assertEqual(contracts_a.dfe_app_sha256, DFE_APP_SHA256)
        self.assertEqual(contracts_a.dfe_core_sgc_base_vtable, 0x6451D758)
        self.assertEqual(contracts_a.drv_sgc_vtable, 0x6451DC30)
        self.assertEqual(contracts_a.sync_sequencer_vtable, 0x6451F308)
        self.assertEqual(contracts_a.rdc_w0_vtable, 0x6451DB28)

        m = contracts_a.drv_sgc_methods
        self.assertEqual(m.enable, 0x63CEE744)
        self.assertEqual(m.disable, 0x63CEE7D6)
        self.assertEqual(m.start_one_shot, 0x63CEE992)
        self.assertEqual(m.is_config_enabled, 0x63CEE77E)
        self.assertEqual(m.is_running, 0x63CEE792)
        self.assertEqual(m.dump_diagnostics, 0x63797D54)
        self.assertEqual(m.configure_outputs, 0x63CEE9E2)
        self.assertEqual(m.set_timing, 0x63CEEA6C)
        self.assertEqual(m.drv_sgc0_start, 0x63797CA8)
        self.assertEqual(m.set_drv_sgc0_nlr, 0x63CEEAFC)
        self.assertEqual(contracts_a.syncseq_setup, 0x635C85F0)

        avcam_a_bytes = A6400A_AVCAM.read_bytes()
        mode1_a = unicorn_replay_drv_sgc0_mode1(avcam_a_bytes, contracts=contracts_a)
        starts_a = unicorn_replay_drv_sgc0_start_modes(avcam_a_bytes, contracts=contracts_a)
        syncseq_a = unicorn_replay_syncseq_setup(avcam_a_bytes, contracts=contracts_a)

        self.assertEqual(mode1_a.final_regs[0xFC0A1120], 0x026A0793)
        self.assertEqual(mode1_a.final_regs[0xFC0A11A8], 0x026B0001)
        self.assertEqual(mode1_a.final_regs[0xFC0A11AC], 0x00110061)
        self.assertEqual(syncseq_a.final_regs[0xFC080038], MODE1_FRAME_TICKS)
        self.assertEqual(syncseq_a.final_regs[0xFC0800F4], MODE1_EXT_TRIG_TICKS)

        if A6400_AVCAM.is_file():
            avcam_bytes = A6400_AVCAM.read_bytes()
            mode1_base = unicorn_replay_drv_sgc0_mode1(avcam_bytes)
            starts_base = unicorn_replay_drv_sgc0_start_modes(avcam_bytes)
            syncseq_base = unicorn_replay_syncseq_setup(avcam_bytes)
            self.assertEqual(
                [(w.addr, w.value) for w in mode1_a.writes],
                [(w.addr, w.value) for w in mode1_base.writes],
            )
            self.assertEqual(
                [(w.addr, w.value) for w in syncseq_a.writes],
                [(w.addr, w.value) for w in syncseq_base.writes],
            )
            for tmode in (0, 1, 2):
                self.assertEqual(
                    [(w.addr, w.value) for w in starts_a[tmode].writes],
                    [(w.addr, w.value) for w in starts_base[tmode].writes],
                )


if __name__ == "__main__":
    unittest.main()
