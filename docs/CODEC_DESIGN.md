# Codec10: a >8-bit video codec for the ILCE-6400 Cortex-A5

Status: design plus offline implementation (2026-11-28). Measured results are in
[E0758](evidence/0758-codec10-high-bit-depth-cortex-a5-codec.md). Nothing here records
video on the camera. The only physical step was a CPU-only benchmark under `/tmp_bt`.

## 1. Constraints (observed, not assumed)

| Fact | Source | Label |
| --- | --- | --- |
| 4 × Cortex-A5 r0p1 (`MIDR` part `0xC05`), `vfpv4 vfpv3d16`, **no NEON** | live `/proc/cpuinfo`; `/proc/1/auxv` `AT_HWCAP = 0x1E0D7` (NEON bit 12 = 0, VFPD32 bit 19 = 0, IDIVA bit 17 = 0) | PHYSICAL (E0758) |
| The QEMU `cortex-a5` model has the same ID: `MVFR1 = 0x01000011` ("no Advanced SIMD") | `emulators/qemu-a6400/patches/qemu-v11.1.3-sony-a6400.patch` | OFFLINE |
| No hardware integer divide | `AT_HWCAP` IDIVA = 0 | PHYSICAL |
| Linux owns only 112,544 kB of RAM, with about 1.2 MB free and 50 MB in page cache | live `/proc/meminfo` | PHYSICAL |
| Cores are about 17 % busy (out of the 4 × 250k jiffies of a running camera) before any recording | live `/proc/stat` | PHYSICAL (one sample) |
| Kernel `3.0.27_nl-rt106` SMP PREEMPT RT, Sony glibc 2.11 (no `clock_gettime` in libc) | `uname`, `tools/sony_sysroot` | PHYSICAL / OFFLINE |
| Every IMX284 mode outputs 11/12/14-bit Bayer, never 8-bit; the hardware AVC encoder stops at 8-bit 4:2:0 | map `img.recording-paths` C06, C18 (E0739) | OFFLINE |
| Movie RAW frames live in DDR-B (`0x80000000..0xC0000000`), owned by the LIRO RTOS, not by Linux | map C18 (E0739) | OFFLINE |
| `/dev/mem` reads of DRAM are forbidden (E0647 policy) | `tools/devmem_policy.py` | POLICY |

Consequences:

1. **No NEON on this body.** Every "NEON path" plan for the a6400 is void. The fast path uses what
   ARMv7-A always provides: ARMv6 SIMD32 (`UQSUB16`, `USAT16`, ...), `CLZ`, `UBFX`/`BFI` and
   barrel-shifted operands. A NEON variant is still built, but it is selected only when
   `AT_HWCAP` reports NEON (other BIONZ X bodies with an A5+MPE, or host test runs under QEMU
   `cortex-a9`).
2. **No division in the hot loop.** Rice parameters come from `CLZ`, not from dividing.
3. **Memory is the scarce resource, not just CPU.** A ring of three 1080p frames (16-bit) is
   12.4 MB, a tenth of Linux's RAM. The ring is small and pre-allocated, and the 4K "RAM burst" idea
   from map C19 cannot live in Linux memory: it would have to stay in DDR-B.

## 2. Why the >170 ms/core codecs failed

X-OCN-class or wavelet codecs spend roughly 50–100 operations per pixel (transforms with
multiplies, context modelling, arithmetic coding) and rely on wide SIMD. On a single-issue A5
without NEON that is more than 80 cycles per pixel: 2.07 MP × 80 ≈ 166 M cycles ≈ 170 ms at about
1 GHz. Codec10 limits itself to integer operations per pixel with no multiplies:

| Stage | Instructions per pixel (approx.) |
| --- | --- |
| Black offset (`UQSUB16`, 2 px per instruction) | 0.5 |
| Log curve (minifloat: `CLZ`, `SUB`, `USAT`, `LSR`, `ADD ... LSL`) | 5 |
| Tight pack (10-bit: 16 px → 5 words) | 2–3 |
| Optional MED predictor + zigzag + block-adaptive Rice | 10–14 |

## 3. Architecture

**Input:** a Bayer CFA frame of `uint16` samples (the tap the sensor produces), `in_bits` ∈ 10..16.
Grayscale (one plane, distance-1 prediction) is also supported. A 10-bit YUV 4:2:2 planar input
would reuse the grayscale stripe coder once per plane (a container format ID is reserved; not
implemented).

**Stage 1: offset.** `x' = max(x − (black − pedestal), 0)`. When the offset is zero this stage is
lossless. A small pedestal (default 64 DN at 14-bit) keeps the read-noise floor below black.

**Stage 2: curve (`curve_id`).**

- `0` linear: `c = x'` (lossless).
- `1` minifloat-`M`: `b = 31 − clz(x'|1)`, `e = max(b − M, 0)`, `c = (e << M) + (x' >> e)`.
  The curve is branchless and monotone and needs no lookup table (a 16K-entry `uint16` LUT would
  fill the whole 32 KB L1 D-cache). It is linear below `2^(M+1)` and halves its precision every
  octave above that, which follows photon shot noise.
  14-bit → 10-bit uses `M = 7`: steps of 1 DN up to 255, and 64 DN at 8192–16383 (relative step
  ≤ 1/128 = 0.011 EV). At full scale the shot noise σ ≈ √16383 ≈ 128 DN (at about 1 e⁻/DN) is
  larger than the step. The decoder reconstructs at the step midpoint.

**Stage 3: entropy (`mode`, chosen per stripe).**

