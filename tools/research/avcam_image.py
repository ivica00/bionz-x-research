#!/usr/bin/env python3
"""Static primitives for a flat ``ORIL`` v4 BIONZ X image (``av-cam.bin``).

``av-cam.bin`` is not an ELF: it is a flat image that the loader maps at
``0x635c6000`` (every body observed so far uses that base), with a small header
(entry branch, ``ORIL`` magic, format version, vector words), Thumb-2 code,
string/tables rodata, a zero-filled ``.bss`` extent and initialised RW data.
This module turns those bytes into the facts an index can be built from:

* the header and its vector words, including the ARM-mode boot stub that
  follows the vectors (the ninth entry the first plan counted is code, not a
  pointer -- see :func:`parse_header`);
* measured regions (code / rodata / data / bss), block-classified rather than
  copied from a table that may be wrong for the next body;
* candidate function starts, from Thumb prologues, ``BL``/``BLX`` targets,
  header vectors and command-table handlers, with their origins recorded so a
  reader can tell a prologue-derived start from a call-target-only one;
* call edges, literal-pool loads, string references and MMIO literals.

Everything here is static. A start is a *candidate*: a value in a literal pool
that points into the image can produce a false start, and Thumb data tables
inside a code block decode as instructions. The caller must keep that
distinction; nothing in this module claims a function behaves a certain way.

Usage (see :mod:`tools.research.avcam_indexer` for the SQLite writer)::

    python -m tools.research.avcam_image <av-cam.bin> --report
"""
from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
import re
import struct

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from capstone.arm import (
    ARM_GRP_CALL,
    ARM_GRP_JUMP,
    ARM_INS_LDR,
    ARM_OP_IMM,
    ARM_OP_MEM,
    ARM_OP_REG,
    ARM_REG_PC,
)

#: Mapping base used by every body observed so far (E0521, E0644, av_command_table).
ORIL_BASE = 0x635C6000
ORIL_MAGIC = b"ORIL"
#: The only container version this tooling understands.
ORIL_VERSION = 4
#: Header words are read from ``0x0c`` up to this bound; a valid image has far fewer.
MAX_VECTOR_WORDS = 64

#: A 64 KiB block is a code block when it contains at least this many in-image
#: ``BL``/``BLX`` targets *and* this fraction of them lands on a prologue-shaped
#: halfword. Both numbers were measured on ILCE-6400 ``av-cam.bin``
#: (90d515cf...) and separate its code from its pointer/table data with a wide
#: margin: code blocks scored 0.07-0.66, data blocks at most 0.02.
BLOCK_SIZE = 0x10000
CODE_MIN_CALLS = 64
CODE_MIN_CALL_RATIO = 0.05
ZERO_BLOCK_FRACTION = 0.995
ASCII_BLOCK_FRACTION = 0.85

#: A run of printable bytes shorter than this is not indexed as a string.
MIN_STRING = 6
#: A ``[TAG]`` token inside a string. Both subsystems (upper case, ``[ADF]``)
#: and class names (``[AdfCmdInputCtrl]``) match; the first token is the
#: string's tag, all tokens are indexed.
TAG_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.\-/]{0,40})\]")
_PRINTABLE = bytes(range(0x20, 0x7F))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def va(offset: int, base: int = ORIL_BASE) -> int:
    return base + offset


def offset(address: int, base: int = ORIL_BASE) -> int:
    return address - base


@dataclass(frozen=True)
class OrilHeader:
    """The container header, measured rather than assumed."""

    base: int
    version: int
    entry_va: int
    vectors: tuple[int, ...]
    header_end: int
    """File offset of the first byte after the vector words."""
    stub_end: int
    """File offset after the ARM-mode boot stub that follows the vectors."""
    stub_insns: tuple[tuple[int, str], ...]
    """``(va, 'mnemonic op_str')`` for the ARM stub words, for the record."""

    @property
    def vector_table_va(self) -> int:
        return self.base + 0x0C


