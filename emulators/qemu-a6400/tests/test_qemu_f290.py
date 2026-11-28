#!/usr/bin/env python3
"""L0116 static check: the clkrst3 0xF2900000 rule window, ZERO-only rows.

F-0104 identifies 0xF2900000..0xF2900FFF as the CXD90045 clock/reset bank
(`clkrst3`).  L0116 maps it as a per-offset window cxd90045.f290 (size 0x1000,
priority -600) whose nine rows are all CXD90045_F292_ZERO: +0x004, +0x008,
+0x028, +0x034, +0x038, +0x044, +0x0A4, +0x0A8, +0x0C4.  +0x030, whose read is
branched on (F-0104), has one CXD90045_F292_CONST row holding the value observed
on an ILCE-6100.

Static checks only (no QEMU boot):
  S.rows_are_the_ten_offsets    the nine ZERO offsets plus +0x030
  S.no_RF_row                   no CXD90045_F292_RF row
  S.nine_rows_ZERO              the nine write-side offsets are ZERO
  S.row_0x030_is_captured       +0x030 is CONST 0x008E56FC, read by the model
  S.window_created_once         one cxd90045_f292_create call at 0xF2900000,
                                size 0x1000, using cxd90045_f290_rules

Run from the repository root:
  python3 -m unittest emulators/qemu-a6400/tests/test_qemu_f290.py -q
"""
import re
import unittest
from pathlib import Path

SOURCE = Path("emulators/qemu-a6400/qemu-overlay/hw/arm/sony_a6400.c")
RULED_OFF = [0x004, 0x008, 0x028, 0x034, 0x038, 0x044, 0x0A4, 0x0A8, 0x0C4]
CAPTURED_OFF = 0x030


def table_rows():
    text = SOURCE.read_text(errors="replace")
    start = text.index("static const Cxd90045F292Rule cxd90045_f290_rules[]")
    body = text[start:text.index("};", start)]
    rows = re.findall(r"\{\s*(0x[0-9a-fA-F]+),\s*(CXD90045_F292_\w+),", body)
    return [(int(off, 16), kind) for off, kind in rows]


def create_calls():
    text = SOURCE.read_text(errors="replace")
    return re.findall(
        r"cxd90045_f292_create\(sysmem,\s*(0x[0-9A-Fa-f]+)ULL,\s*"
        r"(0x[0-9A-Fa-f]+)ULL?,\s*\"cxd90045\.f290\",\s*"
        r"cxd90045_f290_rules,\s*ARRAY_SIZE\(cxd90045_f290_rules\)\)",
        text)


class F290StaticTest(unittest.TestCase):
    def test_rows_are_the_ten_offsets(self):
        offs = sorted(off for off, _ in table_rows())
        self.assertEqual(offs, sorted(RULED_OFF + [CAPTURED_OFF]))

    def test_no_RF_row(self):
        kinds = [k for _, k in table_rows()]
        self.assertEqual(kinds.count("CXD90045_F292_RF"), 0)

    def test_nine_rows_ZERO(self):
        zero = sorted(o for o, k in table_rows() if k == "CXD90045_F292_ZERO")
        self.assertEqual(zero, sorted(RULED_OFF))

    def test_row_0x030_is_captured(self):
        self.assertEqual(dict(table_rows())[CAPTURED_OFF],
                         "CXD90045_F292_CONST")
        text = SOURCE.read_text(errors="replace")
        start = text.index("static const Cxd90045F292Rule cxd90045_f290_rules[]")
        body = text[start:text.index("};", start)]
        self.assertRegex(body, r"\{\s*0x030,[^}]*0x008e56fc\s*\}")
        self.assertRegex(text, r"r->kind == CXD90045_F292_CONST\) \{\s*"
                               r"v = r->value;")

    def test_window_created_once(self):
        calls = create_calls()
        self.assertEqual(len(calls), 1)
        base, size = calls[0]
        self.assertEqual(int(base, 16), 0xF2900000)
        self.assertEqual(int(size, 16), 0x1000)


if __name__ == "__main__":
    unittest.main()
