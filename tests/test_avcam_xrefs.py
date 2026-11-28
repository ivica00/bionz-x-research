"""Tests for the pointer-flow string xref pass (``tools/research/avcam_xrefs.py``).

The fixture is constructed: a code block with three functions that reach two
strings through a literal base, an indexed table and ``adr``, plus the exact
pointer words. The test asserts each resolution kind is found, that the
direct-literal floor is preserved, and that no reference is invented for a
string whose address is never formed.
"""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "research"))

import avcam_image as image  # noqa: E402
import avcam_xrefs as xrefs  # noqa: E402

BASE = 0x1000
SIZE = 0x400
FUNC_A = 0x1000  # literal base + field load
FUNC_B = 0x1010  # indexed table load
FUNC_C = 0x1030  # adr
STRING_A = 0x1100
STRING_B = 0x1120
TABLE = 0x1180
POOL = 0x11C0


def build_fixture() -> bytes:
    data = bytearray(SIZE)
    # func A: push {lr}; ldr r1,[pc,#0x1bc] (=POOL -> TABLE); ldr r0,[r1,#0]; pop {pc}
    struct.pack_into("<HHHH", data, FUNC_A - BASE, 0xB500, 0x496F, 0x6808, 0xBD00)
    # func B: push {lr}; ldr r1,[pc,#0x1ac] (=POOL); ldr r0,[r1,r2,lsl #2]; pop {pc}
    struct.pack_into("<HHHH", data, FUNC_B - BASE, 0xB500, 0x496B, 0x5888, 0xBD00)
    # func C: push {lr}; adr r0,#0xcc (=STRING_A); pop {pc}
    struct.pack_into("<HHHH", data, FUNC_C - BASE, 0xB500, 0xA033, 0xBD00, 0xBF00)
    for offset in (0x1008, 0x1018, 0x1036):
        struct.pack_into("<H", data, offset - BASE, 0xBF00)
    data[STRING_A - BASE:STRING_A - BASE + 12] = b"[TAG]alpha\x00\x00"
    data[STRING_B - BASE:STRING_B - BASE + 12] = b"[TAG]beta\x00\x00\x00"
    struct.pack_into("<III", data, TABLE - BASE, STRING_B, STRING_A, 0)
    struct.pack_into("<I", data, POOL - BASE, TABLE)
    return bytes(data)


def records() -> tuple[list, list]:
    functions = [
        image.FunctionRec(FUNC_A, FUNC_B - FUNC_A, "code", ("prologue",)),
        image.FunctionRec(FUNC_B, FUNC_C - FUNC_B, "code", ("prologue",)),
        image.FunctionRec(FUNC_C, 0x1040 - FUNC_C, "code", ("prologue",)),
    ]
    strings = [
        image.StringRec(STRING_A, "[TAG]alpha", ("TAG",)),
        image.StringRec(STRING_B, "[TAG]beta", ("TAG",)),
        image.StringRec(0x1140, "[TAG]gamma", ("TAG",)),  # never referenced
    ]
    return functions, strings


class PointerXrefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = build_fixture()
        self.functions, self.strings = records()

    def collect(self, direct=()):
        return {(ref.function_va, ref.string_va): ref
                for ref in xrefs.collect(self.data, self.functions, self.strings, BASE, direct)}

    def test_base_register_load_resolves_the_field_string(self) -> None:
        found = self.collect()
        ref = found[(FUNC_A, STRING_B)]
        self.assertEqual(ref.via, "base")
        self.assertEqual(ref.site_va, FUNC_A + 4)

    def test_indexed_table_load_resolves_both_entries(self) -> None:
        found = self.collect()
        self.assertEqual(found[(FUNC_B, STRING_B)].via, "table")
        self.assertEqual(found[(FUNC_B, STRING_A)].via, "table")

    def test_adr_resolves_the_near_string(self) -> None:
        found = self.collect()
        self.assertEqual(found[(FUNC_C, STRING_A)].via, "adr")

    def test_direct_literal_floor_is_preserved(self) -> None:
        direct = ((FUNC_A, STRING_A),)
        found = self.collect(direct)
        self.assertEqual(found[(FUNC_A, STRING_A)].via, "direct")

    def test_unreferenced_string_gets_no_entry(self) -> None:
        found = self.collect()
        self.assertNotIn(0x1140, {string for _function, string in found})

    def test_pointer_words_are_located(self) -> None:
        pointers = xrefs.locate_pointers(self.data, self.strings, BASE)
        pairs = {(pointer.string_va, pointer.location_va) for pointer in pointers}
        self.assertIn((STRING_B, TABLE), pairs)
        self.assertIn((STRING_A, TABLE + 4), pairs)
        self.assertEqual(len({p.string_va for p in pointers}), 2)


class ScanIntegrationTests(unittest.TestCase):
    def test_scan_image_reports_pointers_and_references(self) -> None:
        from tests import test_avcam_image as fixture
        index = image.scan_image(fixture.build_image(), Path("synthetic.bin"))
        self.assertGreaterEqual(index.stats["string_refs"], 1)
        self.assertEqual(index.stats["string_refs"], len(index.string_refs))
        self.assertGreaterEqual(index.stats["stored_string_pointers"], 1)
        self.assertTrue(all(ref.via in xrefs.VIA_RANK for ref in index.xrefs))
        self.assertTrue(all(ref.site_va for ref in index.xrefs if ref.via != "direct"))


if __name__ == "__main__":
    unittest.main()