def parse_header(data: bytes, base: int = ORIL_BASE) -> OrilHeader:
    """Read the ``ORIL`` header, refusing anything that does not fit the shape.

    The vector table is the run of words from ``+0x0c`` that point inside the
    image. Scanning stops at the first word that does not, which is where the
    ARM boot stub starts; the stub's extent is then measured up to and
    including its first register ``BX`` (the interworking return that enters
    Thumb code). On ILCE-6400 ``av-cam.bin`` (90d515cf...) that gives nine
    vectors at ``+0x0c..+0x2c``, ``header_end = 0x30``, and a 20-byte ARM stub
    ``ldr r0,[pc,#0x8a0]; mov r1,#0xff; strb r1,[r0,#0x21]; ldr r0,[pc,#0x898];
    bx r0`` from ``+0x30`` to ``+0x44``. The measured header is shorter than the
    180-byte extent the first plan quoted; the difference is Thumb prologue code,
    not header data.
    """
    if len(data) < 0x40:
        raise ValueError("image is shorter than an ORIL header")
    if data[4:8] != ORIL_MAGIC:
        raise ValueError(f"not an ORIL image: magic at +4 is {data[4:8]!r}")
    version = struct.unpack_from("<I", data, 8)[0]
    if version != ORIL_VERSION:
        raise ValueError(f"unsupported ORIL version {version}")
    branch = struct.unpack_from("<I", data, 0)[0]
    displacement = branch & 0x00FFFFFF
    if displacement & 0x00800000:
        displacement -= 1 << 24
    entry_va = (base + 8 + (displacement << 2)) & 0xFFFFFFFF
    end = base + len(data)
    vectors: list[int] = []
    pos = 0x0C
    while pos + 4 <= 0x0C + MAX_VECTOR_WORDS * 4:
        word = struct.unpack_from("<I", data, pos)[0]
        if not (base <= word < end):
            break
        vectors.append(word)
        pos += 4
    header_end = pos
    stub_end, stub_insns = _scan_arm_stub(data, header_end, base)
    return OrilHeader(base, version, entry_va, tuple(vectors), header_end, stub_end,
                      stub_insns)


def _scan_arm_stub(data: bytes, start: int, base: int) -> tuple[int, tuple[tuple[int, str], ...]]:
    """Measure the ARM stub: words up to the first register ``BX`` (or 0x100 bytes)."""
    from capstone import CS_MODE_ARM
    md = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    insns: list[tuple[int, str]] = []
    pos = start
    limit = min(len(data), start + 0x100)
    while pos + 4 <= limit:
        word = struct.unpack_from("<I", data, pos)[0]
        if (word & 0x0FFFFFF0) == 0x012FFF10:  # bx <reg>
            insns.append((base + pos, "bx"))
            return pos + 4, tuple(insns)
        if (word & 0x0FFF0000) in (0xE92D0000, 0xE8BD0000):
            break
        decoded = next(iter(md.disasm(data[pos:pos + 4], base + pos)), None)
        if decoded is None:
            break
        insns.append((base + pos, f"{decoded.mnemonic} {decoded.op_str}".strip()))
        pos += 4
    return start, tuple(insns)


@dataclass(frozen=True)
class Region:
    kind: str
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def start_va(self) -> int:
        return va(self.start)

    @property
    def end_va(self) -> int:
        return va(self.end)


def _block_stats(data: bytes, start_off: int, size: int) -> tuple[int, float, float, float]:
    """Return ``(in_image_calls, prologue_hits/calls, zero_fraction, ascii_fraction)``.

    Only 32-bit Thumb ``BL`` pairs at even offsets are counted. A random 32-bit
    word in a pointer table almost never satisfies both the ``BL`` prefix and an
    in-image target, which is why this counter separates code from data even
    though both contain many ``0xF0`` bytes. A call counts as a *hit* when the
    image byte at its target is a ``PUSH``/``PUSH.W`` with ``lr`` saved.
    """
    block = data[start_off:start_off + size]
    calls = 0
    hits = 0
    n = len(block)
    limit = len(data)
    for pos in range(0, n - 4, 2):
        if block[pos + 1] & 0xF8 != 0xF0:
            continue
        second = block[pos + 2] | (block[pos + 3] << 8)
        if (second & 0xD000) != 0xD000:
            continue
        first = block[pos] | (block[pos + 1] << 8)
        sign = (first >> 10) & 1
        imm10 = first & 0x03FF
        j1 = (second >> 13) & 1
        j2 = (second >> 11) & 1
        i1 = 1 - (j1 ^ sign)
        i2 = 1 - (j2 ^ sign)
        displacement = ((sign << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12)
                        | ((second & 0x07FF) << 1))
        if sign:
            displacement -= 1 << 25
        t = start_off + pos + 4 + displacement
        if t < 0 or t + 4 > limit:
            continue
        calls += 1
        hw = data[t] | (data[t + 1] << 8)
        if (hw & 0xFF00) == 0xB500 or hw == 0xE92D:
            hits += 1
    zeros = block.count(0) / n if n else 0.0
    ascii_ = sum(block.count(b) for b in _PRINTABLE) / n if n else 0.0
    return calls, (hits / calls if calls else 0.0), zeros, ascii_


