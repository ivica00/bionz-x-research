#!/usr/bin/env python3
"""Mod development runtime for the sony-a6400 QEMU guest (WSL side).

Runs the unchanged ARM Mod Loader v2 inside the emulated camera and keeps it in sync with host
build outputs:

  1. Before boot, the guard and its request are staged on the run's disposable /setting overlay
     (they are too large for the serial line). See ``setting_files``.
  2. After the guest shell answers and the main firmware reaches its menu checkpoint, a tmpfs is
     mounted at /tmp_bt (guest only), the core and every mod are pushed over the serial console,
     each file is checked with the guard's ``fnv`` operation and published by rename, and the
     guard runs ``check`` then ``install``. The guard relocates the shim and hooks for this
     process layout; nothing here writes guest memory directly.
  3. A watcher polls the host files. A changed ``.mod`` is pushed and handed to the core's reload
     request (``/tmp_bt/mlv2/reload``, see docs/evidence/0576-mod-loader-v2-reload-request.md); a changed ``core.bin`` is
     pushed and picked up by the shim at the next menu root. The core acts on the Sony UI thread
     at the next menu refresh, so a ``nudge`` callback can send menu keys when nothing happens.
  4. The panel toolbar queues actions (``request``): load or watch another mod file, force a
     reload, stop watching and delete a mod file in the guest, re-push the core. The watcher
     thread runs them one at a time between polls, so the serial console has one user.

The serial console uses QEMU's ``-serial unix:PATH,server=on,wait=off`` (run.sh ``SERIAL``).
"""
from __future__ import annotations

import os
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

GUARD_NAME, REQ_NAME = "MLV2GRD.ELF", "MLV2.REQ"
GUEST_GUARD, GUEST_REQ = "/tmp_bt/mlv2_guard", "/tmp_bt/mlv2.req"
RELOAD_REQ, RELOAD_RESULT = "/tmp_bt/mlv2/reload", "/tmp_bt/mlv2/reload.result"
MOD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}\.mod$")
CHUNK = 200            # bytes per printf line: 4 chars each; the guest tty truncates lines near 1 KiB


def fnv1a_32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def push_commands(path: str, data: bytes) -> list[str]:
    """Shell lines that recreate ``data`` at ``path`` with the ash printf builtin."""
    if not path.startswith("/") or any(c in path for c in " '\"$`\\\n"):
        raise ValueError(f"unsupported guest path {path!r}")
    chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    lines = []
    for i, c in enumerate(chunks):
        escaped = "".join("\\%03o" % b for b in c)     # octal escapes in the format: no literal %
        lines.append("printf '" + escaped + "' " + (">" if i == 0 else ">>") + " " + path)
    return lines


def parse_fnv(text: str) -> tuple[int, int] | None:
    m = re.search(r"FNV size=(\d+) fnv=([0-9a-f]{8})", text or "")
    return (int(m.group(1)), int(m.group(2), 16)) if m else None


def setting_files(guard: Path, request: Path) -> str:
    """run.sh SETTING_FILES value (host paths must not contain spaces)."""
    for p in (guard, request):
        if " " in str(p):
            raise ValueError(f"stage {p} from a path without spaces")
    return f"{guard}:{GUARD_NAME} {request}:{REQ_NAME}"


class Console:
    """The guest shell on a QEMU unix-socket serial port. Output is mirrored to a log file."""

    def __init__(self, path: Path, log: Path, process=None):
        self.buf = bytearray()
        self.lock = threading.Condition()
        self.sock = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic() + 20
        while True:
            try:
                self.sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if (process and process.poll() is not None) or time.monotonic() > deadline:
                    raise RuntimeError("QEMU serial socket did not start")
                time.sleep(0.1)
        self.log = open(log, "ab", buffering=0)
        self.tag = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                with self.lock:
                    self.closed = True
                    self.lock.notify_all()
                return
            self.log.write(data)
            with self.lock:
                self.buf += data
                if len(self.buf) > 4 << 20:
                    del self.buf[:2 << 20]
                self.lock.notify_all()

    closed = False

    def wait_for(self, needle: bytes, timeout: float, start: int = 0) -> int:
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                i = self.buf.find(needle, start)
                if i >= 0:
                    return i
                left = deadline - time.monotonic()
                if left <= 0 or self.closed:
                    return -1
                self.lock.wait(min(left, 0.5))

    def mark(self) -> int:
        with self.lock:
            return len(self.buf)

    def send(self, line: str):
        self.sock.sendall(line.encode() + b"\r")

    def run(self, command: str, timeout: float = 60) -> str | None:
        """Run one shell command and return its output (between unique markers)."""
        self.tag += 1
        begin, end = f"@@B{self.tag}@@", f"@@E{self.tag}@@"
        start = self.mark()
        self.send(f'echo {begin[:3]}""{begin[3:]}; {command}; echo {end[:3]}""{end[3:]}')
        b = self.wait_for(begin.encode(), timeout, start)
        if b < 0:
            return None
        e = self.wait_for(end.encode(), timeout, b)
        if e < 0:
            return None
        with self.lock:
            return self.buf[b + len(begin):e].decode("latin1").replace("\r", "")


