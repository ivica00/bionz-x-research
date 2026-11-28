"""Pure-Python reference implementation of the Codec10 bitstream (docs/CODEC_DESIGN.md).

Written independently of mods/codec10/codec10.c from the specification so the two can check
each other bit-exactly. Small frames only (pure Python): production decoding uses c10tool.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

FILE_MAGIC = 0x56303143  # "C10V"
FRAME_MAGIC = 0x304D5246  # "FRM0"
VERSION = 1
FILE_HDR = 64
FRAME_HDR = 32
STRIPE_HDR = 4
RICE_BLOCK = 16
RICE_ESCAPE = 24
BFL_BLOCK = 16

FMT_BAYER, FMT_GRAY = 1, 2
CURVE_LINEAR, CURVE_MINIFLOAT = 0, 1
MODE_PACK, MODE_RICE_LEFT, MODE_RICE_MED, MODE_BFL_AVG = 0, 1, 2, 3
CFA_NAMES = ("RGGB", "GRBG", "GBRG", "BGGR")


class CodecError(ValueError):
    pass


@dataclass
class Params:
    width: int
    height: int
    format: int = FMT_BAYER
    cfa: int = 0
    in_bits: int = 14
    curve: int = CURVE_MINIFLOAT
    curve_m: int = 7
    mode: int = MODE_RICE_MED
    stripe_rows: int = 16
    black: int = 512
    white: int = 0
    pedestal: int = 64
    fps_num: int = 24
    fps_den: int = 1
    coded_bits: int = 0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.format not in (FMT_BAYER, FMT_GRAY):
            raise CodecError("format")
        if not (4 <= self.width <= 8192 and 2 <= self.height <= 8192) or self.width % 2 or self.height % 2:
            raise CodecError("geometry")
        if self.format == FMT_BAYER and not 0 <= self.cfa <= 3:
            raise CodecError("cfa")
        if not 8 <= self.in_bits <= 16:
            raise CodecError("in_bits")
        if self.stripe_rows < 2 or self.stripe_rows % 2 or self.stripe_rows > 254:
            raise CodecError("stripe_rows")
        if self.mode not in (MODE_PACK, MODE_RICE_LEFT, MODE_RICE_MED, MODE_BFL_AVG):
            raise CodecError("mode")
        if not self.fps_num or not self.fps_den:
            raise CodecError("fps")
        maxin = (1 << self.in_bits) - 1
        if self.black > maxin or self.pedestal > self.black:
            raise CodecError("black/pedestal")
        if self.white == 0:
            self.white = maxin
        if self.white > maxin or self.white <= self.black:
            raise CodecError("white")
        if self.curve == CURVE_LINEAR:
            self.curve_m = 0
            self.coded_bits = self.in_bits
        elif self.curve == CURVE_MINIFLOAT:
            if self.curve_m < 1 or self.curve_m + 2 > self.in_bits:
                raise CodecError("curve_m")
            emax = self.in_bits - 1 - self.curve_m
            self.coded_bits = ((emax << self.curve_m) + (2 << self.curve_m) - 1).bit_length()
        else:
            raise CodecError("curve")
        if self.mode == MODE_BFL_AVG and self.coded_bits > 15:
            raise CodecError("bfl coded_bits > 15")

    @property
    def offset(self) -> int:
        return self.black - self.pedestal

    @property
    def distance(self) -> int:
        return 2 if self.format == FMT_BAYER else 1

    def n_stripes(self) -> int:
        return -(-self.height // self.stripe_rows)

    def stripe_rows_at(self, s: int) -> int:
        return min(self.stripe_rows, self.height - s * self.stripe_rows)

    def pack_bytes(self, rows: int) -> int:
        return ((rows * self.width * self.coded_bits + 31) >> 5) << 2

    def stripe_bound(self, rows: int) -> int:
        return STRIPE_HDR + self.pack_bytes(rows)


# ---------- curve ----------
def curve_fwd(p: Params, x: int) -> int:
    if p.curve != CURVE_MINIFLOAT:
        return x
    e = max((x | 1).bit_length() - 1 - p.curve_m, 0)
    return (e << p.curve_m) + (x >> e)


def curve_inv(p: Params, c: int) -> int:
    if p.curve != CURVE_MINIFLOAT:
        return c
    e = max((c >> p.curve_m) - 1, 0)
    mant = c - (e << p.curve_m)
    return (mant << e) + ((1 << (e - 1)) if e else 0)


def code_sample(p: Params, x: int) -> int:
    v = max(x - p.offset, 0)
    return curve_fwd(p, min(v, (1 << p.in_bits) - 1))


def uncode_sample(p: Params, c: int) -> int:
    return min(curve_inv(p, c) + p.offset, (1 << p.in_bits) - 1)


# ---------- bit I/O ----------
class _PackWriter:  # LSB-first, little-endian 32-bit words
    def __init__(self) -> None:
        self.acc = 0
        self.n = 0
        self.out = bytearray()

    def put(self, v: int, bits: int) -> None:
        self.acc |= v << self.n
        self.n += bits
        while self.n >= 32:
            self.out += struct.pack("<I", self.acc & 0xFFFFFFFF)
            self.acc >>= 32
            self.n -= 32

    def flush(self) -> bytes:
        if self.n:
            self.out += struct.pack("<I", self.acc & 0xFFFFFFFF)
            self.acc = self.n = 0
        return bytes(self.out)


class _PackReader:  # LSB-first, little-endian 32-bit words
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.acc = 0
        self.n = 0

    def get(self, bits: int) -> int:
        if self.n < bits:
            if self.pos + 4 > len(self.data):
                raise CodecError("truncated stripe")
            self.acc |= struct.unpack_from("<I", self.data, self.pos)[0] << self.n
            self.pos += 4
            self.n += 32
        v = self.acc & ((1 << bits) - 1)
        self.acc >>= bits
        self.n -= bits
        return v


class _RiceWriter:  # MSB-first, big-endian 32-bit words
    def __init__(self) -> None:
        self.bits: list[str] = []
        self.count = 0

    def put(self, v: int, n: int) -> None:
        if n:
            self.bits.append(format(v, f"0{n}b"))
            self.count += n

    def nbytes(self) -> int:
        return (self.count >> 5) << 2

    def flush(self) -> bytes:
        s = "".join(self.bits)
        s += "0" * (-len(s) % 32)
        return int(s, 2).to_bytes(len(s) // 8, "big") if s else b""


class _RiceReader:
    def __init__(self, data: bytes) -> None:
        self.s = "".join(format(b, "08b") for b in data)
        self.pos = 0

    def get(self, n: int) -> int:
        if self.pos + n > len(self.s):
            raise CodecError("truncated stripe")
        v = int(self.s[self.pos:self.pos + n], 2) if n else 0
        self.pos += n
        return v

    def unary(self) -> int:
        q = 0
        while q < RICE_ESCAPE:
            if self.get(1):
                return q
            q += 1
        return RICE_ESCAPE


# ---------- prediction ----------
def _predict(cur, up, i: int, y: int, d: int, med: bool, mid: int) -> int:
    if y < d:
        return mid if i < d else cur[i - d]
    if i < d:
        return up[i]
    a = cur[i - d]
    if not med:
        return a
    b, c = up[i], up[i - d]
    return min(max(a + b - c, min(a, b)), max(a, b))


def _pred_avg(cur, up, i: int, y: int, d: int, mid: int) -> int:
    if y < d:
        return mid if i < d else cur[i - d]
    if i < d:
        return up[i]
    return (cur[i - d] + up[i]) >> 1


def _rice_k(total: int) -> int:
    return min((total >> 5).bit_length(), 15)


# ---------- stripe coding ----------
def encode_stripe(p: Params, frame: list[int], s: int, mode: int | None = None) -> bytes:
    """frame: flat list of sensor samples (width*height). Returns the stripe bytes."""
    mode = p.mode if mode is None else mode
    rows, w = p.stripe_rows_at(s), p.width
    y0 = s * p.stripe_rows
    coded = [[code_sample(p, frame[(y0 + y) * w + x]) for x in range(w)] for y in range(rows)]
    hdr = lambda m: bytes((m, 0)) + struct.pack("<H", rows)
    if mode == MODE_PACK:
        pw = _PackWriter()
        for row in coded:
            for c in row:
                pw.put(c, p.coded_bits)
        return hdr(MODE_PACK) + pw.flush()
    if mode == MODE_BFL_AVG:
        d, cb = p.distance, p.coded_bits
        mid, bound = 1 << (cb - 1), p.stripe_bound(rows)
        pw = _PackWriter()
        for y in range(rows):
            cur, up = coded[y], coded[y - d] if y >= d else coded[y]
            for i0 in range(0, w, BFL_BLOCK):
                nblk = min(BFL_BLOCK, w - i0)
                rs = [cur[i0 + j] - _pred_avg(cur, up, i0 + j, y, d, mid) for j in range(nblk)]
                t = 0
                for rv in rs:
                    t |= rv if rv >= 0 else ~rv
                n = t.bit_length() + 1
                mask = (1 << n) - 1
                pw.put(n - 1, 4)
                for rv in rs:
                    pw.put(rv & mask, n)
            if STRIPE_HDR + len(pw.out) >= bound:
                return encode_stripe(p, frame, s, MODE_PACK)
        body = pw.flush()
        if STRIPE_HDR + len(body) > bound:
            return encode_stripe(p, frame, s, MODE_PACK)
        return hdr(MODE_BFL_AVG) + body
    d, med, cb = p.distance, mode == MODE_RICE_MED, p.coded_bits
    mid, bound = 1 << (cb - 1), p.stripe_bound(rows)
    rw = _RiceWriter()
    for y in range(rows):
        cur, up = coded[y], coded[y - d] if y >= d else coded[y]
        for i0 in range(0, w, RICE_BLOCK):
            us = []
            for i in range(i0, min(i0 + RICE_BLOCK, w)):
                r = cur[i] - _predict(cur, up, i, y, d, med, mid)
                us.append((r << 1) if r >= 0 else ((-r << 1) - 1))
            k = _rice_k(sum(us))
            rw.put(k, 4)
            for z in us:
                q = z >> k
                if q < RICE_ESCAPE:
                    rw.put(0, q)
                    rw.put((1 << k) | (z & ((1 << k) - 1)), k + 1)
                else:
                    rw.put(0, RICE_ESCAPE)
                    rw.put(z, cb + 1)
        if STRIPE_HDR + rw.nbytes() >= bound:
            return encode_stripe(p, frame, s, MODE_PACK)
    body = rw.flush()
    if STRIPE_HDR + len(body) > bound:
        return encode_stripe(p, frame, s, MODE_PACK)
    return hdr(mode) + body


def decode_stripe(p: Params, data: bytes, s: int, out: list[int]) -> None:
    """Decode coded values of stripe s into out (flat, width*height)."""
    rows, w = p.stripe_rows_at(s), p.width
    if len(data) < STRIPE_HDR or len(data) % 4 or data[1] != 0 or struct.unpack_from("<H", data, 2)[0] != rows:
        raise CodecError("stripe header")
    mode, base, cb = data[0], s * p.stripe_rows * w, p.coded_bits
    if mode == MODE_PACK:
        if len(data) != p.stripe_bound(rows):
            raise CodecError("pack size")
        acc = int.from_bytes(data[STRIPE_HDR:], "little")
        mask = (1 << cb) - 1
        for j in range(rows * w):
            out[base + j] = (acc >> (j * cb)) & mask
        return
    if mode == MODE_BFL_AVG:
        if cb > 15:
            raise CodecError("bfl coded_bits > 15")
        d, mid, lim = p.distance, 1 << (cb - 1), 1 << cb
        pr = _PackReader(data[STRIPE_HDR:])
        for y in range(rows):
            cur = out[base + y * w: base + (y + 1) * w]
            up = out[base + (y - d) * w: base + (y - d + 1) * w] if y >= d else cur
            for i0 in range(0, w, BFL_BLOCK):
                nb = pr.get(4) + 1
                sign = 1 << (nb - 1)
                for j in range(min(BFL_BLOCK, w - i0)):
                    v = pr.get(nb)
                    r = v - (1 << nb) if (v & sign) else v
                    x = _pred_avg(cur, up, i0 + j, y, d, mid) + r
                    if not 0 <= x < lim:
                        raise CodecError("sample out of range")
                    cur[i0 + j] = x
            out[base + y * w: base + (y + 1) * w] = cur
        return
    if mode not in (MODE_RICE_LEFT, MODE_RICE_MED):
        raise CodecError("stripe mode")
    d, med, mid = p.distance, mode == MODE_RICE_MED, 1 << (cb - 1)
    rr = _RiceReader(data[STRIPE_HDR:])
    for y in range(rows):
        cur = out[base + y * w: base + (y + 1) * w]
        up = out[base + (y - d) * w: base + (y - d + 1) * w] if y >= d else cur
        for i0 in range(0, w, RICE_BLOCK):
            k = rr.get(4)
            for i in range(i0, min(i0 + RICE_BLOCK, w)):
                q = rr.unary()
                z = rr.get(cb + 1) if q == RICE_ESCAPE else (q << k) | rr.get(k)
                r = (z >> 1) if not z & 1 else -((z + 1) >> 1)
                v = _predict(cur, up, i, y, d, med, mid) + r
                if not 0 <= v < (1 << cb):
                    raise CodecError("sample out of range")
                cur[i] = v
        out[base + y * w: base + (y + 1) * w] = cur


# ---------- container ----------
def file_header(p: Params) -> bytes:
    h = bytearray(FILE_HDR)
    struct.pack_into("<IHHHHBBBBBBBBHHH", h, 0, FILE_MAGIC, VERSION, FILE_HDR, p.width, p.height,
                     p.format, p.cfa, p.in_bits, p.coded_bits, p.curve, p.curve_m, p.mode,
                     p.stripe_rows, p.black, p.white, p.pedestal)
    struct.pack_into("<II", h, 28, p.fps_num, p.fps_den)
    struct.pack_into("<I", h, 60, zlib.crc32(bytes(h[:60])))
    return bytes(h)


def parse_file_header(h: bytes) -> Params:
    if len(h) < FILE_HDR or struct.unpack_from("<I", h, 0)[0] != FILE_MAGIC:
        raise CodecError("not a .c10 file")
    if struct.unpack_from("<I", h, 60)[0] != zlib.crc32(h[:60]):
        raise CodecError("file header CRC")
    (_, ver, size, w, hh, fmt, cfa, ib, cb, curve, m, mode, sr, black, white, ped) = struct.unpack_from(
        "<IHHHHBBBBBBBBHHH", h, 0)
    if ver != VERSION or size != FILE_HDR:
        raise CodecError("version")
    fn, fd = struct.unpack_from("<II", h, 28)
    p = Params(w, hh, fmt, cfa, ib, curve, m, mode, sr, black, white, ped, fn, fd)
    if p.coded_bits != cb:
        raise CodecError("coded_bits mismatch")
    return p


def fletcher(data: bytes, a: int = 0, b: int = 0) -> tuple[int, int]:
    for (w,) in struct.iter_unpack("<I", data):
        a = (a + w) & 0xFFFFFFFF
        b = (b + a) & 0xFFFFFFFF
    return a, b


def frame_record(p: Params, index: int, pts_us: int, stripes: list[bytes]) -> bytes:
    table = b"".join(struct.pack("<I", len(s)) for s in stripes)
    payload = table + b"".join(stripes)
    a, b = fletcher(payload)
    chk = a ^ (((b << 13) | (b >> 19)) & 0xFFFFFFFF)
    h = struct.pack("<IIQIHHI", FRAME_MAGIC, index, pts_us, len(payload), len(stripes), 0, chk)
    return h + struct.pack("<I", zlib.crc32(h)) + payload


def encode_frame(p: Params, frame: list[int], index: int = 0, pts_us: int = 0) -> bytes:
    return frame_record(p, index, pts_us, [encode_stripe(p, frame, s) for s in range(p.n_stripes())])


@dataclass
class Frame:
    index: int
    pts_us: int
    offset: int
    stripes: list[bytes]


@dataclass
class ScanResult:
    params: Params
    frames: list[Frame]
    tail_bytes: int
    skipped_bytes: int


def _frame_at(p: Params, data: bytes, off: int) -> Frame | None:
    if off + FRAME_HDR > len(data):
        return None
    magic, index, pts, size, n, _flags, chk, crc = struct.unpack_from("<IIQIHHII", data, off)
    if magic != FRAME_MAGIC or crc != zlib.crc32(data[off:off + 28]):
        return None
    if n != p.n_stripes() or size < 4 * n or size % 4 or off + FRAME_HDR + size > len(data):
        return None
    payload = data[off + FRAME_HDR: off + FRAME_HDR + size]
    sizes = struct.unpack_from(f"<{n}I", payload, 0)
    if 4 * n + sum(sizes) != size:
        return None
    a, b = fletcher(payload)
    if chk != a ^ (((b << 13) | (b >> 19)) & 0xFFFFFFFF):
        return None
    stripes, pos = [], 4 * n
    for sz in sizes:
        stripes.append(payload[pos:pos + sz])
        pos += sz
    return Frame(index, pts, off, stripes)


def scan(data: bytes, resync: bool = False) -> ScanResult:
    """Crash-safe scan: accept only frames whose header CRC, size and payload checksum match."""
    p = parse_file_header(data[:FILE_HDR])
    off, frames, skipped = FILE_HDR, [], 0
    while off < len(data):
        fr = _frame_at(p, data, off)
        if fr is None:
            if not resync:
                break
            nxt = data.find(struct.pack("<I", FRAME_MAGIC), off + 1)
            if nxt < 0:
                break
            skipped += nxt - off
            off = nxt
            continue
        frames.append(fr)
        off = fr.offset + FRAME_HDR + 4 * len(fr.stripes) + sum(len(s) for s in fr.stripes)
    return ScanResult(p, frames, max(len(data) - off, 0), skipped)


def decode_frame(p: Params, fr: Frame, coded: bool = False) -> list[int]:
    out = [0] * (p.width * p.height)
    for s, data in enumerate(fr.stripes):
        decode_stripe(p, data, s, out)
    if not coded:
        out = [uncode_sample(p, c) for c in out]
    return out