def classify_regions(data: bytes, block_size: int = BLOCK_SIZE) -> tuple[Region, ...]:
    """Classify 64 KiB blocks and merge them into regions.

    Thresholds are module constants with the measurement that set them in their
    docstring. The four kinds answer "what can a scanner read here", not "what
    does the vendor call this section": the section names in the first plan
    (``.text``, ``.rodata``, ``.bss``, ``LIRO`` ``.data``) are kept only as
    labels for the regions whose content matches.
    """
    kinds: list[str] = []
    for start in range(0, len(data), block_size):
        calls, ratio, zeros, ascii_ = _block_stats(data, start, min(block_size, len(data) - start))
        if zeros >= ZERO_BLOCK_FRACTION:
            kinds.append("bss")
        elif ascii_ >= ASCII_BLOCK_FRACTION:
            kinds.append("rodata")
        elif calls >= CODE_MIN_CALLS and ratio >= CODE_MIN_CALL_RATIO:
            kinds.append("code")
        else:
            kinds.append("data")
    regions: list[Region] = []
    for index, kind in enumerate(kinds):
        start = index * block_size
        end = min(start + block_size, len(data))
        if regions and regions[-1].kind == kind and regions[-1].end == start:
            regions[-1] = Region(kind, regions[-1].start, end)
        else:
            regions.append(Region(kind, start, end))
    return tuple(regions)


@dataclass(frozen=True)
class CallEdge:
    site_va: int
    caller_va: int
    callee_va: int
    kind: str  # "bl" | "blx"


@dataclass(frozen=True)
class Literal:
    function_va: int
    value: int


@dataclass(frozen=True)
class StringRec:
    va: int
    text: str
    tags: tuple[str, ...]

    @property
    def tag(self) -> str | None:
        return self.tags[0] if self.tags else None

    @property
    def end_va(self) -> int:
        return self.va + len(self.text) + 1


@dataclass(frozen=True)
class FunctionRec:
    va: int
    size: int
    region_kind: str
    origins: tuple[str, ...]


@dataclass
class ImageIndex:
    path: Path
    sha256: str
    size: int
    header: OrilHeader
    regions: tuple[Region, ...]
    functions: tuple[FunctionRec, ...]
    calls: tuple[CallEdge, ...]
    literals: tuple[Literal, ...]
    strings: tuple[StringRec, ...]
    string_refs: tuple[tuple[int, int], ...]  # (function_va, string_va), all resolutions
    mmio: tuple[tuple[int, int], ...]  # (function_va, address)
    commands: tuple[tuple[int, int, int, int], ...]  # id, handler, flag, checker
    command_offset: int
    stats: dict[str, int] = field(default_factory=dict)
    xrefs: tuple = ()  # avcam_xrefs.XRef records, with site and resolution kind
    string_pointers: tuple = ()  # avcam_xrefs.PointerRec: every stored string address

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.strings:
            for tag in rec.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts


def _scan_instructions(data: bytes, regions: tuple[Region, ...], start_offset: int):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    md.skipdata = True
    for region in regions:
        if region.kind != "code":
            continue
        begin = max(region.start, start_offset)
        if begin >= region.end:
            continue
        for ins in md.disasm(data[begin:region.end], va(begin)):
            yield ins


def _literal_value(data: bytes, ins, base: int, size: int) -> int | None:
    """Read the word an ``LDR Rd,[pc,#imm]`` points at, or ``None``."""
    disp = None
    for op in ins.operands:
        if op.type == ARM_OP_MEM and op.mem.base == ARM_REG_PC:
            disp = op.mem.disp
            break
    if disp is None:
        return None
    target = ((ins.address + 4) & ~3) + disp
    off = offset(target, base)
    if 0 <= off <= size - 4:
        return struct.unpack_from("<I", data, off)[0]
    return None


