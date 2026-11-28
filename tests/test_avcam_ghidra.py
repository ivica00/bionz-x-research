"""Tests for the flat-ORIL Ghidra staging and receipt checks.

These do not need Ghidra or WSL: they exercise the staged inputs and the receipt
logic on the synthetic ORIL image. A real headless import is recorded once in
``artifacts/analysis/avcam-map/ghidra/import_receipt.json`` and summarised in
E0759; the checks below are the ones that receipt is graded against.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "research"))

import avcam_ghidra as ghidra  # noqa: E402
import avcam_image as image  # noqa: E402
import avcam_indexer as indexer  # noqa: E402
from tests import test_avcam_image as fixture  # noqa: E402

ARTIFACTS = ROOT / "artifacts" / "tests"


def staged_summary(staged: dict) -> dict:
    return {
        "status": "ok",
        "ghidra_version": "12.1.4",
        "language": ghidra.LANGUAGE,
        "image_base": staged["base"],
        "source_sha256": staged["sha256"],
        "source_size": 0,
        "blocks": [{"name": f"{i:02d}_code", "start": staged["base"], "size": 1,
                    "execute": i == 0} for i in range(staged["regions"])],
        "functions_requested": staged["max_functions"],
        "functions_created": staged["max_functions"],
        "functions_disassembled": staged["max_functions"],
        "decompile": [{"address": "0x0", "function": "sub_0", "status": "ok",
                       "c_lines": 2, "c_preview": "undefined4 sub_0(void)"}],
    }


class GhidraStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.dir = ARTIFACTS / f"avcam-ghidra-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir()
        self.index = image.scan_image(fixture.build_image(), Path("synthetic.bin"))
        indexer.build_database(self.index, self.dir / "avcam_map.sqlite")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_stage_writes_every_input(self) -> None:
        staged = ghidra.stage(self.dir, max_functions=8)
        self.assertEqual(staged["base"], f"{image.ORIL_BASE:#x}")
        self.assertEqual(staged["regions"], len(self.index.regions))
        self.assertEqual(staged["max_functions"], 8)
        ghidra_dir = self.dir / "ghidra"
        for name in ("regions.json", "functions.csv", "decompile.csv",
                     "bounded_query.json", "import_command.sh"):
            self.assertTrue((ghidra_dir / name).is_file(), name)
        payload = json.loads((ghidra_dir / "regions.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["base"], f"{image.ORIL_BASE:#x}")
        self.assertEqual(len(payload["regions"]), len(self.index.regions))
        code = [region for region in payload["regions"] if region["kind"] == "code"]
        self.assertTrue(code and code[0]["execute"])
        self.assertFalse(code[0]["write"])
        bss = [region for region in payload["regions"] if region["kind"] == "bss"]
        self.assertTrue(bss and not bss[0]["execute"])

    def test_command_script_has_both_decompiler_paths(self) -> None:
        ghidra.stage(self.dir, max_functions=8)
        script = (self.dir / "ghidra" / "import_command.sh").read_text(encoding="utf-8")
        self.assertIn("BinaryLoader", script)
        self.assertIn(image.ORIL_BASE.__format__("#x"), script)
        self.assertIn("AvcamOrilImport.java", script)
        self.assertIn("BoundedThumbProcedures.java", script)

    def test_functions_csv_starts_with_the_decompile_addresses(self) -> None:
        ghidra.stage(self.dir, max_functions=8)
        lines = [line for line in (self.dir / "ghidra" / "functions.csv")
                 .read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        decompile = [line for line in (self.dir / "ghidra" / "decompile.csv")
                     .read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
        first = [line.split(",")[0] for line in lines[:len(decompile)]]
        self.assertEqual([int(value, 16) for value in first],
                         [int(value, 16) for value in decompile])

    def test_verify_summary_accepts_a_matching_receipt(self) -> None:
        staged = {"base": f"{image.ORIL_BASE:#x}", "sha256": self.index.sha256,
                  "regions": len(self.index.regions), "max_functions": 8}
        checks = ghidra.verify_summary(staged_summary(staged), staged)
        self.assertTrue(all(checks.values()), checks)

    def test_verify_summary_rejects_a_wrong_base_or_language(self) -> None:
        staged = {"base": f"{image.ORIL_BASE:#x}", "sha256": self.index.sha256,
                  "regions": len(self.index.regions), "max_functions": 8}
        summary = staged_summary(staged)
        summary["image_base"] = "0x0"
        self.assertFalse(ghidra.verify_summary(summary, staged)["image_base"])
        summary = staged_summary(staged)
        summary["language"] = "ARM:LE:32:v8"
        self.assertFalse(ghidra.verify_summary(summary, staged)["language"])
        summary = staged_summary(staged)
        summary["source_sha256"] = "0" * 64
        self.assertFalse(ghidra.verify_summary(summary, staged)["source_sha256"])

    def test_layout_check_rejects_a_missing_block(self) -> None:
        staged = {"base": f"{image.ORIL_BASE:#x}", "sha256": self.index.sha256,
                  "regions": len(self.index.regions), "max_functions": 8}
        summary = staged_summary(staged)
        summary["blocks"] = summary["blocks"][:-1]
        with self.assertRaises(SystemExit):
            ghidra._check_layout(summary, staged)

    def test_verify_without_a_summary_is_refused(self) -> None:
        ghidra.stage(self.dir, max_functions=8)
        with self.assertRaises(SystemExit):
            ghidra.verify(self.dir)


if __name__ == "__main__":
    unittest.main()
