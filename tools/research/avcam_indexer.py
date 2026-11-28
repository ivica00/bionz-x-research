#!/usr/bin/env python3
"""Build the SQLite map of a flat ``ORIL`` ``av-cam.bin`` image (Phase 0).

The map is the shared read-only base that parallel shard workers read, in the
sense of the av-cam research plan: one build, then many readers. It holds only
facts a scanner can measure — regions, candidate functions, call edges, literal
values, strings and their ``[TAG]`` tokens, MMIO literals, and the 285-entry
``av_command_table`` — plus the identity of the image they came from.

What this tool deliberately does not do: it does not decompile, does not name a
function, and does not decide that a handler implements a command. Those are
shard work, recorded per domain. A count here is a denominator, never progress.

Commands::

    python -m tools.research.avcam_indexer build <av-cam.bin> --out <dir>
    python -m tools.research.avcam_indexer index <av-cam.bin> --out <dir>
    python -m tools.research.avcam_indexer report <db> --out <dir>
    python -m tools.research.avcam_indexer query <db> sql "SELECT ..."

``build`` also writes the shard skeletons and the progress dashboard, so a
single command produces the whole Phase 0 skeleton. The sub-commands exist so a
worker can rebuild one stage without touching the others.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time

try:  # imported as tools.research.avcam_indexer
    from tools.research import avcam_image as image
except ImportError:  # imported with tools/research on sys.path
    import avcam_image as image  # type: ignore

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE region(kind TEXT NOT NULL, start_va INTEGER NOT NULL,
                    end_va INTEGER NOT NULL, start_off INTEGER NOT NULL,
                    size INTEGER NOT NULL);
CREATE TABLE function(va INTEGER PRIMARY KEY, size INTEGER NOT NULL,
                      region TEXT NOT NULL, origins TEXT NOT NULL);
CREATE TABLE call_edge(site_va INTEGER NOT NULL, caller_va INTEGER NOT NULL,
                       callee_va INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TABLE literal_ref(function_va INTEGER NOT NULL, value INTEGER NOT NULL,
                         count INTEGER NOT NULL);
CREATE TABLE string(va INTEGER PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE string_tag(string_va INTEGER NOT NULL, tag TEXT NOT NULL,
                        position INTEGER NOT NULL);
CREATE TABLE string_ref(function_va INTEGER NOT NULL, string_va INTEGER NOT NULL,
                        via TEXT NOT NULL, site_va INTEGER NOT NULL,
                        PRIMARY KEY(function_va, string_va));
CREATE TABLE string_pointer(string_va INTEGER NOT NULL, location_va INTEGER NOT NULL);
CREATE INDEX string_pointer_string ON string_pointer(string_va);
CREATE TABLE mmio_ref(function_va INTEGER NOT NULL, address INTEGER NOT NULL,
                      page INTEGER NOT NULL);
CREATE TABLE command(command_id INTEGER PRIMARY KEY, handler INTEGER NOT NULL,
                     checker INTEGER NOT NULL, flag INTEGER NOT NULL,
                     handler_va INTEGER NOT NULL);
CREATE INDEX call_caller ON call_edge(caller_va);
CREATE INDEX call_callee ON call_edge(callee_va);
CREATE INDEX literal_function ON literal_ref(function_va);
CREATE INDEX string_tag_tag ON string_tag(tag);
CREATE INDEX mmio_page ON mmio_ref(page);
CREATE INDEX command_handler ON command(handler_va);
"""


