"""Offline Unicorn test of SMOD v2 OpenGateA1.mod (no camera).

Usage: python mods/opengate_a1/test_mod_v2_unicorn.py [BUILD_DIR]
"""
import struct
import sys
from pathlib import Path
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_INTR
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R7, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
    UC_ARM_REG_CPSR,
)

BUILD_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/mods/opengate_a1")
mod = (BUILD_DIR / "OpenGateA1.mod").read_bytes()

magic, ver, flags, name, code_sz, fnv, bss_sz, on_off, off_off, hooks_cnt = struct.unpack_from("<III32s6I", mod, 0)
assert magic == 0x444F4D53 and ver == 2 and hooks_cnt == 2, f"bad header: {hex(magic)} v{ver} h={hooks_cnt}"
code = mod[68 + 40 * hooks_cnt : 68 + 40 * hooks_cnt + code_sz]

# Read ELF symbol offsets for tkhd_hook_tramp, stsd_hook_tramp, ml_orig_tkhd, ml_orig_stsd
from elftools.elf.elffile import ELFFile
syms = {}
with (BUILD_DIR / "oga1.elf").open("rb") as f:
    elf = ELFFile(f)
    symtab = elf.get_section_by_name(".symtab")
    for s in symtab.iter_symbols():
        if s.name:
            syms[s.name] = s["st_value"]

MOD_BASE = 0x10000000
VU_BASE  = 0x155E6000
AV_BASE  = 0x20000000  # mock /dev/mem mmap region
STACK_TOP = 0x30010000
STOP_LR  = 0x70000000

import re

def load_av_patches():
    src = Path("mods/opengate_a1/opengate_a1.c").read_text(encoding="utf-8")
    m = re.search(r"AV_PATCHES\[AV_PATCH_COUNT\]\s*=\s*\{(.*?)\};", src, re.S)
    assert m, "AV_PATCHES not found in opengate_a1.c"
    rows = []
    for pa, st, pt in re.findall(r"\{\s*(0x[0-9A-Fa-f]+)u\s*,\s*(\d+)u\s*,\s*(\d+)u\s*\}", m.group(1)):
        rows.append((int(pa, 16), int(st), int(pt)))
    assert len(rows) == 337, f"expected 337 patches, got {len(rows)}"
    return rows

AV_PATCHES = load_av_patches()
AV_PAGE_MAP = {
    0x03E14: 0x0000,
    0x03E15: 0x1000,
    0x03E16: 0x2000,
    0x03E19: 0x3000,
    0x03E25: 0x4000,
    0x04201: 0x5000,
}
VU_CELLS = [0x77D0F0, 0x77D130, 0x77D210, 0x77D250, 0x77D450, 0x77D490, 0x77D570, 0x77D5B0]

uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
uc.mem_map(MOD_BASE, 0x10000)
uc.mem_write(MOD_BASE, code)
uc.mem_map(VU_BASE + 0x77D000, 0x2000)
for off in VU_CELLS:
    uc.mem_write(VU_BASE + off + 0x0C, struct.pack("<I", 0x404E0000))
    uc.mem_write(VU_BASE + off + 0x1C, struct.pack("<I", 0x40768000))

uc.mem_map(AV_BASE, 0x20000)
for pa, st, _ in AV_PATCHES:
    m_off = AV_PAGE_MAP[pa >> 12] + (pa & 0xFFF)
    uc.mem_write(AV_BASE + m_off, struct.pack("<H", st))

uc.mem_map(0x30000000, 0x20000)  # stack + scratch
uc.mem_map(STOP_LR, 0x1000)
uc.mem_map(0x40000000, 0x2000)  # mock orig_tkhd / orig_stsd stubs

maps_bytes = b"155e6000-15e92000 r-xp 00000000 fc:0f 22532 /usr/lib/viewUnified2.so\n"
open_fds = {}
next_fd = 10
log_buf = bytearray()

