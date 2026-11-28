"""ILCE-6400 open-gate test kit for the OpenGateA1 mod (mods/opengate_a1).

Commands (run from the repository root; add --host <camera IP> to use the Wi-Fi TCP debugger):

    python -m tools.kits.opengate_a6400 check     # read-only: VERDICT READY / NOT READY
    python -m tools.kits.opengate_a6400 apply     # needs a fresh READY check; stages the .mod in /tmp_bt/mods
    python -m tools.kits.opengate_a6400 verify    # read-only: mod log, DIAG/HOOK lines, live screen words and hooks
    python -m tools.kits.opengate_a6400 watch     # read-only: live timeline of log lines, sites, hooks, restarts
    python -m tools.kits.opengate_a6400 reload    # replaces the enabled mod in place (Mod Loader v2 reload request)
    python -m tools.kits.opengate_a6400 cycle     # build + Unicorn gate + (apply | reload) + watch + verify
    python -m tools.kits.opengate_a6400 putback   # after you switch the mod OFF: confirms stock, removes the staged .mod
    python -m tools.kits.opengate_a6400 export    # read-only: zips this kit's receipts and a log snapshot
    python -m tools.kits.opengate_a6400 selftest  # no camera: runs the whole flow against a simulated camera

What the kit touches on the camera:
  * check / verify / watch / export only read: /proc files, the im.elf process memory (debugger OP_MEM),
    library files, and logs under /tmp_bt. No /dev/mem access at all: tools/devmem_policy.py forbids
    /dev/mem reads of DRAM, where the av-cam.bin tables live.
  * apply writes one file, /tmp_bt/mods/OpenGateA1.mod (read back and hash-checked). The mod itself
    is switched ON by the user in MENU -> Mods; Mod Loader v2 must already be installed.
  * reload writes the same file through a .part + rename and publishes /tmp_bt/mlv2/reload; the loader
    core swaps the enabled copy on the Sony UI thread (E0576) and the kit reads the hooks back.
  * putback removes that file again, only after the mod's log reports a full OFF.

The site tables are parsed from mods/opengate_a1/opengate_a1.c and build_v2_wsl.sh, so the kit and
the mod cannot drift apart. Receipts go to mods/opengate_a1/kit/camera_private/ (ignored by Git).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
MOD_DIR = ROOT / "mods" / "opengate_a1"
KIT_DIR = MOD_DIR / "kit"
MOD_SOURCE = MOD_DIR / "opengate_a1.c"
BUILD_SCRIPT = MOD_DIR / "build_v2_wsl.sh"
UNICORN_TEST = MOD_DIR / "test_mod_v2_unicorn.py"
IDENTITY_FILE = KIT_DIR / "identity.json"
PRIVATE_DIR = KIT_DIR / "camera_private"

REMOTE_MOD = "/tmp_bt/mods/OpenGateA1.mod"
REMOTE_LOG = "/tmp_bt/mods/opengate_a1.log"
REMOTE_LOADER_LOG = "/tmp_bt/mods/mod_loader.log"
RELOAD_REQ = "/tmp_bt/mlv2/reload"
RELOAD_RESULT = "/tmp_bt/mlv2/reload.result"
READY_MAX_AGE_S = 30 * 60
MAX_MOD_BYTES = 1024 * 1024
MAX_LOG_BYTES = 64 * 1024
HOOK_SEARCH_WINDOW = 0x10000  # +/- bytes around the expected hook offset searched in live memory
OP_INFO = 1  # tools.camera_debugger.OP_INFO
OP_EXEC = 3  # tools.camera_debugger.OP_EXEC
OP_PS = 6  # tools.camera_debugger.OP_PS
ENOENT = 2
# Mod Loader v2 writes `ldr.w pc, [pc, #4]; nop; nop; <address>` over a hooked target (SMOD v2 template).
HOOK_JUMP_PREFIX = bytes.fromhex("dff804f000bf00bf")
# exec commands the kit may repeat after a dropped link: they only read.
READONLY_EXEC_PREFIXES = ("cat ", "test -f ", "busybox sha256sum ", "sha256sum ", "busybox tail ", "ls ")


class KitError(RuntimeError):
    """A refusal: the kit stops before doing anything it cannot justify."""


# --------------------------------------------------------------------------- mod specification

@dataclass(frozen=True)
class AvPatch:
    pa: int
    stock: int
    patched: int


@dataclass(frozen=True)
class Hook:
    name: str
    ref_va: int
    stock: bytes
    lib: str
    offset: int


@dataclass(frozen=True)
class ModSpec:
    av_patches: tuple[AvPatch, ...]
    vu_offsets: tuple[int, ...]
    vu_words: tuple[tuple[int, int, int], ...]  # (cell offset, stock, patched)
    hooks: tuple[Hook, ...]

    @property
    def expected_ok(self) -> int:
        return len(self.av_patches) + 2 * len(self.vu_offsets)


_AV_ROW = re.compile(r"\{\s*0x([0-9A-Fa-f]+)u?\s*,\s*(\d+)u?\s*,\s*(\d+)u?\s*\}")
_HOOK = re.compile(r'--hook\s+"0x([0-9A-Fa-f]+):([0-9A-Fa-f]+):([A-Za-z0-9_]+)"')
_MAP_LINE = re.compile(r"^([0-9a-f]+)-([0-9a-f]+)\s+(\S+)\s+([0-9a-f]+)\s+\S+\s+\d+\s+(\S+)\s*$")
_SWAP = re.compile(r"swap_proc_u32\(cell \+ 0x([0-9A-Fa-f]+)u,\s*enable \? 0x([0-9A-Fa-f]+)u : 0x([0-9A-Fa-f]+)u")


def parse_maps(text: str) -> dict[str, int]:
    """Base address of each file mapped r-xp at file offset 0."""
    bases: dict[str, int] = {}
    for line in text.splitlines():
        m = _MAP_LINE.match(line.strip())
        if m and m.group(3).startswith("r-x") and int(m.group(4), 16) == 0:
            bases.setdefault(m.group(5), int(m.group(1), 16))
    return bases


def parse_mod_spec(source: str, build_script: str, reference_layout: str) -> ModSpec:
    count = re.search(r"#define\s+AV_PATCH_COUNT\s+(\d+)", source)
    table = re.search(r"AV_PATCHES\[AV_PATCH_COUNT\]\s*=\s*\{(.*?)\n\};", source, re.S)
    if not count or not table:
        raise KitError("could not find AV_PATCHES in opengate_a1.c")
    av = tuple(AvPatch(int(a, 16), int(s), int(p)) for a, s, p in _AV_ROW.findall(table.group(1)))
    if len(av) != int(count.group(1)):
        raise KitError(f"AV_PATCHES has {len(av)} rows, AV_PATCH_COUNT says {count.group(1)}")
    vu_block = re.search(r"VU_CELL_OFFS\[\d+\]\s*=\s*\{(.*?)\};", source, re.S)
    if not vu_block:
        raise KitError("could not find VU_CELL_OFFS in opengate_a1.c")
    vu = tuple(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]+)u?", vu_block.group(1)))
    # swap_proc_u32(cell + 0x0Cu, enable ? <expected-when-ON = stock> : <patched>, ...)
    words = tuple((int(o, 16), int(stock, 16), int(patched, 16)) for o, stock, patched in _SWAP.findall(source))
    if len(words) != 2:
        raise KitError("expected two viewUnified2 word swaps in opengate_a1.c")
    bases = parse_maps(reference_layout)
    hooks = []
    for va, stock, name in _HOOK.findall(build_script):
        ref_va = int(va, 16)
        owner = next(((lib, base) for lib, base in sorted(bases.items(), key=lambda kv: -kv[1]) if base <= ref_va),
                     None)
        if owner is None:
            raise KitError(f"hook {name} at 0x{ref_va:08x} is outside the reference layout")
        hooks.append(Hook(name, ref_va, bytes.fromhex(stock), owner[0], ref_va - owner[1]))
    return ModSpec(av, vu, words, tuple(hooks))


def load_mod_spec(root: Path = ROOT) -> ModSpec:
    identity = load_identity(root)
    layout = (root / identity["mod_loader"]["reference_layout"]).read_text(encoding="utf-8")
    return parse_mod_spec(
        (root / MOD_SOURCE.relative_to(ROOT)).read_text(encoding="utf-8"),
        (root / BUILD_SCRIPT.relative_to(ROOT)).read_text(encoding="utf-8"),
        layout,
    )


def load_identity(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / IDENTITY_FILE.relative_to(ROOT)).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- mod log

_LOG = re.compile(r"^(ON|OFF) av=(?:w([0-9a-f]{4}),a([0-9a-f]{4}),x([0-9a-f]{4})|(err)) "
                  r"vu=(nomap|[a-z]{16}) ok=([0-9a-f]{8})$")
_DIAG = re.compile(r"^DIAG ([xmv]) i=([0-9a-f]{4}) v=([0-9a-f]{4})$")
_HOOK_LINE = re.compile(r"^HOOK ([ts]) (?:kind|hit)=([0-9a-f]{8}) w=([0-9a-f]{8}) h=([0-9a-f]{8}) r=([0-9a-f]{8})$")
_DIAG_MEANING = {"x": "neither stock nor patched", "m": "mmap2 of the page failed",
                 "v": "written value did not read back"}
_VU_MEANING = {"w": "written", "a": "already", "x": "unexpected value", "p": "mprotect failed",
               "v": "write did not read back"}


@dataclass(frozen=True)
class LogLine:
    action: str
    written: int
    already: int
    mismatched: int
    av_error: bool
    vu: str
    ok: int

    @property
    def vu_complete(self) -> bool:
        return self.vu != "nomap" and all(c in "wa" for c in self.vu)


def parse_log_line(line: str) -> LogLine | None:
    m = _LOG.match(line.strip())
    if not m:
        return None
    action, w, a, x, err, vu, ok = m.groups()
    return LogLine(action, int(w or "0", 16), int(a or "0", 16), int(x or "0", 16), bool(err), vu, int(ok, 16))


def last_log_line(text: str) -> LogLine | None:
    for line in reversed(text.strip().splitlines()):
        parsed = parse_log_line(line)
        if parsed:
            return parsed
    return None


def describe_diag(line: str, spec: ModSpec, action: str | None) -> str | None:
    """Human form of a mod DIAG line: which av-cam.bin site, what was expected, what was found."""
    m = _DIAG.match(line.strip())
    if not m:
        return None
    kind, index, value = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
    if index >= len(spec.av_patches):
        return f"site #{index} (outside the {len(spec.av_patches)}-row table): {_DIAG_MEANING[kind]}"
    site = spec.av_patches[index]
    expected = (site.stock if action == "ON" else site.patched) if action else None
    found = "n/a" if kind == "m" else str(value)
    return (f"site #{index} PA 0x{site.pa:08X}: {_DIAG_MEANING[kind]}; found {found}, stock {site.stock}, "
            f"patched {site.patched}" + (f", expected {expected} before {action}" if expected is not None else ""))


def describe_hook_line(line: str) -> str | None:
    """Human form of a mod HOOK line (one per tkhd/stsd call while recording)."""
    m = _HOOK_LINE.match(line.strip())
    if not m:
        return None
    tag, first, w, h, r = m.group(1), int(m.group(2), 16), int(m.group(3), 16), int(m.group(4), 16), \
        int(m.group(5), 16)
    h_in, h_out = h >> 16, h & 0xFFFF
    if tag == "t":
        return f"tkhd kind={first} {w}x{h_in} -> {w}x{h_out} (returned 0x{r:x})"
    return f"stsd {'rewritten' if first else 'passed through'} {w}x{h_in} -> {w}x{h_out} (returned 0x{r:x})"


def parse_mod_log(text: str, spec: ModSpec) -> dict[str, Any]:
    """Groups the mod log: DIAG lines belong to the ON/OFF line that follows them; HOOK lines to the ON before."""
    events: list[dict[str, Any]] = []
    pending: list[str] = []
    hooks_since_on: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        summary = parse_log_line(line)
        if summary:
            events.append({"line": line, "action": summary.action,
                           "diag": [describe_diag(d, spec, summary.action) for d in pending]})
            pending = []
            if summary.action == "ON":
                hooks_since_on = []
        elif _DIAG.match(line):
            pending.append(line)
        elif _HOOK_LINE.match(line):
            hooks_since_on.append(describe_hook_line(line) or line)
    return {"events": events[-6:], "last": events[-1] if events else None, "hooks_since_last_on": hooks_since_on,
            "unterminated_diag": pending}


def classify_log(line: LogLine | None, spec: ModSpec) -> tuple[str, str]:
    """(state, explanation) for the newest log line: APPLIED, STOCK, MIXED or NONE."""
    if line is None:
        return "NONE", "the mod has not logged an ON or OFF in this power-on"
    total = len(spec.av_patches)
    clean = (not line.av_error and line.mismatched == 0 and line.written + line.already == total
             and line.vu_complete and line.ok == spec.expected_ok)
    if clean:
        return ("APPLIED" if line.action == "ON" else "STOCK"), (
            f"{line.action}: {line.written} av sites written, {line.already} already in place, 0 mismatched; "
            f"viewUnified2 {line.vu}")
    vu_bad = "" if line.vu == "nomap" else ", ".join(
        f"word {i}={_VU_MEANING.get(c, c)}" for i, c in enumerate(line.vu) if c not in "wa")
    return "MIXED", (
        f"{line.action}: av written={line.written} already={line.already} mismatched={line.mismatched}"
        f"{' (open /dev/mem failed)' if line.av_error else ''}, viewUnified2={line.vu}"
        f"{' (' + vu_bad + ')' if vu_bad else ''}, ok={line.ok} of {spec.expected_ok}")


# --------------------------------------------------------------------------- camera access helpers

def _exec(dbg: Any, command: str) -> str:
    return dbg.request(OP_EXEC, command.encode("utf-8"))["payload"].decode("utf-8", "replace")


def read_text(dbg: Any, path: str, limit: int = MAX_LOG_BYTES) -> str | None:
    """Whole small camera file through OP_READ (no shell applets); None when it does not exist.

    The camera's shell has no `tail` link (only `busybox tail`), so logs are never read through `tail`.
    Files larger than limit keep their newest limit bytes only when the debugger allows an offset read.
    """
    try:
        data, truncated = dbg.read_all(path, limit=limit)
    except Exception as exc:  # DebuggerError from the real debugger, SimDebuggerError from the simulation
        if getattr(exc, "status", None) == -ENOENT:
            return None
        raise
    text = data.decode("utf-8", "replace")
    return text + ("\n[truncated]" if truncated else "")


def tail(text: str | None, lines: int) -> list[str]:
    return (text or "").strip().splitlines()[-lines:]


def _uptime(dbg: Any) -> float:
    text = read_text(dbg, "/proc/uptime", 64) or ""
    parts = text.split()
    if not parts:
        raise KitError("could not read /proc/uptime")
    return float(parts[0])


def find_im_pid(dbg: Any) -> int:
    listing = dbg.request(OP_PS)["payload"].decode("utf-8", "replace")
    pids = [int(p[0]) for p in (line.strip().split(None, 1) for line in listing.splitlines())
            if len(p) == 2 and p[0].isdigit() and p[1].strip() == "im.elf"]
    if len(pids) != 1:
        raise KitError(f"expected exactly one im.elf process, found {pids}")
    return pids[0]


def remote_sha256(dbg: Any, path: str, size: int) -> str | None:
    """SHA-256 of a camera file: `busybox sha256sum` (present on 2.00), else a bounded read-back."""
    for command in (f"busybox sha256sum {path} 2>/dev/null", f"sha256sum {path} 2>/dev/null"):
        out = _exec(dbg, command).strip().split()
        if out and re.fullmatch(r"[0-9a-f]{64}", out[0]):
            return out[0]
    data, truncated = dbg.read_all(path, limit=size + 1)
    if truncated:
        return None
    return hashlib.sha256(data).hexdigest()


def probe_transport(dbg: Any) -> dict[str, Any]:
    """Which debugger answers and which operations it serves (tcp_debugd lists them on OP_INFO)."""
    info = dbg.request(OP_INFO)["payload"].decode("utf-8", "replace")
    # tcp_debugd: "protocol=2 pid=.. transport=tcp\nops=a,b,c"; a6400_usb_debugd: one key=value per line.
    fields = dict(token.split("=", 1) for token in info.split() if "=" in token)
    transport = fields.get("transport", "unknown")
    ops = fields.get("ops")
    result = {"transport": transport, "pid": fields.get("pid"),
              "ops": ops.split(",") if ops else None}
    if transport == "tcp" and (ops is None or "mem" not in ops.split(",")):
        raise KitError("the camera's tcp_debugd has no OP_MEM, so the hook and screen sites cannot be read. "
                       "Start tcp-debugd-build-8 or later (custom_os/launcher/build_tcp_debugd_wsl.sh)")
    return result


class ResilientDebugger:
    """Debugger proxy that reconnects once and repeats read-only requests after a dropped Wi-Fi link.

    Writes (push, non-allowlisted exec) are never repeated: a repeated write could double-apply.
    """

    def __init__(self, opener: Callable[[], Any]):
        self._opener = opener
        self._cm: Any = None
        self.dbg: Any = None
        self.retries = 0
        self._open()

    def _open(self, attempts: int = 3) -> None:
        for attempt in range(attempts):
            try:
                self._cm = self._opener()
                self.dbg = self._cm.__enter__()
                return
            except OSError:
                self._cm = None
                if attempt == attempts - 1:
                    raise
                self.retries += 1
                time.sleep(1.0 + attempt)

    def close(self) -> None:
        if self._cm is not None:
            with contextlib.suppress(Exception):
                self._cm.__exit__(None, None, None)
            self._cm = None

    def _retry(self, call: Callable[[], Any], retryable: bool) -> Any:
        try:
            return call()
        except OSError:
            if not retryable:
                raise
            self.retries += 1
            self.close()
            time.sleep(0.5)
            self._open()
            return call()

    def request(self, op: int, payload: bytes = b"", arg0: int = 0, arg1: int = 0) -> dict[str, Any]:
        retryable = op != OP_EXEC or payload.decode("utf-8", "replace").startswith(READONLY_EXEC_PREFIXES)
        return self._retry(lambda: self.dbg.request(op, payload, arg0, arg1), retryable)

    def read_all(self, path: str, limit: int = 2 * 1024 * 1024) -> tuple[bytes, bool]:
        return self._retry(lambda: self.dbg.read_all(path, limit), True)

    def memory(self, pid: int, address: int, size: int) -> bytes:
        return self._retry(lambda: self.dbg.memory(pid, address, size), True)

    def push(self, path: str, data: bytes) -> dict[str, Any]:
        return self.dbg.push(path, data)

    def ping(self) -> str:
        return self._retry(lambda: self.dbg.ping(), True)


def read_memory(dbg: Any, pid: int, address: int, size: int, chunk: int = 4000) -> bytes:
    out = bytearray()
    while len(out) < size:
        n = min(chunk, size - len(out))
        out.extend(dbg.memory(pid, address + len(out), n))
    return bytes(out)


# --------------------------------------------------------------------------- reports

@dataclass
class Report:
    command: str
    started_utc: str
    verdict: str = "NOT READY"
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def fail(self, reason: str) -> None:
        self.reasons.append(reason)

    @contextlib.contextmanager
    def timed(self, step: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.facts.setdefault("timings_ms", {})[step] = round((time.perf_counter() - start) * 1000.0, 1)

    def to_json(self) -> dict[str, Any]:
        return {"kit": "opengate_a6400", "command": self.command, "started_utc": self.started_utc,
                "verdict": self.verdict, "reasons": self.reasons, "notes": self.notes, "facts": self.facts}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- hook diagnostics

def classify_hook_bytes(live: bytes, hook: Hook) -> str:
    if live == hook.stock:
        return "stock"
    if live.startswith(HOOK_JUMP_PREFIX) and len(live) >= len(HOOK_JUMP_PREFIX) + 4:
        return "hooked"
    return "foreign"


def find_unique(data: bytes, pattern: bytes) -> list[int]:
    hits, i = [], data.find(pattern)
    while i >= 0 and len(hits) < 4:
        hits.append(i)
        i = data.find(pattern, i + 1)
    return hits


def suggest_hook_fix(hook: Hook, found_offset: int) -> dict[str, Any]:
    shift = found_offset - hook.offset
    va = hook.ref_va + shift
    return {"found_offset": f"0x{found_offset:x}", "shift": f"{'-' if shift < 0 else '+'}0x{abs(shift):x}",
            "build_v2_wsl_line": f'--hook "0x{va:08x}:{hook.stock.hex()}:{hook.name}"'}


def locate_hook_live(dbg: Any, pid: int, base: int, hook: Hook, window: int = HOOK_SEARCH_WINDOW) -> dict[str, Any]:
    """Searches the live library around the expected offset for the hook's stock bytes (read-only OP_MEM)."""
    start = max(0, hook.offset - window)
    blob = read_memory(dbg, pid, base + start, window * 2 + len(hook.stock))
    hits = [start + h for h in find_unique(blob, hook.stock)]
    result: dict[str, Any] = {"searched": f"+0x{start:x}..+0x{start + len(blob):x}", "hits": [f"0x{h:x}" for h in hits]}
    if len(hits) == 1:
        result.update(suggest_hook_fix(hook, hits[0]))
    return result


