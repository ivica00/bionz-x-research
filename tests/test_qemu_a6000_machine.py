"""QEMU sony-a6000 (ILCE-6000, CXD90014) machine: firmware-free checks.

The machine boots only with the user's own Sony updater and NAND dump
(emulators/a6000/README.md), so these tests do not boot it. They check that
the machine registers, that it builds its board with a placeholder kernel
image, and that the a6000 tooling keeps its inputs out of Git.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from project_paths import PROJECT_ROOT
from tests.test_qemu_emmc import NATIVE_AVAILABLE, QEMU

A6000_DIR = PROJECT_ROOT / "emulators/a6000"
_SPEC = importlib.util.spec_from_file_location(
    "a6000_inputs", A6000_DIR / "tools/inputs.py")
inputs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inputs)
_SPEC = importlib.util.spec_from_file_location(
    "a6000_mknand_synth", A6000_DIR / "tools/mknand_synth.py")
synth = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(synth)

# Device models that only the A6000 board instantiates.
A6000_ONLY_DEVICES = {
    "cxd90014-aif", "cxd90014-campig", "cxd90014-cec", "cxd90014-ddrc",
    "cxd90014-dfe", "cxd90014-dmac", "cxd90014-hdmi", "cxd90014-idt",
    "cxd90014-jog",
    "cxd90014-ldec", "cxd90014-mdma", "cxd90014-nand", "cxd90014-opd",
    "cxd90014-sysctl", "cxd90014-usbhdc", "a6k-nfc", "a6k-fbview",
    "ar6003-sdio",
}
# Shared CXD90014 models the A6000 board reuses from the a6400 overlay.
SHARED_DEVICES = {
    "cxd90014-adc", "cxd90014-evsel", "cxd90014-gpio", "cxd90014-p2s",
    "cxd90014-regfile", "cxd90014-sa", "cxd90014-sio", "cxd90014-vph",
    "cxd4115-timer", "dmp-smaph-s30",
}


def _hmp(machine: str, commands: list[str], kernel: Path | None) -> subprocess.CompletedProcess:
    args = [str(QEMU), "-M", machine, "-display", "none", "-S",
            "-monitor", "stdio", "-serial", "null"]
    if kernel is not None:
        args += ["-kernel", str(kernel)]
    script = "".join(c + "\n" for c in commands + ["quit"])
    return subprocess.run(args, input=script, capture_output=True, text=True, timeout=60)


@unittest.skipUnless(NATIVE_AVAILABLE, "Optional WSL/Linux sony-a6400 QEMU build absent")
class QemuA6000MachineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Placeholder kernel: the machine is only constructed (-S), never run.
        self.kernel = Path(tmp.name) / "placeholder.bin"
        self.kernel.write_bytes(bytes(4096))

    def test_machine_is_registered(self) -> None:
        out = subprocess.run([str(QEMU), "-M", "help"], capture_output=True,
                             text=True, timeout=30, check=True).stdout
        self.assertRegex(out, r"(?m)^sony-a6000\s+Sony ILCE-6000 \(CXD90014")

    def test_requires_a_kernel(self) -> None:
        proc = _hmp("sony-a6000", [], kernel=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("-kernel vmlinux.bin is required", proc.stderr + proc.stdout)

    def test_board_instantiates_a6000_and_shared_devices(self) -> None:
        proc = _hmp("sony-a6000", ["info qtree"], self.kernel)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        types = {line.split()[1].rstrip(",")
                 for line in proc.stdout.splitlines() if line.strip().startswith("dev:")}
        self.assertLessEqual(A6000_ONLY_DEVICES | SHARED_DEVICES, types)

    def test_nand_snapshot_opens_the_file_read_only(self) -> None:
        nand = self.kernel.with_name("a6000.nand")
        with nand.open("wb") as stream:
            stream.truncate(inputs.NAND_SIZE)
        nand.chmod(0o444)
        before = nand.stat().st_mtime_ns
        proc = _hmp(f"sony-a6000,nand={nand},nand-snapshot=on", [], self.kernel)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(nand.stat().st_mtime_ns, before)
        if os.geteuid() != 0:
            writable = _hmp(f"sony-a6000,nand={nand}", [], self.kernel)
            self.assertIn("cannot open", writable.stderr)

    def test_nand_snapshot_rejects_a_wrong_size_file(self) -> None:
        nand = self.kernel.with_name("short.nand")
        nand.write_bytes(b"short")
        proc = _hmp(f"sony-a6000,nand={nand},nand-snapshot=on", [], self.kernel)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("wrong size", proc.stderr)
        self.assertEqual(nand.read_bytes(), b"short")

    def test_vph_scan_without_arm_is_a6000_only(self) -> None:
        a6000 = _hmp("sony-a6000",
                     ["qom-get /machine/peripheral/vph scan-without-arm"], self.kernel)
        self.assertIn("true", a6000.stdout)
        a6400 = subprocess.run(
            [str(QEMU), "-device", "cxd90014-vph,help"],
            capture_output=True, text=True, timeout=30).stdout
        self.assertRegex(a6400, r"scan-without-arm=<bool>.*\(default: off\)")


class A6000InputManifestTests(unittest.TestCase):
    """tools/inputs.py against a constructed updater tree and dump."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "inputs.local.json"
        self.dump = self.root / "dump.bin"
        self.dump.write_bytes(b"constructed dump")
        for name in inputs.FW_FILES:
            path = self.root / "fw" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        (self.root / "data").mkdir()
        for name in inputs.DATA_FILES:
            (self.root / "data" / name).write_bytes(name.encode())

    def run_inputs(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return inputs.main(["--config", str(self.config), *args])

    def init(self) -> int:
        return self.run_inputs("init", "--dump", str(self.dump),
                               "--fw", str(self.root / "fw"),
                               "--data", str(self.root / "data"))

    def test_mismatch_is_reported_and_not_refreshed(self) -> None:
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.run_inputs("check"), 0)
        recorded = self.config.read_bytes()
        self.dump.write_bytes(b"changed")
        self.assertEqual(self.run_inputs("check"), 1)
        self.assertEqual(self.init(), 1)
        self.assertEqual(self.config.read_bytes(), recorded)

    def test_recorded_nand_is_checked(self) -> None:
        nand = self.root / "a6000.nand"
        nand.write_bytes(b"constructed nand")
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.run_inputs("check", "--nand"), 1)
        self.assertEqual(self.run_inputs("record-nand", str(nand)), 0)
        self.assertEqual(self.run_inputs("check", "--nand"), 0)
        nand.write_bytes(b"written by a run")
        self.assertEqual(self.run_inputs("check", "--nand"), 1)

    def test_adopted_nand_is_a_read_only_copy(self) -> None:
        source = self.root / "provisioned.nand"
        with source.open("wb") as stream:
            stream.truncate(inputs.NAND_SIZE)
        base = self.root / "state" / "a6000.nand"
        self.assertEqual(self.run_inputs(
            "init", "--nand", str(source), "--base", str(base),
            "--fw", str(self.root / "fw"), "--data", str(self.root / "data")), 0)
        self.assertEqual(base.stat().st_mode & 0o222, 0)
        self.assertEqual(self.run_inputs("check", "--nand"), 0)