def svc_hook(u, intno, user_data):
    global next_fd
    r7 = u.reg_read(UC_ARM_REG_R7)
    r0 = u.reg_read(UC_ARM_REG_R0)
    r1 = u.reg_read(UC_ARM_REG_R1)
    r2 = u.reg_read(UC_ARM_REG_R2)
    r5 = u.reg_read(0x47)  # UC_ARM_REG_R5
    if r7 == 5:  # open
        bs = bytearray()
        for i in range(128):
            c = u.mem_read(r0 + i, 1)[0]
            if c == 0: break
            bs.append(c)
        path = bs.decode("latin1")
        fd = next_fd; next_fd += 1
        open_fds[fd] = [path, 0]
        u.reg_write(UC_ARM_REG_R0, fd)
    elif r7 == 3:  # read
        info = open_fds[r0]
        if info[0] == "/proc/self/maps":
            chunk = maps_bytes[info[1]:info[1] + r2]
            u.mem_write(r1, chunk)
            info[1] += len(chunk)
            u.reg_write(UC_ARM_REG_R0, len(chunk))
        else:
            u.reg_write(UC_ARM_REG_R0, 0)
    elif r7 == 4:  # write
        if r0 in open_fds and "opengate_a1.log" in open_fds[r0][0]:
            log_buf.extend(u.mem_read(r1, r2))
        u.reg_write(UC_ARM_REG_R0, r2)
    elif r7 == 6:  # close
        u.reg_write(UC_ARM_REG_R0, 0)
    elif r7 == 192:  # mmap2 (r5 = pgoffset)
        pgoff = u.reg_read(UC_ARM_REG_R5 if False else 71)  # R5=71? let's use enum
        u.reg_write(UC_ARM_REG_R0, AV_BASE + (0x0000 if pgoff == 0x03E19 else 0x1000))
    elif r7 == 91 or r7 == 125:  # munmap / mprotect
        u.reg_write(UC_ARM_REG_R0, 0)

from unicorn.arm_const import UC_ARM_REG_R5
def svc_hook2(u, intno, user_data):
    global next_fd
    r7 = u.reg_read(UC_ARM_REG_R7)
    r0 = u.reg_read(UC_ARM_REG_R0)
    r1 = u.reg_read(UC_ARM_REG_R1)
    r2 = u.reg_read(UC_ARM_REG_R2)
    if r7 == 5:
        bs = bytearray()
        for i in range(128):
            c = u.mem_read(r0 + i, 1)[0]
            if c == 0: break
            bs.append(c)
        fd = next_fd; next_fd += 1
        open_fds[fd] = [bs.decode("latin1"), 0]
        u.reg_write(UC_ARM_REG_R0, fd)
    elif r7 == 3:
        info = open_fds[r0]
        chunk = maps_bytes[info[1]:info[1] + r2] if info[0] == "/proc/self/maps" else b""
        if chunk: u.mem_write(r1, chunk)
        info[1] += len(chunk)
        u.reg_write(UC_ARM_REG_R0, len(chunk))
    elif r7 == 4:
        if r0 in open_fds and "opengate_a1.log" in open_fds[r0][0]:
            log_buf.extend(u.mem_read(r1, r2))
        u.reg_write(UC_ARM_REG_R0, r2)
    elif r7 == 6:
        u.reg_write(UC_ARM_REG_R0, 0)
    elif r7 == 192:
        pgoff = u.reg_read(UC_ARM_REG_R5)
        u.reg_write(UC_ARM_REG_R0, AV_BASE + AV_PAGE_MAP[pgoff])
    elif r7 in (91, 125):
        u.reg_write(UC_ARM_REG_R0, 0)

uc.hook_add(UC_HOOK_INTR, svc_hook2)

def call_thumb(addr, r0=0, r1=0, r2=0, r3=0):
    uc.reg_write(UC_ARM_REG_CPSR, 0x33)
    uc.reg_write(UC_ARM_REG_SP, STACK_TOP)
    uc.reg_write(UC_ARM_REG_LR, STOP_LR)
    uc.reg_write(UC_ARM_REG_R0, r0)
    uc.reg_write(UC_ARM_REG_R1, r1)
    uc.reg_write(UC_ARM_REG_R2, r2)
    uc.reg_write(UC_ARM_REG_R3, r3)
    uc.emu_start((addr | 1), STOP_LR)
    return uc.reg_read(UC_ARM_REG_R0)

# 1. Test oga1_on
call_thumb(MOD_BASE + on_off)
for pa, _, pt in AV_PATCHES:
    m_off = AV_PAGE_MAP[pa >> 12] + (pa & 0xFFF)
    got = struct.unpack("<H", uc.mem_read(AV_BASE + m_off, 2))[0]
    assert got == pt, f"ON av {hex(pa)}: {got} != {pt}"
for off in VU_CELLS:
    assert struct.unpack("<I", uc.mem_read(VU_BASE + off + 0x0C, 4))[0] == 0x403A0000
    assert struct.unpack("<I", uc.mem_read(VU_BASE + off + 0x1C, 4))[0] == 0x407AC000