def locate_hook_local(root: Path, identity: dict[str, Any], hook: Hook) -> dict[str, Any] | None:
    """Same search in the pinned local copy of the library, when the capture is present (it is local-only)."""
    pin = identity["libraries"].get(hook.lib, {})
    local = pin.get("local_copy")
    path = root / local if local else None
    if path is None or not path.is_file():
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin.get("sha256"):
        return {"local_copy": local, "error": "local copy does not match the pinned SHA-256"}
    hits = find_unique(data, hook.stock)
    result: dict[str, Any] = {"local_copy": local, "hits": [f"0x{h:x}" for h in hits]}
    if len(hits) == 1:
        result.update(suggest_hook_fix(hook, hits[0]))
    return result


# --------------------------------------------------------------------------- camera inspection

def read_vu_sites(dbg: Any, pid: int, vu_base: int, spec: ModSpec) -> str:
    """s/p/? per swapped word, from one OP_MEM read that spans all cells."""
    lo = min(spec.vu_offsets) + min(o for o, _s, _p in spec.vu_words)
    hi = max(spec.vu_offsets) + max(o for o, _s, _p in spec.vu_words) + 4
    blob = read_memory(dbg, pid, vu_base + lo, hi - lo)
    state = []
    for cell in spec.vu_offsets:
        for word_off, stock, patched in spec.vu_words:
            value = struct.unpack_from("<I", blob, cell + word_off - lo)[0]
            state.append("s" if value == stock else "p" if value == patched else "?")
    return "".join(state)


