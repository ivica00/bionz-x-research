#!/usr/bin/env python3
"""Pointer-flow string cross-references for a flat ``av-cam.bin`` image.

The literal-pool scan in :mod:`avcam_image` sees a string only when a single
``LDR Rd,[pc,#imm]`` resolves to it. That is a floor: most strings in this image
are reached through a pointer table (load the table base, load the entry, pass
the value) or through a base register plus an offset. This module adds three
more resolutions, all local to one function and all checkable against the
literal set:

* ``base`` - a register holds an image address (literal, ``movw``/``movt`` pair,
  ``add``/``sub``/``mov`` from a known register, or a word loaded from a known
  address) and a later load or add forms a string address;
* ``table`` - a load indexed by an unknown register from a known table base,
  where the table holds at least two in-image string pointers;
* ``adr`` - ``adr``/``add rd,pc,#imm`` forms a string address directly.

Every emitted reference names the string it points into and the instruction that
formed the address, so a reader can disassemble the site and check it. The
tracker never guesses an unknown register value: when a base is unknown the
access is skipped, which is why a large residue is expected and not a failure.

The walk is deliberately bounded: one function, no interprocedural flow, no
stack slots. Those are named as open work in :mod:`tools.research.avcam_image`.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from capstone.arm import (
    ARM_GRP_CALL,
    ARM_INS_ADD,
    ARM_INS_LDR,
    ARM_INS_MOV,
    ARM_INS_MOVT,
    ARM_INS_MOVW,
    ARM_INS_SUB,
    ARM_OP_IMM,
    ARM_OP_MEM,
    ARM_OP_REG,
    ARM_REG_PC,
    ARM_REG_SP,
)

#: Priority when the same (function, string) pair is found more than once.
VIA_RANK = {"direct": 4, "adr": 3, "base": 2, "table": 1}
#: A table scan stops after this many entries or three non-pointer words.
MAX_TABLE_ENTRIES = 256
MAX_TABLE_MISSES = 3


@dataclass(frozen=True)
class XRef:
    function_va: int
    string_va: int
    site_va: int
    via: str


class _Strings:
    """Address-to-string lookup over the indexed string table."""

    def __init__(self, strings) -> None:
        self._starts = [rec.va for rec in strings]
        self._records = list(strings)

    def at(self, address: int):
        index = bisect.bisect_right(self._starts, address) - 1
        if index < 0:
            return None
        rec = self._records[index]
        # A reference may point at the start or inside the text; both identify it.
        if rec.va <= address < rec.end_va:
            return rec
        return None


def _word(data: bytes, address: int, base: int) -> int | None:
    offset = address - base
    if 0 <= offset <= len(data) - 4:
        return int.from_bytes(data[offset:offset + 4], "little")
    return None


def _immediate(ins) -> int | None:
    for op in ins.operands:
        if op.type == ARM_OP_IMM:
            return op.imm
    return None


def _dest_reg(ins) -> int | None:
    for op in ins.operands:
        if op.type == ARM_OP_REG and op.access and (op.access & 2):  # WRITE
            return op.reg
    for op in ins.operands:
        if op.type == ARM_OP_REG:
            return op.reg
    return None


def _mem_operand(ins):
    for op in ins.operands:
        if op.type == ARM_OP_MEM:
            return op.mem
    return None


@dataclass(frozen=True)
class PointerRec:
    string_va: int
    location_va: int


def locate_pointers(data: bytes, strings, base: int) -> tuple[PointerRec, ...]:
    """Every 4-byte-aligned word that equals an indexed string address.

    This is the exhaustive half of the xref question: if a string's address is
    not stored anywhere in the image, no code reachable through a stored pointer
    can reference it, and any static function attribution would have to come
    from a computed offset rather than a pointer. Reporting the locations keeps
    that boundary measurable instead of implied.
    """
    starts = {rec.va for rec in strings}
    found: list[PointerRec] = []
    for offset in range(0, len(data) - 4, 4):
        value = int.from_bytes(data[offset:offset + 4], "little")
        if value in starts:
            found.append(PointerRec(value, base + offset))
    return tuple(found)


def collect(data: bytes, functions, strings, base: int,
            direct_refs: tuple[tuple[int, int], ...] = ()) -> tuple[XRef, ...]:
    """Return string references resolved inside each function's own body.

    ``direct_refs`` are the literal-pool pairs already known; they are carried
    through so the result is a superset of the floor and so a direct hit always
    wins the priority vote.
    """
    lookup = _Strings(strings)
    found: dict[tuple[int, int], XRef] = {}
    for function_va, string_va in direct_refs:
        found[(function_va, string_va)] = XRef(function_va, string_va, 0, "direct")

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    md.skipdata = True
    image_end = base + len(data)

    for function in functions:
        start = function.va
        end = function.va + function.size
        constants: dict[int, int] = {}
        table_hints: dict[int, int] = {}

        def note(reg: int | None, address: int, site: int, via: str,
                 exact: bool = False) -> None:
            rec = lookup.at(address)
            if rec is None:
                return
            if exact and rec.va != address:
                # A computed address is only trusted when it lands on the start of
                # a string; "somewhere inside a string" is how a misdecoded data
                # word looks, and that is the false positive this pass avoids.
                return
            key = (function.va, rec.va)
            current = found.get(key)
            if current is None or VIA_RANK[via] > VIA_RANK[current.via]:
                found[key] = XRef(function.va, rec.va, site, via)

        def scan_table(table: int, site: int, via: str = "table") -> None:
            hits = 0
            misses = 0
            for index in range(MAX_TABLE_ENTRIES):
                entry = _word(data, table + index * 4, base)
                if entry is None:
                    break
                rec = lookup.at(entry) if base <= entry < image_end else None
                if rec is None:
                    misses += 1
                    if misses >= MAX_TABLE_MISSES and hits:
                        break
                    if misses >= MAX_TABLE_MISSES * 4:
                        break
                    continue
                misses = 0
                hits += 1
                note(None, entry, site, via, exact=True)

        for ins in md.disasm(data[start - base:end - base], start):
            if ins.id == 0:
                continue
            if ARM_GRP_CALL in ins.groups:
                for reg in (0, 1, 2, 3, 12):
                    constants.pop(reg, None)
                    table_hints.pop(reg, None)
                continue

            written = set(ins.regs_write)
            assigned: dict[int, int] = {}
            hints: dict[int, int] = {}
            mnemonic = ins.mnemonic
            dest = _dest_reg(ins)
            mem = _mem_operand(ins)

            if ins.id in (ARM_INS_MOVW, ARM_INS_MOVT):
                value = _immediate(ins)
                if dest is not None and value is not None:
                    if ins.id == ARM_INS_MOVW:
                        high = constants.get(dest, 0) & 0xFFFF0000
                        assigned[dest] = high | (value & 0xFFFF)
                    else:
                        low = constants.get(dest, 0) & 0xFFFF
                        assigned[dest] = ((value & 0xFFFF) << 16) | low
            elif mnemonic.startswith("adr"):
                value = _immediate(ins)
                if dest is not None and value is not None:
                    # Capstone reports the displacement for some encodings and the
                    # absolute address for others; only an address inside the image
                    # can be taken as resolved.
                    address = value if base <= value < image_end \
                        else ((ins.address + 4) & ~3) + value
                    assigned[dest] = address
                    note(dest, address, ins.address, "adr")
            elif ins.id in (ARM_INS_ADD, ARM_INS_SUB) and dest is not None:
                operands = ins.operands
                if len(operands) == 2 and ins.id == ARM_INS_ADD \
                        and operands[1].type == ARM_OP_REG and operands[1].reg == ARM_REG_PC \
                        and dest in constants:
                    # PIC idiom: ldr rD,[pc,#k] ; add rD, pc  => literal + page(PC)
                    value = constants[dest] + ((ins.address + 4) & ~3)
                    assigned[dest] = value
                    note(dest, value, ins.address, "base", exact=True)
                elif len(operands) >= 3 and operands[1].type == ARM_OP_REG:
                    src = operands[1].reg
                    if operands[2].type == ARM_OP_IMM:
                        if src == ARM_REG_PC:
                            value = ((ins.address + 4) & ~3) + operands[2].imm
                            assigned[dest] = value
                            note(dest, value, ins.address, "adr", exact=True)
                        elif src in constants:
                            value = constants[src]
                            value = value + operands[2].imm if ins.id == ARM_INS_ADD \
                                else value - operands[2].imm
                            assigned[dest] = value
                            note(dest, value, ins.address, "base", exact=True)
                    elif operands[2].type == ARM_OP_REG and ins.id == ARM_INS_ADD:
                        other = operands[2].reg
                        if src in constants and other in constants:
                            value = constants[src] + constants[other]
                            assigned[dest] = value
                            note(dest, value, ins.address, "base", exact=True)
                        elif src in constants:
                            hints[dest] = constants[src]
                        elif other in constants:
                            hints[dest] = constants[other]
            elif ins.id == ARM_INS_MOV and dest is not None:
                operands = ins.operands
                if len(operands) >= 2 and operands[1].type == ARM_OP_REG \
                        and operands[1].reg in constants:
                    assigned[dest] = constants[operands[1].reg]
            elif ins.id == ARM_INS_LDR and mem is not None:
                address = None
                if mem.base == ARM_REG_PC:
                    value = _word(data, ((ins.address + 4) & ~3) + mem.disp, base)
                    if value is not None and dest is not None:
                        assigned[dest] = value
                        note(dest, value, ins.address, "direct")
                elif mem.base in constants and mem.index == 0:
                    address = constants[mem.base] + mem.disp
                    value = _word(data, address, base)
                    if value is not None and dest is not None:
                        assigned[dest] = value
                        note(dest, value, ins.address, "base", exact=True)
                elif mem.base in constants and mem.index != 0:
                    scan_table(constants[mem.base], ins.address)
                elif mem.base in table_hints:
                    scan_table(table_hints[mem.base], ins.address)
                if address is not None:
                    note(dest, address, ins.address, "base", exact=True)

            for reg, value in assigned.items():
                constants[reg] = value
            for reg, table in hints.items():
                table_hints[reg] = table
            for reg in written:
                if reg not in assigned:
                    constants.pop(reg, None)
                    table_hints.pop(reg, None)
            # SP and PC are never tracked; a stale value there would be noise.
            constants.pop(ARM_REG_SP, None)
            constants.pop(ARM_REG_PC, None)

    return tuple(sorted(found.values(), key=lambda ref: (ref.function_va, ref.string_va)))
