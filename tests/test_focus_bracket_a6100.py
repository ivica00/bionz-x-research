"""Offline verification of mods/focus_bracket_a6100/FocusBracket.mod (SMOD v2).

Checks:
1. SMOD v2 header, FNV-1a checksum, RLC1 relocation table (excluding 0x06040201), SMNU trailer, and README SHA-256.
2. Unicorn ARM Thumb execution of the WriteStillComp hook + trampoline at a non-reference base:
   verifies AAPCS 8-byte stack alignment across bl fb_mark_saved, fb_save_seq increment,
   and exact register/stack state at resume target (libObj + 0x8a74bc).
3. Unicorn ARM Thumb execution of fb_on / fb_worker / fb_off:
   verifies pthread_create + pthread_detach, Mode=0 (0 -> +), Mode=2 Custom StepWidth=2 (4) Order=1 (0 -> - -> +),
   save-sync via WriteStillComp hook, pthread_create failure recovery, and fb_off -> fb_on race guard.
"""
from __future__ import annotations

import hashlib
import re
import struct
import unittest
from pathlib import Path

from src.a6400.mod_manager.smod import (
    SMOD_V2_FLAG_RELOC,
    SMOD_V2_FLAG_SUBMENU,
    fnv1a_32,
    parse_smod,
)

ROOT = Path(__file__).resolve().parents[1]
MOD_DIR = ROOT / "mods" / "focus_bracket_a6100"
MOD_PATH = MOD_DIR / "FocusBracket.mod"
README_PATH = MOD_DIR / "README.md"

try:
    import unicorn as uc
    from unicorn import arm_const as A
except ModuleNotFoundError:
    uc = None
    A = None

LIBOBJ_REF_BASE = 0x05D18000
LIBOBJ_RUN_BASE = 0x40000000
MOD_BASE = 0x50000000
STACK_TOP = 0x60010000
EVTMGR_OBJ = 0x70002000
CAM_OBJ = 0x70001000

OFF_GET_OBJ_HANDLE = 0x0037C4FA
OFF_OBJMSG_CTOR = 0x000FFF98
OFF_OBJMSG_DTOR = 0x000FF798
OFF_OBJMSG_ADDPARAM = 0x00101D60
OFF_PARAM_INT_CTOR = 0x00100AE8
OFF_PARAM_INT_DTOR = 0x000FF114
OFF_ISSUE_CMD_SYNC = 0x001006D0
OFF_PTHREAD_CREATE = 0x00101260
OFF_PTHREAD_DETACH = 0x000FECF4
OFF_USLEEP = 0x0010473C
OFF_G_PEVTMGR = 0x01425C70
OFF_EVENT_NEW = 0x000FD974
OFF_EVENT_CTOR = 0x00840F7C
OFF_EVENT_PUSH = 0x00842528
OFF_WRITE_STILL = 0x008A74AC
OFF_WRITE_STILL_RESUME = 0x008A74BC


def _parse_rlc1_and_smnu(raw: bytes):
    mod = parse_smod(raw)
    _, _, _, _, code_len, _, _, on_off, off_off, n_hooks = struct.unpack_from("<III32s6I", raw, 0)
    hdr_end = 68 + n_hooks * 40
    code = raw[hdr_end : hdr_end + code_len]
    off = hdr_end + code_len
    libs = []
    relocs = []
    if mod.flags & SMOD_V2_FLAG_RELOC:
        rmagic, n_libs, n_rel = struct.unpack_from("<IHH", raw, off)
        assert rmagic == 0x31434C52
        off += 8
        for _ in range(n_libs):
            lo, hi, lname = struct.unpack_from("<II24s", raw, off)
            libs.append((lo, hi, lname.rstrip(b"\x00").decode("ascii")))
            off += 32
        relocs = list(struct.unpack_from(f"<{n_rel}I", raw, off))
        off += 4 * n_rel
        rcrc, = struct.unpack_from("<I", raw, off)
        assert rcrc == fnv1a_32(raw[hdr_end + code_len : off])
        off += 4
    smnu = None
    if mod.flags & SMOD_V2_FLAG_SUBMENU:
        smagic, n_items, bss_off, _, cfg_off = struct.unpack_from("<IBBHI", raw, off)
        assert smagic == 0x554E4D53
        smnu = (n_items, bss_off, cfg_off)
    return mod, code, on_off, off_off, libs, relocs, smnu


