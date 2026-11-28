"""Tests for the flat ORIL image scanner (``tools/research/avcam_image.py``).

Every fixture is constructed here: a synthetic image with the measured shape
(header, vector words, ARM stub, Thumb code block, printable rodata, zero bss,
data block, command table). The real ``av-cam.bin`` is never required, so CI
exercises the parser's refusals and its arithmetic on a machine that has never
unpacked a Sony image.
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

BASE = image.ORIL_BASE
BLOCK = 0x10000
CODE_END = BLOCK
RODATA_START = BLOCK
RODATA_END = 2 * BLOCK
BSS_END = 3 * BLOCK
DATA_END = 4 * BLOCK
STRING_VA = BASE + RODATA_START + 0x40
MMIO_VALUE = 0xF3030010
COMMAND_HANDLER = BASE + 0x31  # odd, Thumb bit set
COMMAND_CHECKER = BASE + 0x15


def encode_bl(site: int, target: int) -> bytes:
    """Thumb-2 ``BL`` encoding, enough to build a fixture and round-trip the decoder."""
    offset = target - (site + 4)
    sign = 1 if offset < 0 else 0
    if sign:
        offset += 1 << 25
    if not 0 <= offset < (1 << 25):
        raise ValueError("branch out of range")
    i1 = (offset >> 23) & 1
    i2 = (offset >> 22) & 1
    imm10 = (offset >> 12) & 0x3FF
    imm11 = (offset >> 1) & 0x7FF
    j1 = (i1 ^ sign) ^ 1
    j2 = (i2 ^ sign) ^ 1
    first = 0xF000 | (sign << 10) | imm10
    second = 0xD000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", first, second)


def build_image() -> bytes:
    data = bytearray(DATA_END)
    # Header: b entry, magic, version, one vector, then a two-word ARM stub ending in bx lr.
    struct.pack_into("<I", data, 0x00, 0xEA000000 | ((0x14 - 8) >> 2))
    data[4:8] = image.ORIL_MAGIC
    struct.pack_into("<I", data, 0x08, image.ORIL_VERSION)
    struct.pack_into("<I", data, 0x0C, BASE + 0x14)
    struct.pack_into("<I", data, 0x10, 0xE12FFF1E)  # bx lr -> stub_end 0x14

    # 0x14: a small function with two literal loads (a string and an MMIO word).
    struct.pack_into("<HHHH", data, 0x14, 0xB510, 0x4804, 0x4904, 0xBD10)
    for offset in range(0x1C, 0x28, 2):
        data[offset:offset + 2] = b"\x00\xbf"  # nop
    struct.pack_into("<I", data, 0x28, STRING_VA)
    struct.pack_into("<I", data, 0x2C, MMIO_VALUE)

    # From 0x30: 8-byte functions [push {lr}][bl next][pop {pc}] so the block is
    # unambiguously code by the scanner's own call-density test.
    for start in range(0x30, CODE_END, 8):
        data[start:start + 2] = struct.pack("<H", 0xB500)
        data[start + 2:start + 6] = encode_bl(BASE + start + 2, BASE + start + 8)
        data[start + 6:start + 8] = struct.pack("<H", 0xBD00)

    # Printable rodata with one NUL-terminated tagged string and filler.
    data[RODATA_START:RODATA_END] = b"A" * (RODATA_END - RODATA_START)
    text = b"[TEST]hello world"
    data[RODATA_START + 0x3F] = 0
    data[RODATA_START + 0x40:RODATA_START + 0x40 + len(text) + 1] = text + b"\x00"

    # bss stays zero; the data block holds a filler whose high bytes cannot form
    # a Thumb BL prefix (so it stays data) plus one shape-valid command table.
    for offset in range(BSS_END, DATA_END):
        data[offset] = 0x80 | (offset & 0x7F)
    table = BSS_END + 0x100
    for index in range(40):
        struct.pack_into("<IIII", data, table + index * 16, 0x4000 + index,
                         COMMAND_HANDLER, 0, COMMAND_CHECKER)
    return bytes(data)


class HeaderTests(unittest.TestCase):
    def test_header_is_measured_not_assumed(self) -> None:
        header = image.parse_header(build_image())
        self.assertEqual(header.base, BASE)
        self.assertEqual(header.version, 4)
        self.assertEqual(header.entry_va, BASE + 0x14)
        self.assertEqual(header.vectors, (BASE + 0x14,))
        self.assertEqual(header.header_end, 0x10)
        self.assertEqual(header.stub_end, 0x14, "the ARM stub ends after its bx")

    def test_wrong_magic_is_refused(self) -> None:
        data = bytearray(build_image())
        data[4:8] = b"XXXX"
        with self.assertRaises(ValueError):
            image.parse_header(bytes(data))

    def test_wrong_version_is_refused(self) -> None:
        data = bytearray(build_image())
        struct.pack_into("<I", data, 0x08, 5)
        with self.assertRaises(ValueError):
            image.parse_header(bytes(data))

    def test_truncated_header_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            image.parse_header(b"\x00" * 8)


class RegionTests(unittest.TestCase):
    def test_regions_are_classified_by_content(self) -> None:
        regions = image.classify_regions(build_image())
        kinds = [(region.kind, region.start, region.end) for region in regions]
        self.assertEqual(kinds, [
            ("code", 0, CODE_END),
            ("rodata", RODATA_START, RODATA_END),
            ("bss", RODATA_END, BSS_END),
            ("data", BSS_END, DATA_END),
        ])

    def test_a_data_block_is_not_mistaken_for_code(self) -> None:
        regions = image.classify_regions(build_image())
        data_region = regions[-1]
        self.assertEqual(data_region.kind, "data")


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = build_image()
        cls.index = image.scan_image(cls.data, Path("synthetic.bin"))

    def test_functions_come_from_prologues_and_vectors(self) -> None:
        starts = {function.va for function in self.index.functions}
        self.assertIn(BASE + 0x14, starts, "header vector")
        self.assertIn(BASE + 0x30, starts, "push {lr} prologue")
        self.assertTrue(any("command-handler" in function.origins
                            for function in self.index.functions))
        self.assertGreater(len(self.index.functions), 1000)

    def test_call_edges_use_the_encoded_target(self) -> None:
        self.assertTrue(self.index.calls)
        first = self.index.calls[0]
        self.assertEqual(first.callee_va, first.site_va + 6, "bl site +6 is the next prologue")
        self.assertEqual(first.kind, "bl")
        self.assertTrue(all(edge.caller_va <= edge.site_va for edge in self.index.calls))

    def test_string_and_tag_are_extracted(self) -> None:
        texts = {record.text: record for record in self.index.strings}
        self.assertIn("[TEST]hello world", texts)
        self.assertEqual(texts["[TEST]hello world"].tags, ("TEST",))
        self.assertEqual(self.index.tag_counts(), {"TEST": 1})

    def test_literal_loads_reach_the_string_and_the_mmio_page(self) -> None:
        self.assertIn((BASE + 0x14, STRING_VA), self.index.string_refs)
        self.assertIn((BASE + 0x14, MMIO_VALUE), self.index.mmio)
        literal_values = {literal.value for literal in self.index.literals}
        self.assertIn(MMIO_VALUE, literal_values)

    def test_command_table_is_found_by_shape(self) -> None:
        self.assertEqual(len(self.index.commands), 40)
        self.assertEqual(self.index.commands[0][0], 0x4000)
        self.assertEqual(self.index.stats["commands"], 40)

    def test_scanning_is_deterministic(self) -> None:
        again = image.scan_image(self.data, Path("synthetic.bin"))
        self.assertEqual(again.stats, self.index.stats)
        self.assertEqual(again.sha256, self.index.sha256)
        self.assertEqual([f.va for f in again.functions], [f.va for f in self.index.functions])


if __name__ == "__main__":
    unittest.main()
