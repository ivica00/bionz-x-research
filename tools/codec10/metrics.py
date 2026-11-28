"""Quality metrics for the Codec10 curve on synthetic gradients and noisy charts.

    python -m tools.codec10.metrics            # table for 10/12/14-bit inputs

Definitions (all in sensor DN above black, noise model sigma^2 = K*s + read^2):
- psnr_db: 20*log10(white-black) - 10*log10(MSE) of uncode(code(x)) over every input level.
- worst_q_over_sigma: max over levels of (quantization step / sqrt(12)) / sigma(s), i.e. the
  quantization RMS relative to the photon+read noise already present. Levels where the curve keeps
  the sensor's own 1-DN step are excluded (that quantization is the sensor's, not the codec's), so a
  lossless configuration reports 0. Below about 0.3 the
  quantization noise adds less than 5 % to the total noise (sqrt(1 + 0.3^2) = 1.044).
- max_step_over_sigma: the largest reconstructed-level step wider than 1 DN, divided by sigma there
  (banding risk: steps below 1 sigma are dithered by the sensor's own noise).
- stops_with_8_codes: how many stops, counted down from white, still get at least 8 distinct codes.
- monotone: reconstruction is non-decreasing in the input.
"""
from __future__ import annotations

import json
import math
import random

from tools.codec10 import reference as ref


def curve_report(p: ref.Params, k_dn_per_e: float = 1.0, read_dn: float = 2.5) -> dict:
    lo, hi = p.black, p.white
    se, worst_q, worst_step, prev, monotone = 0.0, 0.0, 0.0, None, True
    level_codes: dict[int, set] = {}
    recon_prev = None
    for x in range(lo, hi + 1):
        c = ref.code_sample(p, x)
        y = ref.uncode_sample(p, c)
        se += (y - x) ** 2
        if recon_prev is not None and y < recon_prev:
            monotone = False
        recon_prev = y
        s = x - lo
        sigma = math.sqrt(max(s, 0) * k_dn_per_e + read_dn * read_dn)
        if p.curve == ref.CURVE_MINIFLOAT:
            e = max((c >> p.curve_m) - 1, 0)
            step = 1 << e
        else:
            step = 1
        if step > 1:
            worst_q = max(worst_q, step / math.sqrt(12) / sigma)
        if prev is not None and c != prev and step > 1:
            worst_step = max(worst_step, (y - ref.uncode_sample(p, prev)) / sigma)
        prev = c
        if s > 0:
            level_codes.setdefault(int(math.floor(math.log2(s))), set()).add(c)
    mse = se / (hi - lo + 1)
    top = int(math.floor(math.log2(hi - lo)))
    stops = 0
    for st in range(top, -1, -1):
        if len(level_codes.get(st, ())) >= 8:
            stops += 1
        else:
            break
    return {
        "in_bits": p.in_bits, "coded_bits": p.coded_bits, "curve": "mf" if p.curve else "linear",
        "m": p.curve_m, "black": p.black, "pedestal": p.pedestal,
        "psnr_db": round(20 * math.log10(hi - lo) - 10 * math.log10(mse), 2) if mse else math.inf,
        "worst_q_over_sigma": round(worst_q, 4), "max_step_over_sigma": round(worst_step, 4),
        "stops_with_8_codes": stops, "monotone": monotone,
    }


def noisy_chart_psnr(p: ref.Params, n: int = 20000, k_dn_per_e: float = 1.0, read_dn: float = 2.5,
                     seed: int = 7) -> dict:
    """Signal PSNR of noisy samples, with and without the curve: how much the codec adds."""
    rnd = random.Random(seed)
    se_codec, se_noise = 0.0, 0.0
    for i in range(n):
        s = (p.white - p.black) * 2.0 ** (-14 * rnd.random())
        x = p.black + s + rnd.gauss(0, math.sqrt(s * k_dn_per_e + read_dn * read_dn))
        x = int(min(max(round(x), 0), (1 << p.in_bits) - 1))
        y = ref.uncode_sample(p, ref.code_sample(p, x))
        se_codec += (y - x) ** 2
        se_noise += (x - (p.black + s)) ** 2
    full = p.white - p.black
    return {"psnr_codec_vs_input_db": round(20 * math.log10(full) - 10 * math.log10(se_codec / n + 1e-12), 2),
            "psnr_input_vs_truth_db": round(20 * math.log10(full) - 10 * math.log10(se_noise / n), 2),
            "noise_increase_pct": round(100 * (math.sqrt((se_noise + se_codec) / se_noise) - 1), 3)}


CONFIGS = [  # (in_bits, black, pedestal, curve, m)
    (14, 512, 64, ref.CURVE_MINIFLOAT, 7),   # 14 -> 10
    (14, 512, 64, ref.CURVE_MINIFLOAT, 9),   # 14 -> 12 (0..3071)
    (12, 128, 16, ref.CURVE_MINIFLOAT, 7),   # 12 -> 10
    (12, 128, 128, ref.CURVE_LINEAR, 0),     # 12 lossless
    (10, 64, 64, ref.CURVE_LINEAR, 0),       # 10 lossless
]


def main() -> int:
    for ib, black, ped, curve, m in CONFIGS:
        p = ref.Params(64, 32, in_bits=ib, black=black, pedestal=ped, curve=curve, curve_m=m)
        k = 2.0 ** (ib - 14)  # same full-well electrons: DN per electron scales with depth
        r = curve_report(p, k_dn_per_e=k, read_dn=2.5 * k)
        r.update(noisy_chart_psnr(p, k_dn_per_e=k, read_dn=2.5 * k))
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