def read_hook_states(dbg: Any, pid: int, bases: dict[str, int], spec: ModSpec) -> dict[str, Any]:
    hooks = {}
    for hook in spec.hooks:
        base = bases.get(hook.lib)
        if base is None:
            hooks[hook.name] = {"lib": hook.lib, "mapped": False, "state": "unmapped"}
            continue
        live = dbg.memory(pid, base + hook.offset, len(hook.stock))
        state = classify_hook_bytes(live, hook)
        entry = {"lib": hook.lib, "offset": f"0x{hook.offset:x}", "live": live.hex(),
                 "expected_stock": hook.stock.hex(), "stock": state == "stock", "state": state}
        if state == "hooked":
            entry["jumps_to"] = f"0x{struct.unpack_from('<I', live, len(HOOK_JUMP_PREFIX))[0]:08x}"
        hooks[hook.name] = entry
    return hooks


def inspect(dbg: Any, spec: ModSpec, identity: dict[str, Any], report: Report,
            root: Path = ROOT) -> dict[str, Any]:
    """Read-only snapshot used by check, verify, putback, reload and cycle."""
    facts: dict[str, Any] = report.facts
    with report.timed("transport"):
        facts["transport"] = probe_transport(dbg)
        facts["uptime_start"] = _uptime(dbg)
        pid = find_im_pid(dbg)
        facts["im_pid"] = pid
        maps_raw, _ = dbg.read_all(f"/proc/{pid}/maps", limit=256 * 1024)
        bases = parse_maps(maps_raw.decode("utf-8", "replace"))
        facts["bases"] = {lib: f"0x{bases[lib]:08x}" for lib in identity["libraries"] if lib in bases}

    with report.timed("library_hashes"):
        libs: dict[str, Any] = {}
        for path, pin in identity["libraries"].items():
            digest = remote_sha256(dbg, path, int(pin["bytes"]))
            libs[path] = {"sha256": digest, "match": digest == pin["sha256"], "base": bases.get(path)}
        facts["libraries"] = libs

    with report.timed("sites"):
        vu_base = bases.get("/usr/lib/viewUnified2.so")
        facts["viewunified2_sites"] = read_vu_sites(dbg, pid, vu_base, spec) if vu_base is not None else ""
        hooks = read_hook_states(dbg, pid, bases, spec)
        for hook in spec.hooks:
            entry = hooks[hook.name]
            if entry["state"] == "foreign":
                # Where are the stock bytes, then? Answering this here turns a NOT READY into a one-line fix.
                entry["live_search"] = locate_hook_live(dbg, pid, bases[hook.lib], hook)
                local = locate_hook_local(root, identity, hook)
                if local is not None:
                    entry["local_search"] = local
        facts["hooks"] = hooks

    with report.timed("logs"):
        facts["mod_loader_core"] = _exec(dbg, f"test -f {identity['mod_loader']['core']} && echo YES").strip() == "YES"
        staged = _exec(dbg, f"test -f {REMOTE_MOD} && echo YES").strip() == "YES"
        facts["staged_mod"] = None
        if staged:
            blob, truncated = dbg.read_all(REMOTE_MOD, limit=MAX_MOD_BYTES + 1)
            facts["staged_mod"] = None if truncated else hashlib.sha256(blob).hexdigest()
        log_text = read_text(dbg, REMOTE_LOG) or ""
        facts["mod_log_tail"] = tail(log_text, 12)
        facts["mod_log"] = parse_mod_log(log_text, spec)
        state, why = classify_log(last_log_line(log_text), spec)
        facts["mod_log_state"] = state
        facts["mod_log_detail"] = why
        facts["loader_log_tail"] = tail(read_text(dbg, REMOTE_LOADER_LOG), 12)
        facts["reload_result"] = (read_text(dbg, RELOAD_RESULT, 256) or "").strip() or None
        facts["uptime_end"] = _uptime(dbg)
    if facts["uptime_end"] < facts["uptime_start"]:
        report.fail("camera uptime went backwards during the session: it restarted")
    if hasattr(dbg, "retries"):
        facts["transport_retries"] = dbg.retries
    report.notes.append(identity["avcam"]["note"])
    return facts


