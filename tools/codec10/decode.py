"""Decode a .c10 recording to a DNG sequence, raw uint16 frames or 16-bit PGM.

    python -m tools.codec10.decode IN.c10 OUTDIR [--format dng|raw16|pgm] [--resync]
                                   [--native PATH_TO_c10tool] [--coded]

Without --native the pure-Python reference decodes (fine for small frames and tests). With
--native, c10tool (built by mods/codec10/build_wsl.sh) decodes to raw16 first; this script then
validates its frame count against its own crash-safe scan and wraps each frame.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from array import array
from fractions import Fraction
from pathlib import Path

from tools.codec10 import dng, reference as ref


def _write_pgm(path: Path, w: int, h: int, maxval: int, samples) -> None:
    a = array("H", samples)
    if sys.byteorder == "little":
        a.byteswap()  # PGM 16-bit is big-endian
    path.write_bytes(f"P5\n{w} {h}\n{maxval}\n".encode() + a.tobytes())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--format", choices=("dng", "raw16", "pgm"), default="dng")
    ap.add_argument("--resync", action="store_true", help="skip corrupt regions instead of stopping")
    ap.add_argument("--coded", action="store_true", help="write coded values (no inverse curve)")
    ap.add_argument("--native", type=Path, help="c10tool binary for fast decoding (run directly or via wsl)")
    ap.add_argument("--model", default="Codec10 BIONZ X", help="DNG UniqueCameraModel")
    a = ap.parse_args(argv)
    data = a.input.read_bytes()
    scan = ref.scan(data, resync=a.resync)
    p = scan.params
    a.outdir.mkdir(parents=True, exist_ok=False)
    native_frames: dict[int, list[int]] = {}
    if a.native:
        tmp = a.outdir / "_raw16"
        tmp.mkdir()
        cmd = [str(a.native), "decode", str(a.input), str(tmp)] + (["--resync"] if a.resync else []) + \
              (["--coded"] if a.coded else [])
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        info = json.loads(out.strip().splitlines()[-1])
        if info["valid_frames"] != len(scan.frames) or info["errors"]:
            raise SystemExit(f"native decoder disagrees with the Python scan: {info}")
        for fr in scan.frames:
            raw = array("H")
            raw.frombytes((tmp / f"frame_{fr.index:06d}.raw16").read_bytes())
            native_frames[fr.index] = list(raw)
    fps = Fraction(p.fps_num, p.fps_den)
    for fr in scan.frames:
        samples = native_frames.get(fr.index) or ref.decode_frame(p, fr, coded=a.coded)
        stem = a.outdir / f"frame_{fr.index:06d}"
        if a.format == "dng":
            if p.format != ref.FMT_BAYER or a.coded:
                raise SystemExit("DNG output needs Bayer sensor values (drop --coded)")
            dng.write_dng(stem.with_suffix(".dng"), p.width, p.height, samples, cfa=p.cfa,
                          black=p.black, white=p.white, fps=fps, model=a.model)
        elif a.format == "pgm":
            maxval = (1 << (p.coded_bits if a.coded else p.in_bits)) - 1
            _write_pgm(stem.with_suffix(".pgm"), p.width, p.height, maxval, samples)
        else:
            stem.with_suffix(".raw16").write_bytes(array("H", samples).tobytes())
    print(json.dumps({"frames": len(scan.frames), "tail_bytes": scan.tail_bytes,
                      "skipped_bytes": scan.skipped_bytes, "width": p.width, "height": p.height,
                      "cfa": ref.CFA_NAMES[p.cfa], "in_bits": p.in_bits, "coded_bits": p.coded_bits}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
