# E0761 — A zero write to `0xF3040100` clears `0xF3040110` so CPU2 leaves its post-index-42 wait

Date: 2026-11-28.

This record covers one emulator change and its qtest guard. The clear is a **declared assumption**
about the firmware's own stop-and-wait sequence: no hardware capture shows it. It is not a claim
about physical hardware, and no camera was accessed.

## Observation

- After sequencer index 42 (last step `0x897F`), CPU2 polls `[0xF3040110]` in a loop at
  `0x637881D2-0x637881DE` and leaves only when it reads 0. The firmware writes 0 to `+0x100`
  (store at `0x63788398`) immediately before polling `+0x110`; the only writer of `+0x110` writes 1
  (PC `0x637883CC`, host 42.8-44.8 s), and the image has no path that writes 0 there.
- Before L0108 gave `+0x110` a read-back rule (upstream through PR #106, merged), the catch-all
  returned 0 and the wait passed at once.

## Declared assumption

A 4-byte write of 0 to `0xF3040100` clears the value stored at `0xF3040110`. This is the firmware's
stop-and-wait sequence as read from the image; no capture observes the hardware. Other writes to
`+0x100` and any write to `+0x110` keep the existing read-back behaviour. The rule applies only
inside the `cxd90045.f304` window at `0xF3040000`.

## Change

- `emulators/qemu-a6400/qemu-overlay/hw/arm/sony_a6400.c`: `cxd90045_f292_write()` clears
  `regs[0x110 >> 2]` when `base == 0xF3040000`, `w == 0x100`, `size == 4` and `v == 0`; comments
  mark it as the L0179 declared assumption. No other rule changes.
- `emulators/qemu-a6400/tests/test_qemu_f304.py`: four checks: `+0x110` reads 1; a nonzero `+0x100` write
  keeps it 1; a zero `+0x100` write makes it 0; a later `+0x110` write of 1 reads back 1.

## Method

Two full boots with `loop/scratch/L0179/boot_probe.py --seconds 130 --interval 1`, one QEMU at a
time, baseline environment and captured inputs-a, `-icount shift=0,sleep=on`: the port
(`send-L0179`, `qemu-system-arm` sha256 `5c1e4c5c3da0…`) and plain upstream `a9d986a`
(`db5ff344539c…`). Samples every 1 s; wait PCs `0x637881D2-E0`, `0x637883D4-D8`,
`0x63CE78E0/E6`; saved `lr 0x637881DD`; read `0xF3040110` and `0xF3040100`. The oracle ran
`oracle.py rerun send-L0179 loop/scratch/orch/send/request-send-L0179.json --repeat 2`.

## Result

| Check | send-L0179 | up-a9d986a |
| --- | --- | --- |
| Shell prompt | 11.56 s | 11.09 s |
| Index 42 span | 50.5-157.5 s | 51.0-157.0 s |
| CPU2 in the wait, post-42 samples | 0/108 | 70/107 (68/107 on L0178's stricter list) |
| Saved `lr 0x637881DD` | 0 | 50/107 |
| `0xF3040110` | 1 in 30 samples, then 0 in 78 (first 0 at 80.5 s) | 1 in all 107 |
| `0xF3040100` | 0 in 108 | 0 in 107 |

No serial fault lines in either log. Oracle: 42 / `0x897F` / 0 mismatched modules, 2 distinct
unmodelled reads (`0x1050a0`, `0x2900030`), `t_reach` 52.74 / 52.45 s; the final state equals the
baseline, so `moved` is false and `regressed` is false. The device tests pass 19/19 on the branch,
including `test_qemu_f304.py`.

## Red-team correction

Upheld with correction: the earlier `0xF3040118` read count (x91) is stale. The boot probe's
`qemu-d.log` has 131 sampler lines in both arms (one per sample); these are the sampler's own QMP
reads, not guest accesses. The guest reads `+0x110` only at `0x637883C8` and `0x637883D6`.

## Limits

- The clear is a declared assumption, not observed hardware behaviour, and it is tested on one
  captured input set.
- `up-a9d986a` is the same synced-overlay tree without this rule, not stock QEMU and not the
  certified baseline build; the split shows the rule is present or absent, not a scored delta.
- A write of 1 and then 0 to `+0x110` between two 1 s samples is not excluded.
- The L0176 snapshot resume did not reproduce the wait PC; the wait was reproduced in full boots.
- The change does not move the oracle: the final state and the picture are unchanged. It is kept by
  the human, not accepted by the scorer.

## Reproduce

Build with `emulators/qemu-a6400/setup-qemu.sh`, then run
`A6400_QEMU_DIR=<build> python3 -m unittest emulators/qemu-a6400/tests/test_qemu_f304.py -q`, and the boot
probe with the captured inputs. The boot probe, sampler and input set are private tooling.