def capture_failure_context(dbg: Any, report: Report) -> None:
    """On any non-success verdict: kernel log tail next to the mod and loader logs, all read-only."""
    with contextlib.suppress(Exception):
        report.facts["klog_tail"] = tail(_exec(dbg, "busybox tail -n 40 /proc/klog 2>/dev/null"), 40)


def _hook_reason(name: str, info: dict[str, Any]) -> str:
    reason = (f"hook {name}: {info['lib']}+{info['offset']} holds {info['live']}, the mod expects "
              f"{info['expected_stock']}; Mod Loader v2 would refuse or misplace this hook")
    search = info.get("live_search") or {}
    if search.get("build_v2_wsl_line"):
        reason += (f". The stock bytes are at +{search['found_offset']} in the live library (shift "
                   f"{search['shift']}): use {search['build_v2_wsl_line']} in build_v2_wsl.sh")
    elif search:
        reason += f". Live search {search.get('searched')} found {len(search.get('hits', []))} copies"
    return reason


def check(dbg: Any, spec: ModSpec, identity: dict[str, Any], root: Path = ROOT) -> Report:
    report = Report("check", _now())
    facts = inspect(dbg, spec, identity, report, root)
    for path, info in facts["libraries"].items():
        if info["base"] is None:
            report.fail(f"{path} is not mapped in im.elf")
        if not info["match"]:
            report.fail(f"{path} SHA-256 {info['sha256']} is not the {identity['firmware']} build this kit was "
                        f"made for ({identity['libraries'][path]['sha256']})")
    vu = facts["viewunified2_sites"]
    if len(vu) != 2 * len(spec.vu_offsets) or set(vu) != {"s"}:
        report.fail(f"viewUnified2 screen sites are not all stock (s=stock, p=patched, ?=unknown): {vu or 'unread'}")
    for name, info in facts["hooks"].items():
        if not info.get("mapped", True):
            report.fail(f"{info['lib']} is not mapped, so hook {name} cannot be checked")
        elif info["state"] == "hooked":
            report.fail(f"hook {name} is already installed (jumps to {info['jumps_to']}): the mod is not off")
        elif not info["stock"]:
            report.fail(_hook_reason(name, info))
    if not facts["mod_loader_core"]:
        report.fail("Mod Loader v2 is not installed in this power-on (no /tmp_bt/mlv2/core.bin)")
    if facts["mod_log_state"] in ("APPLIED", "MIXED"):
        report.fail(f"the mod is not off: {facts['mod_log_detail']}")
    if not report.reasons:
        report.verdict = "READY (stock preflight)"
    else:
        capture_failure_context(dbg, report)
    return report


def verify(dbg: Any, spec: ModSpec, identity: dict[str, Any], root: Path = ROOT) -> Report:
    report = Report("verify", _now())
    facts = inspect(dbg, spec, identity, report, root)
    state = facts["mod_log_state"]
    vu = facts["viewunified2_sites"]
    hook_states = {name: info["state"] for name, info in facts["hooks"].items()}
    facts["hook_states"] = hook_states
    hooks_on = bool(hook_states) and set(hook_states.values()) == {"hooked"}
    hooks_off = bool(hook_states) and set(hook_states.values()) == {"stock"}
    last = facts["mod_log"]["last"]
    if state == "APPLIED" and vu and set(vu) == {"p"} and hooks_on:
        report.verdict = "APPLIED"
    elif state in ("STOCK", "NONE") and vu and set(vu) == {"s"} and hooks_off:
        report.verdict = "STOCK"
    else:
        report.verdict = "MIXED"
        report.fail(f"mod log: {facts['mod_log_detail']}; live viewUnified2 sites: {vu}; hooks: "
                    + ", ".join(f"{n}={s}" for n, s in hook_states.items()))
        if state == "APPLIED" and not hooks_on:
            report.fail("the mod's ON ran but its MP4 hooks are not installed: Mod Loader v2 refused them "
                        "(see loader_log_tail) or another mod owns those bytes")
        for line in (last or {}).get("diag", []):
            report.fail(f"DIAG {line}")
        report.notes.append("Switch the mod OFF in MENU -> Mods and run verify again. If it still is not STOCK, "
                            "switch the camera off and pull the battery for 10 seconds (a cold boot reloads the "
                            "libraries and av-cam.bin from flash; inferred, not yet observed for this mod).")
        capture_failure_context(dbg, report)
    hooks_seen = facts["mod_log"]["hooks_since_last_on"]
    if report.verdict == "APPLIED":
        report.notes.append("MP4 hook calls since ON: " + ("; ".join(hooks_seen) if hooks_seen else
                            "none yet (record a clip; each recording logs one tkhd and one stsd line)"))
    return report


# --------------------------------------------------------------------------- receipts

def write_receipt(report: Report, private_dir: Path = PRIVATE_DIR) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = private_dir / f"{stamp}-{report.command}"
    suffix = 1
    while folder.exists():
        suffix += 1
        folder = private_dir / f"{stamp}-{report.command}-{suffix}"
    folder.mkdir(parents=True)
    path = folder / f"{report.command}.json"
    path.write_text(json.dumps(report.to_json(), indent=2) + "\n", encoding="utf-8")
    return path


def latest_ready(private_dir: Path = PRIVATE_DIR) -> dict[str, Any] | None:
    receipts = sorted(private_dir.glob("*-check*/check.json")) if private_dir.is_dir() else []
    for path in reversed(receipts):
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("verdict", "").startswith("READY") else None
    return None