class _FakeWbi:
    """Virtual memory and raw RAM standing in for the factory warm-boot image."""

    def __init__(self, ram: bytes = b"") -> None:
        self.memory: dict[int, int] = {}
        self.ram = ram

    def put(self, va: int, data: bytes) -> None:
        for index, byte in enumerate(data):
            self.memory[va + index] = byte

    def read(self, va: int, length: int) -> bytes:
        return bytes(self.memory.get(va + index, 0) for index in range(length))


def _words(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}I", *values)


class A6000SyntheticNandTests(unittest.TestCase):
    END = 0xFFFFFFFF

    def test_table_sector_holds_partinf_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "0300_partconf/partinf.conf"
            conf.parent.mkdir()
            conf.write_text("# header\n\n0x00040000,0x00600000,0x00000001,0xFFFFFFFF\n"
                            "0x08040000,0x00000000,0x00000000,0x00000000\n", encoding="latin-1")
            rows = synth.partitions(Path(tmp))
        sector = synth.table_sector(rows)
        self.assertEqual(sector[:8], b"82461.00")
        self.assertEqual(struct.unpack_from("<I", sector, 8)[0], 2)
        self.assertEqual(struct.unpack_from("<4I", sector, 0x20), (0x40000, 0x600000, 1, self.END))
        self.assertEqual(len(sector), 512)

    def test_dmm_config_serializes_tables_in_read_order(self) -> None:
        wbi, base, end = _FakeWbi(), synth.DMM_BSS, self.END
        (groups1, entries1), (groups2, entries2) = synth.DMM_EXT_TABLES
        # dmm stores pointers in group word 0 and unit word 1; the file has 0.
        wbi.put(base + groups1, _words(0x3F000001, 7, 1, *[0] * 17) + _words(0, 0, end, *[0] * 17))
        wbi.put(base + entries1, _words(1, 2, 3, 4, 5) + _words(0, 0, end, 0, 0))
        wbi.put(base + groups2, _words(0, 0, end, *[0] * 17))
        wbi.put(base + synth.DMM_UNITS, _words(0x2D7, 0x3F000002) + _words(end, 0))
        wbi.put(base + synth.DMM_UNIT_IDS, _words(0x3EF, end))
        wbi.put(base + synth.DMM_VERSION, b"for test build".ljust(32, b"\0"))
        expected = (_words(0, 7, 1, *[0] * 17) + _words(1, 2, 3, 4, 5) + _words(0, 0, end, 0, 0)
                    + _words(0, 0, end, *[0] * 17) + _words(0, 0, end, *[0] * 17)
                    + _words(0x2D7, 0, 0x3EF, end) + _words(end, 0)
                    + b"for test build".ljust(32, b"\0"))
        self.assertEqual(synth.dmm_config(wbi), expected)

    def test_dmm_config_refuses_an_image_without_tables(self) -> None:
        wbi = _FakeWbi()
        end = self.END
        for groups, _ in synth.DMM_EXT_TABLES:
            wbi.put(synth.DMM_BSS + groups, _words(0, 0, end))
        wbi.put(synth.DMM_BSS + synth.DMM_UNITS, _words(end, 0))
        with self.assertRaises(ValueError):
            synth.dmm_config(wbi)

    def test_ulogio_config_takes_the_one_record_chain(self) -> None:
        chain = _words(12) + b"LGNM" + b"name" + _words(12) + b"RECO" + _words(1)
        noise = b"LGNM" + b"\xff" * 12          # the tag alone, no valid chain
        wbi = _FakeWbi(b"\x55" * 64 + noise + b"\0" * 8 + chain + _words(0, 0))
        self.assertEqual(synth.ulogio_config(wbi), chain)
        with self.assertRaises(ValueError):
            synth.ulogio_config(_FakeWbi(chain + _words(0) + chain + _words(0)))

    @unittest.skipUnless(shutil.which("mkfs.vfat") and shutil.which("fsck.vfat"),
                         "dosfstools absent")
    def test_fat_writer_makes_a_clean_volume(self) -> None:
        fat = synth.blank_fat(4 * 1024 * 1024)
        fat.add_file("DmmConfig.bin", bytes(range(256)) * 200)
        fat.add_file("empty.txt", b"")
        fat.add_dir("appfw_infra")
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "fat.img"
            image.write_bytes(fat.image)
            result = subprocess.run(["fsck.vfat", "-n", "-l", str(image)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("DmmConfig.bin", "empty.txt", "appfw_infra"):
            self.assertIn(name, result.stdout)


class A6000ToolingTests(unittest.TestCase):
    def test_run_sh_uses_instruction_count_time(self) -> None:
        text = (A6000_DIR / "run.sh").read_text(encoding="utf-8")
        self.assertIn('-icount "${A6K_ICOUNT:-shift=0,sleep=on}"', text)

    def test_local_inputs_are_ignored(self) -> None:
        ignore = (A6000_DIR / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("/firmware/", "/data/", "/state/", "/dumps/",
                      "/provision/a6kcopy", "/provision/*.cpio",
                      "/inputs.local.json*"):
            self.assertIn(entry, ignore)

    def test_scripts_carry_licence_and_provenance(self) -> None:
        for path in sorted(A6000_DIR.rglob("*")):
            if path.suffix not in {".sh", ".py", ".c"} or "pmca-socket" in path.parts:
                continue
            head = path.read_text(encoding="utf-8")[:600]
            with self.subTest(path=path.relative_to(PROJECT_ROOT).as_posix()):
                self.assertIn("SPDX-License-Identifier: GPL-2.0-or-later", head)
                self.assertIn("voxivoid/a6000-qemu", head)


if __name__ == "__main__":
    unittest.main()
