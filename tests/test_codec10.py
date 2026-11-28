"""Codec10: reference round-trips, curve quality, crash-safe container, DNG, and a bit-exact
cross-check of the C encoder (mods/codec10) against the independent Python reference."""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import struct
import subprocess
import unittest
import uuid
from array import array
from fractions import Fraction
from pathlib import Path

from tools.codec10 import dng, metrics, reference as ref

ROOT = Path(__file__).resolve().parents[1]


def noisy_frame(p: ref.Params, seed: int, read: float = 3.0, spikes: int = 0) -> list[int]:
    rnd = random.Random(seed)
    maxv = (1 << p.in_bits) - 1
    out = []
    for y in range(p.height):
        for x in range(p.width):
            s = (p.white - p.black) * (0.02 + 0.9 * x / p.width) * (0.6 if (x ^ y) & 1 else 1.0)
            v = p.black + s + rnd.gauss(0, math.sqrt(s * 2.0 ** (p.in_bits - 14) + read * read))
            out.append(int(min(max(round(v), 0), maxv)))
    for _ in range(spikes):
        out[rnd.randrange(len(out))] = rnd.choice((0, maxv))
    return out


def lossless(**kw) -> ref.Params:
    kw.setdefault("curve", ref.CURVE_LINEAR)
    kw.setdefault("black", 0)
    kw.setdefault("pedestal", 0)
    return ref.Params(**kw)


class ParamsTests(unittest.TestCase):
    def test_coded_bits(self):
        self.assertEqual(ref.Params(64, 32, in_bits=14, curve_m=7).coded_bits, 10)
        self.assertEqual(ref.Params(64, 32, in_bits=14, curve_m=9).coded_bits, 12)
        self.assertEqual(ref.Params(64, 32, in_bits=12, curve_m=7, black=128, pedestal=16).coded_bits, 10)
        self.assertEqual(lossless(width=64, height=32, in_bits=16).coded_bits, 16)

    def test_rejects_bad_params(self):
        for kw in ({"width": 63}, {"height": 3}, {"stripe_rows": 3}, {"in_bits": 17}, {"curve_m": 13},
                   {"pedestal": 600}, {"mode": 4}, {"cfa": 4},
                   {"mode": ref.MODE_BFL_AVG, "curve": ref.CURVE_LINEAR, "in_bits": 16}):
            args = {"width": 64, "height": 32, **kw}
            with self.assertRaises(ref.CodecError, msg=str(kw)):
                ref.Params(**args)


class CurveTests(unittest.TestCase):
    def test_minifloat_error_bounded_and_monotone(self):
        p = ref.Params(64, 32, in_bits=14, curve_m=7, black=0, pedestal=0)
        prev = -1
        for x in range(1 << 14):
            c = ref.curve_fwd(p, x)
            self.assertLess(c, 1 << p.coded_bits)
            self.assertGreaterEqual(c, prev)
            prev = c
            e = max((c >> 7) - 1, 0)
            self.assertLessEqual(abs(ref.curve_inv(p, c) - x), (1 << e) >> 1)
        self.assertEqual([ref.curve_fwd(p, x) for x in range(256)], list(range(256)))

    def test_quality_thresholds(self):
        p = ref.Params(64, 32, in_bits=14, curve_m=7)
        r = metrics.curve_report(p)
        self.assertTrue(r["monotone"])
        self.assertLess(r["worst_q_over_sigma"], 0.3)      # < 4.4 % added noise at any level
        self.assertLess(r["max_step_over_sigma"], 1.0)     # no step wider than the sensor noise
        self.assertGreaterEqual(r["stops_with_8_codes"], 11)
        n = metrics.noisy_chart_psnr(p, n=4000)
        self.assertLess(n["noise_increase_pct"], 2.0)
        lin = metrics.curve_report(lossless(width=64, height=32, in_bits=12, black=128, pedestal=128))
        self.assertEqual(lin["psnr_db"], math.inf)