def require_fresh_ready(previous: dict[str, Any] | None, current: Report) -> None:
    """apply only runs after a READY check from this power-on and this camera build."""
    if previous is None:
        raise KitError("no READY check on record: run `check` first")
    if not current.verdict.startswith("READY"):
        raise KitError("the check made just now is NOT READY: " + "; ".join(current.reasons))
    then = previous["facts"]
    now = current.facts
    if now["uptime_start"] < then["uptime_end"]:
        raise KitError("the camera restarted since the READY check: run `check` again")
    if now["uptime_start"] - then["uptime_end"] > READY_MAX_AGE_S:
        raise KitError("the READY check is older than 30 minutes: run `check` again")
    for path, info in now["libraries"].items():
        if then["libraries"].get(path, {}).get("sha256") != info["sha256"]:
            raise KitError(f"{path} changed since the READY check")


# --------------------------------------------------------------------------- commands

def apply(dbg: Any, spec: ModSpec, identity: dict[str, Any], mod_bytes: bytes,
          confirm: Callable[[str], bool], private_dir: Path = PRIVATE_DIR) -> Report:
    previous = latest_ready(private_dir)
    current = check(dbg, spec, identity)
    write_receipt(current, private_dir)
    require_fresh_ready(previous, current)
    if not 0 < len(mod_bytes) <= MAX_MOD_BYTES:
        raise KitError("the .mod file is empty or larger than 1 MiB")
    digest = hashlib.sha256(mod_bytes).hexdigest()
    if not confirm(f"Stage OpenGateA1.mod ({len(mod_bytes)} bytes, sha256 {digest[:16]}...) in {REMOTE_MOD}?"):
        raise KitError("not confirmed; nothing was written")
    report = Report("apply", _now(), verdict="STAGED")
    with report.timed("push"):
        result = dbg.push(REMOTE_MOD, mod_bytes)
    report.facts.update({"remote_path": REMOTE_MOD, "sha256": digest, "bytes": len(mod_bytes),
                         "readback_verified": bool(result.get("readback_verified")),
                         "uptime": _uptime(dbg)})
    report.notes.append("Next, on the camera: MENU -> tab 7 (Mods) -> switch 'OpenGate A1' ON, leave the menu, "
                        "then run `verify` (or let `watch --until APPLIED` follow it). Record only XAVC S 4K 24p "
                        "while it is applied.")
    return report


def putback(dbg: Any, spec: ModSpec, identity: dict[str, Any], private_dir: Path = PRIVATE_DIR) -> Report:
    report = verify(dbg, spec, identity)
    report.command = "putback"
    if report.verdict != "STOCK":
        report.notes.insert(0, "Switch 'OpenGate A1' OFF in MENU -> Mods first, then run putback again.")
        return report
    staged = report.facts.get("staged_mod")
    pushed = [json.loads(p.read_text(encoding="utf-8")).get("facts", {}).get("sha256")
              for p in sorted(private_dir.glob("*-apply*/apply.json"))
              + sorted(private_dir.glob("*-reload*/reload.json"))] if private_dir.is_dir() else []
    if staged and staged in pushed:
        _exec(dbg, f"rm {REMOTE_MOD}")
        gone = _exec(dbg, f"test -f {REMOTE_MOD} || echo GONE").strip() == "GONE"
        report.facts["staged_mod_removed"] = gone
        if not gone:
            report.verdict = "STOCK (staged .mod not removed)"
    elif staged:
        report.notes.append(f"{REMOTE_MOD} was not staged by this kit (hash unknown); it was left in place.")
    return report


