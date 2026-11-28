# E0758 - Codec10: >8-bit Bayer codec for the Cortex-A5 (no NEON); first physical timing

- **Date:** 2026-11-28
- **Scope:** Offline build and tests, qemu-arm user mode, plus a live `ILCE-6400` session limited to
  read-only `/proc` reads and a CPU-only benchmark binary under `/tmp_bt` (removed afterwards). No
  video recorded, no SD/flash writes, no hardware access by the benchmark. Design: `docs/CODEC_DESIGN.md`.
- **Status:** IN PROGRESS. `pack` meets 1080p24 real-time on 3 threads (25.2 ms wall, 62.22 MB/s); `bfl` and `rice` exceed the 41.67 ms 24p budget (see §3).

## 1. Platform facts (PHYSICAL)

- `/proc/cpuinfo`: 4 × Cortex-A5 r0p1, `vfpv4 vfpv3d16`, no `neon`. `/proc/1/auxv` `AT_HWCAP = 0x1E0D7`
  (NEON = 0, VFPD32 = 0, IDIVA = 0). Saved: `artifacts/codec10/camera/auxv_pid1.bin`.
- `/proc/meminfo`: MemTotal 112,544 kB, MemFree about 1.2–3 MB, Cached about 48–50 MB.
- Dependent-add calibration (`bench_codec --calibrate`): **about 520 MHz** effective core clock (an
  estimate, not a measured clock register).
- QEMU fidelity finding (OFFLINE): the patched `cortex-a5` model sets `MVFR1 = 0x01000011`
  (SIMDFMAC/FMAC field 0). Linux sets `HWCAP_VFPv4` only when `MVFR1[31:28] == 1`, and the live body
  reports `vfpv4`, so the model under-reports VFPv4 FMA. `vfma` code SIGILLs under QEMU and runs on
  the camera. Codec10 builds VFP code as `vfpv3-d16` to run on both. The model is not changed here.

## 2. Offline verification

- Build: `bash mods/codec10/build_wsl.sh artifacts/tests/codec10-build-3` (`-Wall -Wextra -Werror -O3`,
  zero warnings). It checks that the camera binaries need nothing newer than glibc 2.11 and contain
  no NEON instructions, and that the SMOD v2 PIE object (`11,152` B text in build-3) has no absolute or GOT
  relocations, no undefined symbols and no `.data`/`.bss`.
- `bench_codec_sony` sha256: build-1 `e92e731a…05aa`, build-2 `7a319191…ecd7`, build-3 `d30b6210…31c6`.
- Host, qemu-arm `cortex-a5` (Sony glibc via `-L`) and `cortex-a9` with the NEON path produced
  **byte-identical** `.c10` files for pack/left/med (build-1). Every `--verify` passed: the decoder
  reproduces the reference coded values, and the fast bitstream equals the reference bitstream.
- `tests/test_codec10.py`: 13 tests passed in WSL (`python3 -m unittest tests.test_codec10`),
  including the C encoder vs the independent Python reference (bit-exact on 7 configurations,
  including `bfl` mf and linear), every truncation point of a 3-frame file, corruption with resync,
  and DNG tags. Under the Windows venv the native test skips (the session sandbox denies `wsl.exe`).
- Curve metrics (`python -m tools.codec10.metrics`): 14→10 (M=7) adds 1.0 % noise, worst
  quantization 0.20 σ, maximum step 0.71 σ, monotone, 11 stops with ≥ 8 codes. 14→12 (M=9) adds 0.07 %.

## 3. Physical timing (one thread, CPU ms per frame, synthetic chart, 14-bit input)

Receipt: `artifacts/codec10/camera/session-1.log`. Every run had verify PASS where requested.

| Build | Mode / curve | 640×480 ns/px | ≈ cycles/px @ 520 MHz | ratio vs pack |
| --- | --- | --- | --- | --- |
| 1 | pack mf (separate code + pack) | 50 | 26 | 1.00 |
| 1 | rice-left mf | 120 | 63 | 1.44 |
| 1 | rice-med mf | 133 | 69 | 1.68 |
| 2 | **pack mf (fused + PLD)** | **32.6** | **17** | 1.00 |
| 2 | bfl mf | 83 | 43 | 1.37 |
| 3 | **bfl mf (`bfl_row_fused`, no `res[]`, local bitwriter)** | **66.6** | **35** | **1.37** |
| 2 | pack linear 14-bit (generic packer) | 56 | 29 | 1.00 |

Rice-med with 3 threads: 16.5 ms wall vs 44 ms single-thread (2.7× scaling).

### Live 1920×1080 (`2.07 MP`) scaling on `ILCE-6400` (`build-3`, `session-2.log`, 14→10 mf)

| Mode | Threads | Wall avg (ms) | Wall min (ms) | CPU/frame (ms) | MB/s @ 24p | Verify |
| --- | --- | --- | --- | --- | --- | --- |
| `pack` | 1 | 73.915 | 69.686 | 65.292 | 62.22 | — |
| `pack` | 2 | 36.034 | 33.492 | 62.985 | 62.22 | — |
| `pack` | **3** | **25.222** | **22.774** | 65.806 | **62.22** | PASS (`25.398 ms`) |
| `bfl` | 1 | 163.545 | 155.508 | 142.731 | 38.29 | — |
| `bfl` | 2 | 77.385 | 74.718 | 140.349 | 38.29 | — |
| `bfl` | 3 | 57.988 | 52.397 | 142.268 | 38.29 | PASS (`56.474 ms`) |

`pack` meets 1080p24 real-time (41.67 ms/frame budget) on 2–3 threads (`25.2 ms` wall on 3 threads);
`bfl` (`1.625×` smaller at 1080p) needs ~1.39× further speedup to hit 24p.

## 4. Next boundary

1. Further optimize `bfl` (or target 2K/1080p@12-15p) if `38.29 MB/s` vs `62.22 MB/s` SD bandwidth is required.
2. The frame tap (CODEC_DESIGN §7) is still open.