class RoundTripTests(unittest.TestCase):
    def roundtrip(self, p: ref.Params, frame: list[int]) -> list[bytes]:
        stripes = [ref.encode_stripe(p, frame, s) for s in range(p.n_stripes())]
        out = [0] * (p.width * p.height)
        for s, data in enumerate(stripes):
            self.assertLessEqual(len(data), p.stripe_bound(p.stripe_rows_at(s)))
            self.assertEqual(len(data) % 4, 0)
            ref.decode_stripe(p, data, s, out)
        self.assertEqual(out, [ref.code_sample(p, x) for x in frame])
        return stripes

    def test_lossless_all_modes_formats_depths(self):
        for fmt in (ref.FMT_BAYER, ref.FMT_GRAY):
            for bits in (10, 12, 14, 16):
                for mode in (ref.MODE_PACK, ref.MODE_RICE_LEFT, ref.MODE_RICE_MED, ref.MODE_BFL_AVG):
                    if mode == ref.MODE_BFL_AVG and bits == 16:
                        continue
                    p = lossless(width=40, height=20, format=fmt, in_bits=bits, mode=mode, stripe_rows=8)
                    frame = noisy_frame(p, seed=bits * 7 + mode)
                    self.roundtrip(p, frame)
                    dec = [0] * len(frame)
                    for s in range(p.n_stripes()):
                        ref.decode_stripe(p, ref.encode_stripe(p, frame, s), s, dec)
                    self.assertEqual([ref.uncode_sample(p, c) for c in dec], frame)

    def test_lossy_curve_decodes_to_coded_values(self):
        for m, mode in ((7, ref.MODE_RICE_MED), (9, ref.MODE_RICE_LEFT), (7, ref.MODE_PACK),
                        (7, ref.MODE_BFL_AVG), (9, ref.MODE_BFL_AVG)):
            p = ref.Params(48, 12, in_bits=14, curve_m=m, mode=mode, stripe_rows=4)
            self.roundtrip(p, noisy_frame(p, seed=m))

    def test_escape_codes_survive_impulses(self):
        p = lossless(width=64, height=8, in_bits=14, mode=ref.MODE_RICE_MED, stripe_rows=8)
        stripes = self.roundtrip(p, noisy_frame(p, seed=3, spikes=40))
        self.assertEqual(stripes[0][0], ref.MODE_RICE_MED)

    def test_incompressible_stripe_falls_back_to_pack(self):
        p = lossless(width=32, height=4, in_bits=16, mode=ref.MODE_RICE_MED, stripe_rows=4)
        rnd = random.Random(1)
        frame = [rnd.randrange(1 << 16) for _ in range(32 * 4)]
        stripes = self.roundtrip(p, frame)
        self.assertEqual(stripes[0][0], ref.MODE_PACK)
        self.assertEqual(len(stripes[0]), p.stripe_bound(4))


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.p = ref.Params(32, 12, in_bits=14, curve_m=7, stripe_rows=4)
        self.frames = [noisy_frame(self.p, seed=i) for i in range(3)]
        self.records = [ref.encode_frame(self.p, f, i, i * 41667) for i, f in enumerate(self.frames)]
        self.blob = ref.file_header(self.p) + b"".join(self.records)

    def test_every_truncation_keeps_complete_frames(self):
        ends = [ref.FILE_HDR]
        for r in self.records:
            ends.append(ends[-1] + len(r))
        for cut in range(ref.FILE_HDR, len(self.blob) + 1, 7):
            res = ref.scan(self.blob[:cut])
            complete = sum(1 for e in ends[1:] if e <= cut)
            self.assertEqual(len(res.frames), complete, cut)
            for fr in res.frames:
                dec = ref.decode_frame(self.p, fr, coded=True)
                self.assertEqual(dec, [ref.code_sample(self.p, x) for x in self.frames[fr.index]])

    def test_corruption_detected_and_resync(self):
        bad = bytearray(self.blob)
        bad[ref.FILE_HDR + len(self.records[0]) + 60] ^= 0x40   # inside frame 1's payload
        self.assertEqual([f.index for f in ref.scan(bytes(bad)).frames], [0])
        res = ref.scan(bytes(bad), resync=True)
        self.assertEqual([f.index for f in res.frames], [0, 2])
        self.assertGreater(res.skipped_bytes, 0)

    def test_header_crc(self):
        bad = bytearray(self.blob)
        bad[9] ^= 1
        with self.assertRaises(ref.CodecError):
            ref.scan(bytes(bad))


