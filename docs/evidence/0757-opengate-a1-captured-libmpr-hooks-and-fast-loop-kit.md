# E0757 - OpenGateA1 hooks retargeted to the captured `libmpr.so`; diagnostic kit for a fast send/test/fix loop; `tcp_debugd` OP_MEM and a failed in-place swap

- **Date:** 2026-11-28
- **Scope:** Offline mod rebuild, Unicorn test and kit tests, plus a live `ILCE-6400` session limited to
  RAM-only Wi-Fi bring-up, `tcp_debugd` staging under `/tmp_bt`, and read-only reads. **The open-gate
  test itself did not run on the camera**: the camera dropped off USB and then off Wi-Fi before `check`.
- **Predecessor:** `E0756` (hook sites matched the updater build, not the camera).

## 1. Hook retarget (offline, verified)

| Input | SHA-256 |
| --- | --- |
| `artifacts/dumps/a6400-live-lib/libmpr.so` (captured camera build, 12,665,872 B) | `bb31351c…8ab8af` |
| `artifacts/tests/opengate-a1-build-2/OpenGateA1.mod` (4,660 B) | `16f7c974c1d5f0b25a8e7873a5ec9f421f9e8d0874d892ceaed3dd10c0e95c17` |

- Each 12-byte stock pattern occurs exactly once in the captured library: `tkhd` at `+0x8612AC`,
  `stsd` at `+0x8615F4` (the `E0756` shift of `-0xF480`). With the reference layout base `0x07144000`,
  `build_v2_wsl.sh` now declares `0x079a52ac` and `0x079a55f4`.
- Capstone disassembly of the captured `tkhd` function: the copied prologue
  (`push {r3-r7,lr}; mov r4,r0; ldr.w r5,[r0,#0xd8]; add r7,sp,#0`) is position-independent; the next
  instruction `cbnz r5, +0x8612FC` is taken only when `[model+0xd8] != 0` and that path returns `4`,
  which `tkhd_hook_c` reproduces before it calls the original. The copied `stsd` prologue
  (`push; sub sp,#0x4c; mov r4,r1; add r7,sp,#0; mov r6,r0; ldr r3,[r1]`) is position-independent.
- The packed hooks carry target `0x79a52ac` / `0x79a55f4`, 12 original bytes, fixup at byte 8 and the
  loader jump template `dff804f0 00bf00bf <addr>`.

## 2. Mod diagnostics (offline, Unicorn)

`opengate_a1.c` keeps its ON/OFF summary line unchanged and adds:

- `DIAG <x|m|v> i=<site> v=<value>` for the first 8 av-cam.bin sites that did not switch
  (`x` neither stock nor patched, `m` mmap2 failed, `v` written value did not read back);
- `HOOK t kind=… w=… h=<in<<16|out> r=…` and `HOOK s hit=…` once per `tkhd` / `stsd` call, so a
  recording shows whether the MP4 header height was rewritten (2160 → 2560).

`python mods/opengate_a1/test_mod_v2_unicorn.py artifacts/tests/opengate-a1-build-2`: PASS. It checks
ON/OFF restore, the `tkhd`/`stsd` results through the r4 trampoline path, both HOOK lines, the busy
`tkhd` path (returns 4, no HOOK line), and a planted foreign value (`0xBEEF` at site 5) giving
`DIAG x i=0005 v=beef`, `x0001`, and an untouched foreign value after OFF.

## 3. Kit (offline, 35 unit tests and selftest)

`tools/kits/opengate_a6400.py` changes, each aimed at a shorter send → test → fix loop:

| Change | Why |
| --- | --- |
| Logs are read through `OP_READ`, not `tail` | Live observation: the camera shell has no `tail` link (`sh: tail: not found`); `busybox tail` exists. The E0756 kit would always have read an empty mod log and reported `NONE` |
| `busybox sha256sum` first | Live: `sha256sum`, `md5sum`, `dd` exist only as BusyBox applets; without this the kit fell back to a 12 MB read-back per library |
| `probe_transport` (`OP_INFO`) | A `tcp_debugd` without `OP_MEM` is refused with the remedy instead of failing mid-check |
| Hook state `stock` / `hooked` / `foreign`, live search ±64 KiB and local-copy search | A shifted hook now produces the exact `--hook "0x…:…:…"` line for `build_v2_wsl.sh` |
| `verify` requires the hooks installed for `APPLIED` | ON with refused hooks is now `MIXED` with a pointer to the loader log |
| `DIAG` / `HOOK` lines decoded (site index → PA, stock, patched, found) | `MIXED` names the sites, not only counts |
| `klog_tail`, loader log tail, `reload.result` on failures; per-step `timings_ms`; reconnect-and-retry for read-only requests only | One run gives the context needed for the next fix |
| `watch` | Live timeline (mod log, loader log, screen words, hooks, reload results), restart detection by uptime regression, JSONL receipt |
| `reload` | Uses the installed core's reload request (`E0576`) to replace the enabled copy without the menu toggle; reads hooks and resident code back |
| `cycle --build` | WSL build into a new directory, Unicorn gate, then `apply` + `watch --until APPLIED` (or `reload` if the mod is ON), then `verify` |

`python -m unittest tests.test_opengate_a6400_kit`: 35 tests OK. `selftest` shows the shifted-hook
check suggesting `--hook "0x07995e2c:…:tkhd_hook_tramp"` and a refusal over an old `tcp_debugd`.

## 4. Live session (physical `ILCE-6400`, RAM only)

- Wi-Fi: `custom_os.launcher.camera_wifi --action up --ssid HUAWEI-g9XR` (passphrase entered by the
  user) → `192.168.100.17`, 2.4 GHz channel 11, −48 dBm; receipt `artifacts/tests/camera-wifi-sta-9/`.
  `/tmp_bt/wifi/wpa.conf` (derived PSK) was removed afterwards; `wpa_supplicant` stayed associated.
  MemFree dropped from 9,348 kB to about 2,736 kB.
- `tcp_debugd` `tcp-debugd-build-6` (`5e051211…`) answered `PONG … transport=tcp` (pid 1526);
  uptime monotonic 318.90 → 327.03 → 648.05 s.
- Read-only observations: `/tmp_bt/mlv2/core.bin` 11,136 B, SHA-256 `58a35fa1…2fcc2`, contains
  `/tmp_bt/mlv2/reload` (reload-capable) and `/tmp_bt/mods/mod_loader.log`; `/tmp_bt/mlv2/receipt`
  says `MLV2_SD_INSTALLED`, hooks were stock; library sizes match the pins; `im.elf` pid 149.
- One `OP_READ` of `core.bin` timed out once and succeeded on retry (0.28 s); hence the read-only retry.
- **Failed:** the in-place swap to `tcp-debugd-build-7` (`OP_MEM` added) left port 4640 with no
  listener (connection refused while ICMP answered). Cause in the source: `handle_exec` children
  inherited the listen socket (no `FD_CLOEXEC`), so the detached swap script and the new daemon held it
  and the new `bind()` failed, and the fallback restart failed the same way. Build 8 sets `FD_CLOEXEC` on
  the listen and client sockets and closes descriptors ≥ 3 in exec children. Not yet run on the camera.
- Before the swap the camera's USB debugger had already disappeared from the host (no `VID_054C`
  device); after the swap the camera stopped answering ICMP. No camera write beyond `/tmp_bt` happened.

## 5. First physical OpenGateA1 run (same session, after USB reconnect)

- `tcp_debugd` build 8 (`06e54122…`) started over USB as pid 1845; `OP_INFO` lists `mem`. The failed
  swap's log on the camera confirms the cause: `bind(4640) failed errno=98`, `SWAP_FALLBACK`. Uptime
  1672 s: no restart. Wi-Fi was still associated, so no passphrase was needed.