def reload(dbg: Any, spec: ModSpec, identity: dict[str, Any], mod_bytes: bytes,
           confirm: Callable[[str], bool], *, timeout: float = 120.0,
           wait: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> Report:
    """Replace the enabled OpenGateA1 copy without the menu toggle (Mod Loader v2 reload request, E0576)."""
    from tools.research.reload_mod import hook_bytes, parse_result, parse_smod_v2
    report = Report("reload", _now())
    before = inspect(dbg, spec, identity, Report("reload-precheck", _now()))
    report.facts["before"] = {k: before[k] for k in ("mod_log_state", "viewunified2_sites", "hooks", "staged_mod")}
    if before["mod_log_state"] != "APPLIED":
        raise KitError(f"reload replaces an enabled mod; the mod log says {before['mod_log_state']} "
                       f"({before['mod_log_detail']}). For a first run use apply and switch it ON")
    core, _ = dbg.read_all(identity["mod_loader"]["core"], limit=256 * 1024)
    if RELOAD_REQ.encode() not in core:
        raise KitError("the installed Mod Loader v2 core has no reload request path (not reload-capable)")
    try:
        smod = parse_smod_v2(mod_bytes)
    except ValueError as exc:
        raise KitError(f"not a valid SMOD v2 file: {exc}") from exc
    for h in smod["hooks"]:
        match = [x for x in spec.hooks if x.ref_va == h["target"]]
        if not match or h["orig"] != match[0].stock:
            raise KitError(f"the .mod hook at 0x{h['target']:08x} does not match build_v2_wsl.sh; rebuild the mod")
    digest = hashlib.sha256(mod_bytes).hexdigest()
    if not confirm(f"Replace the enabled OpenGateA1 copy with {len(mod_bytes)} bytes (sha256 {digest[:16]}...)?"):
        raise KitError("not confirmed; nothing was written")
    name = Path(REMOTE_MOD).name
    with report.timed("stage"):
        report.facts["push"] = dbg.push(f"/tmp_bt/mods/.{name}.part", mod_bytes)
        _exec(dbg, f"mv -f /tmp_bt/mods/.{name}.part {REMOTE_MOD}")
        _exec(dbg, f"rm -f {RELOAD_RESULT}")
        dbg.push(RELOAD_REQ + ".part", name.encode() + b"\n")
        _exec(dbg, f"mv -f {RELOAD_REQ}.part {RELOAD_REQ}")
    say("Reload request published: open the Sony menu or move within it so the loader core acts.")
    result = None
    with report.timed("wait_result"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = read_text(dbg, RELOAD_RESULT, 256)
            result = parse_result(text.split("\n")[0]) if text and text.strip() else None
            if result:
                break
            wait(1.0)
    report.facts.update({"sha256": digest, "bytes": len(mod_bytes),
                         "result": f"{result[0]} {result[1]:08x}" if result else None})
    if result is None:
        report.verdict = "NO RESULT"
        report.fail(f"no {RELOAD_RESULT} within {timeout:.0f} s; the request may still be pending "
                    f"({RELOAD_REQ} is consumed at the next menu refresh)")
        capture_failure_context(dbg, report)
        return report
    if result[0] != "reload_ok":
        report.verdict = "RELOAD REFUSED"
        report.fail(f"loader answered {result[0]} 0x{result[1]:08x}; the old copy stays active")
        report.facts["loader_log_tail"] = tail(read_text(dbg, REMOTE_LOADER_LOG), 12)
        capture_failure_context(dbg, report)
        return report
    copy_base = result[1]
    pid = find_im_pid(dbg)
    maps_raw, _ = dbg.read_all(f"/proc/{pid}/maps", limit=256 * 1024)
    bases = parse_maps(maps_raw.decode("utf-8", "replace"))
    hooks_ok = True
    for h in smod["hooks"]:
        hook = next(x for x in spec.hooks if x.ref_va == h["target"])
        live = dbg.memory(pid, bases[hook.lib] + hook.offset, h["size"])
        expected = hook_bytes(h, copy_base)
        report.facts.setdefault("hooks_after", {})[hook.name] = {"live": live.hex(), "expected": expected.hex()}
        hooks_ok &= live == expected
    code = read_memory(dbg, pid, copy_base, smod["code_size"])
    report.facts["hooks_point_to_new_copy"] = hooks_ok
    report.facts["resident_code_matches_file"] = code == smod["code"]
    after = verify(dbg, spec, identity)
    report.facts["verify_after"] = {"verdict": after.verdict, "reasons": after.reasons}
    report.verdict = "RELOADED" if hooks_ok and code == smod["code"] and after.verdict == "APPLIED" else "MIXED"
    if report.verdict != "RELOADED":
        report.fail("after reload_ok the hooks or the resident code do not match the new file, or verify is "
                    f"{after.verdict}")
        capture_failure_context(dbg, report)
    return report


def export(dbg: Any | None, private_dir: Path = PRIVATE_DIR) -> Path:
    """Zip this kit's receipts plus a read-only log snapshot. Never reads /setting or Backup files."""
    private_dir.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, str] = {}
    if dbg is not None:
        snapshot["uptime.txt"] = read_text(dbg, "/proc/uptime", 64) or ""
        snapshot["version.txt"] = read_text(dbg, "/proc/version", 1024) or ""
        snapshot["opengate_a1.log"] = read_text(dbg, REMOTE_LOG) or ""
        snapshot["mod_loader.log"] = read_text(dbg, REMOTE_LOADER_LOG) or ""
        snapshot["reload.result"] = read_text(dbg, RELOAD_RESULT, 256) or ""
        snapshot["klog-tail.txt"] = _exec(dbg, "busybox tail -n 200 /proc/klog 2>/dev/null")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = private_dir / f"opengate_a6400-logs-{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(private_dir.rglob("*.json")) + sorted(private_dir.rglob("*.jsonl")):
            zf.write(path, path.relative_to(private_dir).as_posix())
        for name, text in snapshot.items():
            zf.writestr(f"snapshot/{name}", text)
    return out


# --------------------------------------------------------------------------- watch

def quick_state(dbg: Any, spec: ModSpec, pid: int, bases: dict[str, int]) -> dict[str, Any]:
    """Cheap poll (about six requests): uptime, logs, screen words, hooks."""
    state: dict[str, Any] = {"uptime": _uptime(dbg)}
    state["mod_log"] = read_text(dbg, REMOTE_LOG) or ""
    state["loader_log"] = read_text(dbg, REMOTE_LOADER_LOG) or ""
    state["reload_result"] = (read_text(dbg, RELOAD_RESULT, 256) or "").strip()
    vu_base = bases.get("/usr/lib/viewUnified2.so")
    state["vu"] = read_vu_sites(dbg, pid, vu_base, spec) if vu_base is not None else ""
    state["hooks"] = {n: i["state"] for n, i in read_hook_states(dbg, pid, bases, spec).items()}
    state["log_state"] = classify_log(last_log_line(state["mod_log"]), spec)[0]
    return state


def summarize_state(state: dict[str, Any]) -> str:
    vu = state["vu"]
    vu_short = "stock" if vu and set(vu) == {"s"} else "patched" if vu and set(vu) == {"p"} else vu or "unread"
    hooks = ",".join(f"{n.split('_')[0]}={s}" for n, s in state["hooks"].items())
    return f"log={state['log_state']} screen={vu_short} hooks[{hooks}] uptime={state['uptime']:.1f}"


def watch_verdict(state: dict[str, Any]) -> str:
    vu, hooks = state["vu"], set(state["hooks"].values())
    if state["log_state"] == "APPLIED" and vu and set(vu) == {"p"} and hooks == {"hooked"}:
        return "APPLIED"
    if state["log_state"] in ("STOCK", "NONE") and vu and set(vu) == {"s"} and hooks == {"stock"}:
        return "STOCK"
    return "MIXED"


def new_lines(old: str, new: str) -> list[str]:
    """Lines appended since the previous poll (logs only grow within a power-on)."""
    if new.startswith(old):
        return [line for line in new[len(old):].splitlines() if line.strip()]
    return [line for line in new.splitlines() if line.strip()]


def watch(opener: Callable[[], Any], spec: ModSpec, *, interval: float = 1.0, until: str | None = None,
          timeout: float | None = None, timeline: Path | None = None, say: Callable[[str], None] = print,
          wait: Callable[[float], None] = time.sleep, max_polls: int | None = None) -> dict[str, Any]:
    """Follows the camera live and prints only changes. Detects link loss and restarts (uptime going back).

    Returns {"verdict": last watch verdict, "reached": until reached?, "events": count, "restarts": n}.
    """
    started = time.monotonic()
    prev: dict[str, Any] | None = None
    last_uptime: float | None = None
    restarts = polls = events = 0
    verdict = "UNKNOWN"
    out = timeline.open("a", encoding="utf-8") if timeline else None

    def emit(kind: str, text: str, **extra: Any) -> None:
        nonlocal events
        events += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        say(f"[{stamp}] {kind:<8} {text}")
        if out:
            out.write(json.dumps({"t": _now(), "kind": kind, "text": text, **extra}) + "\n")
            out.flush()

    try:
        while True:
            if timeout is not None and time.monotonic() - started > timeout:
                emit("TIMEOUT", f"stopped after {timeout:.0f} s; last verdict {verdict}")
                return {"verdict": verdict, "reached": False, "events": events, "restarts": restarts}
            try:
                with opener() as dbg:
                    pid = find_im_pid(dbg)
                    maps_raw, _ = dbg.read_all(f"/proc/{pid}/maps", limit=256 * 1024)
                    bases = parse_maps(maps_raw.decode("utf-8", "replace"))
                    emit("LINK", f"connected; im.elf pid {pid}")
                    while True:
                        state = quick_state(dbg, spec, pid, bases)
                        polls += 1
                        if last_uptime is not None and state["uptime"] < last_uptime:
                            restarts += 1
                            emit("RESTART", f"uptime went from {last_uptime:.1f} to {state['uptime']:.1f}: "
                                            "the camera rebooted", uptime=state["uptime"])
                            prev = None
                        last_uptime = state["uptime"]
                        if prev is None:
                            emit("STATE", summarize_state(state))
                        else:
                            for line in new_lines(prev["mod_log"], state["mod_log"]):
                                text = describe_hook_line(line) or (describe_diag(line, spec, None) and
                                                                    "DIAG " + describe_diag(line, spec, None)) or line
                                emit("MODLOG", text)
                            for line in new_lines(prev["loader_log"], state["loader_log"]):
                                emit("LOADER", line)
                            if state["reload_result"] != prev["reload_result"] and state["reload_result"]:
                                emit("RELOAD", state["reload_result"])
                            if (state["vu"], state["hooks"], state["log_state"]) != (
                                    prev["vu"], prev["hooks"], prev["log_state"]):
                                emit("STATE", summarize_state(state))
                        verdict = watch_verdict(state)
                        prev = state
                        if until and verdict == until:
                            emit("REACHED", until)
                            return {"verdict": verdict, "reached": True, "events": events, "restarts": restarts}
                        if max_polls is not None and polls >= max_polls:
                            return {"verdict": verdict, "reached": False, "events": events, "restarts": restarts}
                        if timeout is not None and time.monotonic() - started > timeout:
                            break
                        wait(interval)
            except (OSError, KitError) as exc:
                emit("LINK", f"lost: {exc}; retrying every {max(interval, 2.0):.0f} s")
                polls += 1
                if max_polls is not None and polls >= max_polls:
                    return {"verdict": verdict, "reached": False, "events": events, "restarts": restarts}
                wait(max(interval, 2.0))
    finally:
        if out:
            out.close()


# --------------------------------------------------------------------------- cycle (send -> test -> fix)

def build_mod(out_dir: Path, run: Callable[..., Any] = subprocess.run) -> tuple[Path, dict[str, Any]]:
    """WSL build into a new directory, then the offline Unicorn gate; returns the .mod path and step log."""
    if out_dir.exists():
        raise KitError(f"build directory {out_dir} already exists; builds always go to a new directory")
    rel = out_dir.relative_to(ROOT).as_posix()
    steps: dict[str, Any] = {}
    start = time.perf_counter()
    built = run(["wsl.exe", "--exec", "bash", BUILD_SCRIPT.relative_to(ROOT).as_posix(), rel], cwd=ROOT,
                capture_output=True, text=True)
    steps["build"] = {"returncode": built.returncode, "ms": round((time.perf_counter() - start) * 1000),
                      "tail": tail(built.stdout + built.stderr, 12)}
    if built.returncode != 0:
        raise KitError("mod build failed:\n" + "\n".join(steps["build"]["tail"]))
    start = time.perf_counter()
    tested = run([sys.executable, UNICORN_TEST.relative_to(ROOT).as_posix(), rel], cwd=ROOT,
                 capture_output=True, text=True)
    steps["unicorn"] = {"returncode": tested.returncode, "ms": round((time.perf_counter() - start) * 1000),
                        "tail": tail(tested.stdout + tested.stderr, 8)}
    if tested.returncode != 0:
        raise KitError("Unicorn gate failed (nothing sent to the camera):\n" + "\n".join(steps["unicorn"]["tail"]))
    mod = out_dir / "OpenGateA1.mod"
    steps["mod_sha256"] = hashlib.sha256(mod.read_bytes()).hexdigest()
    return mod, steps


def next_build_dir(base: Path = ROOT / "artifacts" / "tests") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / f"opengate-a1-loop-{stamp}"


# --------------------------------------------------------------------------- simulated camera

class SimDebuggerError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"simulated debugger error {status}")
        self.status = status


