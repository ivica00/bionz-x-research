"""Pack a position-independent ARM Thumb ELF (linked at 0) into an SMOD v2 (.mod) file.

SMOD v2 mods need no code cave: Mod Loader v2 allocates memory for them, applies hook
templates with the load address, and calls the optional on()/off() lifecycle entries.

Example:
  python -m tools.research.pack_mod_v2 --elf mod.elf --name "My Mod" --on my_on --off my_off \
      --hook 0x064b54b6:f8b500af0d460446fff7e2ff:my_hook --out MyMod.mod \
      [--reloc-layout tools/native_camera/mlv2/reference_layout_ilce6400.txt]

--reloc-layout adds a relocation table (SMOD_V2_FLAG_RELOC, see tools/native_camera/mlv2/mlv2.h).
Firmware addresses in mod sources are ILCE-6400 reference addresses. Every 32-bit literal word
(inside an ARM "$d" mapping-symbol region of .text) whose value lies in a library range of the
reference layout is listed with that library; hook targets select their library the same way.
The loader then moves them by (runtime base - reference base), so one .mod runs in any process
layout of the same library builds (the camera, QEMU). Values in anonymous memory or the heap are
never relocated; the JSON report lists every relocated word for review. Embedded data blobs
(icons, tables) also sit in "$d" regions: name them with --data-symbol so their bytes are never
taken for addresses. A "$d" region longer than 256 bytes that would yield relocations stops the
build unless it is named that way, because literal pools are short.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from tools.research.pack_mod import extract_elf_text, fnv1a_32

SMOD_MAGIC = 0x444F4D53
NO_FIXUP = 0xFF
FLAG_RELOC = 4
FLAG_AUTO_ON = 8
RELOC_MAGIC = 0x31434C52


def read_layout(path: Path) -> dict[str, tuple[int, int]]:
    """Library file name -> reference range: all mappings of that file plus a directly following
    anonymous mapping (its bss), from a /proc/PID/maps capture."""
    ranges: dict[str, list[int]] = {}
    last = None
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 5 or "-" not in parts[0]:
            continue
        lo, hi = (int(x, 16) for x in parts[0].split("-"))
        name = parts[5].rsplit("/", 1)[-1] if len(parts) > 5 and parts[5].startswith("/") else None
        if name:
            r = ranges.setdefault(name, [lo, hi])
            r[0], r[1] = min(r[0], lo), max(r[1], hi)
            last = name
        elif len(parts) == 5 and last and ranges[last][1] == lo:
            ranges[last][1] = hi            # bss directly after the library
            last = None
        else:
            last = None
    return {k: (v[0], v[1]) for k, v in ranges.items()}


LARGE_POOL = 256


def data_words(elf: bytes, data_symbols=()) -> list[tuple[int, int]]:
    """(offset, region length) of 4-aligned words in .text inside "$d" mapping-symbol regions,
    excluding regions that start at one of ``data_symbols``."""
    hdr = struct.unpack_from("<16sHHIIIIIHHHHHH", elf)
    secs = [struct.unpack_from("<10I", elf, hdr[6] + i * 40) for i in range(hdr[-2])]
    names = elf[secs[hdr[-1]][4]:secs[hdr[-1]][4] + secs[hdr[-1]][5]]
    text_idx = next(i for i, sec in enumerate(secs) if names[sec[0]:names.index(0, sec[0])] == b".text")
    text = secs[text_idx]
    marks, named = [], set()
    for sec in secs:
        if sec[1] != 2:
            continue
        strtab = secs[sec[6]]
        for off in range(0, sec[5], 16):
            st_name, value, _, _, _, shndx = struct.unpack_from("<IIIBBH", elf, sec[4] + off)
            name = elf[strtab[4] + st_name:elf.index(0, strtab[4] + st_name)]
            if shndx == text_idx and name[:2] in (b"$d", b"$t", b"$a"):
                marks.append((value - text[3], name[:2]))
            elif shndx == text_idx and name.decode(errors="replace") in data_symbols:
                named.add(value - text[3])
    marks.sort()
    out = []
    for i, (start, kind) in enumerate(marks):
        if kind != b"$d":
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else text[5]
        if any(start <= n < end for n in named):
            continue
        out += [(o, end - start) for o in range((start + 3) & ~3, end - 3, 4)]
    return out


def reloc_table(code: bytes, elf: bytes, layout: dict[str, tuple[int, int]], hook_targets, skip=(),
                data_symbols=()):
    def lib_of(v):
        return next((n for n, (lo, hi) in layout.items() if lo <= v < hi), None)

    used, rel, report = [], [], []
    for t in hook_targets:
        n = lib_of(t)
        if n is None:
            raise ValueError(f"hook target {t:#x} is not inside a library of the reference layout")
        if n not in used:
            used.append(n)
    for off, region in data_words(elf, data_symbols):
        v = struct.unpack_from("<I", code, off)[0]
        n = lib_of(v)
        if n is None or v in skip:
            continue
        if region > LARGE_POOL:
            raise ValueError(f"word {off:#x} ({v:#x}) is in a {region}-byte data region; name embedded data "
                             "with --data-symbol or check that this is a literal pool")
        if n not in used:
            used.append(n)
        rel.append(off | used.index(n) << 24)
        report.append({"offset": hex(off), "value": hex(v), "lib": n})
    if len(used) > 8 or len(rel) > 1024:
        raise ValueError("relocation table exceeds 8 libraries or 1024 words")
    blob = struct.pack("<IHH", RELOC_MAGIC, len(used), len(rel))
    for n in used:
        if len(n) > 23:
            raise ValueError(f"library name too long: {n}")
        blob += struct.pack("<II24s", *layout[n], n.encode())
    blob += struct.pack(f"<{len(rel)}I", *rel)
    blob += struct.pack("<I", fnv1a_32(blob))
    return blob, report, {n: [hex(layout[n][0]), hex(layout[n][1])] for n in used}


def thumb_abs_template(target: int, entry_off: int, length: int) -> tuple[bytes, int]:
    """Absolute Thumb jump into the mod: ldr.w pc, [pc, #k]; .word (base + entry_off).

    Returns (template, fixup_offset). 4-aligned targets use 12 bytes, 2-mod-4 targets 10+2."""
    if target % 4 == 0:
        tmpl = b"\xdf\xf8\x04\xf0\x00\xbf\x00\xbf" + struct.pack("<I", entry_off)   # ldr.w pc,[pc,#4]; nop; nop
        fix = 8
    elif target % 4 == 2:
        tmpl = b"\x00\xbf\xdf\xf8\x00\xf0" + struct.pack("<I", entry_off)           # nop; ldr.w pc,[pc,#0]
        fix = 6
    else:
        raise ValueError("hook target must be halfword aligned")
    if length < len(tmpl):
        raise ValueError(f"hook at {target:#x} needs {len(tmpl)} original bytes, got {length}")
    return tmpl.ljust(length, b"\x00"), fix   # bytes after the jump never execute


def pack_smod_v2(name, code, on_off=0, off_off=0, bss_size=0, hooks=(), flags=0):
    if not code or len(code) % 4:
        raise ValueError("code must be non-empty and 4-byte aligned")
    for off in (on_off, off_off):
        if off and (not off & 1 or off >= len(code)):
            raise ValueError("lifecycle offsets must be Thumb entries inside the code")
    hdr = struct.pack("<III32s6I", SMOD_MAGIC, 2, flags, name.encode("ascii")[:31],
                      len(code), fnv1a_32(code), bss_size, on_off, off_off, len(hooks))
    blob = bytearray(hdr)
    for target, orig, tmpl, fix in hooks:
        if not 1 <= len(orig) <= 16 or len(tmpl) != len(orig):
            raise ValueError(f"invalid hook at {target:#x}")
        blob += struct.pack("<IBB2x16s16s", target, len(orig), fix, orig.ljust(16, b"\0"), tmpl.ljust(16, b"\0"))
    return bytes(blob) + code


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--elf", type=Path, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--on", help="symbol called after ON (receives 1)")
    ap.add_argument("--off", help="symbol called before OFF (receives 0)")
    ap.add_argument("--bss", type=int, default=0, help="zero-initialized bytes after the code")
    ap.add_argument("--hook", action="append", default=[], help="TARGET:ORIGHEX:SYMBOL (absolute Thumb jump)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reloc-layout", type=Path, help="reference /proc/PID/maps capture (adds a relocation table)")
    ap.add_argument("--no-reloc", action="append", default=[], help="literal value that must not be relocated")
    ap.add_argument("--auto-on", action="store_true", help="the core switches the mod ON when it first lists it")
    ap.add_argument("--data-symbol", action="append", default=[],
                    help="symbol of an embedded data blob whose bytes are never relocated")
    a = ap.parse_args()
    elf = a.elf.read_bytes()
    _, code, syms = extract_elf_text(elf, expected_code_addr=0)

    def sym(n):
        if n not in syms or not syms[n] & 1:
            raise SystemExit(f"symbol {n} missing or not Thumb")
        return syms[n]

    hooks = []
    for spec in a.hook:
        t, orig_hex, s = spec.split(":")
        target, orig = int(t, 0), bytes.fromhex(orig_hex)
        tmpl, fix = thumb_abs_template(target, sym(s), len(orig))
        hooks.append((target, orig, tmpl, fix))
    table, report, libs = b"", [], {}
    if a.reloc_layout:
        missing = [d for d in a.data_symbol if d not in syms]
        if missing:
            raise SystemExit(f"unknown --data-symbol {missing}")
        table, report, libs = reloc_table(code, elf, read_layout(a.reloc_layout), [h[0] for h in hooks],
                                          {int(v, 0) for v in a.no_reloc}, a.data_symbol)
    blob = pack_smod_v2(a.name, code, sym(a.on) if a.on else 0, sym(a.off) if a.off else 0, a.bss, hooks,
                        flags=(FLAG_RELOC if table else 0) | (FLAG_AUTO_ON if a.auto_on else 0)) + table
    a.out.write_bytes(blob)
    info = {"name": a.name, "file": str(a.out), "code_bytes": len(code), "hooks": len(hooks),
            "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
    if table:
        info.update(reloc_libs=libs, reloc_words=report)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
