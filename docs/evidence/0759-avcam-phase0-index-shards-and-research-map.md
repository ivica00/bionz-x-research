# E0759 - `av-cam.bin` Phase 0: ORIL static index, domain shards and deep-research map

- **Date:** 2026-11-28
- **Scope:** 100 % offline. Read-only analysis of one captured image on the host, plus one
  Ghidra 12.1.4 Headless raw import in WSL on a temporary project. No camera, USB,
  `/dev/mem` or SD operation was performed.
- **Status:** COMPLETE for Phase 0 (infrastructure + area map). No area has been
  researched; every maturity numerator is intentionally zero.
- **Deliverables:** `tools/research/avcam_image.py`, `avcam_domains.py`,
  `avcam_shards.py`, `avcam_indexer.py`, `avcam_dashboard.py`, `avcam_xrefs.py`,
  `avcam_ghidra.py`, `tools/research/ghidra/AvcamOrilImport.java`,
  [docs/AVCAM_RESEARCH_MAP.md](../AVCAM_RESEARCH_MAP.md), tests
  `tests/test_avcam_image.py`, `test_avcam_dashboard.py`, `test_avcam_xrefs.py`,
  `test_avcam_ghidra.py`.

## 1. Input (read-only)

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `artifacts/qemu-a6400/capture-20261117/run/av-cam.bin` | 16,674,456 | `90d515cfec8b936f1ba9a69dcac462816b880297752a652474d67c4bba39125e` |

The hash, size and ORIL base `0x635c6000` match the identity the research plan
declared, so the index describes the intended image and not another body's.

## 2. Exact commands and observed output

```powershell
.\venv\Scripts\python.exe -m tools.research.avcam_indexer build `
    artifacts\qemu-a6400\capture-20261117\run\av-cam.bin --out artifacts\analysis\avcam-map
