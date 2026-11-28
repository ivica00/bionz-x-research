# ILCE-6400 open gate: test kit (OpenGateA1, firmware 2.00)

| | |
| :--- | :--- |
| **Status** | **Ran once on one `ILCE-6400` (2026-11-28, [E0757](../../../docs/evidence/0757-opengate-a1-captured-libmpr-hooks-and-fast-loop-kit.md)):** check READY, apply, ON gave APPLIED, OFF and putback gave STOCK. **The output stayed 16:9** (live view and clip) and the MP4 hooks were never called. 35 offline tests |
| **Tested on** | Nothing yet. The mod's av-cam.bin table step (`A5`, 4896 × 3376) comes from live RAM tests on one `ILCE-6400` that all restored to stock ([E0752](../../../docs/evidence/0752-a6400-open-gate-a12-to-a19-dual-pipe-live.md)); no 3:2 clip has been recorded |
| **Known issues** | **No 3:2 output.** The pipeline's output descriptors stay 3840 × 2160, the rear LCD words have no visible effect, and recording does not call the hooked `tkhd`/`stsd` functions (their live bytes do match). `tcp_debugd` serves one client at a time, so stop `watch` before other commands |
| **Not tested yet** | Everything on a camera: the screen step, recording, the MP4 headers, the power switch |
| **How to revert** | Switch `OpenGate A1` OFF in `MENU → Mods`, run `putback`. If it does not say STOCK: power off and pull the battery for 10 seconds |
| **What to send back** | The `export` zip and the console output, via a [physical test report](../../../.github/ISSUE_TEMPLATE/physical-test-report.yml) |

This kit wraps the [OpenGateA1](../opengate_a1.c) Mod Loader v2 mod in a guarded flow:
a read-only check that must say **READY** before anything is written, one staged file, a
read-only verify, a put-back that confirms stock, and a log zip for testers. The mod changes
three things, all in RAM:

1. **av-cam.bin tables** (337 sites: `fe_rec_0a`, `fe_rec_19`, `rcv_desc`, `size_rows[16]`, `rt_t14[16]`), the A5 4896 × 3376 sensor window.
2. **MP4 headers** in `libmpr.so` (`tkhd`, `stsd`): 3840 × 2160 becomes 3840 × 2560.
3. **Rear LCD box** in `viewUnified2.so`: the Movie 16:9 box (0, 60, 640, 360) becomes 3:2 (0, 26, 640, 428).

## MP4 header hooks (fixed in E0757)

The first build hooked `libmpr.so` at the **2.00 updater's** offsets (`0x87072C`, `0x870A74`). The
camera's own `libmpr.so` (`artifacts/dumps/a6400-live-lib/`) has the same bytes `0xF480` lower, at
`0x8612AC` and `0x8615F4` ([E0756](../../../docs/evidence/0756-opengate-a1-libmpr-hook-sites-and-guarded-test-kit.md)).
`build_v2_wsl.sh` now declares those (`0x079a52ac`, `0x079a55f4`). If a camera has yet another build,
`check` searches the live library and prints the corrected `--hook` line.

## Mod log lines

`/tmp_bt/mods/opengate_a1.log` (read by `verify`, `watch` and `export`):

| Line | Meaning |
| :--- | :--- |
| `ON av=w0151,a0000,x0000 vu=wwwwwwwwwwwwwwww ok=00000161` | Summary of one ON or OFF: av-cam sites written / already / mismatched, 16 screen words, total OK |
| `DIAG x i=0005 v=beef` | Up to 8 per ON/OFF: site 5 held 0xBEEF (`x` neither stock nor patched, `m` mmap failed, `v` no read-back) |
| `HOOK t kind=00000005 w=00000f00 h=08700a00 r=…` | A recording wrote the track header: 3840 wide, height 2160 → 2560 |
| `HOOK s hit=00000001 w=00000f00 h=08700a00 r=…` | The sample entry was rewritten the same way |

## Launchers

Windows: `windows\<name>.cmd`. macOS/Linux: `macos/<name>.command`. Or, from the repository root,
`python -m tools.kits.opengate_a6400 <name>`.

