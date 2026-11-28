# E0763 — Model returns the captured `0xF2900030` value `0x008E56FC` (observed on an ILCE-6100)

Date: 2026-11-28.

This record covers one emulator constant, read at `0xF2900030` in the clkrst3 bank. The value was
observed on an ILCE-6100, not on the emulated ILCE-6400, and it is a post-init idle value, not a
reset value. It is not a claim about the A6400 or about any physical camera behaviour beyond that
one observation.

## Observation

- The clkrst3 window at `0xF2900000` had rows only for nine write-side offsets. `+0x030` was
  deliberately left without a row because the guest branches on its read (F-0104).
- On 2026-11-26 the human read `0xF2900030` on their ILCE-6100 as `0x008E56FC` (idle; the no-lens
  status is inferred from the session-B protocol, not a field of that capture row; camera kernel
  3.0.27 build scm-354, 2021-04-06, against the emulated ILCE-6400 kernel build scm-348,
  2021-08-10). The capture files are private and are not in this repository (F-0170).
- This record uses only that one value. Other values from the same capture session are not part of
  this change and are not modelled here.

## Change

- `emulators/qemu-a6400/qemu-overlay/hw/arm/sony_a6400.c`: a new rule kind `CXD90045_F292_CONST` reads the
  constant stored in the rule, and writes are stored but not returned. One `+0x030` row carries
  `0x008E56FC`, labelled "observed on ILCE-6100, idle". The nine ZERO rows are unchanged, and
  unlisted offsets in the page still log as unmodelled.
- `emulators/qemu-a6400/tests/test_qemu_f290.py`: static checks for the ten rows, the nine ZERO rows, the
  CONST kind and value, and the CONST read path; no QEMU boot.

## Method

- qtest, no boot: `-M sony-a6400 -S -display none -serial null -qtest unix:<sock>` with the raw
  qtest protocol (`readl`, `writel`) through the address space the guest uses, one QEMU at a time.
  The probe is `loop/scratch/orch/send/l0170-qtest/qtest_probe.py`; `0xF2900004` is an existing ZERO
  row and serves as a control that writes reach the model.
- Oracle: `oracle.py rerun send-L0170 loop/scratch/orch/send/request-send-L0170.json --repeat 2`,
  three times, then compare with the baseline.

## Result

| Check | send-L0170 (`c01be062…`) | up-a9d986a (`db5ff344…`) |
| --- | --- | --- |
| `readl 0xF2900030` | `0x008E56FC` | `0x00000000` |
| `writel 0xF2900030 0xFFFFFFFF`, then read | `0x008E56FC` | `0x00000000` |
| `readl 0xF2900004`, then write `0x12345678`, then read | 0, 0 | 0, 0 |

- The write does not change the reply on the port: the CONST row is read-only. The ZERO control row
  also keeps its reply as expected.
- Oracle: 42 / `0x897F` / 0 mismatched modules, one distinct unmodelled read (`0x1050a0`),
  `t_reach` 52.71 / 52.67 s. The upstream control has two distinct unmodelled reads
  (`0x1050a0`, `0x2900030`). The final state is unchanged, so this is coverage, one address.
  All three reruns were admissible; 6/6 boots reached index 42, step `0x897F`, with 0 mismatches,
  but the score file cited here is the last rerun: it backs 2/2 boots, not 6/6. An earlier single
  boot did not identify the sequencer object and was not admissible; it did not recur in the quiet
  reruns.
- The device tests pass 19/19 on the branch, including `test_qemu_f290.py`.

## Limits

- Observed on an ILCE-6100, not an A6400; different camera and kernel build; one idle post-init
  sample; no reset value and no side effects are claimed.
- The qtest checks the model's reply, not a guest driver's use of the value.
- No speed claim: the `t_reach` difference is inside the oracle's 5 s sampling grid.
- A read of a ruled ZERO offset still returns 0 silently, as before; unlisted offsets still log.

## Reproduce

Build with `emulators/qemu-a6400/setup-qemu.sh`, run the qtest probe against the build, and run
`python3 -m unittest emulators/qemu-a6400/tests/test_qemu_f290.py -q`. The boot score needs the private
captured inputs.