class SimulatedCamera:
    """A debugger stand-in for selftest and unit tests: stock libraries, Mod Loader present."""

    VU_BASE = 0x155E6000
    MPR_BASE = 0x07144000
    COPY_BASE = 0x1F000000

    def __init__(self, spec: ModSpec, identity: dict[str, Any], *, libmpr_shift: int = 0, transport: str = "sim",
                 ops: str = "info,read,exec,mem,ps"):
        self.spec = spec
        self.identity = identity
        self.uptime = 100.0
        self.files: dict[str, bytes] = {identity["mod_loader"]["core"]: b"core" + RELOAD_REQ.encode()}
        self.mem: dict[int, int] = {}
        for cell in spec.vu_offsets:
            for word_off, stock, _patched in spec.vu_words:
                self._put32(self.VU_BASE + cell + word_off, stock)
        self.libmpr_shift = libmpr_shift
        for hook in spec.hooks:
            self._put_bytes(self.MPR_BASE + hook.offset + libmpr_shift, hook.stock)
        self.log: list[str] = []
        self.loader_log: list[str] = []
        self.lib_hashes = {p: pin["sha256"] for p, pin in identity["libraries"].items()}
        self.commands: list[str] = []
        self.info = f"protocol=2 pid=1 transport={transport}\nops={ops}\n"

    def _put32(self, addr: int, value: int) -> None:
        self._put_bytes(addr, struct.pack("<I", value))

    def _put_bytes(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    # debugger interface -------------------------------------------------------------------
    def request(self, op: int, payload: bytes = b"", arg0: int = 0, arg1: int = 0) -> dict[str, Any]:
        if op == OP_PS:
            return {"payload": b"  1 init\n412 im.elf\n", "status": 0, "flags": 0}
        if op == OP_INFO:
            return {"payload": self.info.encode(), "status": 0, "flags": 0}
        command = payload.decode()
        self.commands.append(command)
        self.uptime += 0.5
        out = ""
        words = command.split()
        if command == "cat /proc/uptime":
            out = f"{self.uptime:.2f} 10.00\n"
        elif command.startswith(("busybox sha256sum ", "sha256sum ")):
            path = words[2] if words[0] == "busybox" else words[1]
            out = f"{self.lib_hashes.get(path, '0' * 64)}  {path}\n"
        elif command.startswith("test -f ") and "&& echo YES" in command:
            out = "YES\n" if words[2] in self.files else ""
        elif command.startswith("test -f ") and "|| echo GONE" in command:
            out = "" if words[2] in self.files else "GONE\n"
        elif command.startswith("rm "):
            self.files.pop(words[-1], None)
        elif command.startswith("mv -f "):
            self.files[words[3]] = self.files.pop(words[2])
        elif command.startswith("busybox tail "):
            out = "simulated klog\n"
        return {"payload": out.encode(), "status": 0, "flags": 0}

    def read_all(self, path: str, limit: int = 2 * 1024 * 1024) -> tuple[bytes, bool]:
        self.commands.append(f"read {path}")
        if path == "/proc/uptime":
            self.uptime += 0.5
            return f"{self.uptime:.2f} 10.00\n".encode(), False
        if path.endswith("/maps"):
            text = (f"{self.MPR_BASE:08x}-07d32000 r-xp 00000000 fc:0f 22451      /usr/lib/libmpr.so\n"
                    f"{self.VU_BASE:08x}-15e92000 r-xp 00000000 fc:0f 22532      /usr/lib/viewUnified2.so\n")
            return text.encode(), False
        if path == REMOTE_LOG:
            if not self.log:
                raise SimDebuggerError(-ENOENT)
            data = ("\n".join(self.log) + "\n").encode()
        elif path == REMOTE_LOADER_LOG:
            if not self.loader_log:
                raise SimDebuggerError(-ENOENT)
            data = ("\n".join(self.loader_log) + "\n").encode()
        elif path in self.files:
            data = self.files[path]
        elif path == "/proc/version":
            data = b"simulated\n"
        else:
            raise SimDebuggerError(-ENOENT)
        return data[:limit], len(data) > limit

    def memory(self, pid: int, address: int, size: int) -> bytes:
        return bytes(self.mem.get(address + i, 0) for i in range(size))

    def push(self, path: str, data: bytes) -> dict[str, Any]:
        self.files[path] = bytes(data)
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "readback_verified": True}

    # user actions on the camera ------------------------------------------------------------
    def _write_hooks(self, on: bool, base: int) -> None:
        for hook in self.spec.hooks:
            addr = self.MPR_BASE + hook.offset + self.libmpr_shift
            if on:
                self._put_bytes(addr, HOOK_JUMP_PREFIX + struct.pack("<I", base + 0x609))
            else:
                self._put_bytes(addr, hook.stock)

    def toggle(self, on: bool, *, mismatched: int = 0, hooks: bool = True) -> None:
        total = len(self.spec.av_patches)
        for cell in self.spec.vu_offsets:
            for word_off, stock, patched in self.spec.vu_words:
                self._put32(self.VU_BASE + cell + word_off, patched if on else stock)
        if hooks:
            self._write_hooks(on, self.COPY_BASE)
        for i in range(min(mismatched, 8)):
            self.log.append(f"DIAG x i={i:04x} v=beef")
        ok = total - mismatched + 2 * len(self.spec.vu_offsets)
        self.log.append(f"{'ON' if on else 'OFF'} av=w{total - mismatched:04x},a0000,x{mismatched:04x} "
                        f"vu={'w' * 16} ok={ok:08x}")

    def record_clip(self) -> None:
        self.log.append("HOOK t kind=00000005 w=00000f00 h=08700a00 r=00000000")
        self.log.append("HOOK s hit=00000001 w=00000f00 h=08700a00 r=00000000")

    def core_consumes_reload(self, ok: bool = True) -> None:
        """What the loader core does with /tmp_bt/mlv2/reload at the next menu refresh."""
        from tools.research.reload_mod import hook_bytes, parse_smod_v2
        if RELOAD_REQ not in self.files:
            return
        del self.files[RELOAD_REQ]
        if not ok:
            self.files[RELOAD_RESULT] = b"reload_refused 00000003\n"
            self.loader_log.append("reload OpenGateA1.mod refused hook_target")
            return
        smod = parse_smod_v2(self.files[REMOTE_MOD])
        self.COPY_BASE += 0x10000
        for h in smod["hooks"]:
            hook = next(x for x in self.spec.hooks if x.ref_va == h["target"])
            self._put_bytes(self.MPR_BASE + hook.offset + self.libmpr_shift, hook_bytes(h, self.COPY_BASE))
        self._put_bytes(self.COPY_BASE, smod["code"])
        self.files[RELOAD_RESULT] = f"reload_ok {self.COPY_BASE:08x}\n".encode()
        self.loader_log.append(f"reload OpenGateA1.mod ok base={self.COPY_BASE:08x}")