def extract_strings(data: bytes, regions: tuple[Region, ...]) -> tuple[StringRec, ...]:
    """Index NUL-terminated printable runs in every non-code region.

    Code is excluded because a 6-byte printable run inside instructions is not
    a string. Data blocks are included because initialised RW data carries
    configuration strings too; that is why the count here is larger than the
    string-only regions alone would give.
    """
    records: list[StringRec] = []
    for region in regions:
        if region.kind in ("code", "bss"):
            continue
        chunk = data[region.start:region.end]
        pos = 0
        while True:
            end = chunk.find(b"\x00", pos)
            if end < 0:
                break
            run = chunk[pos:end]
            if len(run) >= MIN_STRING and all(0x20 <= b < 0x7F for b in run):
                text = run.decode("ascii")
                tags = tuple(m.group(1) for m in TAG_RE.finditer(text))
                records.append(StringRec(va(region.start + pos), text, tags))
            pos = end + 1
    return tuple(records)


def scan_image(data: bytes, path: Path = Path("av-cam.bin")) -> ImageIndex:
    """Scan a flat image. Pure function of the bytes; no network, no device."""
    header = parse_header(data)
    regions = classify_regions(data)
    strings = extract_strings(data, regions)
    string_starts = [rec.va for rec in strings]

    command_offset = 0
    commands: tuple[tuple[int, int, int, int], ...] = ()
    try:
        from tools.research.av_command_table import find_table
    except ImportError:  # imported with tools/research on sys.path
        from av_command_table import find_table
    table = find_table(data)
    command_offset = table.offset
    commands = tuple((c.command_id, c.handler, c.flag, c.checker) for c in table.records)

    sweep_start = header.stub_end if header.stub_end > 0 else header.header_end
    code_regions = tuple(r for r in regions if r.kind == "code")

    starts: dict[int, set[str]] = {}
    for vector in header.vectors:
        starts.setdefault(vector & ~1, set()).add("header-vector")
    for _cid, handler, _flag, checker in commands:
        starts.setdefault(handler & ~1, set()).add("command-handler")
        starts.setdefault(checker & ~1, set()).add("command-checker")

    raw_calls: list[tuple[int, int, str]] = []
    raw_literals: list[tuple[int, int]] = []
    for ins in _scan_instructions(data, regions, sweep_start):
        if ins.id == 0:  # skipdata placeholder
            continue
        if ARM_GRP_CALL in ins.groups:
            target = None
            for op in ins.operands:
                if op.type == ARM_OP_IMM:
                    target = op.imm
            if target is not None and not (target & 1) and _in_regions(target, code_regions):
                kind = "blx" if ins.mnemonic.startswith("blx") else "bl"
                raw_calls.append((ins.address, target, kind))
        if ins.id == ARM_INS_LDR:
            value = _literal_value(data, ins, header.base, len(data))
            if value is not None:
                raw_literals.append((ins.address, value))
        mnemonic = ins.mnemonic
        if mnemonic.startswith("push") and "lr" in ins.op_str:
            starts.setdefault(ins.address, set()).add("prologue")

    # A ``BL`` target inside a function's prologue-derived range is an internal
    # entry point (error path, loop head), not a second function. Keeping the
    # distinction in the stats matters: the candidate function count is a
    # prologue count plus the handful of starts known from header vectors and the
    # command table, not a count of every address a call reaches.
    ordered = sorted((addr, tuple(sorted(origins))) for addr, origins in starts.items()
                     if _in_regions(addr, code_regions))
    functions = _ranges(ordered, code_regions)
    entry_points = {target for _s, target, _k in raw_calls} - {f.va for f in functions}

    # Attribute every site to the function whose start precedes it in the same region.
    func_starts = [f.va for f in functions]
    calls: list[CallEdge] = []
    literals: list[Literal] = []
    for site, target, kind in raw_calls:
        caller = _owner(site, func_starts, functions)
        if caller is not None:
            calls.append(CallEdge(site, caller, target, kind))
    for site, value in raw_literals:
        caller = _owner(site, func_starts, functions)
        if caller is not None:
            literals.append(Literal(caller, value))

    refs: set[tuple[int, int]] = set()
    mmio: set[tuple[int, int]] = set()
    for literal in literals:
        value = literal.value
        if 0xF0000000 <= value <= 0xFFFFFFFF:
            mmio.add((literal.function_va, value))
        index = bisect.bisect_right(string_starts, value) - 1
        if index >= 0:
            rec = strings[index]
            if rec.va <= value < rec.end_va:
                refs.add((literal.function_va, rec.va))

    try:  # imported as tools.research.avcam_image
        from tools.research import avcam_xrefs
    except ImportError:
        import avcam_xrefs  # type: ignore
    xrefs = avcam_xrefs.collect(data, functions, strings, header.base, tuple(sorted(refs)))
    pointers = avcam_xrefs.locate_pointers(data, strings, header.base)
    referenced_strings = {ref.string_va for ref in xrefs}
    tagged_strings = {rec.va for rec in strings if rec.tags}

    index = ImageIndex(
        path=path,
        sha256=sha256(data),
        size=len(data),
        header=header,
        regions=regions,
        functions=tuple(functions),
        calls=tuple(calls),
        literals=tuple(literals),
        strings=strings,
        string_refs=tuple(sorted({(ref.function_va, ref.string_va) for ref in xrefs})),
        xrefs=xrefs,
        string_pointers=pointers,
        mmio=tuple(sorted(mmio)),
        commands=commands,
        command_offset=command_offset,
    )
    index.stats = {
        "functions": len(functions),
        "functions_prologue": sum(1 for f in functions if "prologue" in f.origins),
        "functions_without_prologue": sum(1 for f in functions if "prologue" not in f.origins),
        "functions_command": sum(1 for f in functions
                                 if {"command-handler", "command-checker"} & set(f.origins)),
        "calls": len(calls),
        "call_targets": len({target for _s, target, _k in raw_calls}),
        "internal_entry_points": len(entry_points),
        "literals": len(literals),
        "strings": len(strings),
        "tags": len(index.tag_counts()),
        "tag_occurrences": sum(index.tag_counts().values()),
        "strings_referenced": len({s for _f, s in index.string_refs}),
        "string_refs": len(index.string_refs),
        "string_refs_direct": sum(1 for ref in xrefs if ref.via == "direct"),
        "string_refs_base": sum(1 for ref in xrefs if ref.via == "base"),
        "string_refs_table": sum(1 for ref in xrefs if ref.via == "table"),
        "string_refs_adr": sum(1 for ref in xrefs if ref.via == "adr"),
        "functions_with_string_refs": len({f for f, _s in index.string_refs}),
        "tagged_strings": len(tagged_strings),
        "tagged_strings_with_refs": len(tagged_strings & referenced_strings),
        "tags_with_function_refs": len({tag for rec in strings
                                        if rec.va in referenced_strings for tag in rec.tags}),
        "strings_with_stored_pointer": len({p.string_va for p in pointers}),
        "stored_string_pointers": len(pointers),
        "mmio_literals": len(mmio),
        "mmio_pages": len({a & ~0xFFF for _f, a in mmio if a <= 0xFCFFFFFF}),
        "commands": len(commands),
    }
    return index