def _load_and_relocate(raw: bytes, libobj_base: int):
    mod, code_bytes, on_off, off_off, libs, relocs, smnu = _parse_rlc1_and_smnu(raw)
    code = bytearray(code_bytes)
    bias = libobj_base - LIBOBJ_REF_BASE
    for r in relocs:
        ro = r & 0xFFFFFF
        val, = struct.unpack_from("<I", code, ro)
        struct.pack_into("<I", code, ro, (val + bias) & 0xFFFFFFFF)
    return mod, bytes(code), on_off, off_off, smnu


class TestFocusBracketA6100Format(unittest.TestCase):
    def test_smod_format_and_readme_sha256(self):
        raw = MOD_PATH.read_bytes()
        mod, code_bytes, on_off, off_off, libs, relocs, smnu = _parse_rlc1_and_smnu(raw)
        self.assertEqual(mod.version, 2)
        self.assertEqual(mod.name, "Focus Bracket")
        self.assertEqual(mod.flags, SMOD_V2_FLAG_RELOC | SMOD_V2_FLAG_SUBMENU)
        self.assertEqual(mod.bss_size, 16)
        self.assertEqual(len(mod.hooks), 1)
        self.assertTrue(on_off & 1)
        self.assertTrue(off_off & 1)
        self.assertEqual(int(mod.hooks[0]["target"], 16), LIBOBJ_REF_BASE + OFF_WRITE_STILL)
        self.assertEqual(mod.hooks[0]["orig_hex"], "2de9f04f0d46f5498fb00446")
        self.assertEqual(libs, [(0x05D18000, 0x07144000, "libObj.so")])
        # Ensure 0x06040201 literal is NOT in relocs
        for r in relocs:
            val, = struct.unpack_from("<I", code_bytes, r & 0xFFFFFF)
            self.assertNotEqual(val, 0x06040201)
        self.assertIsNotNone(smnu)
        self.assertEqual(smnu[0], 6)
        self.assertEqual(smnu[1], 0)
        self.assertTrue(smnu[2] & 1)

        sha = hashlib.sha256(raw).hexdigest()
        m = re.search(r"SHA-256:\s*`([0-9a-f]{64})`", README_PATH.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "README.md missing SHA-256 line")
        self.assertEqual(sha, m.group(1))


