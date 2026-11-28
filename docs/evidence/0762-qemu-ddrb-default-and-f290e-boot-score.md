# E0762 — Boot score for the DDR-B controller model on by default and ZERO rows at `0xF290E000`

Date: 2026-11-27 (port measurement and writing); renumbered 2026-11-28.

This record was first filed as E0733. Upstream took E0733 for a different record
(`0733-tests-gate-zma1-sidecar-test.md`), so it is renumbered to E0762; the measurements are
unchanged.

This record covers one emulator change ported onto the current upstream tree: the DDR-B controller
model (`ddrb-control`) is now enabled unless the machine option `ddrb-control=off` is given, and
one `0x1000` page at `0xF290E000` gets three ZERO-only rows. The change was first measured on the
accepted lead's older tree on 2026-11-25; this record states which numbers come from that older
tree and which were measured on the port to current upstream main. This record does not claim any
physical camera result.

## Observation

- An 80 s capture on the older build before the ZERO rows logged exactly three accesses in the
  `0xF290E000` page: `0x290e210`, `0x290e008` and `0x290e004`, each a single 4-byte write of 0,
  with no reads and no other offset in the page.
- That capture's baseline had 66 distinct unmodelled pairs, 5,394,507 unmodelled accesses and 42
  unmodelled reads; its first `unmodelled_top` entry was `read 0x1050a0`.
- On the current upstream base, the only clock/reset window at `0xF2900000` is `0x1000` bytes and
  ends at `0xF2900FFF`; no window covers `0xF290E000` (a search for that base found none), so the
  new page was not already modelled.

## Change

- **Default on:** the board file initialises `sony_a6400_ddrb_control` to `true`. The machine
  property description now says the model is "(on by default; ddrb-control=off disables it)".
  `ddrb-control=off` restores the old behaviour.
- **Three ZERO rows:** a new rule table `cxd90045_f290e_rules` holds `CXD90045_F292_ZERO` rows at
  `+0x004`, `+0x008` and `+0x210`. No row is a read-back row. The comments say each offset was
  write-only in the captured boot, with one write of 0, and that a read returns 0, the catch-all
  value.
- **One window:** `cxd90045_f292_create` creates `cxd90045.f290e` at `0xF290E000` with size
  `0x1000` and that table. Any other offset in the page still logs as unmodelled. The window is
  created unconditionally, so `ddrb-control=off` disables the DDR-B model but does not restore the
  old catch-all logging for the three ruled offsets `+0x004`, `+0x008` and `+0x210`.
- **DDR-B under pmic-reset, no double creation:** upstream creates DDR-B at `0xF0105000` when
  `ddrb-control` is on, and, inside the `pmic-reset` block, creates DDR-A at `0xF0104000` and a
  DDR-B fallback only when `ddrb-control` is off. With the default now on, the first creation is
  taken in every combination and the pmic-reset fallback is skipped, so exactly one DDR-B region
  is created. The first block passes the `pmic-reset` value as the self-refresh flag, so the
  self-refresh behaviour with `pmic-reset=on` is unchanged.
- **Tests:** a new static check (no QEMU boot) asserts the three rows, that the window is created
  once at `0xF290E000` with size `0x1000`, and that the default is on; its three tests pass. The
  liveview test pins one `start_qtest` argument to `sony-a6400,ddrb-control=off` and changes no
  assertion. The test named `test_default_machine_keeps_liveview_blocks_unmodelled` therefore no
  longer exercises the default machine; it would otherwise read the served DDR-B `+0xA0` value
  (`0x01000300`) instead of the old catch-all 0.

## Result

Older-tree columns are the score files recorded on 2026-11-25 when the change was first accepted;
they were not re-run here. The two port columns are two boots measured on 2026-11-27 on a build of
current upstream main with this change. Each column is one run; no values are averaged.

| Measure | Older baseline | Older after (rerun) | Older after (promotion) | Port run 1 | Port run 2 |
| --- | --- | --- | --- | --- | --- |
| Final sequencer step | 42 of 43, `0x897F` | same | same | 42 of 43, `0x897F` | 42 of 43, `0x897F` |
| Mismatch count | 0 | 0 | 0 | 0 | 0 |
| Distinct unmodelled pairs | 66 | 65 | 65 | 1 | 1 |
| Unmodelled reads | 42 | 41 | 41 | 1 | 1 |
| Accesses to unmodelled space | 5,394,507 | 1,200 | 1,236 | 2 | 2 |
| Reads of offset `0x1050a0` | present (first unmodelled read) | 0 | 0 | 0 | 0 |
| `t_reach` | 51.71 / 47.01 s | 47.52 / 51.5 s | 52.06 / 51.93 s | not measured | not measured |

