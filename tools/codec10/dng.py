"""Minimal uncompressed 16-bit CFA DNG writer (one file per frame, CinemaDNG-style sequence).

Tags cover what DaVinci Resolve / Adobe readers need for a Bayer sequence: CFA geometry,
black/white levels, a colour matrix, as-shot neutral and the CinemaDNG FrameRate tag.
"""
from __future__ import annotations

import struct
from array import array
from fractions import Fraction
from pathlib import Path

# XYZ(D65) -> camera matrix from LibRaw's adobe_coeff entry for the Sony ILCE-6400 (shared with
# ILCE-6300/6500). Recorded from that table, not re-measured here: check it before grading.
ILCE6400_COLOR_MATRIX = (5973, -1695, -419, -3826, 11797, 2293, -639, 1398, 5789)

_CFA = {0: (0, 1, 1, 2), 1: (1, 0, 2, 1), 2: (1, 2, 0, 1), 3: (2, 1, 1, 0)}
SHORT, LONG, RATIONAL, SRATIONAL, BYTE, ASCII = 3, 4, 5, 10, 1, 2
_SIZE = {BYTE: 1, ASCII: 1, SHORT: 2, LONG: 4, RATIONAL: 8, SRATIONAL: 8}


def _encode(typ: int, values) -> bytes:
    if typ == ASCII:
        return values.encode("ascii") + b"\0"
    if typ == BYTE:
        return bytes(values)
    if typ == SHORT:
        return struct.pack(f"<{len(values)}H", *values)
    if typ == LONG:
        return struct.pack(f"<{len(values)}I", *values)
    fmt = "<II" if typ == RATIONAL else "<ii"
    return b"".join(struct.pack(fmt, f.numerator, f.denominator) for f in values)


def build_dng(width: int, height: int, samples, *, cfa: int, black: int, white: int,
              fps: Fraction = Fraction(24), model: str = "Codec10 BIONZ X",
              color_matrix=ILCE6400_COLOR_MATRIX, neutral=(Fraction(1, 2), Fraction(1), Fraction(2, 3))) -> bytes:
    pix = array("H", samples)
    if len(pix) != width * height:
        raise ValueError("sample count")
    if pix.itemsize != 2:
        raise ValueError("uint16 array required")
    data = pix.tobytes() if struct.pack("=H", 1) == b"\x01\x00" else (pix.byteswap() or pix).tobytes()
    cm = [Fraction(v, 10000) for v in color_matrix]
    entries = [
        (254, LONG, [0]), (256, LONG, [width]), (257, LONG, [height]), (258, SHORT, [16]),
        (259, SHORT, [1]), (262, SHORT, [32803]), (273, LONG, [0]), (274, SHORT, [1]),
        (277, SHORT, [1]), (278, LONG, [height]), (279, LONG, [len(data)]), (284, SHORT, [1]),
        (33421, SHORT, [2, 2]), (33422, BYTE, list(_CFA[cfa])),
        (50706, BYTE, [1, 4, 0, 0]), (50707, BYTE, [1, 1, 0, 0]), (50708, ASCII, model),
        (50710, BYTE, [0, 1, 2]), (50711, SHORT, [1]), (50713, SHORT, [1, 1]),
        (50714, LONG, [black]), (50717, LONG, [white]), (50721, SRATIONAL, cm),
        (50728, RATIONAL, list(neutral)), (50778, SHORT, [21]), (51044, SRATIONAL, [fps]),
    ]
    entries.sort(key=lambda e: e[0])
    ifd_off = 8
    extra_off = ifd_off + 2 + 12 * len(entries) + 4
    extra = bytearray()
    ifd = bytearray(struct.pack("<H", len(entries)))
    strip_entry = None
    for tag, typ, vals in entries:
        raw = _encode(typ, vals)
        count = len(raw) // _SIZE[typ]
        if len(raw) <= 4:
            field = raw.ljust(4, b"\0")
        else:
            if (extra_off + len(extra)) % 2:
                extra += b"\0"
            field = struct.pack("<I", extra_off + len(extra))
            extra += raw
        if tag == 273:
            strip_entry = len(ifd) + 8
        ifd += struct.pack("<HHI", tag, typ, count) + field
    ifd += struct.pack("<I", 0)
    image_off = extra_off + len(extra)
    image_off += -image_off % 16
    struct.pack_into("<I", ifd, strip_entry, image_off)
    head = b"II*\0" + struct.pack("<I", ifd_off) + bytes(ifd) + bytes(extra)
    return head.ljust(image_off, b"\0") + data


def write_dng(path: Path, *args, **kwargs) -> None:
    Path(path).write_bytes(build_dng(*args, **kwargs))


def read_tags(blob: bytes) -> dict[int, tuple[int, int, bytes]]:
    """Parse IFD0 back (tests): {tag: (type, count, raw value bytes)}."""
    if blob[:4] != b"II*\0":
        raise ValueError("not little-endian TIFF")
    (off,) = struct.unpack_from("<I", blob, 4)
    (n,) = struct.unpack_from("<H", blob, off)
    tags = {}
    for i in range(n):
        tag, typ, count = struct.unpack_from("<HHI", blob, off + 2 + 12 * i)
        size = _SIZE[typ] * count
        pos = off + 2 + 12 * i + 8
        if size > 4:
            (pos,) = struct.unpack_from("<I", blob, pos)
        tags[tag] = (typ, count, blob[pos:pos + size])
    return tags