| Launcher | What it does | Writes to the camera? |
| :--- | :--- | :--- |
| `selftest` | Runs the whole flow against a simulated camera | No (no camera needed) |
| `check` | Hashes `viewUnified2.so` and `libmpr.so` on the camera, reads the 16 screen words and both hook sites from `im.elf` memory, checks that Mod Loader v2 is installed and that the mod is off. Verdict **READY** / **NOT READY** | No |
| `apply` | Needs a READY `check` from this power-on, less than 30 minutes old. Runs the check again, asks for a typed `YES`, and stages `OpenGateA1.mod` in `/tmp_bt/mods/` (read back and hash-checked) | **Yes: one file under `/tmp_bt`** |
| `verify` | Reads the mod's log (summary, DIAG, HOOK lines), the live screen words and both hook sites: **APPLIED** (needs the hooks installed), **STOCK** or **MIXED** with the failing sites named | No |
| `watch` | Prints a live timeline of new log lines, screen words, hooks, reload results and restarts; `--until APPLIED` stops when the mod is fully on | No |
| `reload` | With the mod ON: replaces it in place through the loader's reload request (no menu toggle) and reads the hooks and code back | **Yes: replaces that one file** |
| `cycle` | `--build`: WSL build + Unicorn gate; then `apply` and `watch --until APPLIED`, or `reload` if the mod is ON; then `verify` | Same as `apply` / `reload` |
| `putback` | After you switch the mod OFF: confirms **STOCK**, then removes the staged file (only if this kit staged it) | **Yes: removes that file** |
| `export` | Zips this kit's receipts with the mod log, the loader log, `/proc/version`, `/proc/uptime` and the tail of `/proc/klog` | No |

What the kit never does: read or write `/dev/mem` (`tools/devmem_policy.py` forbids DRAM, where the
av-cam.bin tables live), touch `/setting` or the Backup files, write flash, or switch the mod ON for
you. Because the av-cam.bin tables cannot be read, their identity is **inferred** from the matching
libraries (same captured camera); the mod's per-site stock guard and its log are the only av-cam checks.

## Fast loop for developers (Wi-Fi debugger)

With the camera on Wi-Fi and `tcp_debugd` build 8 or later running (`OP_MEM` is required):

```powershell
.\venv\Scripts\python.exe -m tools.kits.opengate_a6400 cycle --build --host 192.168.100.17
```

The first run stages the build and waits for you to switch the mod ON. While it stays ON, the next
`cycle --build` swaps in the new build through a reload request: open or move within the Sony menu once
so the loader picks it up. `watch --host …` in a second terminal shows every log line as it is written.

## Before you start

1. The camera menu shows **Ver. 2.00**.
2. **Mod Loader v2** is installed in this power-on ([mods/README.md](../../README.md), Option 2), and the USB debugger answers.
3. Build the mod (WSL): `wsl.exe --exec bash mods/opengate_a1/build_v2_wsl.sh artifacts/tests/opengate-a1`. The `apply` launchers use `artifacts/tests/opengate-a1/OpenGateA1.mod` unless you pass another path.
4. A charged battery. In the menu: XAVC S 4K, 24p, Proxy Recording off.

## Running it

1. `check`. Send the folder from `kit/camera_private/` back if it does not say READY.
2. `apply`. Type `YES` when asked.
3. On the camera: `MENU` → tab 7 (`Mods`) → switch **OpenGate A1** ON, leave the menu.
4. `verify`. It must say **APPLIED**. **MIXED** means some sites did not match: switch the mod OFF and run `verify` again.
5. Record 20–30 seconds of XAVC S 4K 24p. Check the clip on a computer, not on the camera.
6. Switch the mod OFF in `MENU → Mods`, then `putback`. It must say **STOCK**.
7. `export`, and attach the zip to a [physical test report](../../../.github/ISSUE_TEMPLATE/physical-test-report.yml).

**If the camera misbehaves** (restart, freeze, black live view) and still responds: run `verify` and
`export` before you pull the battery. If it does not respond, pull the battery.

## Please send back

- The `export` zip and the console output of every step.
- What the rear screen showed after ON (3:2 box? black? stripes?).
- The clip's size, frame rate and codec (MediaInfo text), and whether it shows more sensor height than a stock 4K clip from the same spot.
- What `verify` said after OFF, and after a power-off with the battery in.