- **Older tree:** the change removed exactly `read 0x1050a0` and added no distinct pair; distinct
  fell 66 to 65 and reads 42 to 41. All six runs ended at index 42 of 43, step `0x897F`, with 0
  mismatches. The final sequencer state was the same as the baseline; the comparison was accepted
  as coverage.
- **This port:** both boots ended at index 42 of 43, step `0x897F`, with 0 mismatches. Both logged
  one distinct unmodelled pair, a single read at offset `0x2900030` (two accesses total), and zero
  reads of `0x1050a0`. `0x2900030` is the clkrst3 offset beside `0xF2900000` that is deliberately
  left without a row; it is not part of this change. The port's counts are not comparable with the
  older tree's absolute totals, which came from a different build and input set.
- **Upstream control:** a separate upstream control boot at revision e6620d7 counted about 1.5
  million catch-all reads of offset `0x1050A0` with DDR-B off; enabling the model reduced that to
  one read. The port reproduces the served model: no catch-all read of `0x1050a0` in either run.
- **Timing:** this record makes no speed claim. `t_reach` was not measured on the port runs.

## Silent reads

The ZERO rows are silent on a read. A listed ZERO row returns 0 without logging, while an unlisted
offset in the same window still logs. A later read at `+0x004`, `+0x008` or `+0x210` would
therefore be hidden.

## Inputs

The boot inputs are camera-captured and are not in the repository, so these counts cannot be
reproduced without them.

Boot options of the port runs: `EVSEL_LATE_US=500`, `CAMPIG_ENABLE_READBACK=1`, `SGC1_EVENT_A=1`,
`GATE_FORCE_PROBE=1`, `LIVEVIEW_SOURCE=bars`, `ENTRY_FORCE_PROBE=1`, `WORK_DMA=1`, speed profile
`default`, `-icount shift=0,sleep=on`, and the captured `IDT_CAM` preload. The older-tree runs used
the same options. Every option is identical before and after.

## Build and commands

- The port was applied by hand to the current upstream board file; no cherry-pick and no merge.
- A copied QEMU build tree was rebuilt with `ninja -j3 qemu-system-arm`; the build succeeded.
- Port boots were sampled over QMP with two runs of the local measurement script, each ending when
  the sequencer had stayed at index 42 for 45 s. Both QEMU processes exited 0.
- New static check, run from the tree root:
  `python3 -m unittest emulators/qemu-a6400/tests/test_qemu_l0128b_f290e.py -q`; result `Ran 3 tests ... OK`.
- Liveview test: `python3 -m unittest tests.test_qemu_liveview -q`; result `Ran 8 tests ...
  OK (skipped=1)`.
- On the port-only build tree the full `qemu-a6400` test file set ran 17 passed, 3 failed, 0
  skipped. That line is not from the scored `send-L0128` build, whose suite log is 20 passed, 0
  failed. The three failures on the port-only tree are
  the three camcore `f302` tests, two checks each; they require the camcore block to log unlisted
  reads and writes as `.unmodelled`, which upstream does not do. The same three tests fail with the
  same counts (7/2, 14/2, 10/2) against an upstream build at revision e6620d7, and the only
  `qemu-a6400`/test change between e6620d7 and current upstream main adds an unrelated USB-debugger
  test. They are pre-existing upstream failures, not caused by this change.

## Limits

- Emulator only. This record claims no physical camera result.
- The port is coverage only: the final state stays at index 42, `0x897F`, 0 mismatches.
- The older-tree counts and the port counts were measured on different builds and inputs; only the
  direction (the `0x1050a0` read is gone and the final state is unchanged) is comparable.
- The write-only claim is a static classification of the captured traffic, observed to about 97 s
  only and not proven for the whole boot. A read returns 0, the catch-all value.
- A read at any of the three ruled offsets returns 0 silently, with no unmodelled record, so a
  needed value would not appear. Unlisted offsets in the page still log.
- Not checked: whether the firmware ever reads these offsets after the observed window, and any
  physical camera behaviour. This record does not compare the model with the physical hardware.