- **Live hook bytes:** `im.elf` (pid 149) holds `f8b50446d0f8d85000af0dbb` at `0x079a52ac` and
  `f0b593b00c4600af06460b68` at `0x079a55f4`. The E0756 blocker fix is confirmed on the camera.
- Kit `check --host 192.168.100.17`: `READY (stock preflight)` (transport 488 ms, library hashes 3316 ms,
  sites 41 ms, logs 125 ms). A first attempt failed on a TCP connect timeout; the kit now retries the
  connection three times and prints a `LINK:` message instead of a traceback.
- `cycle --mod artifacts/tests/opengate-a1-build-2/OpenGateA1.mod --yes`: staged 4,660 B (push 229 ms);
  the user switched the mod ON; `watch` saw `ON av=w0151,a0000,x0000 vu=wwwwwwwwwwwwwwww ok=00000161`,
  loader `mod_on 040d7000`, screen words `patched`, both hooks `hooked`; `verify` `APPLIED`. Total 53.4 s.
- **Recording (user report):** XAVC S 4K 24p recorded normally, but **both the live view and the clip are
  16:9**. No restart (uptime 1922 s afterwards).
- **The MP4 hooks never ran:** no `HOOK` line was logged during or after the recording, so neither the
  `tkhd` nor the `stsd` function at the hooked addresses was called. Only `im.elf` maps `libmpr.so`, and
  the hook page is `rwxp` (`079a5000-079a6000`), so the hooks sit in the right process. Whatever writes
  the XAVC S track header on this path is not these two functions (or not with these arguments).
- **The 16:9 output agrees with earlier live evidence:** `E0752` step `A12` (the 337-entry `A5` set plus
  more) already gave a "clean 16:9 live-view", and `E0753` notes that the id-0 `0xAC`/`0x34`/`0x1A4`
  descriptors still describe 3840 × 2160 (1984 × 2160 per pipe). OpenGateA1 changes the sensor window
  tables but not those output descriptors, so the pipeline still crops to 16:9. The 16 viewUnified2
  words read back as patched but had no visible effect on the rear LCD box.
- Not determined: whether the clip's framing differs from a stock 4K clip (a different vertical field of
  view would show that the `A5` window took effect). The clip header was not read: the SD card is mounted
  by im.elf's own file system, not by Linux, and it was not visible to the host.
- **Restore:** the user switched the mod OFF; the log shows `OFF av=w0151,a0000,x0000
  vu=wwwwwwwwwwwwwwww ok=00000161` (all 337 sites and 16 words back to stock, 0 mismatched). Kit
  `putback`: `STOCK`, hooks stock, staged `OpenGateA1.mod` removed; `export` zip
  `opengate_a6400-logs-20261128T104042Z.zip` (local-only). Uptime 2135.90 s: no restart in the session.
  `tcp_debugd`, the Wi-Fi helpers and the `wifi/` run files stay in `/tmp_bt` (RAM) for the next loop.
- **Kit gap found:** `tcp_debugd` serves one client at a time, so a running `watch` blocks every other
  kit command (they time out). Stopping `watch` freed the link.

## 6. Limits and next boundary

- OpenGateA1 applies cleanly and reversibly on paper (log, hooks, screen words), but it does **not**
  produce 3:2 output: the output descriptors stay 3840 × 2160 and the MP4 hooks are not on the recording
  path. Its premise (sensor tables + header rewrite + LCD box) is insufficient.
- Next: switch the mod OFF, `putback` and `export`; compare the clip's framing with a stock 4K clip;
  find the real XAVC S `tkhd`/`stsd` writer (for example by logging hooks on candidate callers in
  `libmpr.so`); and make the output descriptors part of the mod. For the kit: let `tcp_debugd` serve more
  than one client, or route `watch` and commands through one connection.
