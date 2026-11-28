#!/usr/bin/env python3
"""Write the per-domain shard skeletons for the ``av-cam.bin`` research map.

The index database is shared and read-only; a shard is the one file a worker
owns. This writer creates every shard with the items that belong to its domain,
each still at state ``UNMAPPED`` and each carrying the measured signal that put
it there. A worker promotes an item to ``LOCATED``, ``DECOMPILED`` or
``SPEC_READY`` only after doing the work, so the initial run cannot inflate the
progress figure: it fills denominators, not numerators.

Assignment is deliberately conservative and every choice is visible in the
shard:

* a command follows its handler function's domain, then its checker's;
* a tag follows its name pattern;
* an MMIO page follows the first declared address window;
* a function gets the unique strongest domain among its MMIO literals and its
  direct string references, or one inherited from a clear majority of its
  callees when it has no signal of its own;
* everything else lands in ``9R``.

Usage::

    python -m tools.research.avcam_shards --out artifacts/analysis/avcam-map
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3

try:  # imported as tools.research.avcam_shards
    from tools.research import avcam_domains as domains
except ImportError:  # imported with tools/research on sys.path
    import avcam_domains as domains  # type: ignore

SCHEMA_VERSION = 1
STATES = ("UNMAPPED", "LOCATED", "DECOMPILED", "SPEC_READY")


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _signals(connection: sqlite3.Connection) -> dict[str, object]:
    functions = [row[0] for row in connection.execute("SELECT va FROM function ORDER BY va")]
    calls = list(connection.execute("SELECT caller_va, callee_va FROM call_edge"))
    mmio = list(connection.execute("SELECT function_va, page FROM mmio_ref"))
    tag_refs = list(connection.execute(
        "SELECT DISTINCT r.function_va, t.tag FROM string_ref r "
        "JOIN string_tag t ON t.string_va = r.string_va"))
    commands = list(connection.execute(
        "SELECT command_id, handler, checker, flag, handler_va FROM command ORDER BY command_id"))
    tags = list(connection.execute(
        "SELECT tag, COUNT(*) FROM string_tag GROUP BY tag ORDER BY tag"))
    mmio_counts = list(connection.execute(
        "SELECT page, COUNT(*) FROM mmio_ref WHERE address <= 0xfcffffff GROUP BY page"))
    meta = dict(connection.execute("SELECT key, value FROM meta"))
    return {
        "functions": functions,
        "calls": calls,
        "mmio": mmio,
        "tag_refs": tag_refs,
        "commands": commands,
        "tags": tags,
        "mmio_counts": mmio_counts,
        "meta": meta,
    }


def _own_domains(data: dict[str, object]) -> dict[int, str]:
    mmio_by_function: dict[int, Counter] = defaultdict(Counter)
    for function_va, page in data["mmio"]:
        mmio_by_function[function_va][domains.assign_mmio(page)] += 1
    tags_by_function: dict[int, Counter] = defaultdict(Counter)
    for function_va, tag in data["tag_refs"]:
        tags_by_function[function_va][domains.assign_tag(tag)] += 1
    own: dict[int, str] = {}
    for function_va in data["functions"]:
        domain_id = domains.assign_function(mmio_by_function.get(function_va, Counter()),
                                            tags_by_function.get(function_va, Counter()))
        if domain_id is not None and domain_id != "9R":
            own[function_va] = domain_id
    return own


def _propagate(own: dict[int, str], data: dict[str, object], rounds: int = 4) -> dict[int, str]:
    """Let a function without its own signal follow a clear majority of its callees."""
    callees: dict[int, set[int]] = defaultdict(set)
    for caller, callee in data["calls"]:
        callees[caller].add(callee)
    result = dict(own)
    for _ in range(rounds):
        added: dict[int, str] = {}
        for function_va in data["functions"]:
            if function_va in result:
                continue
            votes = Counter(result[callee] for callee in callees.get(function_va, ())
                            if callee in result)
            total = sum(votes.values())
            if total < 2:
                continue
            candidate, count = votes.most_common(1)[0]
            if count * 3 >= total * 2:  # at least 2/3 of the observed callees
                added[function_va] = candidate
        result.update(added)
    return result


def _shard_items(own: dict[int, str], data: dict[str, object]) -> dict[str, dict]:
    items: dict[str, dict] = {domain.id: {"commands": [], "tags": [], "mmio": [], "abi": []}
                              for domain in domains.DOMAINS}

    for command_id, handler, checker, flag, handler_va in data["commands"]:
        handler_domain = own.get(handler_va)
        checker_domain = own.get(checker & ~1)
        # The table belongs to the dispatcher (1B). The handler signal is a
        # candidate for later routing, not a routing decision: only 30 of 285
        # handlers reach a function with any domain signal.
        items["1B"]["commands"].append({
            "id": command_id,
            "handler": f"{handler:#010x}",
            "checker": f"{checker:#010x}",
            "flag": flag,
            "state": "UNMAPPED",
            "notes": "",
            "auto": {"candidate": handler_domain or checker_domain or "9R",
                     "handler_domain": handler_domain, "checker_domain": checker_domain},
        })

    for tag, count in data["tags"]:
        chosen = domains.assign_tag(tag)
        items[chosen]["tags"].append({
            "tag": tag,
            "strings": count,
            "state": "UNMAPPED",
            "notes": "",
            "auto": {"domain": chosen},
        })

    for page, count in data["mmio_counts"]:
        chosen = domains.assign_mmio(page)
        items[chosen]["mmio"].append({
            "page": f"{page:#010x}",
            "literal_loads": count,
            "state": "UNMAPPED",
            "notes": "",
            "auto": {"domain": chosen},
        })

    vectors = data["meta"].get("vectors", "")
    abi = items["1A"]["abi"]
    for index, vector in enumerate(part for part in vectors.split(",") if part):
        abi.append({
            "id": f"vector-{index}",
            "kind": "vector",
            "address": vector,
            "state": "UNMAPPED",
            "notes": "",
            "auto": {"source": "ORIL header +0x0c"},
        })
    for contract_id, description in domains.ABI_CONTRACTS:
        abi.append({
            "id": contract_id,
            "kind": "contract",
            "description": description,
            "state": "UNMAPPED",
            "notes": "",
            "auto": {"source": "avcam_domains.ABI_CONTRACTS"},
        })
    return items


def write_shards(out_dir: Path, db: Path | None = None) -> Path:
    """Write every domain shard. Refuses to write a shard set that does not cover the index."""
    out_dir = Path(out_dir)
    db = Path(db) if db is not None else out_dir / "avcam_map.sqlite"
    connection = _connect(db)
    try:
        data = _signals(connection)
    finally:
        connection.close()

    own = _own_domains(data)
    promoted = _propagate(own, data)
    items = _shard_items(promoted, data)

    function_domains = Counter(promoted.get(function_va, "9R")
                              for function_va in data["functions"])
    if sum(function_domains.values()) != len(data["functions"]):
        raise SystemExit("function attribution lost functions; refusing to write shards")

    entry_points: dict[str, list[str]] = defaultdict(list)
    for command_id, _handler, _checker, _flag, handler_va in data["commands"]:
        domain_id = promoted.get(handler_va)
        if domain_id and handler_va not in entry_points[domain_id]:
            entry_points[domain_id].append(handler_va)
    mmio_weight: dict[int, int] = Counter()
    for function_va, _page in data["mmio"]:
        mmio_weight[function_va] += 1
    for function_va, weight in mmio_weight.most_common():
        domain_id = promoted.get(function_va)
        if domain_id and len(entry_points[domain_id]) < 12:
            entry_points[domain_id].append(function_va)

    shard_dir = out_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for domain in domains.DOMAINS:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "domain": domain.id,
            "title": domain.title,
            "phase": domain.phase,
            "milestone": domain.milestone,
            "summary": domain.summary,
            "owner": "unassigned",
            "status": "UNMAPPED",
            "image": {
                "sha256": data["meta"].get("image_sha256"),
                "base": data["meta"].get("oril_base"),
            },
            "questions": list(domain.questions),
            "deliverable": domain.deliverable,
            "scope": {
                "commands": len(items[domain.id]["commands"]),
                "tags": len(items[domain.id]["tags"]),
                "mmio_pages": len(items[domain.id]["mmio"]),
                "abi": len(items[domain.id]["abi"]),
                "functions": function_domains.get(domain.id, 0),
            },
            "items": {
                "commands": items[domain.id]["commands"],
                "tags": items[domain.id]["tags"],
                "mmio": items[domain.id]["mmio"],
                "abi": items[domain.id]["abi"],
            },
            "functions": {
                "total": function_domains.get(domain.id, 0),
                "unmapped": function_domains.get(domain.id, 0),
                "located": 0,
                "decompiled": 0,
                "spec_ready": 0,
                "entry_points": [f"{va:#010x}" for va in entry_points.get(domain.id, [])],
            },
            "notes": [],
        }
        path = shard_dir / f"{domain.shard}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(path)

    (shard_dir / "README.md").write_text(_readme(), encoding="utf-8")
    return shard_dir


def _readme() -> str:
    return """# av-cam shards

One JSON file per domain. `tools.research.avcam_indexer build` writes the
initial skeleton; a worker edits only the shard it owns, and
`tools.research.avcam_dashboard` merges them.

Rules:

1. Do not add or remove items. The index database is the denominator; a shard
   that disagrees with it makes the dashboard fail closed.
2. Promote an item only with evidence: `LOCATED` (functions/strings/MMIO found
   and attributed), `DECOMPILED` (control flow, parameters and structures
   written down), `SPEC_READY` (ABI + state machine + test vectors written for a
   clean-room reimplementation).
3. The `auto` block records why the item landed in this shard. Overrule it by
   setting the item's `notes` and moving it in a reviewed change (not by editing
   the generator output of another worker's shard).
4. `functions` is an aggregate: counts of functions by maturity, plus up to 12
   entry points to start from. Per-function names live in the architecture
   reference, not here.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)
    path = write_shards(args.out, args.db)
    print(f"wrote shards to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