def selftest(spec: ModSpec, identity: dict[str, Any], private_dir: Path) -> list[str]:
    """Run check -> apply -> verify -> putback on a simulated camera; returns the step log."""
    steps = []
    cam = SimulatedCamera(spec, identity)
    r = check(cam, spec, identity)
    write_receipt(r, private_dir)
    steps.append(f"check: {r.verdict}")
    a = apply(cam, spec, identity, b"SMOD-simulated", lambda _q: True, private_dir)
    write_receipt(a, private_dir)
    steps.append(f"apply: {a.verdict}")
    cam.toggle(True)
    steps.append(f"verify after ON: {verify(cam, spec, identity).verdict}")
    cam.toggle(False)
    p = putback(cam, spec, identity, private_dir)
    steps.append(f"putback after OFF: {p.verdict} (staged .mod removed: {p.facts.get('staged_mod_removed')})")
    shifted = SimulatedCamera(spec, identity, libmpr_shift=-0xF480)
    s = check(shifted, spec, identity)
    hint = next((x for x in s.reasons if "use --hook" in x), "")
    steps.append(f"check with libmpr hooks 0xF480 lower: {s.verdict}"
                 + (f" (suggests {hint.split('use ')[-1].split(' in ')[0]})" if hint else ""))
    tcp_old = SimulatedCamera(spec, identity, transport="tcp", ops="info,read,exec,ps")
    try:
        check(tcp_old, spec, identity)
        steps.append("check over a tcp_debugd without OP_MEM: not refused")
    except KitError as exc:
        steps.append(f"check over a tcp_debugd without OP_MEM: REFUSED ({str(exc)[:48]}...)")
    return steps


# --------------------------------------------------------------------------- CLI

def _opener(args: argparse.Namespace) -> Callable[[], Any]:
    from tools import camera_debugger as cd
    return lambda: cd.connect(args.transport, host=args.host, port=args.port)


@contextlib.contextmanager
def _connect(args: argparse.Namespace) -> Iterator[Any]:
    dbg = ResilientDebugger(_opener(args))
    try:
        yield dbg
    finally:
        dbg.close()


def _print(report: Report, receipt: Path | None) -> None:
    print(f"VERDICT {report.verdict}")
    for reason in report.reasons:
        print(f"  !! {reason}")
    for note in report.notes:
        print(f"  -- {note}")
    timings = report.facts.get("timings_ms")
    if timings:
        print("  .. timings ms: " + ", ".join(f"{k}={v:.0f}" for k, v in timings.items()))
    if report.facts.get("transport_retries"):
        print(f"  .. transport retries after a dropped link: {report.facts['transport_retries']}")
    if receipt:
        print(f"receipt: {receipt.relative_to(ROOT) if receipt.is_relative_to(ROOT) else receipt}")


def _confirm(args: argparse.Namespace) -> Callable[[str], bool]:
    return (lambda _q: True) if args.yes else (lambda q: input(f"{q} Type YES: ").strip() == "YES")


def _timeline_path() -> Path:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    return PRIVATE_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-watch.jsonl"


def _cycle(args: argparse.Namespace, spec: ModSpec, identity: dict[str, Any]) -> int:
    """send -> test -> fix in one command: build + Unicorn gate, then apply or reload, then watch + verify."""
    started = time.perf_counter()
    summary: dict[str, Any] = {}
    if args.build:
        out_dir = next_build_dir()
        print(f"[cycle] building {out_dir.relative_to(ROOT)} and running the Unicorn gate")
        mod_path, steps = build_mod(out_dir)
        summary["build"] = steps
        print(f"[cycle] build {steps['build']['ms']} ms, Unicorn {steps['unicorn']['ms']} ms, "
              f"sha256 {steps['mod_sha256'][:16]}")
    elif args.mod and args.mod.is_file():
        mod_path = args.mod
    else:
        raise KitError("cycle needs --build or --mod <OpenGateA1.mod>")
    mod_bytes = mod_path.read_bytes()
    with _connect(args) as dbg:
        pre = Report("cycle-precheck", _now())
        facts = inspect(dbg, spec, identity, pre)
        staged_same = facts["staged_mod"] == hashlib.sha256(mod_bytes).hexdigest()
        if facts["mod_log_state"] == "APPLIED" and not staged_same:
            print("[cycle] the mod is ON: replacing the enabled copy through a reload request")
            report = reload(dbg, spec, identity, mod_bytes, _confirm(args), timeout=args.timeout)
            write_receipt(report)
            _print(report, None)
            summary["path"] = "reload"
            ok = report.verdict == "RELOADED"
        else:
            if facts["mod_log_state"] == "APPLIED":
                print("[cycle] this exact build is already ON; going straight to watch/verify")
            else:
                ready = check(dbg, spec, identity)
                receipt = write_receipt(ready)
                _print(ready, receipt)
                if not ready.verdict.startswith("READY"):
                    return 1
                staged = apply(dbg, spec, identity, mod_bytes, _confirm(args))
                write_receipt(staged)
                _print(staged, None)
            summary["path"] = "apply"
            ok = True
    if ok and summary["path"] == "apply":
        print("[cycle] switch 'OpenGate A1' ON in MENU -> Mods; watching until APPLIED "
              f"(timeout {args.timeout:.0f} s, Ctrl+C to stop)")
        result = watch(_opener(args), spec, interval=args.interval, until="APPLIED", timeout=args.timeout,
                       timeline=_timeline_path())
        summary["watch"] = result
        ok = result["reached"]
    with _connect(args) as dbg:
        final = verify(dbg, spec, identity)
    receipt = write_receipt(final)
    _print(final, receipt)
    print(f"[cycle] {summary.get('path')} path, final {final.verdict}, total "
          f"{time.perf_counter() - started:.1f} s")
    return 0 if ok and final.verdict == "APPLIED" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.kits.opengate_a6400", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("check", "apply", "verify", "watch", "reload", "cycle", "putback",
                                            "export", "selftest"))
    parser.add_argument("--transport", default="auto", choices=("auto", "winusb", "tcp", "qemu"))
    parser.add_argument("--host", help="TCP debugger host (Wi-Fi debugger)")
    parser.add_argument("--port", type=int, default=4640)
    parser.add_argument("--mod", type=Path, help="OpenGateA1.mod to stage (apply, reload, cycle)")
    parser.add_argument("--build", action="store_true", help="cycle: build the mod in WSL and run the Unicorn gate")
    parser.add_argument("--yes", action="store_true", help="answer the write confirmation with yes")
    parser.add_argument("--until", choices=("APPLIED", "STOCK"), help="watch: stop when this state is reached")
    parser.add_argument("--interval", type=float, default=1.0, help="watch: seconds between polls")
    parser.add_argument("--timeout", type=float, default=300.0, help="watch/reload/cycle: seconds before giving up")
    args = parser.parse_args(argv)

    spec = load_mod_spec()
    identity = load_identity()
    try:
        if args.command == "selftest":
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                for line in selftest(spec, identity, Path(tmp)):
                    print(line)
            return 0
        if args.command == "export":
            with _connect(args) as dbg:
                print(f"log zip: {export(dbg)}")
            return 0
        if args.command == "watch":
            timeline = _timeline_path()
            print(f"watching (Ctrl+C to stop); timeline: {timeline.relative_to(ROOT)}")
            try:
                result = watch(_opener(args), spec, interval=args.interval, until=args.until,
                               timeout=args.timeout if args.until else None, timeline=timeline)
            except KeyboardInterrupt:
                return 0
            return 0 if result.get("reached", True) else 1
        if args.command == "cycle":
            return _cycle(args, spec, identity)
        with _connect(args) as dbg:
            if args.command == "check":
                report = check(dbg, spec, identity)
            elif args.command == "verify":
                report = verify(dbg, spec, identity)
            elif args.command == "putback":
                report = putback(dbg, spec, identity)
            else:
                if not args.mod or not args.mod.is_file():
                    raise KitError(f"{args.command} needs --mod <path to the built OpenGateA1.mod> "
                                   "(wsl.exe --exec bash mods/opengate_a1/build_v2_wsl.sh <out-dir>)")
                if args.command == "reload":
                    report = reload(dbg, spec, identity, args.mod.read_bytes(), _confirm(args), timeout=args.timeout)
                else:
                    report = apply(dbg, spec, identity, args.mod.read_bytes(), _confirm(args))
        receipt = write_receipt(report)
        _print(report, receipt)
        return 0 if report.verdict.startswith(("READY", "STAGED", "APPLIED", "STOCK", "RELOADED")) else 1
    except KitError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"LINK: cannot reach the camera debugger ({exc}). Nothing more was sent. Check that the camera "
              "is on and on Wi-Fi (ping), or that tcp_debugd still runs; then repeat the command.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