def _ranges(ordered, code_regions: tuple[Region, ...]) -> list[FunctionRec]:
    """Turn sorted starts into records; each size runs to the next start or region end."""
    functions: list[FunctionRec] = []
    for index, (addr, origins) in enumerate(ordered):
        region = _region_of(addr, code_regions)
        if index + 1 < len(ordered):
            end = min(ordered[index + 1][0], region.end_va)
        else:
            end = region.end_va
        functions.append(FunctionRec(addr, end - addr, region.kind, origins))
    return functions


def _in_regions(address: int, regions: tuple[Region, ...]) -> bool:
    for region in regions:
        if region.start_va <= address < region.end_va:
            return True
    return False


def _region_of(address: int, regions: tuple[Region, ...]) -> Region:
    for region in regions:
        if region.start_va <= address < region.end_va:
            return region
    raise ValueError(f"address {address:#x} is not in a code region")


def _owner(site: int, starts: list[int], functions: list[FunctionRec]) -> int | None:
    """The function containing ``site``, or ``None`` when it lies in a data gap."""
    index = bisect.bisect_right(starts, site) - 1
    if index < 0:
        return None
    rec = functions[index]
    return rec.va if site < rec.va + rec.size else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path)
    parser.add_argument("--report", action="store_true", help="print the measured layout")
    args = parser.parse_args(argv)
    data = args.image.read_bytes()
    index = scan_image(data, args.image)
    print(f"# {args.image} sha256={index.sha256} size={index.size}")
    print(f"base={index.header.base:#x} version={index.header.version} "
          f"entry={index.header.entry_va:#010x} vectors={len(index.header.vectors)} "
          f"header_end={index.header.header_end:#x} stub_end={index.header.stub_end:#x}")
    if args.report:
        print("regions:")
        for region in index.regions:
            print(f"  {region.kind:6s} {region.start_va:#010x}..{region.end_va:#010x} "
                  f"{region.size:8d} B")
    for key, value in index.stats.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