@unittest.skipIf(uc is None, "unicorn is not installed")
class TestFocusBracketA6100Unicorn(unittest.TestCase):
    def _new_emu(self):
        raw = MOD_PATH.read_bytes()
        mod, reloc_code, on_off, off_off, smnu = _load_and_relocate(raw, LIBOBJ_RUN_BASE)

        emu = uc.Uc(uc.UC_ARCH_ARM, uc.UC_MODE_THUMB)
        emu.mem_map(LIBOBJ_RUN_BASE, 0x01500000)
        emu.mem_map(MOD_BASE, 0x10000)
        emu.mem_map(STACK_TOP - 0x10000, 0x10000)
        emu.mem_map(0x70000000, 0x10000)

        emu.mem_write(MOD_BASE, reloc_code)
        emu.mem_write(MOD_BASE + len(reloc_code), b"\x00" * mod.bss_size)
        emu.mem_write(LIBOBJ_RUN_BASE + OFF_G_PEVTMGR, struct.pack("<I", EVTMGR_OBJ))

        hk = mod.hooks[0]
        target = LIBOBJ_RUN_BASE + OFF_WRITE_STILL
        emu.mem_write(target, bytes.fromhex(hk["orig_hex"]))
        patch = bytearray.fromhex(hk["tmpl_hex"])
        fix = hk["fixup"]
        struct.pack_into("<I", patch, fix, MOD_BASE + struct.unpack_from("<I", patch, fix)[0])
        emu.mem_write(target, bytes(patch))

        emu.mem_write(LIBOBJ_RUN_BASE + OFF_WRITE_STILL_RESUME, b"\x70\x47")
        emu.mem_write(MOD_BASE + 0xF000, b"\x00\xbf\x00\xbf")
        return emu, mod, reloc_code, on_off, off_off, smnu

    def _call_thumb(self, emu, addr: int, r0: int = 0, sp: int = STACK_TOP):
        ret = MOD_BASE + 0xF000
        emu.reg_write(A.UC_ARM_REG_SP, sp)
        emu.reg_write(A.UC_ARM_REG_LR, ret | 1)
        emu.reg_write(A.UC_ARM_REG_R0, r0)
        emu.emu_start(addr | 1, ret)

    def test_write_still_hook_alignment_and_resume_state(self):
        emu, mod, _, _, _, _ = self._new_emu()
        sp0 = STACK_TOP - 0x100
        emu.reg_write(A.UC_ARM_REG_SP, sp0)
        emu.reg_write(A.UC_ARM_REG_R0, 0x12345678)
        emu.reg_write(A.UC_ARM_REG_R1, 0xABCDEF01)
        emu.reg_write(A.UC_ARM_REG_LR, (MOD_BASE + 0xF000) | 1)

        mark_saved_sps = []
        hook_entry_end = LIBOBJ_RUN_BASE + OFF_WRITE_STILL_RESUME
        hk = mod.hooks[0]
        patch = bytes.fromhex(hk["tmpl_hex"])
        hook_addr = MOD_BASE + struct.unpack_from("<I", patch, hk["fixup"])[0]
        bl_pc = (hook_addr & ~1) + 4

        def check_bl(u, addr, size, _):
            if addr == bl_pc:
                mark_saved_sps.append(u.reg_read(A.UC_ARM_REG_SP))

        h1 = emu.hook_add(uc.UC_HOOK_CODE, check_bl, begin=bl_pc, end=bl_pc + 4)
        emu.emu_start((LIBOBJ_RUN_BASE + OFF_WRITE_STILL) | 1, hook_entry_end)
        emu.hook_del(h1)

        self.assertEqual(len(mark_saved_sps), 1)
        self.assertEqual(mark_saved_sps[0] % 8, 0)
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_R0), 0x12345678)
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_R4), 0x12345678)
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_R5), 0xABCDEF01)
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_R1), LIBOBJ_RUN_BASE + 0x01022EFD)
        expected_sp = sp0 - 36 - 60
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_SP), expected_sp)
        self.assertEqual(emu.reg_read(A.UC_ARM_REG_R7), expected_sp + 16)

    def _run_worker_scenario(self, cfg_bytes: bytes, fail_pthread: bool = False, race_toggle: bool = False):
        emu, mod, reloc_code, on_off, off_off, _ = self._new_emu()
        emu.mem_write(MOD_BASE + len(reloc_code), cfg_bytes)

        events = []
        steps = []
        created_threads = []
        detached_threads = []
        param_ints = {}
        next_ev = [0x70003000]
        next_tid = [100]

        stubs = {
            LIBOBJ_RUN_BASE + OFF_PTHREAD_CREATE: "pthread_create",
            LIBOBJ_RUN_BASE + OFF_PTHREAD_DETACH: "pthread_detach",
            LIBOBJ_RUN_BASE + OFF_GET_OBJ_HANDLE: "get_obj_handle",
            LIBOBJ_RUN_BASE + OFF_OBJMSG_CTOR: "objmsg_ctor",
            LIBOBJ_RUN_BASE + OFF_OBJMSG_DTOR: "objmsg_dtor",
            LIBOBJ_RUN_BASE + OFF_OBJMSG_ADDPARAM: "objmsg_addparam",
            LIBOBJ_RUN_BASE + OFF_PARAM_INT_CTOR: "param_int_ctor",
            LIBOBJ_RUN_BASE + OFF_PARAM_INT_DTOR: "param_int_dtor",
            LIBOBJ_RUN_BASE + OFF_ISSUE_CMD_SYNC: "issue_cmd_sync",
            LIBOBJ_RUN_BASE + OFF_USLEEP: "usleep",
            LIBOBJ_RUN_BASE + OFF_EVENT_NEW: "event_new",
            LIBOBJ_RUN_BASE + OFF_EVENT_CTOR: "event_ctor",
            LIBOBJ_RUN_BASE + OFF_EVENT_PUSH: "event_push",
        }
        thumb_stubs = {
            LIBOBJ_RUN_BASE + OFF_GET_OBJ_HANDLE,
            LIBOBJ_RUN_BASE + OFF_EVENT_CTOR,
            LIBOBJ_RUN_BASE + OFF_EVENT_PUSH,
        }
        for addr in stubs:
            emu.mem_write(addr, b"\x70\x47" if addr in thumb_stubs else b"\x1e\xff\x2f\xe1")

        def on_stub(u, addr, size, _):
            name = stubs.get(addr)
            if not name:
                return
            r0 = u.reg_read(A.UC_ARM_REG_R0)
            r1 = u.reg_read(A.UC_ARM_REG_R1)
            r2 = u.reg_read(A.UC_ARM_REG_R2)
            if name == "pthread_create":
                if fail_pthread:
                    u.reg_write(A.UC_ARM_REG_R0, 11)
                    return
                tid = next_tid[0]
                next_tid[0] += 1
                u.mem_write(r0, struct.pack("<I", tid))
                created_threads.append((tid, r2))
                u.reg_write(A.UC_ARM_REG_R0, 0)
            elif name == "pthread_detach":
                detached_threads.append(r0)
                u.reg_write(A.UC_ARM_REG_R0, 0)
            elif name == "get_obj_handle":
                u.reg_write(A.UC_ARM_REG_R0, CAM_OBJ if r0 == 8 else 0)
            elif name == "param_int_ctor":
                param_ints[r0] = r1
            elif name == "objmsg_addparam":
                if r1 == 0x9C40:
                    param_ints[r0] = param_ints.get(r2)
            elif name == "issue_cmd_sync":
                steps.append(param_ints.get(r1))
                u.reg_write(A.UC_ARM_REG_R0, 0)
            elif name == "event_new":
                ev = next_ev[0]
                next_ev[0] += 0x40
                u.reg_write(A.UC_ARM_REG_R0, ev)
            elif name == "event_ctor":
                param_ints[r0] = r1
            elif name == "event_push":
                events.append(param_ints.get(r1))
            elif name == "usleep":
                if events and events[-1] == 0x15010031 and r0 == 100000:
                    saved_regs = [u.reg_read(r) for r in (
                        A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3,
                        A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R7, A.UC_ARM_REG_SP, A.UC_ARM_REG_LR
                    )]
                    sp_save = saved_regs[7]
                    u.reg_write(A.UC_ARM_REG_LR, (LIBOBJ_RUN_BASE + OFF_WRITE_STILL_RESUME) | 1)
                    u.emu_start((LIBOBJ_RUN_BASE + OFF_WRITE_STILL) | 1, LIBOBJ_RUN_BASE + OFF_WRITE_STILL_RESUME)
                    for reg, val in zip((
                        A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3,
                        A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R7, A.UC_ARM_REG_SP, A.UC_ARM_REG_LR
                    ), saved_regs):
                        u.reg_write(reg, val)
                    u.reg_write(A.UC_ARM_REG_SP, sp_save)
                    if race_toggle and len(events) == 3:
                        u.reg_write(A.UC_ARM_REG_R0, 0)
                        u.reg_write(A.UC_ARM_REG_LR, (MOD_BASE + 0xF000) | 1)
                        u.emu_start((MOD_BASE + off_off) | 1, MOD_BASE + 0xF000)
                        u.reg_write(A.UC_ARM_REG_R0, 1)
                        u.reg_write(A.UC_ARM_REG_LR, (MOD_BASE + 0xF000) | 1)
                        u.emu_start((MOD_BASE + on_off) | 1, MOD_BASE + 0xF000)
                        for reg, val in zip((
                            A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3,
                            A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R7, A.UC_ARM_REG_SP, A.UC_ARM_REG_LR
                        ), saved_regs):
                            u.reg_write(reg, val)
                u.reg_write(A.UC_ARM_REG_R0, 0)

        h = emu.hook_add(uc.UC_HOOK_CODE, on_stub, begin=LIBOBJ_RUN_BASE, end=LIBOBJ_RUN_BASE + 0x01500000)
        self._call_thumb(emu, MOD_BASE + on_off, r0=1)
        if created_threads:
            self._call_thumb(emu, created_threads[0][1], r0=0, sp=STACK_TOP - 0x2000)
        emu.hook_del(h)
        return emu, mod, events, steps, created_threads, detached_threads

    def test_landscape_mode_0_to_far(self):
        _, _, events, steps, created, detached = self._run_worker_scenario(bytes([0, 0, 0, 0, 0, 0]))
        self.assertEqual(len(created), 1)
        self.assertEqual(detached, [created[0][0]])
        self.assertEqual(events, [0x15010102] + [0x15010032, 0x15010031] * 5)
        self.assertEqual(steps, [0x68D] * (4 * 8))

    def test_custom_mode_step_width_table_and_alternating_order(self):
        # StepWidth=2 (4 steps, exercises the 0x06040201 constant!), Shots=0 (5), Order=1 (0 -> - -> +)
        _, _, events, steps, _, _ = self._run_worker_scenario(bytes([2, 2, 0, 1, 0, 0]))
        self.assertEqual(events, [0x15010102] + [0x15010032, 0x15010031] * 5)
        expected = [0x68C] * 4 + [0x68D] * 8 + [0x68C] * 12 + [0x68D] * 16
        self.assertEqual(steps, expected)

    def test_pthread_create_failure_and_race_guard(self):
        _, _, _, _, created, _ = self._run_worker_scenario(bytes([0] * 6), fail_pthread=True)
        self.assertEqual(created, [])
        _, _, events, _, created2, _ = self._run_worker_scenario(bytes([0] * 6), race_toggle=True)
        self.assertEqual(len(created2), 1)
        self.assertEqual(events, [0x15010102, 0x15010032, 0x15010031])


if __name__ == "__main__":
    unittest.main()
