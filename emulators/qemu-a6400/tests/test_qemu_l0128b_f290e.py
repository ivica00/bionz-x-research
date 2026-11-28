#!/usr/bin/env python3
"""L0128b static check: ZERO rows for the 0xF290E000 writes exposed by DDR-B.

With ddrb-control on by default (L0128) the UPM/DFS chain writes three
clock/reset-group words at 0xF290E004, 0xF290E008 and 0xF290E210.  The
captured L0128 boot log shows them write-only, each with one write of 0.
One page window at 0xF290E000 with three CXD90045_F292_ZERO rows retires
them; any other offset in the page still logs as unmodelled.

Facts are hard-coded here; this test does not read loop/oracle.

Static checks only (no QEMU boot):
  S.table_rows              exactly three ZERO rows at +0x004/+0x008/+0x210
  S.window_created_once     one create call at 0xF290E000, size 0x1000
  S.ddrb_control_default_on sony_a6400_ddrb_control = true is present

Run from the worktree root:
  python3 -m unittest emulators/qemu-a6400/tests/test_qemu_l0128b_f290e.py -q
"""
import re
import unittest
from pathlib import Path

SOURCE = Path("emulators/qemu-a6400/qemu-overlay/hw/arm/sony_a6400.c")

TABLE = "f290e"
BASE = 0xF290E000
SIZE = 0x1000
WINDOW_NAME = "cxd90045.f290e"

EXPECTED_ROWS = [
    (0x004, "CXD90045_F292_ZERO"),
    (0x008, "CXD90045_F292_ZERO"),
    (0x210, "CXD90045_F292_ZERO"),
]


def table_rows(name):
    text = SOURCE.read_text(errors="replace")
    marker = f"static const Cxd90045F292Rule cxd90045_{name}_rules[]"
    start = text.index(marker)
    body = text[start:text.index("};", start)]
    rows = re.findall(r"\{\s*(0x[0-9a-fA-F]+),\s*(CXD90045_F292_\w+),", body)
    return [(int(off, 16), kind) for off, kind in rows]


def all_create_calls():
    text = SOURCE.read_text(errors="replace")
    pattern = (r"cxd90045_f292_create\(sysmem,\s*(0x[0-9A-Fa-f]+)(?:ULL)?,\s*"
               r"(0x[0-9A-Fa-f]+)(?:ULL)?,\s*\"(cxd90045\.\w+)\",\s*"
               r"(cxd90045_\w+_rules),\s*ARRAY_SIZE\(\w+\)\)")
    return [(int(b, 16), int(s, 16), name, table)
            for b, s, name, table in re.findall(pattern, text)]


def ddrb_default_on():
    text = SOURCE.read_text(errors="replace")
    return re.search(r"static bool sony_a6400_ddrb_control = true;", text) is not None


class L0128bStaticTest(unittest.TestCase):
    def test_table_rows(self):
        self.assertEqual(table_rows(TABLE), EXPECTED_ROWS)

    def test_window_created_once(self):
        matches = [c for c in all_create_calls() if c[0] == BASE]
        self.assertEqual(len(matches), 1)
        _, size, win_name, table = matches[0]
        self.assertEqual(size, SIZE)
        self.assertEqual(win_name, WINDOW_NAME)
        self.assertEqual(table, f"cxd90045_{TABLE}_rules")

    def test_ddrb_control_default_on(self):
        self.assertTrue(ddrb_default_on())


if __name__ == "__main__":
    unittest.main()