- `0` PACK: an LSB-first little-endian bitstream of `coded_bits` per sample. 10-bit and 12-bit have
  unrolled 16-sample and 8-sample kernels.
- `1` RICE-LEFT / `2` RICE-MED: same-colour prediction (distance 2 in a Bayer CFA).
  MED = `clamp(a + b − c, min(a,b), max(a,b))`, the LOCO-I median written branchlessly. The
  residual is zigzag mapped, then coded with block-adaptive Rice (16 samples per block, 4-bit `k`
  from `CLZ` of the block sum, escape after 24 unary bits to `coded_bits + 1` raw bits).
- **Bounded output:** after every row the encoder checks whether the Rice stripe has grown past
  the PACK size and, if so, re-encodes that stripe as PACK. A frame is therefore never larger
  than the PACK frame plus 4 bytes per stripe, which is a hard ceiling for SD planning.

**Parallelism:** a frame is cut into horizontal stripes (`stripe_rows`, default 16, even). Each
stripe depends only on itself: the first two rows predict from the left only. Each one decodes on
its own, and workers take stripes from an atomic counter. In MED mode the working set is three
coded rows plus one input row, about 15 KB at 1920 px, which fits L1 next to the bitstream.

**Hot-loop memory:** none allocated. The caller supplies every buffer; `codec10.c` uses only
`<stdint.h>`/`<stddef.h>`, has no libc calls and no division, and compiles as SMOD v2
position-independent code. `build_wsl.sh` enforces this with the same relocation and helper checks
Mod Loader v2 applies.

## 4. Container (`.c10`): append-only and crash-safe

```
FileHeader  64 B  "C10V", v1, geometry, format, CFA, in/coded bits, curve, mode,
                  stripe_rows, black, white, pedestal, fps, CRC-32 of bytes 0..59
Frame*      FrameHeader 32 B: "FRM0", index, pts_us (u64), payload_size, n_stripes,
                  flags, Fletcher-2x32 of payload, CRC-32 of header bytes 0..27
            StripeTable n_stripes × u32 sizes
            Stripe*     4 B (mode, rows) + bitstream, padded to 4 B
```

The writer appends whole frames with a single `writev`. A reader accepts a frame only when its
header CRC, size and payload checksum all check out, so a torn last frame (power loss, a full card)
is dropped and every earlier frame still decodes. `--resync` scans for the next `FRM0` after
corruption in the middle. The payload uses a Fletcher checksum over 32-bit words (about
0.5 cycle/byte). CRC-32 on the A5 costs about 7 cycles/byte (≈ 435 M cycles/s at 62 MB/s), so it
covers only the 32-byte headers.

## 5. Bandwidth and timing budget

MB = 10⁶ bytes. UHS-I: 104 MB/s bus (SDR104); 40–70 MB/s realistic sustained (V30–V60 cards).
"Rice target" assumes the 1.5–1.8× lossless ratio that this design must show on noisy 14-bit
data (measured values are in E0758).

| Mode | MP/s | 16-bit MB/s | 12-bit pack | 10-bit pack | Rice target (10-bit) | Fits UHS-I sustained? |
| --- | --- | --- | --- | --- | --- | --- |
| 1920×1080 24p | 49.8 | 99.5 | 74.6 | 62.2 | 35–41 | 10-bit pack: marginal; Rice: yes |
| 1920×1080 30p | 62.2 | 124.4 | 93.3 | 77.8 | 43–52 | Rice only |
| 1996×1122 24p (IMX284 2K) | 53.7 | 107.5 | 80.6 | 67.2 | 37–45 | Rice only on V60 |
| 3840×2160 24p | 199.1 | 398.1 | 298.6 | 248.8 | 138–166 | **No** (above the 104 MB/s bus); RAM burst only |

CPU budget at 1080p24: 41.7 ms per frame. Target: **≤ 20 ms wall per frame across 2–3 workers**,
about 40–60 ms single-thread for a whole frame, so at least half of every frame interval stays
free for `im.elf`, LIRO and AF. That is about 29·f cycles/pixel single-thread at f GHz. 4K24 would
need about 4× that throughput from cores that are also serving the RTOS, so 4K continuous is ruled
out by CPU as well as by SD bandwidth.

## 6. SMOD v2 integration shape (not deployed)

- `codec10.c` builds as PIE with `-ffreestanding -nostdlib` and no absolute relocations, so it
  links into a Mod Loader v2 `.mod` (with `--reloc-layout` for any firmware references the wrapper
  adds).
- The capture side (a future hook on the RAW frame-done event) only enqueues a frame pointer into
  a single-producer/single-consumer ring. Encoder workers and one writer thread are separate, so a
  slow card drops whole frames (counted in the frame index gap) and never stalls the capture
  callback.
- The writer appends to a file only. It never writes raw blocks and touches no flash
  (`/dev/nflasha*` is excluded). Any SD write needs its own explicit authorisation (AGENTS.md §2).

## 7. Open boundary: the frame tap

The codec cannot reach a real >8-bit frame yet. Movie Bayer frames live in DDR-B, which `/dev/mem`
policy forbids, and FOPD/AIF0 W2 cap at 640×480 (E9105). The next concrete step is the map's F01
frontier: qualify the Movie-mode CamPig/IDT plus `0xF3030000` cRAW2 frame capture in QEMU, then
find a Sony-sanctioned shared-memory route (a DMM/UIPC buffer that Linux user space already maps)
that hands a DDR-B frame to a user process.
