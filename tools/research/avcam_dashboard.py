#!/usr/bin/env python3
"""Merge the ``av-cam.bin`` domain shards into the progress dashboard.

The dashboard is the only place where a percentage exists. It reads the index
database for denominators, reads every shard for states, checks that the two
agree exactly, and then computes the five metrics of the research plan:

=================  ======  =================================================
Metric             Weight  Denominator
=================  ======  =================================================
ORIL ABI & boot    15 %    nine header vectors + the declared ABI contracts
av_command_table   20 %    every command record in the database
[TAG] subsystems   15 %    every distinct tag in the database
functions          30 %    every candidate function in the database
MMIO registers     20 %    every 4 KiB MMIO page in 0xf0000000..0xfcffffff
=================  ======  =================================================

Maturity of one item: ``UNMAPPED`` 0, ``LOCATED`` 0.25, ``DECOMPILED`` 0.6,
``SPEC_READY`` 1.0. A fresh build therefore reads 0.0 %: the skeleton fills the
denominators and every numerator stays empty until a worker records evidence.

Validation is fail-closed. A shard that adds, drops, duplicates or renames an
item, or whose function totals do not sum to the database count, makes the
command exit non-zero instead of printing a percentage over a wrong base.

Usage::

    python -m tools.research.avcam_dashboard --out artifacts/analysis/avcam-map
    python -m tools.research.avcam_dashboard --check --out artifacts/analysis/avcam-map
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

try:  # imported as tools.research.avcam_dashboard
    from tools.research import avcam_domains as domains
except ImportError:  # imported with tools/research on sys.path
    import avcam_domains as domains  # type: ignore

MATURITY = {"UNMAPPED": 0.0, "LOCATED": 0.25, "DECOMPILED": 0.6, "SPEC_READY": 1.0}
WEIGHTS = (("abi", 0.15), ("commands", 0.20), ("tags", 0.15),
           ("functions", 0.30), ("mmio", 0.20))
FUNCTION_FIELDS = ("unmapped", "located", "decompiled", "spec_ready")


class DashboardError(RuntimeError):
    """The shard set does not describe the index database."""


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _denominators(db: Path) -> dict[str, set]:
    connection = _connect(db)
    try:
        command_ids = {row[0] for row in connection.execute("SELECT command_id FROM command")}
        tags = {row[0] for row in connection.execute("SELECT DISTINCT tag FROM string_tag")}
        pages = {row[0] for row in connection.execute(
            "SELECT DISTINCT page FROM mmio_ref WHERE address <= 0xfcffffff")}
        functions = {row[0] for row in connection.execute("SELECT va FROM function")}
        meta = dict(connection.execute("SELECT key, value FROM meta"))
    finally:
        connection.close()
    vectors = [part for part in meta.get("vectors", "").split(",") if part]
    return {
        "commands": command_ids,
        "tags": tags,
        "mmio": pages,
        "functions": functions,
        "function_count": len(functions),
        "abi": {f"vector-{index}" for index in range(len(vectors))}
        | {contract for contract, _ in domains.ABI_CONTRACTS},
        "meta": meta,
    }


def _denominators_from_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise DashboardError(f"{path}: unsupported denominators schema_version")
    return {
        "commands": set(payload["commands"]),
        "tags": set(payload["tags"]),
        "mmio": {int(p, 0) if isinstance(p, str) else int(p) for p in payload["mmio"]},
        "functions": None,
        "function_count": int(payload["function_count"]),
        "abi": set(payload["abi"]),
        "meta": payload["meta"],
    }


def load_denominators(out_dir: Path, db: Path | None = None) -> dict:
    db_path = Path(db) if db is not None else Path(out_dir) / "avcam_map.sqlite"
    if db_path.is_file():
        return _denominators(db_path)
    denom_path = Path(out_dir) / "denominators.json"
    if denom_path.is_file():
        return _denominators_from_json(denom_path)
    raise DashboardError(f"neither {db_path} nor {denom_path} exists")


def export_denominators(db: Path, dest: Path) -> Path:
    base = _denominators(db)
    payload = {
        "schema_version": 1,
        "meta": base["meta"],
        "function_count": base["function_count"],
        "abi": sorted(base["abi"]),
        "commands": sorted(base["commands"]),
        "tags": sorted(base["tags"]),
        "mmio": [f"0x{page:08x}" for page in sorted(base["mmio"])],
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def load_shards(out_dir: Path) -> dict[str, dict]:
    shard_dir = Path(out_dir) / "shards"
    if not shard_dir.is_dir():
        raise DashboardError(f"no shard directory at {shard_dir}; run avcam_shards first")
    shards: dict[str, dict] = {}
    for path in sorted(shard_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise DashboardError(f"{path.name}: unsupported schema_version")
        domain_id = payload.get("domain")
        if domain_id not in domains.DOMAIN_BY_ID:
            raise DashboardError(f"{path.name}: unknown domain {domain_id!r}")
        if domain_id in shards:
            raise DashboardError(f"domain {domain_id} has more than one shard")
        shards[domain_id] = payload
    missing = set(domains.DOMAIN_BY_ID) - set(shards)
    if missing:
        raise DashboardError(f"missing shards for domains: {', '.join(sorted(missing))}")
    return shards


def _item_state(item: dict, where: str) -> str:
    state = item.get("state")
    if state not in MATURITY:
        raise DashboardError(f"{where}: invalid state {state!r}")
    return state


def _normalize(kind: str, value):
    """Shards store MMIO pages as hex strings; the database stores integers."""
    if kind == "mmio":
        return int(value, 0) if isinstance(value, str) else int(value)
    if kind == "commands":
        return int(value)
    return str(value)


def validate(shards: dict[str, dict], base: dict[str, set]) -> None:
    """Every denominator item appears once, in one shard, with a valid state."""
    seen = {"commands": {}, "tags": {}, "mmio": {}, "abi": {}}
    for domain_id, shard in shards.items():
        items = shard.get("items", {})
        for kind, key in (("commands", "id"), ("tags", "tag"),
                          ("mmio", "page"), ("abi", "id")):
            for item in items.get(kind, []):
                value = _normalize(kind, item.get(key))
                if value in seen[kind]:
                    raise DashboardError(f"{kind} {value!r} appears in both "
                                         f"{seen[kind][value]} and {domain_id}")
                seen[kind][value] = domain_id
                _item_state(item, f"{domain_id}/{kind}/{value}")
    for kind in seen:
        missing = {_normalize(kind, value) for value in base[kind]} - set(seen[kind])
        extra = set(seen[kind]) - {_normalize(kind, value) for value in base[kind]}
        if missing:
            raise DashboardError(f"{kind}: {len(missing)} database items are in no shard, "
                                 f"e.g. {sorted(missing)[:3]}")
        if extra:
            raise DashboardError(f"{kind}: {len(extra)} shard items are not in the database, "
                                 f"e.g. {sorted(extra)[:3]}")
    total = 0
    for domain_id, shard in shards.items():
        functions = shard.get("functions", {})
        counts = {field: functions.get(field) for field in FUNCTION_FIELDS}
        if any(not isinstance(value, int) or value < 0 for value in counts.values()):
            raise DashboardError(f"{domain_id}: function counts are not non-negative integers")
        if sum(counts.values()) != functions.get("total"):
            raise DashboardError(f"{domain_id}: function counts do not sum to total")
        total += functions["total"]
    expected_functions = base.get("function_count")
    if expected_functions is None:
        expected_functions = len(base["functions"])
    if total != expected_functions:
        raise DashboardError(f"function totals sum to {total}, database has "
                             f"{expected_functions}")


def _collect(shards: dict[str, dict]) -> dict[str, dict[str, Counter]]:
    stats: dict[str, dict[str, Counter]] = {}
    for domain_id, shard in shards.items():
        entry = {
            "commands": Counter(), "tags": Counter(), "mmio": Counter(), "abi": Counter(),
        }
        for kind in entry:
            for item in shard.get("items", {}).get(kind, []):
                entry[kind][item["state"]] += 1
        functions = shard.get("functions", {})
        entry["functions"] = Counter({
            "UNMAPPED": functions.get("unmapped", 0),
            "LOCATED": functions.get("located", 0),
            "DECOMPILED": functions.get("decompiled", 0),
            "SPEC_READY": functions.get("spec_ready", 0),
        })
        stats[domain_id] = entry
    return stats


def _score(counter: Counter) -> tuple[float, int]:
    total = sum(counter.values())
    if not total:
        return 0.0, 0
    numerator = sum(MATURITY[state] * count for state, count in counter.items())
    return numerator / total, total


def compute(shards: dict[str, dict]) -> dict:
    stats = _collect(shards)
    metrics: dict[str, dict] = {}
    for kind, weight in WEIGHTS:
        aggregate: Counter = Counter()
        for entry in stats.values():
            aggregate.update(entry[kind])
        score, total = _score(aggregate)
        metrics[kind] = {"weight": weight, "score": score, "total": total,
                         "states": dict(aggregate)}
    overall = sum(metrics[kind]["weight"] * metrics[kind]["score"] for kind, _w in WEIGHTS)
    per_domain = []
    for domain in domains.DOMAINS:
        entry = stats[domain.id]
        items = Counter()
        for kind in ("commands", "tags", "mmio", "abi"):
            items.update(entry[kind])
        items_score, items_total = _score(items)
        function_score, function_total = _score(entry["functions"])
        combined = (items_score * items_total + function_score * function_total) / (
            items_total + function_total) if items_total + function_total else 0.0
        per_domain.append({
            "domain": domain.id,
            "title": domain.title,
            "phase": domain.phase,
            "milestone": domain.milestone,
            "owner": shards[domain.id].get("owner", "unassigned"),
            "status": shards[domain.id].get("status", "LOCATED"),
            "maturity": combined,
            "items": {
                kind: dict(entry[kind]) for kind in ("commands", "tags", "mmio", "abi")
            },
            "functions": dict(entry["functions"]),
        })
    return {"metrics": metrics, "overall": overall, "domains": per_domain}


def render(result: dict, meta: dict) -> str:
    lines = [
        "# av-cam.bin research progress",
        "",
        f"- image `{Path(meta.get('image_path', '?')).name}`",
        f"- sha256 `{meta.get('image_sha256')}`",
        f"- ORIL base `{meta.get('oril_base')}`, `{meta.get('command_table_size')}` commands at "
        f"`{meta.get('command_table_offset')}`",
        "",
        f"**Overall maturity: {result['overall'] * 100:.2f} %** "
        f"(infrastructure complete, evidence pending)",
        "",
        "| Metric | Weight | Maturity | Denominator | States |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    labels = {
        "abi": "ABI & boot contract",
        "commands": "`av_command_table` commands",
        "tags": "`[TAG]` subsystems",
        "functions": "candidate functions",
        "mmio": "MMIO pages",
    }
    for kind, weight in WEIGHTS:
        metric = result["metrics"][kind]
        states = ", ".join(f"{state}: {count:,}"
                           for state, count in sorted(metric["states"].items()))
        lines.append(f"| {labels[kind]} | {weight:.0%} | {metric['score'] * 100:.2f} % "
                     f"| {metric['total']:,} | {states} |")
    lines += [
        "",
        "Formula: `0.15*A + 0.20*C + 0.15*T + 0.30*F + 0.20*M`; an item is `UNMAPPED` 0, "
        "`LOCATED` 0.25, `DECOMPILED` 0.6, `SPEC_READY` 1.0.",
        "",
        "## Domains",
        "",
        "| Domain | Phase | Milestone | Functions | Commands | Tags | MMIO | ABI | Maturity |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for domain in result["domains"]:
        functions = domain["functions"]
        function_total = sum(functions.values())
        items = domain["items"]
        lines.append(
            f"| {domain['domain']} {domain['title']} | {domain['phase']} | "
            f"{domain['milestone']} | {function_total:,} | {sum(items['commands'].values()):,} | "
            f"{sum(items['tags'].values()):,} | {sum(items['mmio'].values()):,} | "
            f"{sum(items['abi'].values()):,} | {domain['maturity'] * 100:.2f} % |")
    lines += [
        "",
        "A domain's maturity counts its own item states and its function counts; it is a "
        "reading aid, not part of the five weighted metrics.",
        "",
        "## What a zero means",
        "",
        "The skeleton assigns every item to exactly one shard and leaves it `UNMAPPED`. The "
        "dashboard therefore reports 0.00 % until a worker records evidence in a shard. That "
        "is the intended meaning: the denominators are measured, the numerators are not.",
        "",
    ]
    return "\n".join(lines)


def write(out_dir: Path, db: Path | None = None) -> dict:
    out_dir = Path(out_dir)
    base = load_denominators(out_dir, db)
    shards = load_shards(out_dir)
    validate(shards, base)
    result = compute(shards)
    markdown = render(result, base["meta"])
    (out_dir / "avcam_progress.md").write_text(markdown, encoding="utf-8")
    payload = {
        "schema_version": 1,
        "image": base["meta"].get("image_path"),
        "image_sha256": base["meta"].get("image_sha256"),
        "weights": dict(WEIGHTS),
        "overall": round(result["overall"], 6),
        "metrics": result["metrics"],
        "domains": result["domains"],
    }
    (out_dir / "avcam_progress.json").write_text(json.dumps(payload, indent=2) + "\n",
                                                 encoding="utf-8")
    print(f"overall maturity {result['overall'] * 100:.2f} % -> "
          f"{out_dir / 'avcam_progress.md'}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--check", action="store_true",
                        help="validate the shard set without writing a dashboard")
    parser.add_argument("--export-denominators", type=Path, default=None,
                        help="export lightweight denominators.json from the SQLite index")
    args = parser.parse_args(argv)
    if args.export_denominators is not None:
        db = Path(args.db) if args.db else Path(args.out) / "avcam_map.sqlite"
        out_path = export_denominators(db, Path(args.export_denominators))
        print(f"exported denominators -> {out_path}")
        return 0
    if args.check:
        base = load_denominators(Path(args.out), Path(args.db) if args.db else None)
        shards = load_shards(Path(args.out))
        validate(shards, base)
        fn_count = base.get("function_count") or len(base["functions"])
        print(f"shards cover {len(base['commands'])} commands, {len(base['tags'])} tags, "
              f"{len(base['mmio'])} MMIO pages, {len(base['abi'])} ABI items and "
              f"{fn_count} functions")
        return 0
    write(Path(args.out), Path(args.db) if args.db else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