```

```
indexed 58144 functions, 285 commands, 748 tags, 1220 MMIO pages in 23.4 s
report: artifacts\analysis\avcam-map\avcam_index_report.md
overall maturity 0.00 % -> artifacts\analysis\avcam-map\avcam_progress.md
```

```powershell
.\venv\Scripts\python.exe -m tools.research.avcam_dashboard --check --out artifacts\analysis\avcam-map
```

```
shards cover 285 commands, 748 tags, 1220 MMIO pages, 17 ABI items and 58144 functions
```

Measured regions (64 KiB block classification, thresholds in
`avcam_image.CODE_MIN_CALLS` / `CODE_MIN_CALL_RATIO` / `ZERO_BLOCK_FRACTION` /
`ASCII_BLOCK_FRACTION`):

| Kind | VA range | Size |
| --- | --- | ---: |
| code | `0x635c6000..0x63dd6000` | 8,454,144 B |
| data | `0x63dd6000..0x63e86000` | 720,896 B |
| rodata | `0x63e86000..0x63f26000` | 655,360 B |
| data | `0x63f26000..0x63f46000` | 131,072 B |
| rodata | `0x63f46000..0x63f86000` | 262,144 B |
| data | `0x63f86000..0x64046000` | 786,432 B |
| bss | `0x64046000..0x644c6000` | 4,718,592 B |
| data | `0x644c6000..0x645ace98` | 945,816 B |

Header measurement: 9 vector words `0x635c6044`, `0x635c6058`, `0x635c60b4`,
`0x635c6068`, `0x635c6078`, `0x635c6088`, `0x635c6098`, `0x635c609c`,
`0x635c60a0` at `+0x0c..+0x2c`; header ends `+0x30`; a 20-byte ARM stub
(`ldr r0,[pc,#0x8a0]; mov r1,#0xff; strb r1,[r0,#0x21]; ldr r0,[pc,#0x898];
bx r0`) ends `+0x44`, where Thumb code begins. The entry branch from `+0x000`
targets `0x635c6030`, inside the stub.

Index counts (`avcam_index_report.md`): 58,144 candidate functions (57,940
prologue `PUSH` with `lr`, 204 starts from vectors/commands only; 500 distinct
command handler/checker functions); 301,722 `BL`/`BLX` edges, 59,399 distinct
targets, 33,665 targets inside a known function; 171,061 literal-pool loads;
21,046 NUL-terminated printable strings outside code; 748 distinct `[TAG]`
tokens in 5,531 occurrences; 4,652 MMIO literals over 1,220 distinct 4 KiB pages
in `0xf0000000..0xfcffffff`; 285 command records at file offset `0xfb13c8`
(VA `0x645773c8`), ids `0x4000..0x413a`.

Function→string references: **10,837** references over 7,625 strings and 5,465
functions. Direct literal 167 (142 strings), PIC base register 10,437 (7,333
strings), indexed pointer table 233 (153 strings). 2,103 of 4,177 tagged strings
are referenced, covering **511 of 748 tags**. 6,470 strings have their address
stored as a 4-byte word (7,330 words; `string_pointer` table).

Corrected against the first plan: code ends at `0x63dd6000` (not `0x63ebxxxx`);
the string count under the NUL-terminated method is 21,046 (not 44,036); tags
are 748, not 774; `.bss` is 4.5 MiB from `0x64046000`; `.data` starts at
`0x644c6000`. The 285-command table, the 9 vectors and the
`0xf0000000..0xfcffffff` MMIO range are confirmed.

### 2.1 Reproducibility

A second `build` into `artifacts/analysis/avcam-map-repeat` produced
byte-identical outputs: 14 of 14 compared files (index report and JSON, progress
report and JSON, all ten shards) have the same SHA-256, at 56.9 s and 56.4 s
wall time. Wall-clock time is printed by the CLI and never written into an
artifact, so the reports, shards and dashboard are a pure function of the image
bytes.

### 2.2 Ghidra raw import (plan task 0.1)

```powershell
.\venv\Scripts\python.exe -m tools.research.avcam_ghidra stage  --out artifacts\analysis\avcam-map
wsl.exe bash "/mnt/c/Users/Administrator/Desktop/a6400 project/artifacts/analysis/avcam-map/ghidra/import_command.sh"
.\venv\Scripts\python.exe -m tools.research.avcam_ghidra verify --out artifacts\analysis\avcam-map
```

`import_command.sh` runs Ghidra 12.1.4 Headless with:
`-processor ARM:LE:32:v7 -loader BinaryLoader -loader-baseAddr 0x635c6000
-noanalysis -postScript AvcamOrilImport.java ... -postScript
BoundedThumbProcedures.java ...`. Observed output:

```
Using Language/Compiler: ARM:LE:32:v7:default (ProgramLoader)
AvcamOrilImport: 512 functions, 8 blocks, sha256=90d515cf... (GhidraScript)
BOUNDED_THUMB "0x6395103a" "completed"
BOUNDED_THUMB "0x6375160a" "completed"
```

`verify` receipt `artifacts/analysis/avcam-map/ghidra/import_receipt.json`:
status PASS, Ghidra 12.1.4, `image_base 0x635c6000`, 8 blocks, 512 functions,
16 by-address decompiles. All checks PASS: language, image base, source SHA-256,
block count, function count, decompile status/preview, block layout, bounded
decompile and bounded pseudocode. Examples: handler `0x63bdfd40` → 111 C lines,
`0x635c60b4` → 79 lines, `0x63bdf05c` → 70 lines; bound queries
`0x6395103a` (746 body bytes) and `0x6375160a` (116 body bytes) completed with
0 flows outside their bounds. The raw program's `executable_sha256` equals the
file SHA-256, so the existing `BoundedThumbProcedures` identity guard accepts
the same value the index records.

The eight blocks are the measured regions with code executable, rodata
read-only, and data/bss read-write; the single `BinaryLoader` block is replaced,
not duplicated. The host used here denies a `wsl.exe` grandchild started from
Python (WSL `E_ACCESSDENIED`), so the shell script path was used and the receipt
records `"mode": "verified-from-summary"`; the checks are identical.

### 2.3 Pointer-flow string xrefs (plan task 0.2)

`tools/research/avcam_xrefs.py` adds, per function, a constant tracker over the
same Thumb stream: literal loads, `movw`/`movt` pairs, `mov`, `add`/`sub` with
immediates or registers, `adr`, the PIC idiom `ldr rD,[pc,#k]; add rD,pc`, known
base loads with one level of memory dereference, and indexed table scans.
Computed addresses are accepted only when they land exactly on an indexed string
start; that rule removes misdecoded-data false positives (an earlier candidate
variant produced 34 refs whose sites disassembled as data).

Validation on the real image:

| Resolution | Refs | Functions | Strings |
| --- | ---: | ---: | ---: |
| direct literal (the previous floor) | 167 | 46 | 142 |
| PIC base register | 10,437 | 5,403 | 7,333 |
| indexed table | 233 | 20 | 153 |
| total | 10,837 | 5,465 | 7,625 |

Three sites were disassembled independently and their arithmetic checked:
`0x635c78ec` (`ldr r0,[pc,#0x50]` = `0x8c932f`; `add r0,pc` with PC
`0x635c78f0` → `0x63e90c1f`, the `[ADF]AdfDebugSystemFrameEdge` string),
`0x635c79a0` (`0x8ca5c0 + 0x635c79a4 = 0x63e91f64`, `[ADF]Audio OutPin Not
Connect!`) and `0x635cb398` (`0x92e6aa + 0x635cb39c = 0x63ef9a46`, the `LC:
SCANLESS Data...` string). Each is a real `ldr`+`add pc` sequence, not a
misdecode.

The pointer index is exhaustive for 4-byte addresses: 7,330 aligned words equal
an indexed string start, covering 6,470 strings (mostly initialised RW
descriptors). The 13,421 strings with no code reference therefore have no stored
pointer either, or their descriptor is read by a generic helper that takes it as
an argument; resolving those needs interprocedural object typing and remains
open (§5.1 of the map).

## 3. Shard coverage and the starting percentage

| Metric | Weight | Denominator | Fresh maturity |
| --- | ---: | ---: | ---: |
| ORIL ABI & boot | 15 % | 17 (9 vectors + 8 contracts) | 0.00 % |
| `av_command_table` | 20 % | 285 | 0.00 % |
| `[TAG]` tokens | 15 % | 748 | 0.00 % |
| Candidate functions | 30 % | 58,144 | 0.00 % |
| MMIO pages | 20 % | 1,220 | 0.00 % |
| **Overall** | 100 % | | **0.00 %** |

Domain scope as written to the shards: 1A 226 functions / 121 pages / 17 ABI;
1B 218 / 0 / 285 commands; 1C 336 / 294; 2A 20 / 0 / 25 tags; 2B 167 / 53;
2C 156 / 24; 2D 95 / 5; 2E 50 / 32; 2F 94 / 52; 9R 56,782 / 639 / 618 tags.
The xref pass raised attributed functions from 881 to 1,362; the residue still
holds 56,782 functions that need decompilation to place.

The plan estimated the skeleton would read about 10 %. That estimate described
infrastructure readiness, not evidence; this record reports 0.00 % because every
item is `UNMAPPED`. The skeleton fills the denominators and assigns every item
to exactly one shard, which is what a parallel fan-out needs; it does not
pretend that finding an item maps it.

## 4. Tests

```powershell
.\venv\Scripts\python.exe -m unittest tests.test_avcam_image tests.test_avcam_dashboard `
    tests.test_avcam_xrefs tests.test_avcam_ghidra -q
```

38 tests pass. They cover: header parsing and its refusal of bad magic/version/
short input; block classification on a constructed image (code/rodata/bss/data);
Thumb prologue and `BL` decoding (a test-side encoder round-trips through the
scanner); literal loads resolving to a string and an MMIO value; `[TAG]`
extraction; determinism of two scans; domain tag/window/first-match rules and
the longest-pattern rule (`RcvRMC` beats `RC`); fail-closed dashboard validation
for a missing item, a duplicate, an invalid state and a function-total mismatch;
the weighted maturity arithmetic; an end-to-end synthetic pipeline where a fresh
skeleton reads 0.00 % and one `DECOMPILED` command moves the total by exactly
`0.20 × 0.6 / 285`; the xref fixture for all three resolutions (literal base,
indexed table, `adr`), the no-invention case and the pointer index; and the
Ghidra staging (region permissions, both decompiler scripts in the command,
decompile addresses first in the function list) with accept/reject receipt
checks.

The full offline suite (`unittest discover -s tests -q`) reports 2,948 tests,
2 failures, 159 skips. Both failures are local-environment issues unrelated to
this change, unchanged from before it: the local `apps/camera-daemon/build/`
binary differs from the E0476 pinned artifact, and
`find_pmca_dir(root=tmp)` finds the workspace `Sony-PMCA-RE/` checkout. No test
involving the av-cam modules fails.

## 5. Inference and limits

- Function starts are candidates. Data tables inside a code block decode as
  instructions; the count of 58,144 is a prologue-plus-entry count, not a
  decompiled function list.
- Domain attribution of functions is a static-signal heuristic (MMIO literal
  windows and string references, then a two-thirds callee majority for up to
  four rounds). It attributes 1,362 functions; 56,782 remain in the residue and
  require decompilation to place.
- Only 30 of 285 command handlers reach a function with a domain signal, so all
  commands stay in the dispatcher shard (1B) with the signal recorded as a
  candidate. Routing commands to subsystems is research, not Phase 0 output.
- String references are intraprocedural. 7,625 of 21,046 strings (511 of 748
  tags) have a function reference; the rest have no stored pointer or are read
  through a descriptor parameter, so they need interprocedural typing. The exact
  start rule rejects computed addresses that land inside a string.
- The Ghidra import is a bounded sample (512 functions) used to prove the raw
  path, not a full analysis; the full candidate list is staged in
  `ghidra/functions.csv` for later decompilation. No symbol or type is invented.
- The persistent `symbol_index` daemon still opens ELF programs; pointing it at a
  saved raw project is a separate integration. The headless path and the
  existing `BoundedThumbProcedures` decompiler are validated here, which is what
  a by-address worker needs.
- `avcam_progress.md`, `avcam_index_report.md`, the SQLite map, the shards and
  the Ghidra receipt live under `artifacts/` (git-ignored) and are regenerated
  by the documented commands; `docs/AVCAM_RESEARCH_MAP.md` is the tracked
  summary.

## 6. Next boundary

Run Phase 1 on the 1A and 2A shards: decompile the nine vector targets and the
sequencer entry functions (found from `[STATECACHE]`/`[DBGCMD]` strings and from
callers of the 1B handlers) with the now-working raw import, record the ABI
contracts in the 1A shard, and confirm each finding with a `qemu_tcg_trace` run
on the saved `sony-a6400` snapshot. The interprocedural xref expansion
(argument flow for descriptor readers) is the other open infrastructure item.
Nothing in this record authorises a camera-side run.