def build_database(index: image.ImageIndex, path: Path) -> None:
    """Write the index to a fresh SQLite database (WAL, so readers can share it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=OFF")
        connection.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                ("schema_version", "1"),
                ("image_path", str(index.path)),
                ("image_sha256", index.sha256),
                ("image_size", str(index.size)),
                ("oril_base", f"{index.header.base:#x}"),
                ("oril_version", str(index.header.version)),
                ("entry_va", f"{index.header.entry_va:#x}"),
                ("header_end", f"{index.header.header_end:#x}"),
                ("stub_end", f"{index.header.stub_end:#x}"),
                ("vectors", ",".join(f"{v:#x}" for v in index.header.vectors)),
                ("command_table_offset", f"{index.command_offset:#x}"),
                ("command_table_size", str(len(index.commands))),
            ],
        )
        connection.executemany(
            "INSERT INTO region VALUES (?,?,?,?,?)",
            [(r.kind, r.start_va, r.end_va, r.start, r.size) for r in index.regions],
        )
        connection.executemany(
            "INSERT INTO function VALUES (?,?,?,?)",
            [(f.va, f.size, f.region_kind, ",".join(f.origins)) for f in index.functions],
        )
        connection.executemany(
            "INSERT INTO call_edge VALUES (?,?,?,?)",
            [(c.site_va, c.caller_va, c.callee_va, c.kind) for c in index.calls],
        )
        counts: dict[tuple[int, int], int] = {}
        for literal in index.literals:
            key = (literal.function_va, literal.value)
            counts[key] = counts.get(key, 0) + 1
        connection.executemany(
            "INSERT INTO literal_ref VALUES (?,?,?)",
            [(function_va, value, count) for (function_va, value), count in counts.items()],
        )
        connection.executemany(
            "INSERT INTO string VALUES (?,?)",
            [(rec.va, rec.text) for rec in index.strings],
        )
        connection.executemany(
            "INSERT INTO string_tag VALUES (?,?,?)",
            [(rec.va, tag, position)
             for rec in index.strings for position, tag in enumerate(rec.tags)],
        )
        connection.executemany(
            "INSERT INTO string_ref VALUES (?,?,?,?)",
            [(ref.function_va, ref.string_va, ref.via, ref.site_va) for ref in index.xrefs],
        )
        connection.executemany(
            "INSERT INTO string_pointer VALUES (?,?)",
            [(pointer.string_va, pointer.location_va) for pointer in index.string_pointers],
        )
        connection.executemany(
            "INSERT INTO mmio_ref VALUES (?,?,?)",
            [(function_va, address, address & ~0xFFF)
             for function_va, address in index.mmio],
        )
        connection.executemany(
            "INSERT INTO command VALUES (?,?,?,?,?)",
            [(cid, handler, checker, flag, handler & ~1)
             for cid, handler, checker, flag in index.commands],
        )
        connection.commit()
    finally:
        connection.close()


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM meta"))


def report(index: image.ImageIndex, out_dir: Path) -> Path:
    """Write the human-readable index report and its JSON twin.

    The artifacts are a pure function of the image bytes: the wall-clock scan
    time is printed by the caller and never written here, so two builds of the
    same image hash produce byte-identical reports and shards.
    """
    tags = index.tag_counts()
    mmio_pages: dict[int, int] = {}
    for _function, address in index.mmio:
        page = address & ~0xFFF
        mmio_pages[page] = mmio_pages.get(page, 0) + 1
    lines = [
        f"# av-cam.bin static index - {Path(index.path).name}",
        "",
        f"- sha256 `{index.sha256}`",
        f"- size {index.size:,} B, ORIL v{index.header.version} at "
        f"`{index.header.base:#x}`, entry `{index.header.entry_va:#x}`",
        f"- {len(index.header.vectors)} vectors at `{index.header.vector_table_va:#x}`, "
        f"header ends `+{index.header.header_end:#x}`, ARM stub ends "
        f"`+{index.header.stub_end:#x}`",
        f"- command table at `{index.header.base + index.command_offset:#x}` "
        f"({index.command_offset:#x}), {len(index.commands)} records",
        "",
        "Wall-clock scan time is printed by the CLI and deliberately not stored, so "
        "this report is a pure function of the image bytes.",
        "",
        "## Measured regions",
        "",
        "| Kind | VA range | Size |",
        "| --- | --- | ---: |",
    ]
    for region in index.regions:
        lines.append(f"| {region.kind} | `{region.start_va:#010x}..{region.end_va:#010x}` "
                     f"| {region.size:,} B |")
    lines += [
        "",
        "## Counts",
        "",
        "| Fact | Value |",
        "| --- | ---: |",
    ]
    labels = {
        "functions": "candidate functions (prologue/vector/command starts)",
        "functions_prologue": "functions whose start is a PUSH with `lr`",
        "functions_without_prologue": "starts from vectors/commands without a PUSH",
        "functions_command": "distinct command handler/checker functions",
        "calls": "`BL`/`BLX` call edges with an in-image target",
        "call_targets": "distinct call targets",
        "internal_entry_points": "call targets inside a known function",
        "literals": "literal-pool loads (`LDR Rd,[pc,#imm]`)",
        "strings": "NUL-terminated printable strings outside code",
        "tags": "distinct `[TAG]` tokens",
        "tag_occurrences": "`[TAG]` token occurrences",
        "strings_referenced": "strings with at least one reference (all resolutions)",
        "string_refs": "function -> string references (all resolutions)",
        "string_refs_direct": "references via one literal-pool load",
        "string_refs_base": "references via a tracked base register",
        "string_refs_table": "references via an indexed pointer table",
        "string_refs_adr": "references via `adr`/`add rd,pc,#imm`",
        "functions_with_string_refs": "functions with at least one string reference",
        "tagged_strings": "strings that carry at least one `[TAG]` token",
        "tagged_strings_with_refs": "tagged strings with a function reference",
        "tags_with_function_refs": "distinct tags with at least one referenced string",
        "strings_with_stored_pointer": "strings whose address is stored as a 4-byte word",
        "stored_string_pointers": "4-byte words equal to an indexed string address",
        "mmio_literals": "literal loads with a value >= `0xf0000000`",
        "mmio_pages": "distinct 4 KiB pages in `0xf0000000..0xfcffffff`",
        "commands": "`av_command_table` records",
    }
    for key, value in index.stats.items():
        lines.append(f"| {labels.get(key, key)} | {value:,} |")
    lines += [
        "",
        "## Top `[TAG]` tokens",
        "",
        "| Tag | Strings |",
        "| --- | ---: |",
    ]
    for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:40]:
        lines.append(f"| `[{tag}]` | {count:,} |")
    lines += [
        "",
        "## MMIO pages by reference count",
        "",
        "| Page | Literal loads |",
        "| --- | ---: |",
    ]
    for page, count in sorted(mmio_pages.items(), key=lambda item: (-item[1], item[0]))[:60]:
        lines.append(f"| `{page:#010x}` | {count:,} |")
    lines += [
        "",
        "## Method and limits",
        "",
        "- Regions are classified per 64 KiB block: a block is code when it holds at",
        f"  least {image.CODE_MIN_CALLS} in-image `BL` pairs and at least",
        f"  {image.CODE_MIN_CALL_RATIO:.0%} of them land on a `PUSH`/`PUSH.W` with `lr`.",
        "  Data blocks with >= 99.5 % zero bytes are `bss`, blocks with >= 85 % printable",
        "  bytes are `rodata`.",
        "- A candidate function start is a Thumb prologue (`push {..., lr}`), a header",
        "  vector, or a command-table handler/checker. A `BL` target inside such a range",
        "  is counted as an internal entry point, not as a second function.",
        "- String references combine a literal-pool floor with a per-function pointer",
        "  walk (`avcam_xrefs`): tracked literals, `movw`/`movt` pairs, `adr`, the PIC",
        "  `ldr rD,[pc,#k]; add rD,pc` idiom, known-base loads/adds with one level of",
        "  memory dereference, and indexed table scans. Computed addresses are accepted",
        "  only when they land exactly on a string start, which removes misdecoded-data",
        "  false positives. No interprocedural flow or stack slots are followed, so the",
        "  count remains a floor; every reference records its site and kind (`direct`,",
        "  `base`, `table`, `adr`) so a site can be disassembled and checked.",
        "- `avcam_ghidra stage` writes the raw-import inputs; `import`/`verify` record a",
        "  receipt that the flat image opens in Ghidra as ARM:LE:32:v7 at the ORIL base",
        "  with the measured blocks and decompiles by address.",
        "- MMIO values are literal-pool words in `0xf0000000..0xffffffff`; a value above",
        "  `0xfcffffff` is kept in the JSON but excluded from the page denominator.",
        "- Thumb code is swept linearly, so a data table inside a code block can decode",
        "  as instructions. That is why every start is called a candidate.",
        "",
    ]
    markdown = out_dir / "avcam_index_report.md"
    markdown.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "schema_version": 1,
        "image": {
            "path": str(index.path),
            "sha256": index.sha256,
            "size": index.size,
            "base": f"{index.header.base:#x}",
            "version": index.header.version,
            "entry_va": f"{index.header.entry_va:#x}",
            "vectors": [f"{v:#x}" for v in index.header.vectors],
            "header_end": f"{index.header.header_end:#x}",
            "stub_end": f"{index.header.stub_end:#x}",
            "command_table_va": f"{index.header.base + index.command_offset:#x}",
        },
        "regions": [
            {"kind": r.kind, "start_va": f"{r.start_va:#x}", "end_va": f"{r.end_va:#x}",
             "size": r.size}
            for r in index.regions
        ],
        "counts": index.stats,
        "tags": dict(sorted(tags.items(), key=lambda item: (-item[1], item[0]))),
        "mmio_pages": {f"{page:#010x}": count
                       for page, count in sorted(mmio_pages.items())},
    }
    (out_dir / "avcam_index.json").write_text(json.dumps(payload, indent=2) + "\n",
                                              encoding="utf-8")
    return markdown


def cmd_index(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = Path(args.image).read_bytes()
    started = time.monotonic()
    index = image.scan_image(data, Path(args.image))
    elapsed = time.monotonic() - started
    build_database(index, out_dir / "avcam_map.sqlite")
    path = report(index, out_dir)
    print(f"indexed {len(index.functions)} functions, {len(index.commands)} commands, "
          f"{index.stats['tags']} tags, {index.stats['mmio_pages']} MMIO pages "
          f"in {elapsed:.1f} s")
    print(f"report: {path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"no database at {db}; run index first")
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        meta = read_meta(connection)
        counts = {
            "functions": connection.execute("SELECT COUNT(*) FROM function").fetchone()[0],
            "calls": connection.execute("SELECT COUNT(*) FROM call_edge").fetchone()[0],
            "strings": connection.execute("SELECT COUNT(*) FROM string").fetchone()[0],
            "tags": connection.execute("SELECT COUNT(DISTINCT tag) FROM string_tag").fetchone()[0],
            "commands": connection.execute("SELECT COUNT(*) FROM command").fetchone()[0],
            "mmio_pages": connection.execute(
                "SELECT COUNT(DISTINCT page) FROM mmio_ref WHERE page <= 0xfcfff000").fetchone()[0],
        }
    finally:
        connection.close()
    print(json.dumps({"meta": meta, "counts": counts}, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    connection = sqlite3.connect(f"file:{Path(args.db)}?mode=ro", uri=True)
    try:
        cursor = connection.execute(args.sql)
        columns = [d[0] for d in cursor.description or []]
        print("\t".join(columns))
        for row in cursor.fetchall():
            print("\t".join(str(value) for value in row))
    finally:
        connection.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    index_parser = sub.add_parser("index", help="scan an image and write the SQLite map")
    index_parser.add_argument("image", type=Path)
    index_parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    index_parser.set_defaults(func=cmd_index)

    build_parser = sub.add_parser("build", help="index, then shards and dashboard")
    build_parser.add_argument("image", type=Path)
    build_parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    build_parser.set_defaults(func=None)

    report_parser = sub.add_parser("report", help="print counts from an existing database")
    report_parser.add_argument("db", type=Path)
    report_parser.set_defaults(func=cmd_report)

    query_parser = sub.add_parser("query", help="run one read-only SQL statement")
    query_parser.add_argument("db", type=Path)
    query_parser.add_argument("sql")
    query_parser.set_defaults(func=cmd_query)

    args = parser.parse_args(argv)
    if args.command == "build":
        if cmd_index(args) != 0:
            return 1
        try:
            from tools.research import avcam_shards as shards
        except ImportError:
            import avcam_shards as shards  # type: ignore
        shards.write_shards(Path(args.out))
        try:
            from tools.research import avcam_dashboard as dashboard
        except ImportError:
            import avcam_dashboard as dashboard  # type: ignore
        dashboard.write(Path(args.out))
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
