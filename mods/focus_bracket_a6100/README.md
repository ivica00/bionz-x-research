# FocusBracket.mod — ILCE-6100 (SMOD v2)

**Status: built and Unicorn-tested, not run in QEMU or on a camera.**

The mod adds focus bracketing to the **Mods** tab of Mod Loader v2 for the Sony ILCE-6100 (a6100, FW 1.00).

## Menu

| Item | Values |
| --- | --- |
| Mode | Landscape (step 8, 5 shots) / Macro (step 2, 40 shots, 1 s interval) / Custom |
| Step Width | 1 (Narrow) – 10 (Wide) |
| Shots | 5, 15, 35, 40, 60, 100 |
| Order | `0 → +` / `0 → − → +` |
| Exp Smooth | Off / On *(placeholder; not yet wired to AE lock)* |
| Interval | Shortest, 1, 2, 3, 5, 10 s |

## How it works

All addresses are `libObj.so` offsets (ILCE-6100 V1.00), from reference base `0x05d18000`.

- **Focus step:** `IssueCommandSync` (`0x1006d0`) on `ObjCamera` (`get_obj_handle(8)`, `0x37c4fb`), message `(0x3100, 0x6530)`, `PARAMID_STEP_FOCUS_DIR 0x9c40` = `0x68c` near / `0x68d` far.
- **Shutter:** `EventManager::push` S2 press/release, `0x15010032` / `0x15010031`.
- **Save sync:** hook on `WriteStillComp` (`0x8a74ac`). The trampoline replays `0x8a74ac..0x8a74b7` (pre-resolving `0x8a74b2` + `0x8a74b8` `add r1, pc` into `libObj + 0x01022efd`), replays `0x8a74ba` (`add r7, sp, #16`), and resumes at `0x8a74bc`.

## Build

```sh
mods/focus_bracket_a6100/build.sh        # -> mods/focus_bracket_a6100/FocusBracket.mod
```

The committed `FocusBracket.mod` is byte-identical to the output of this script.

SHA-256: `c0b73e5d154d49b34cb4d2a209e5edf39329d60ff7d454a67453943f3f752042`