class DngTests(unittest.TestCase):
    def test_tags_and_pixels(self):
        w, h = 16, 8
        px = list(range(w * h))
        blob = dng.build_dng(w, h, px, cfa=0, black=512, white=16383, fps=Fraction(24000, 1001))
        tags = dng.read_tags(blob)
        self.assertEqual(struct.unpack("<I", tags[256][2])[0], w)
        self.assertEqual(struct.unpack("<I", tags[257][2])[0], h)
        self.assertEqual(tags[33422][2], bytes((0, 1, 1, 2)))
        self.assertEqual(struct.unpack("<I", tags[50714][2])[0], 512)
        self.assertEqual(struct.unpack("<I", tags[50717][2])[0], 16383)
        self.assertEqual(struct.unpack("<ii", tags[51044][2]), (24000, 1001))
        off = struct.unpack("<I", tags[273][2])[0]
        self.assertEqual(array("H", blob[off:off + 2 * w * h]).tolist(), px)


def _native_build() -> Path | None:
    env = os.environ.get("CODEC10_BUILD")
    cands = [Path(env)] if env else sorted((ROOT / "artifacts/tests").glob("codec10-build-*"))
    for c in reversed(cands):
        if (c / "host/bench_codec").exists() and (c / "host/c10tool").exists():
            return c
    return None


def _run_linux(args: list[str]) -> str:
    if os.name == "nt":
        def lin(a: str) -> str:
            pth = Path(a)
            return "/mnt/" + pth.drive[0].lower() + pth.as_posix()[2:] if pth.is_absolute() else a
        args = ["wsl.exe", "--exec"] + [lin(a) for a in args]
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=300).stdout


def _linux_ok() -> bool:
    if os.name != "nt":
        return True
    if not shutil.which("wsl.exe"):
        return False
    try:
        return subprocess.run(["wsl.exe", "--exec", "true"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_native_build() and _linux_ok(),
                     "native Codec10 build or WSL absent (run mods/codec10/build_wsl.sh artifacts/tests/codec10-build-N)")
class NativeCrossCheckTests(unittest.TestCase):
    def test_c_encoder_matches_python_reference_bit_exact(self):
        build = _native_build()
        work = ROOT / "artifacts/tests" / f"codec10-xcheck-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True)
        try:
            cases = [  # (mode name, mode, curve flag, in_bits, black, pedestal, m)
                ("med", ref.MODE_RICE_MED, "mf", 14, 512, 64, 7),
                ("left", ref.MODE_RICE_LEFT, "mf", 14, 512, 64, 9),
                ("pack", ref.MODE_PACK, "mf", 14, 512, 64, 7),
                ("bfl", ref.MODE_BFL_AVG, "mf", 14, 512, 64, 7),
                ("med", ref.MODE_RICE_MED, "linear", 12, 0, 0, 0),
                ("pack", ref.MODE_PACK, "linear", 12, 0, 0, 0),
                ("bfl", ref.MODE_BFL_AVG, "linear", 12, 0, 0, 0),
            ]
            for name, mode, curve, bits, black, ped, m in cases:
                p = ref.Params(96, 36, in_bits=bits, black=black, pedestal=ped, mode=mode,
                               curve=ref.CURVE_MINIFLOAT if curve == "mf" else ref.CURVE_LINEAR,
                               curve_m=m, stripe_rows=16)
                frame = noisy_frame(p, seed=bits + mode + m, spikes=5)
                raw = work / f"in_{name}_{curve}_{bits}.raw16"
                raw.write_bytes(array("H", frame).tobytes())
                out = work / f"c_{name}_{curve}_{bits}.c10"
                args = [str(build / "host/bench_codec"), "--w", "96", "--h", "36", "--bits", str(bits),
                        "--curve", curve, "--m", str(m), "--mode", name, "--black", str(black),
                        "--pedestal", str(ped), "--stripe", "16", "--frames", "2", "--threads", "2",
                        "--input", str(raw), "--out", str(out), "--verify"]
                res = json.loads(_run_linux(args).split("RESULT ", 1)[1])
                self.assertEqual(res["verify"], "PASS")
                expect = ref.file_header(p) + b"".join(
                    ref.encode_frame(p, frame, i, i * 1000000 // 24) for i in range(2))
                self.assertEqual(out.read_bytes(), expect, f"{name}/{curve}/{bits}")
                info = json.loads(_run_linux([str(build / "host/c10tool"), "info", str(out)]))
                self.assertEqual(info["valid_frames"], 2)
                # torn tail: c10tool and the Python scan agree that one frame survives
                torn = work / "torn.c10"
                torn.write_bytes(expect[:-10])
                info = json.loads(_run_linux([str(build / "host/c10tool"), "info", str(torn)]))
                self.assertEqual(info["valid_frames"], len(ref.scan(expect[:-10]).frames))
                self.assertEqual(info["valid_frames"], 1)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