# 2. Test oga1_off
call_thumb(MOD_BASE + off_off)
for pa, st, _ in AV_PATCHES:
    m_off = AV_PAGE_MAP[pa >> 12] + (pa & 0xFFF)
    got = struct.unpack("<H", uc.mem_read(AV_BASE + m_off, 2))[0]
    assert got == st, f"OFF av {hex(pa)}: {got} != {st}"
for off in VU_CELLS:
    assert struct.unpack("<I", uc.mem_read(VU_BASE + off + 0x0C, 4))[0] == 0x404E0000
    assert struct.unpack("<I", uc.mem_read(VU_BASE + off + 0x1C, 4))[0] == 0x40768000

# 3. Test tkhd_hook_tramp & stsd_hook_tramp (stub orig returns r2 / [r1+10])
# orig_tkhd stub at ml_orig_tkhd: mov r0, r2; bx lr -> 10 46 70 47 (Thumb)
uc.mem_write(MOD_BASE + (syms["ml_orig_tkhd"] & ~1), bytes.fromhex("10467047"))
# orig_stsd stub at ml_orig_stsd: ldrh r0, [r1, #10]; bx lr -> 48 89 70 47 (Thumb)
uc.mem_write(MOD_BASE + (syms["ml_orig_stsd"] & ~1), bytes.fromhex("48897047"))

model_ptr = 0x30001000
uc.mem_write(model_ptr + 0xD8, struct.pack("<I", 0))
ret_h = call_thumb(MOD_BASE + syms["tkhd_hook_tramp"], model_ptr, 3840 << 16, 2160 << 16, 5)
assert ret_h == (2560 << 16), f"tkhd_hook expected 2560<<16, got {hex(ret_h)}"

uc.mem_write(model_ptr + 0xD8, struct.pack("<I", 1))
ret_busy = call_thumb(MOD_BASE + syms["tkhd_hook_tramp"], model_ptr, 3840 << 16, 2160 << 16, 5)
assert ret_busy == 4, f"tkhd_hook expected 4 when +0xd8!=0, got {ret_busy}"

desc_ptr = 0x30002000
uc.mem_write(desc_ptr, struct.pack("<IIHH", 0x61766331, 0, 3840, 2160) + b"\x00" * 20)
ret_stsd = call_thumb(MOD_BASE + syms["stsd_hook_tramp"], 0x1234, desc_ptr, 0)
assert ret_stsd == 2560, f"stsd_hook expected 2560, got {ret_stsd}"
assert struct.unpack("<H", uc.mem_read(desc_ptr + 10, 2))[0] == 2160  # original desc untouched!

log_text = log_buf.decode("latin1")
lines = log_text.strip().splitlines()
assert "HOOK t kind=00000005 w=00000f00 h=08700a00 r=0a000000" in lines, lines
assert "HOOK s hit=00000001 w=00000f00 h=08700a00 r=00000a00" in lines, lines
assert not any(line.startswith("DIAG") for line in lines), lines
# The busy tkhd path (+0xd8 != 0) returns 4 without calling orig and without a HOOK line.
assert sum(line.startswith("HOOK t") for line in lines) == 1, lines

# 4. A site holding neither the stock nor the patched value yields MIXED counts and a DIAG line.
bad_pa = AV_PATCHES[5][0]
bad_off = AV_PAGE_MAP[bad_pa >> 12] + (bad_pa & 0xFFF)
uc.mem_write(AV_BASE + bad_off, struct.pack("<H", 0xBEEF))
log_buf.clear()
call_thumb(MOD_BASE + on_off)
on_lines = log_buf.decode("latin1").strip().splitlines()
assert on_lines[0] == "DIAG x i=0005 v=beef", on_lines
assert on_lines[-1].startswith("ON av=w0150,a0000,x0001 vu=wwwwwwwwwwwwwwww ok="), on_lines
log_buf.clear()
call_thumb(MOD_BASE + off_off)
off_lines = log_buf.decode("latin1").strip().splitlines()
assert off_lines[0] == "DIAG x i=0005 v=beef", off_lines
assert struct.unpack("<H", uc.mem_read(AV_BASE + bad_off, 2))[0] == 0xBEEF  # foreign value untouched

print(log_text.strip())
print("\n".join(on_lines))
print(f"UNICORN OPENGATE_A1 PASS ({len(code)} B, hooks=2)")