@dataclass
class ModDev:
    console: Console
    loader: Path                     # Mod Loader v2 build: core.bin, mlv2.req, mlv2_guard_arm
    mods: list[Path]
    nudge: callable = None           # called when the core has not acted yet (e.g. MENU twice)
    status: dict = field(default_factory=lambda: {"state": "waiting for the guest shell", "events": []})
    seen: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)   # one shell command at a time
    actions: queue.Queue = field(default_factory=queue.Queue)       # toolbar requests
    last: dict = field(default_factory=dict)                        # mod name -> last result

    def __post_init__(self):
        self.mods = list(self.mods)
        self.publish()

    def publish(self):
        self.status["mods"] = [{"name": m.name, "path": str(m), "last": self.last.get(m.name, "")}
                               for m in self.mods]

    def request(self, action: str, arg=None):
        """Queue a toolbar action: add (Path), reload (name), remove (name), core, rescan."""
        if action not in ("add", "reload", "remove", "core", "rescan"):
            raise ValueError(f"unknown action {action}")
        if action == "add" and not MOD_NAME.match(Path(arg).name):
            raise ValueError("a mod file name is NAME.mod (letters, digits, _ . -)")
        if action in ("reload", "remove") and arg not in {m.name for m in self.mods}:
            raise ValueError(f"no mod named {arg}")
        self.actions.put((action, arg))
        self.status["queued"] = self.actions.qsize()

    def find(self, name: str) -> Path:
        return next(m for m in self.mods if m.name == name)

    def run_action(self, action: str, arg):
        if action == "add":
            path = Path(arg)
            old = next((m for m in self.mods if m.name == path.name), None)
            if old is not None:
                self.mods[self.mods.index(old)] = path
                self.seen[path] = path.stat().st_mtime_ns
                self.last[path.name] = self.reload(path) or "pending"
            else:
                self.mods.append(path)
                self.push(f"/tmp_bt/mods/{path.name}", path.read_bytes())
                self.seen[path] = path.stat().st_mtime_ns
                self.last[path.name] = "loaded: listed at the next menu open"
                self.event(f"{path.name} added; it is listed when the menu root is set next")
                if self.nudge:
                    self.nudge()
        elif action == "reload":
            m = self.find(arg)
            self.seen[m] = m.stat().st_mtime_ns
            self.last[m.name] = self.reload(m) or "pending"
        elif action == "remove":
            m = self.find(arg)
            self.mods.remove(m)
            self.last.pop(m.name, None)
            self.sh(f"rm -f /tmp_bt/mods/{m.name}")
            self.event(f"{m.name}: no longer watched; guest file deleted (an ON copy stays until switched OFF)")
        elif action == "core":
            self.seen.pop("core", None)
        elif action == "rescan" and self.nudge:
            self.nudge()
            self.event("MENU pressed: the menu root is set again when the menu opens")
        self.publish()

    def event(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        self.status["events"] = (self.status["events"] + [f"{stamp} {text}"])[-30:]
        print(f"[moddev] {text}", flush=True)

    def sh(self, command: str, timeout: float = 60) -> str:
        with self.lock:
            out = self.console.run(command, timeout)
        if out is None:
            raise RuntimeError(f"guest shell did not answer: {command[:60]}")
        return out

    def guest_fnv(self, path: str) -> tuple[int, int] | None:
        return parse_fnv(self.sh(f"{GUEST_GUARD} fnv {path}"))

    def push(self, guest_path: str, data: bytes):
        part = guest_path.rsplit("/", 1)
        tmp = f"{part[0]}/.{part[1]}.part"
        t0 = time.monotonic()
        for line in push_commands(tmp, data):
            self.sh(line, 30)
        got = self.guest_fnv(tmp)
        if got != (len(data), fnv1a_32(data)):
            self.sh(f"rm -f {tmp}")
            raise RuntimeError(f"transfer check failed for {guest_path}: {got}")
        self.sh(f"mv -f {tmp} {guest_path}")
        self.event(f"pushed {guest_path} ({len(data)} B, {time.monotonic() - t0:.1f}s, fnv ok)")

    def wait_menu(self, timeout: float = 240):
        self.status["state"] = "booting"
        if self.console.wait_for(b"built-in shell (ash)", timeout) < 0:
            raise RuntimeError("no guest shell")
        self.console.wait_for(b"# ", 30, self.console.buf.find(b"built-in shell (ash)"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.console.run("grep -c SetContentTarget /proc/snsc_boot_time", 60) or ""
            if re.search(r"^[1-9]\d*$", out.strip().split("\n")[-1] if out.strip() else "", re.M):
                self.event("main firmware reached SetContentTarget")
                return
            time.sleep(2)
        raise RuntimeError("menu checkpoint not reached")

    def install(self):
        self.status["state"] = "installing"
        self.sh("grep -q ' /tmp_bt ' /proc/mounts || mount -t tmpfs tmpfs /tmp_bt; mkdir -p /tmp_bt/mlv2 /tmp_bt/mods")
        self.sh(f"cp /setting/{GUARD_NAME} {GUEST_GUARD} && chmod 700 {GUEST_GUARD} && cp /setting/{REQ_NAME} {GUEST_REQ}")
        for host, guest in ((self.loader / "mlv2_guard_arm", GUEST_GUARD), (self.loader / "mlv2.req", GUEST_REQ)):
            data = host.read_bytes()
            if self.guest_fnv(guest) != (len(data), fnv1a_32(data)):
                raise RuntimeError(f"staged {guest} does not match {host}")
        self.push("/tmp_bt/mlv2/core.bin", (self.loader / "core.bin").read_bytes())
        self.seen["core"] = (self.loader / "core.bin").stat().st_mtime_ns
        for m in self.mods:
            self.push(f"/tmp_bt/mods/{m.name}", m.read_bytes())
            self.seen[m] = m.stat().st_mtime_ns
        check = self.sh(f"{GUEST_GUARD} check {GUEST_REQ}; echo GUARD_EXIT=$?")
        self.event(" ".join(l for l in check.splitlines() if l.startswith(("TARGET", "STATE", "MLV2_RESULT"))))
        if "hooks=stock" not in check:
            raise RuntimeError("view hooks are not stock; not installing")
        out = self.sh(f"{GUEST_GUARD} install {GUEST_REQ}; echo GUARD_EXIT=$?", 120)
        result = next((l for l in out.splitlines() if l.startswith("MLV2_RESULT")), "")
        self.event(f"guard install: {result}")
        if "MLV2_RESULT=0" not in result:
            raise RuntimeError(f"guard install failed: {out}")
        self.status["state"] = "installed: open MENU to load the core"

    def loader_log(self) -> str:
        return self.sh("cat /tmp_bt/mods/mod_loader.log 2>/dev/null")

    def report(self) -> dict:
        """Loader log, last reload result and pending request, read from the guest."""
        if self.status["state"].startswith(("waiting", "booting")):
            return {"state": self.status["state"]}
        out = self.sh(f"cat /tmp_bt/mods/mod_loader.log 2>/dev/null; echo @@RESULT; cat {RELOAD_RESULT} 2>/dev/null;"
                      f" test -f {RELOAD_REQ} && echo PENDING")
        log, _, rest = out.partition("@@RESULT")
        return {"state": self.status["state"], "loader_log": log.strip().splitlines(),
                "reload_result": rest.replace("PENDING", "").strip(), "reload_pending": "PENDING" in rest}

    def reload(self, mod: Path, timeout: float = 30):
        """Push and request a reload. The core acts at the next menu refresh or root setup; without
        one, the nudge presses MENU (opening sets the root) and once more if that closed the menu."""
        self.push(f"/tmp_bt/mods/{mod.name}", mod.read_bytes())
        self.sh(f"rm -f {RELOAD_RESULT}; echo {mod.name} > {RELOAD_REQ}.part && mv -f {RELOAD_REQ}.part {RELOAD_REQ}")
        start, nudges = time.monotonic(), 0
        while time.monotonic() < start + timeout:
            out = self.sh(f"cat {RELOAD_RESULT} 2>/dev/null").strip()
            if out.startswith("reload_"):
                self.event(f"{mod.name}: {out}")
                return out
            if self.nudge and nudges < 2 and time.monotonic() > start + 2 + 3 * nudges:
                self.nudge()
                nudges += 1
            time.sleep(0.5)
        self.event(f"{mod.name}: reload request still pending (open or move in the Sony menu)")
        return None

    def watch_once(self):
        core = self.loader / "core.bin"
        if core.exists() and core.stat().st_mtime_ns != self.seen.get("core"):
            time.sleep(0.3)
            self.seen["core"] = core.stat().st_mtime_ns
            self.push("/tmp_bt/mlv2/core.bin", core.read_bytes())
            self.event("core.bin updated; the shim loads it at the next menu root")
            if self.nudge:
                self.nudge()
        for m in self.mods:
            if m.exists() and m.stat().st_mtime_ns != self.seen.get(m):
                time.sleep(0.3)                       # let the build finish writing
                self.seen[m] = m.stat().st_mtime_ns
                self.last[m.name] = self.reload(m) or "pending"

    def run(self, poll: float = 0.5):
        try:
            self.wait_menu()
            self.install()
            if self.nudge:
                # MENU opens the menu: the shim loads the core at this root setup. The menu instance
                # opened in that same step can still show six tabs, so close and reopen it once.
                self.nudge()
                time.sleep(4)
                self.nudge()
                time.sleep(2)
                self.nudge()
                time.sleep(3)
            log = self.loader_log()
            self.event("loader log: " + " | ".join(log.split("\n")[-5:]))
            if "core_load" not in log:
                self.event("core not loaded yet: press MENU in the panel to open the Sony menu")
            else:
                self.event("menu opened: the Mods tab is the last tab (left of the first one)")
            self.status["state"] = "watching for rebuilt mods"
            while not self.console.closed:
                try:
                    action, arg = self.actions.get(timeout=poll)
                except queue.Empty:
                    action = None
                if action:
                    self.status["queued"] = self.actions.qsize()
                    try:
                        self.run_action(action, arg)
                    except (OSError, RuntimeError, ValueError, StopIteration) as error:
                        self.event(f"{action} failed: {error}")
                self.watch_once()
                self.publish()
        except Exception as error:      # reported in the panel; QEMU keeps running
            self.status["state"] = f"stopped: {error}"
            self.event(f"stopped: {error}")


def host_path(text: str) -> Path:
    """A Windows path typed in the browser (C:\\...) as seen from WSL."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", text.strip().strip('"'))
    if m:
        return Path("/mnt") / m.group(1).lower() / m.group(2).replace("\\", "/")
    return Path(text.strip())


def ensure_linux_inputs(inputs: Path) -> Path:
    """Prepared inputs on /mnt/* are read through WSL's 9P bridge on every guest disk access
    (boot took 63 s instead of 35 s). Copy them once to the Linux filesystem and reuse the copy."""
    if not str(inputs.resolve()).startswith("/mnt/"):
        return inputs
    dest = Path.home() / "a6400-inputs" / inputs.name
    manifest = inputs / "boot-manifest.json"
    if not (dest / "boot-manifest.json").exists() or (dest / "boot-manifest.json").read_bytes() != manifest.read_bytes():
        dest.mkdir(parents=True, exist_ok=True)
        for f in inputs.iterdir():
            if f.is_file():
                tmp = dest / (f.name + ".part")
                with open(f, "rb") as src, open(tmp, "wb") as dst:
                    while chunk := src.read(16 << 20):
                        dst.write(chunk)
                os.replace(tmp, dest / f.name)
    return dest
