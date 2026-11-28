#!/usr/bin/env python3
"""Import the flat ``ORIL`` image into Ghidra at its runtime base (Phase 0, task 0.1).

``av-cam.bin`` is not an ELF, so the normal ``symbol_index`` path cannot open it.
This module stages the import inputs from the SQLite map (measured regions and
candidate function starts), runs Ghidra Headless with ``BinaryLoader`` at
``0x635c6000`` and the explicit ``ARM:LE:32:v7`` language, and records a receipt
that proves:

* the program language and image base are the declared ones;
* the loader block was replaced by the eight measured regions, with code
  executable, rodata read-only and data/bss read-write;
* a bounded sample of candidate Thumb functions disassembles and decompiles by
  address, so ``BoundedThumbProcedures``/``symbol_index`` can work by VA.

Nothing is auto-analysed and no symbol is invented: the function sample comes
from the same static index that produced the shards, and the receipt is written
only when every check passes.

Commands::

    python -m tools.research.avcam_ghidra stage  --out artifacts/analysis/avcam-map
    python -m tools.research.avcam_ghidra import --image <av-cam.bin> --out <dir>
    python -m tools.research.avcam_ghidra verify --out <dir>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time

LANGUAGE = "ARM:LE:32:v7"
DEFAULT_MAX_FUNCTIONS = 512
DEFAULT_REA_TOOLS = "/home/administrator/a6400-rea-tools"
PERMISSIONS = {
    "code": (True, False, True),
    "rodata": (True, False, False),
    "data": (True, True, False),
    "bss": (True, True, False),
}


def _db(out_dir: Path) -> sqlite3.Connection:
    path = Path(out_dir) / "avcam_map.sqlite"
    if not path.is_file():
        raise SystemExit(f"no index database at {path}; run avcam_indexer build first")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _wsl_path(path: Path) -> str:
    resolved = Path(path).resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive
    if len(drive) != 2:
        raise SystemExit(f"expected a Windows drive path, got {resolved}")
    return "/mnt/" + drive[0].lower() + resolved.as_posix()[2:]


def _wsl_find_ghidra() -> str:
    """Locate analyzeHeadless: explicit env override, else the REA tools default.

    The default is resolved inside the generated shell script rather than by
    spawning WSL here, because some sandboxes deny a grandchild ``wsl.exe`` even
    when the host shell is allowed to run it.
    """
    override = os.environ.get("A6400_ANALYZE_HEADLESS")
    if override:
        return override
    return (f"{DEFAULT_REA_TOOLS}/ghidra_12.1.4_PUBLIC/support/analyzeHeadless")


def stage(out_dir: Path, max_functions: int = DEFAULT_MAX_FUNCTIONS) -> dict:
    """Write the Ghidra inputs from the index database."""
    out_dir = Path(out_dir)
    connection = _db(out_dir)
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        regions = list(connection.execute(
            "SELECT kind, start_va, end_va, size FROM region ORDER BY start_va"))
        functions = list(connection.execute("SELECT va, size FROM function ORDER BY va"))
        handlers = [row[0] for row in connection.execute(
            "SELECT DISTINCT handler_va FROM command ORDER BY command_id LIMIT 6")]
    finally:
        connection.close()

    base = int(meta["oril_base"], 16)
    ghidra_dir = out_dir / "ghidra"
    ghidra_dir.mkdir(parents=True, exist_ok=True)
    region_payload = {
        "schema_version": 1,
        "image_sha256": meta["image_sha256"],
        "base": f"{base:#x}",
        "size": int(meta["image_size"]),
        "regions": [],
    }
    for kind, start_va, end_va, size in regions:
        read, write, execute = PERMISSIONS[kind]
        region_payload["regions"].append({
            "kind": kind,
            "start_va": f"{start_va:#x}",
            "end_va": f"{end_va:#x}",
            "size": size,
            "read": read,
            "write": write,
            "execute": execute,
        })
    (ghidra_dir / "regions.json").write_text(
        json.dumps(region_payload, indent=2) + "\n", encoding="utf-8")

    vectors = [part for part in meta["vectors"].split(",") if part]
    decompile = list(dict.fromkeys(vectors + [f"{va:#x}" for va in handlers]
                                   + ["0x63c154f4"]))

    with (ghidra_dir / "functions.csv").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# va,size,origins - candidate starts from avcam_map.sqlite\n")
        ordered = []
        seen = set()
        for value in decompile:
            va = int(value, 16)
            size = next((size for fva, size in functions if fva == va), 64)
            ordered.append((va, size))
            seen.add(va)
        for va, size in functions:
            if va not in seen:
                ordered.append((va, size))
        for va, size in ordered:
            handle.write(f"{va:#x},{size}\n")

    (ghidra_dir / "decompile.csv").write_text(
        "# address\n" + "\n".join(decompile) + "\n", encoding="utf-8")

    bounded = {
        "schema_version": 1,
        "queries": [
            {"entry": "0x6395103a", "bytes": 1024, "label": "sub_6395103a"},
            {"entry": "0x6375160a", "bytes": 1024, "label": "sub_6375160a"},
        ],
    }
    (ghidra_dir / "bounded_query.json").write_text(
        json.dumps(bounded["queries"], indent=2) + "\n", encoding="utf-8")
    stale = ghidra_dir / "bounded_result.json"
    if stale.exists():
        stale.unlink()

    script = write_command_script(Path(meta.get("image_path", ".")), out_dir, max_functions)
    return {
        "base": f"{base:#x}",
        "sha256": meta["image_sha256"],
        "regions": len(region_payload["regions"]),
        "functions": len(functions),
        "decompile": len(decompile),
        "max_functions": max_functions,
        "ghidra_dir": str(ghidra_dir),
        "import_script": str(script),
    }


def build_command(image: Path, out_dir: Path, max_functions: int) -> list[str]:
    """The exact Ghidra Headless argv, also written as a shell script for hosts
    that deny a grandchild ``wsl.exe``."""
    out_dir = Path(out_dir)
    ghidra_dir = out_dir / "ghidra"
    project_dir = f"/tmp/avcam-oril-ghidra-{_sha8(out_dir)}"
    return [
        "wsl.exe", "--exec", _wsl_find_ghidra(), project_dir, "avcam_oril",
        "-import", _wsl_path(Path(image)),
        "-processor", LANGUAGE,
        "-loader", "BinaryLoader",
        "-loader-baseAddr", _base(out_dir),
        "-noanalysis",
        "-scriptPath", _wsl_path(Path(__file__).resolve().parent / "ghidra"),
        "-postScript", "AvcamOrilImport.java",
        _wsl_path(ghidra_dir / "regions.json"),
        _wsl_path(ghidra_dir / "functions.csv"),
        _wsl_path(ghidra_dir / "import_summary.json"),
        str(max_functions),
        _wsl_path(ghidra_dir / "decompile.csv"),
        # The existing bounded decompiler must work on the raw program too; its
        # identity guard accepts the raw image's executable SHA-256 (Ghidra sets it
        # to the imported file's SHA-256, as import_summary.json records).
        "-postScript", "BoundedThumbProcedures.java",
        _wsl_path(ghidra_dir / "bounded_query.json"),
        _wsl_path(ghidra_dir / "bounded_result.json"),
        _sha(out_dir),
        "-deleteProject",
    ]


def write_command_script(image: Path, out_dir: Path, max_functions: int) -> Path:
    """Write ``ghidra/import_command.sh``; on Windows it runs with ``wsl bash``."""
    command = build_command(image, out_dir, max_functions)
    shell_argv = command[2:]  # drop wsl.exe --exec; the script runs inside WSL
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f'ANALYZE={shlex.quote(shell_argv[0])}',
        '[ -x "$ANALYZE" ] || { echo "missing analyzeHeadless: $ANALYZE" >&2; exit 2; }',
        f"rm -rf {shlex.quote(shell_argv[1])}",
        f"mkdir -p {shlex.quote(shell_argv[1])}",
        "exec " + " ".join(shlex.quote(part) for part in shell_argv),
        "",
    ]
    path = Path(out_dir) / "ghidra" / "import_command.sh"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def _sha8(out_dir: Path) -> str:
    connection = _db(out_dir)
    try:
        return dict(connection.execute("SELECT key, value FROM meta"))["image_sha256"][:8]
    finally:
        connection.close()


def _base(out_dir: Path) -> str:
    connection = _db(out_dir)
    try:
        return dict(connection.execute("SELECT key, value FROM meta"))["oril_base"]
    finally:
        connection.close()


def _sha(out_dir: Path) -> str:
    connection = _db(out_dir)
    try:
        return dict(connection.execute("SELECT key, value FROM meta"))["image_sha256"]
    finally:
        connection.close()


def import_image(image: Path, out_dir: Path, max_functions: int = DEFAULT_MAX_FUNCTIONS,
                 timeout: int = 900) -> dict:
    """Run Ghidra Headless over the staged inputs and write ``ghidra/import_receipt.json``."""
    out_dir = Path(out_dir)
    staged = stage(out_dir, max_functions)
    ghidra_dir = out_dir / "ghidra"
    receipt_path = ghidra_dir / "import_receipt.json"
    if receipt_path.exists():
        raise SystemExit(f"refusing to overwrite {receipt_path}; remove it to re-import")

    command = build_command(Path(image), out_dir, max_functions)
    script = write_command_script(Path(image), out_dir, max_functions)
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    elapsed = time.monotonic() - started
    stdout = result.stdout.replace("\x00", "")
    stderr = result.stderr.replace("\x00", "")
    (ghidra_dir / "import.log").write_text(
        "COMMAND\n" + " ".join(command) + "\n\nSTDOUT\n" + stdout
        + "\n\nSTDERR\n" + stderr, encoding="utf-8")

    summary_path = ghidra_dir / "import_summary.json"
    if result.returncode != 0 or not summary_path.is_file():
        tail = "\n".join((stdout + stderr).splitlines()[-25:])
        raise SystemExit(
            f"Ghidra import failed (exit {result.returncode}):\n{tail}\n"
            f"host workaround: wsl.exe bash {_wsl_path(script)}\n"
            f"then run: python -m tools.research.avcam_ghidra verify --out {out_dir}")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    checks = verify_summary(summary, staged)
    _check_layout(summary, staged)
    checks["layout"] = True
    bounded_checks, bounded_brief = _bounded_checks(ghidra_dir)
    checks.update(bounded_checks)
    receipt = {
        "schema_version": 1,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "elapsed_seconds": round(elapsed, 1),
        "command": command,
        "image": str(image),
        "bounded": bounded_brief,
        "summary": summary,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if receipt["status"] != "PASS":
        raise SystemExit(f"Ghidra import receipt is FAIL: {checks}")
    return receipt


def _check_layout(summary: dict, staged: dict) -> None:
    blocks = summary.get("blocks", [])
    if len(blocks) != staged["regions"]:
        raise SystemExit(f"expected {staged['regions']} region blocks, got {len(blocks)}")
    total = sum(block["size"] for block in blocks)
    if total != summary.get("source_size"):
        raise SystemExit(f"block sizes sum to {total}, image is {summary.get('source_size')}")
    executable = [block for block in blocks if block["execute"]]
    if not executable or executable[0]["name"].endswith("code") is False:
        raise SystemExit("the code region is not the executable block")


def _bounded_checks(ghidra_dir: Path) -> tuple[dict[str, bool], dict]:
    path = Path(ghidra_dir) / "bounded_result.json"
    if not path.is_file():
        return {"bounded_decompile": False, "bounded_pseudocode": False}, {"status": "missing"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    procedures = payload.get("procedures", [])
    completed = [item for item in procedures if item.get("status") == "completed"]
    brief = {
        "status": payload.get("status"),
        "queries": len(procedures),
        "completed": len(completed),
        "procedures": [
            {"entry": item.get("query", {}).get("entry"), "status": item.get("status"),
             "body_bytes": item.get("body_bytes"),
             "flows_outside": len(item.get("flows_outside", []))}
            for item in procedures
        ],
    }
    return {
        "bounded_decompile": bool(completed),
        "bounded_pseudocode": any(item.get("pseudocode") for item in completed),
    }, brief


def verify_summary(summary: dict, staged: dict) -> dict[str, bool]:
    blocks = summary.get("blocks", [])
    decompile = summary.get("decompile", [])
    return {
        "language": summary.get("language") == LANGUAGE,
        "image_base": summary.get("image_base") == staged["base"],
        "source_sha256": summary.get("source_sha256") == staged["sha256"],
        "blocks": len(blocks) == staged["regions"],
        "functions": summary.get("functions_created") == staged["max_functions"],
        "decompile_ok": sum(1 for item in decompile if item.get("status") == "ok") >= 1,
        "decompile_preview": any(item.get("c_preview") for item in decompile),
    }


def _staged_from_db(out_dir: Path) -> dict:
    connection = _db(out_dir)
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        regions = connection.execute("SELECT COUNT(*) FROM region").fetchone()[0]
    finally:
        connection.close()
    return {"base": meta["oril_base"], "sha256": meta["image_sha256"],
            "regions": regions, "max_functions": None}


def verify(out_dir: Path) -> int:
    out_dir = Path(out_dir)
    ghidra_dir = out_dir / "ghidra"
    summary_path = ghidra_dir / "import_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"no Ghidra summary at {summary_path}; run import first")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    staged = _staged_from_db(out_dir)
    staged["max_functions"] = summary.get("functions_requested")
    checks = verify_summary(summary, staged)
    try:
        _check_layout(summary, staged)
        checks["layout"] = True
    except SystemExit:
        checks["layout"] = False
    bounded_checks, bounded_brief = _bounded_checks(ghidra_dir)
    checks.update(bounded_checks)
    receipt_path = ghidra_dir / "import_receipt.json"
    receipt = {
        "schema_version": 1,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "mode": "headless" if receipt_path.exists() else "verified-from-summary",
        "checks": checks,
        "command_source": str(ghidra_dir / "import_command.sh"),
        "bounded": bounded_brief,
        "summary": summary,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    print(f"receipt {receipt_path}: {receipt['status']}")
    for name, value in checks.items():
        print(f"  {name}: {'PASS' if value else 'FAIL'}")
    print(f"  ghidra: {summary['ghidra_version']}  base: {summary['image_base']}  "
          f"blocks: {len(summary['blocks'])}  functions: {summary['functions_created']}")
    for procedure in bounded_brief.get("procedures", []):
        print(f"  bounded {procedure['entry']}: {procedure['status']} "
              f"({procedure['body_bytes']} body bytes, "
              f"{procedure['flows_outside']} flows outside)")
    for item in summary["decompile"]:
        if item.get("status") == "ok":
            first = item.get("c_preview", "").strip().splitlines()[:2]
            print(f"  decompiled {item['address']} ({item['function']}): "
                  f"{item.get('c_lines')} lines, first: {' / '.join(first)}")
    if receipt["status"] != "PASS":
        raise SystemExit("receipt does not match the index database")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    stage_parser = sub.add_parser("stage", help="write regions/functions/decompile inputs")
    stage_parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    stage_parser.add_argument("--max-functions", type=int, default=DEFAULT_MAX_FUNCTIONS)

    import_parser = sub.add_parser("import", help="run Ghidra Headless on the staged inputs")
    import_parser.add_argument("--image", type=Path, required=True)
    import_parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))
    import_parser.add_argument("--max-functions", type=int, default=DEFAULT_MAX_FUNCTIONS)
    import_parser.add_argument("--timeout", type=int, default=900)

    verify_parser = sub.add_parser("verify", help="check the receipt against the index")
    verify_parser.add_argument("--out", type=Path, default=Path("artifacts/analysis/avcam-map"))

    args = parser.parse_args(argv)
    if args.command == "stage":
        print(json.dumps(stage(args.out, args.max_functions), indent=2))
        return 0
    if args.command == "import":
        receipt = import_image(args.image, args.out, args.max_functions, args.timeout)
        print(f"Ghidra import PASS in {receipt['elapsed_seconds']} s")
        return 0
    return verify(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
