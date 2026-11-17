#!/usr/bin/env python3
"""Serve the live QEMU LCD and its physical controls on WSL loopback.

With --loader the panel also runs the mod development loop (moddev.py): it boots the prepared
camera-captured inputs (copied to the Linux filesystem for speed), installs the guarded Mod
Loader v2 build in the guest and hot-reloads every --mod file and the loader core when they are
rebuilt. The page itself is re-read on every load, so panel edits show on a browser refresh.

The page toolbar (POST, same-origin with X-Camera-Panel: 1):
  /mods/upload?name=NAME.mod   body = SMOD file (saved under ~/a6400-moddev/mods, then loaded)
  /mods/watch                  body = {"path": host or Windows path of a build output to watch}
  /mods/reload/NAME, /mods/remove/NAME, /loader/core, /loader/rescan
  /emu/pause, /emu/resume, /emu/restart (reboots QEMU; loaded mods are kept)
GET /emu reports the run state.
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
from struct import pack
import subprocess
import shutil
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zlib

import moddev


KEYS = {
    "flash": "l",
    "menu": "m",
    "af-button": "a",
    "af-lever": "b",
    "movie": "r",
    "fn": "f",
    "disp": "up",
    "drive": "left",
    "center": "ret",
    "iso": "right",
    "exposure": "down",
    "wheel-left": "bracket_left",
    "wheel-right": "bracket_right",
    "play": "p",
    "delete": "delete",
}


class Qmp:
    def __init__(self, path, process):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(5)
        deadline = time.monotonic() + 15
        while True:
            try:
                self.socket.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.socket.close()
                    raise RuntimeError("QEMU monitor did not start. See console.log.")
                time.sleep(0.1)
        self.stream = self.socket.makefile("rwb", buffering=0)
        self.lock = threading.Lock()
        self.read()
        self.command("qmp_capabilities")

    def read(self):
        line = self.stream.readline()
        if not line:
            raise RuntimeError("QEMU monitor disconnected")
        return json.loads(line)

    def command(self, name, **arguments):
        request = {"execute": name, "arguments": arguments}
        with self.lock:
            self.socket.sendall(json.dumps(request).encode() + b"\n")
            while True:
                reply = self.read()
                if "error" in reply:
                    raise RuntimeError(reply["error"]["desc"])
                if "return" in reply:
                    return reply["return"]

    def close(self):
        self.stream.close()
        self.socket.close()


def png_from_ppm(image):
    magic, size, depth, pixels = image.split(b"\n", 3)
    width, height = map(int, size.split())
    stride = width * 3
    if magic != b"P6" or depth != b"255" or len(pixels) != stride * height:
        raise ValueError("Unexpected QEMU screendump format")

    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", zlib.crc32(kind + data))

    scanlines = b"".join(b"\0" + pixels[i:i + stride]
                         for i in range(0, len(pixels), stride))
    header = pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))
    return png, pixels.count(0) == len(pixels)


UPLOADS = Path.home() / "a6400-moddev" / "mods"
MAX_MOD = 1 << 20


def check_smod(data):
    if len(data) < 68 or data[:4] != b"SMOD" or data[4:8] not in (b"\1\0\0\0", b"\2\0\0\0"):
        raise ValueError("not an SMOD v1/v2 file")


def serve(args, scratch, server):
    """Run one QEMU instance. Returns True when the toolbar asked for a restart."""
    directory = Path(__file__).resolve().parent
    monitor = scratch / "qmp"
    frame = scratch / "lcd.ppm"
    origin = f"http://127.0.0.1:{server.server_port}"
    env = dict(os.environ, RUN_DIR=str(scratch / "run"), TIMEOUT="0",
               REAR_CONTROLS="1", REAR_LCD_DIAGNOSTIC=str(int(not args.stock)),
               QEMU_EXTRA=f"-icount shift=0,sleep=on -qmp unix:{monitor},server=on,wait=off")
    if args.inputs:
        env["A6400_QEMU_DATA_DIR"] = str(moddev.ensure_linux_inputs(args.inputs))
        env["ULOGIO"] = ""                    # the captured /setting already has its ulogio.bin
    if args.loader:
        for name in ("mlv2_guard_arm", "mlv2.req"):
            shutil.copy(args.loader / name, scratch / name)
        env["SETTING_FILES"] = moddev.setting_files(scratch / "mlv2_guard_arm", scratch / "mlv2.req")
        env["SERIAL"] = f"unix:{scratch / 'serial'},server=on,wait=off"
    with args.log.open("xb") as log:
        process = subprocess.Popen(["bash", str(directory.parent / "run.sh")],
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, env=env)
        qmp = None
        dev = None
        server.restart = False
        started = time.time()
        try:
            qmp = Qmp(monitor, process)
            if args.loader:
                console = moddev.Console(scratch / "serial", args.log.with_suffix(".serial.log"), process)

                def nudge():
                    qmp.command("send-key", keys=[{"type": "qcode", "data": KEYS["menu"]}], **{"hold-time": 180})

                dev = moddev.ModDev(console, args.loader, list(args.mod), nudge=nudge)
                threading.Thread(target=dev.run, daemon=True).start()

            def get(handler):
                if handler.path == "/":
                    handler.reply("text/html; charset=utf-8", (directory / "rear_panel.html").read_bytes())
                elif handler.path == "/moddev":
                    if not dev:
                        handler.send_error(404)
                    else:
                        handler.reply("application/json", json.dumps(dev.status).encode())
                elif handler.path == "/emu":
                    handler.reply("application/json", json.dumps(emu_state()).encode())
                elif handler.path == "/moddev/log" and dev:
                    handler.reply("application/json", json.dumps(dev.report(), indent=1).encode())
                elif handler.path == "/lcd.png":
                    qmp.command("screendump", filename=str(frame), device="vph", head=0)
                    png, blank = png_from_ppm(frame.read_bytes())
                    handler.reply("image/png", png, lcd_state="blank" if blank else "visible")
                else:
                    handler.send_error(404)

            def emu_state():
                status = qmp.command("query-status")
                return {"running": status.get("running"), "state": status.get("status"),
                        "uptime_s": int(time.time() - started), "moddev": bool(dev),
                        "log": str(args.log)}

            def body(handler, limit):
                size = int(handler.headers.get("Content-Length") or 0)
                if size > limit:
                    raise ValueError("request body too large")
                return handler.rfile.read(size)

            def post(handler):
                url = urlsplit(handler.path)
                path = url.path
                if handler.headers.get("Origin") != origin or handler.headers.get("X-Camera-Panel") != "1":
                    handler.send_error(403)
                    return
                name = path.removeprefix("/press/")
                if path == f"/press/{name}" and name in KEYS:
                    qmp.command("send-key", keys=[{"type": "qcode", "data": KEYS[name]}],
                                **{"hold-time": 180})
                    handler.reply("text/plain", b"")
                elif path in ("/emu/pause", "/emu/resume"):
                    qmp.command("stop" if path == "/emu/pause" else "cont")
                    handler.reply("application/json", json.dumps(emu_state()).encode())
                elif path == "/emu/restart":
                    server.restart = True
                    handler.reply("application/json", b'{"restarting": true}')
                elif not dev:
                    handler.send_error(404, "panel started without --loader")
                else:
                    try:
                        if path == "/mods/upload":
                            name = parse_qs(url.query).get("name", [""])[0]
                            if not moddev.MOD_NAME.match(name):
                                raise ValueError("a mod file name is NAME.mod (letters, digits, _ . -)")
                            data = body(handler, MAX_MOD)
                            check_smod(data)
                            UPLOADS.mkdir(parents=True, exist_ok=True)
                            tmp = UPLOADS / (name + ".part")
                            tmp.write_bytes(data)
                            os.replace(tmp, UPLOADS / name)
                            dev.request("add", UPLOADS / name)
                        elif path == "/mods/watch":
                            target = moddev.host_path(json.loads(body(handler, 4096) or b"{}").get("path", ""))
                            if not target.is_file():
                                raise ValueError(f"{target} is not a file visible from WSL")
                            check_smod(target.read_bytes()[:68])
                            dev.request("add", target)
                        elif path.startswith(("/mods/reload/", "/mods/remove/")):
                            dev.request(path.split("/")[2], path.split("/", 3)[3])
                        elif path == "/loader/core":
                            dev.request("core")
                        elif path == "/loader/rescan":
                            dev.request("rescan")
                        else:
                            handler.send_error(404)
                            return
                    except ValueError as error:
                        handler.reply("application/json", json.dumps({"error": str(error)}).encode(), code=400)
                        return
                    handler.reply("application/json", json.dumps(dev.status).encode())

            server.get = get
            server.post = post
            print(f"{origin}/\nQEMU log: {args.log}", flush=True)
            while process.poll() is None and not server.restart:
                server.handle_request()
            if dev:
                args.mod = list(dev.mods)       # a restart reloads the same mods
            return server.restart
        finally:
            if qmp:
                qmp.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


class PanelRequest(BaseHTTPRequestHandler):
    def reply(self, content_type, body, *, lcd_state=None, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if lcd_state is not None:
            self.send_header("X-Camera-LCD", lcd_state)
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, action):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        try:
            action(self)
        except (OSError, RuntimeError, ValueError) as error:
            self.log_error("%s", error)
            self.send_error(503, "QEMU display or input unavailable")

    def do_GET(self):
        self.dispatch(self.server.get)

    def do_POST(self):
        self.dispatch(self.server.post)

    def log_request(self, code="-", size="-"):
        if str(code) != "200":
            super().log_request(code, size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8640)
    parser.add_argument("--log", type=Path, required=True, help="New QEMU serial log path")
    parser.add_argument("--stock", action="store_true", help="Use the stock firmware display selection")
    parser.add_argument("--inputs", type=Path, help="Prepared camera-captured inputs (qemu_boot_inputs prepare)")
    parser.add_argument("--loader", type=Path, help="Mod Loader v2 build directory to install and watch")
    parser.add_argument("--mod", type=Path, action="append", default=[], help="SMOD file to load and hot-reload")
    args = parser.parse_args()
    if args.loader and not args.inputs:
        parser.error("--loader needs --inputs: mods target the camera's own library builds")
    base_log = args.log
    with HTTPServer(("127.0.0.1", args.port), PanelRequest) as server:
        server.timeout = 1
        for run in range(1, 1000):
            args.log = base_log if run == 1 else base_log.with_name(f"{base_log.stem}-r{run}{base_log.suffix}")
            with tempfile.TemporaryDirectory(prefix="a6400-panel-") as directory:
                try:
                    if not serve(args, Path(directory), server):
                        break
                except KeyboardInterrupt:
                    break
            print("restarting QEMU", flush=True)


if __name__ == "__main__":
    main()
