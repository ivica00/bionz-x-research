"""Tests for the av-cam shard merge and the progress dashboard.

The arithmetic tests use a deliberately small index database and hand-written
shards, so a wrong weight or a wrong maturity value fails here rather than in a
report. The end-to-end case builds the real modules over the synthetic image of
``test_avcam_image`` and checks that a fresh skeleton reads 0.00 % and that one
reviewed item moves the number by exactly its weighted share.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "research"))

import avcam_dashboard as dashboard  # noqa: E402
import avcam_domains as domains  # noqa: E402
import avcam_image as image  # noqa: E402
import avcam_indexer as indexer  # noqa: E402
import avcam_shards as shards_writer  # noqa: E402
from tests import test_avcam_image as fixture  # noqa: E402

ARTIFACTS = ROOT / "artifacts" / "tests"


class DomainAssignmentTests(unittest.TestCase):
    def test_longest_tag_pattern_wins(self) -> None:
        self.assertEqual(domains.assign_tag("RcvRMC"), "2F", "RcvRMC must beat RC")
        self.assertEqual(domains.assign_tag("SFRS"), "2D")
        self.assertEqual(domains.assign_tag("AdfCmdInputCtrl"), "1B", "class name follows prefix")
        self.assertEqual(domains.assign_tag("IdtCam"), "2B")
        self.assertEqual(domains.assign_tag("ZZZ"), "9R")

    def test_mmio_windows_are_first_match(self) -> None:
        self.assertEqual(domains.assign_mmio(0xF2405000), "2F", "SIO channel page")
        self.assertEqual(domains.assign_mmio(0xF2402000), "1A", "watchdog page")
        self.assertEqual(domains.assign_mmio(0xF3010000), "2C")
        self.assertEqual(domains.assign_mmio(0xF3031000), "2D")
        self.assertEqual(domains.assign_mmio(0xFC021000), "2B")
        self.assertEqual(domains.assign_mmio(0xFCA00000), "9R")

    def test_function_attribution_refuses_a_tie(self) -> None:
        self.assertIsNone(domains.assign_function({}, {}))
        self.assertIsNone(domains.assign_function({"2C": 2, "2D": 2}, {}))
        self.assertEqual(domains.assign_function({"2C": 3, "2D": 1}, {"1C": 1}), "2C")

    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(weight for _kind, weight in dashboard.WEIGHTS), 1.0)


def _write_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(indexer.SCHEMA)
        connection.executemany("INSERT INTO meta VALUES (?,?)", [
            ("image_path", "synthetic.bin"),
            ("image_sha256", "0" * 64),
            ("oril_base", f"{image.ORIL_BASE:#x}"),
            ("command_table_offset", "0x100"),
            ("command_table_size", "2"),
            ("vectors", f"{image.ORIL_BASE + 0x14:#x},{image.ORIL_BASE + 0x30:#x}"),
        ])
        connection.executemany("INSERT INTO command VALUES (?,?,?,?,?)", [
            (0x4000, image.ORIL_BASE + 0x31, image.ORIL_BASE + 0x15, 0,
             image.ORIL_BASE + 0x30),
            (0x4001, image.ORIL_BASE + 0x31, image.ORIL_BASE + 0x15, 1,
             image.ORIL_BASE + 0x30),
        ])
        connection.executemany("INSERT INTO string_tag VALUES (?,?,?)", [
            (0x1000, "ADF", 0), (0x2000, "IDT", 0),
        ])
        connection.executemany("INSERT INTO mmio_ref VALUES (?,?,?)", [
            (image.ORIL_BASE + 0x14, 0xF3010000, 0xF3010000),
            (image.ORIL_BASE + 0x14, 0xF3030000, 0xF3030000),
        ])
        connection.executemany("INSERT INTO function VALUES (?,?,?,?)", [
            (image.ORIL_BASE + 0x14, 8, "code", "prologue"),
            (image.ORIL_BASE + 0x30, 8, "code", "prologue"),
            (image.ORIL_BASE + 0x38, 8, "code", "prologue"),
        ])
        connection.commit()
    finally:
        connection.close()


def _empty_shard(domain) -> dict:
    return {
        "schema_version": 1,
        "domain": domain.id,
        "title": domain.title,
        "phase": domain.phase,
        "milestone": domain.milestone,
        "owner": "unassigned",
        "status": "LOCATED",
        "items": {"commands": [], "tags": [], "mmio": [], "abi": []},
        "functions": {"total": 0, "unmapped": 0, "located": 0, "decompiled": 0,
                      "spec_ready": 0, "entry_points": []},
    }


def _shards() -> dict[str, dict]:
    result = {domain.id: _empty_shard(domain) for domain in domains.DOMAINS}
    result["1B"]["items"]["commands"] = [
        {"id": 0x4000, "state": "LOCATED"},
        {"id": 0x4001, "state": "DECOMPILED"},
    ]
    result["1B"]["items"]["tags"] = [{"tag": "ADF", "state": "LOCATED"}]
    result["2C"]["items"]["tags"] = [{"tag": "IDT", "state": "SPEC_READY"}]
    result["2C"]["items"]["mmio"] = [{"page": "0xf3010000", "state": "SPEC_READY"}]
    result["2D"]["items"]["mmio"] = [{"page": "0xf3030000", "state": "UNMAPPED"}]
    result["1A"]["items"]["abi"] = (
        [{"id": "vector-0", "state": "SPEC_READY"}, {"id": "vector-1", "state": "UNMAPPED"}]
        + [{"id": contract, "state": "UNMAPPED"} for contract, _ in domains.ABI_CONTRACTS]
    )
    result["1A"]["functions"] = {"total": 3, "unmapped": 0, "located": 1,
                                 "decompiled": 1, "spec_ready": 1, "entry_points": []}
    return result


class DashboardArithmeticTests(unittest.TestCase):
    def setUp(self) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.dir = ARTIFACTS / f"avcam-dashboard-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir()
        self.db = self.dir / "avcam_map.sqlite"
        _write_database(self.db)
        self.base = dashboard._denominators(self.db)
        self.shards = _shards()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_maturity_uses_the_declared_weights(self) -> None:
        dashboard.validate(self.shards, self.base)
        result = dashboard.compute(self.shards)
        abi_total = len(self.base["abi"])
        self.assertEqual(abi_total, 2 + len(domains.ABI_CONTRACTS))
        self.assertAlmostEqual(result["metrics"]["abi"]["score"], 1 / abi_total)
        self.assertAlmostEqual(result["metrics"]["commands"]["score"], (0.25 + 0.6) / 2)
        self.assertAlmostEqual(result["metrics"]["tags"]["score"], (0.25 + 1.0) / 2)
        self.assertAlmostEqual(result["metrics"]["mmio"]["score"], 0.5)
        self.assertAlmostEqual(result["metrics"]["functions"]["score"],
                               (0.25 + 0.6 + 1.0) / 3)
        expected = (0.15 * (1 / abi_total) + 0.20 * 0.425 + 0.15 * 0.625
                    + 0.30 * ((0.25 + 0.6 + 1.0) / 3) + 0.20 * 0.5)
        self.assertAlmostEqual(result["overall"], expected, places=9)

    def test_validate_rejects_a_missing_item(self) -> None:
        self.shards["1B"]["items"]["commands"].pop()
        with self.assertRaises(dashboard.DashboardError):
            dashboard.validate(self.shards, self.base)

    def test_validate_rejects_a_duplicate_item(self) -> None:
        self.shards["2D"]["items"]["tags"].append({"tag": "ADF", "state": "LOCATED"})
        with self.assertRaises(dashboard.DashboardError):
            dashboard.validate(self.shards, self.base)

    def test_validate_rejects_an_invalid_state(self) -> None:
        self.shards["1B"]["items"]["tags"][0]["state"] = "DONE"
        with self.assertRaises(dashboard.DashboardError):
            dashboard.validate(self.shards, self.base)

    def test_validate_rejects_function_count_mismatch(self) -> None:
        self.shards["1A"]["functions"]["total"] = 4
        with self.assertRaises(dashboard.DashboardError):
            dashboard.validate(self.shards, self.base)

    def test_written_dashboard_reports_zero_for_a_fresh_skeleton(self) -> None:
        (self.dir / "shards").mkdir()
        for domain in domains.DOMAINS:
            payload = _empty_shard(domain)
            (self.dir / "shards" / f"{domain.shard}.json").write_text(
                json.dumps(payload), encoding="utf-8")
        empty = {domain.id: _empty_shard(domain) for domain in domains.DOMAINS}
        empty["1B"]["items"]["commands"] = [{"id": 0x4000, "state": "UNMAPPED"},
                                            {"id": 0x4001, "state": "UNMAPPED"}]
        empty["1B"]["items"]["tags"] = [{"tag": "ADF", "state": "UNMAPPED"}]
        empty["2C"]["items"]["tags"] = [{"tag": "IDT", "state": "UNMAPPED"}]
        empty["2C"]["items"]["mmio"] = [{"page": "0xf3010000", "state": "UNMAPPED"}]
        empty["2D"]["items"]["mmio"] = [{"page": "0xf3030000", "state": "UNMAPPED"}]
        empty["1A"]["items"]["abi"] = (
            [{"id": "vector-0", "state": "UNMAPPED"}, {"id": "vector-1", "state": "UNMAPPED"}]
            + [{"id": contract, "state": "UNMAPPED"} for contract, _ in domains.ABI_CONTRACTS]
        )
        empty["1A"]["functions"] = {"total": 3, "unmapped": 3, "located": 0,
                                    "decompiled": 0, "spec_ready": 0, "entry_points": []}
        for domain in domains.DOMAINS:
            (self.dir / "shards" / f"{domain.shard}.json").write_text(
                json.dumps(empty[domain.id]), encoding="utf-8")
        result = dashboard.write(self.dir)
        self.assertEqual(result["overall"], 0.0)
        self.assertTrue((self.dir / "avcam_progress.md").is_file())


class EndToEndTests(unittest.TestCase):
    """Scan -> SQLite -> shards -> dashboard over the synthetic ORIL image."""

    def setUp(self) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.dir = ARTIFACTS / f"avcam-pipeline-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_skeleton_covers_the_index_and_starts_at_zero(self) -> None:
        data = fixture.build_image()
        index = image.scan_image(data, Path("synthetic.bin"))
        indexer.build_database(index, self.dir / "avcam_map.sqlite")
        shards_writer.write_shards(self.dir)
        result = dashboard.write(self.dir)

        self.assertEqual(result["overall"], 0.0)
        self.assertEqual(result["metrics"]["commands"]["total"], len(index.commands))
        self.assertEqual(result["metrics"]["tags"]["total"], len(index.tag_counts()))
        self.assertEqual(result["metrics"]["functions"]["total"], len(index.functions))

        commands = [item for shard in result["domains"]
                    for item in shard["items"]["commands"].values()]
        self.assertEqual(sum(commands), len(index.commands))
        self.assertTrue((self.dir / "avcam_progress.md").is_file())
        self.assertTrue((self.dir / "avcam_progress.json").is_file())

    def test_one_reviewed_command_moves_the_score_by_its_share(self) -> None:
        data = fixture.build_image()
        index = image.scan_image(data, Path("synthetic.bin"))
        indexer.build_database(index, self.dir / "avcam_map.sqlite")
        shards_writer.write_shards(self.dir)
        dashboard.write(self.dir)

        path = self.dir / "shards" / f"{domains.DOMAIN_BY_ID['1B'].shard}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["items"]["commands"][0]["state"] = "DECOMPILED"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = dashboard.write(self.dir)
        expected = 0.20 * (0.6 / len(index.commands))
        self.assertAlmostEqual(result["overall"], expected, places=9)


if __name__ == "__main__":
    unittest.main()
