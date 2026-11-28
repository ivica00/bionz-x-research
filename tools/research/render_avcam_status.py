#!/usr/bin/env python3
"""Render the ``av-cam.bin`` deep-analysis progress dashboard to ``docs/images/avcam-status.svg``.

Pure Python standard-library SVG renderer so GitHub Actions and local checkouts
produce byte-for-byte identical output without Pillow or a headless browser.
Reads tracked shard JSONs and ``denominators.json`` from ``docs/avcam-map``
(no binary firmware or SQLite index required in CI).

Usage::

    python -m tools.research.render_avcam_status --check
    python -m tools.research.render_avcam_status --write
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from xml.sax.saxutils import escape

try:
    from tools.research import avcam_dashboard as dashboard
except ImportError:
    import avcam_dashboard as dashboard  # type: ignore

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAP_DIR = ROOT / "docs" / "avcam-map"
DEFAULT_OUTPUT = ROOT / "docs" / "images" / "avcam-status.svg"

WIDTH = 920
MARGIN = 24
CONTENT_W = WIDTH - 2 * MARGIN

COLORS = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "border": "#30363d",
    "track": "#21262d",
    "text": "#f0f6fc",
    "body": "#c9d1d9",
    "muted": "#8b949e",
    "spec_ready": "#3fb950",
    "decompiled": "#58a6ff",
    "located": "#d29922",
    "unmapped": "#30363d",
    "accent": "#39c5cf",
}

METRIC_LABELS = (
    ("abi", "ORIL ABI & Boot", "15%"),
    ("commands", "av_command_table", "20%"),
    ("tags", "[TAG] Subsystems", "15%"),
    ("functions", "Candidate Functions", "30%"),
    ("mmio", "MMIO 4 KiB Pages", "20%"),
)


def _stacked_bar(
    x: int,
    y: int,
    w: int,
    h: int,
    states: dict[str, int],
    total: int,
) -> list[str]:
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{COLORS["track"]}"/>']
    if total <= 0:
        return out
    segments = (
        ("SPEC_READY", COLORS["spec_ready"]),
        ("DECOMPILED", COLORS["decompiled"]),
        ("LOCATED", COLORS["located"]),
    )
    cursor = float(x)
    for key, color in segments:
        count = states.get(key, 0)
        if count <= 0:
            continue
        seg_w = (count / total) * w
        if seg_w < 1.0:
            seg_w = 1.0
        out.append(
            f'<rect x="{cursor:.1f}" y="{y}" width="{seg_w:.1f}" height="{h}" '
            f'rx="2" fill="{color}"/>'
        )
        cursor += seg_w
    return out


def render_svg(map_dir: Path = DEFAULT_MAP_DIR) -> str:
    """Validate shards and render the deterministic SVG progress dashboard."""
    base = dashboard.load_denominators(map_dir)
    shards = dashboard.load_shards(map_dir)
    dashboard.validate(shards, base)
    result = dashboard.compute(shards)

    overall_pct = result["overall"] * 100.0
    sha_short = str(base["meta"].get("image_sha256", "unknown"))[:12]
    oril_base = str(base["meta"].get("oril_base", "0x635c6000"))

    header_h = 84
    kpi_h = 186
    domain_row_h = 34
    domains_list = result["domains"]
    domains_h = 68 + len(domains_list) * domain_row_h + 20
    footer_h = 34
    height = MARGIN + header_h + 16 + kpi_h + 16 + domains_h + 12 + footer_h + MARGIN

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" '
        f'aria-label="av-cam.bin deep-analysis progress: {overall_pct:.2f}%">',
        "<style>",
        '  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }',
        '  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }',
        "</style>",
        f'<rect width="{WIDTH}" height="{height}" fill="{COLORS["bg"]}"/>',
    ]

    # Header panel
    y = MARGIN
    parts.append(
        f'<rect x="{MARGIN}" y="{y}" width="{CONTENT_W}" height="{header_h}" rx="8" '
        f'fill="{COLORS["panel"]}" stroke="{COLORS["border"]}"/>'
    )
    parts.append(
        f'<text x="{MARGIN + 20}" y="{y + 32}" fill="{COLORS["text"]}" '
        f'font-size="20" font-weight="700">ILCE-6400 av-cam.bin Deep-Analysis Progress</text>'
    )
    parts.append(
        f'<text x="{MARGIN + 20}" y="{y + 58}" fill="{COLORS["muted"]}" '
        f'font-size="12" class="mono">'
        f'ORIL v4 @ {escape(oril_base)}  |  sha256 {escape(sha_short)}...  |  '
        f'10 parallel shards</text>'
    )
    parts.append(
        f'<text x="{WIDTH - MARGIN - 20}" y="{y + 42}" text-anchor="end" '
        f'fill="{COLORS["accent"]}" font-size="28" font-weight="700" class="mono">'
        f'{overall_pct:.2f}%</text>'
    )
    parts.append(
        f'<text x="{WIDTH - MARGIN - 20}" y="{y + 62}" text-anchor="end" '
        f'fill="{COLORS["muted"]}" font-size="11">OVERALL MATURITY</text>'
    )

    # KPI panel
    y += header_h + 16
    parts.append(
        f'<rect x="{MARGIN}" y="{y}" width="{CONTENT_W}" height="{kpi_h}" rx="8" '
        f'fill="{COLORS["panel"]}" stroke="{COLORS["border"]}"/>'
    )
    parts.append(
        f'<text x="{MARGIN + 20}" y="{y + 26}" fill="{COLORS["text"]}" '
        f'font-size="14" font-weight="600">Weighted KPI Metrics</text>'
    )
    # Legend
    lx = WIDTH - MARGIN - 20
    legend = (
        ("SPEC_READY (1.0)", COLORS["spec_ready"]),
        ("DECOMPILED (0.6)", COLORS["decompiled"]),
        ("LOCATED (0.25)", COLORS["located"]),
        ("UNMAPPED (0)", COLORS["unmapped"]),
    )
    for label, color in reversed(legend):
        parts.append(
            f'<text x="{lx}" y="{y + 25}" text-anchor="end" fill="{COLORS["muted"]}" '
            f'font-size="11">{escape(label)}</text>'
        )
        lx -= len(label) * 6 + 14
        parts.append(f'<rect x="{lx}" y="{y + 16}" width="10" height="10" rx="2" fill="{color}"/>')
        lx -= 12

    ky = y + 46
    for key, label, weight_str in METRIC_LABELS:
        m = result["metrics"][key]
        pct = m["score"] * 100.0
        parts.append(
            f'<text x="{MARGIN + 20}" y="{ky + 13}" fill="{COLORS["body"]}" '
            f'font-size="12" font-weight="600">{escape(label)}</text>'
        )
        parts.append(
            f'<text x="{MARGIN + 185}" y="{ky + 13}" fill="{COLORS["muted"]}" '
            f'font-size="11" class="mono">w={weight_str}</text>'
        )
        parts.extend(_stacked_bar(MARGIN + 245, ky + 3, 430, 12, m["states"], m["total"]))
        parts.append(
            f'<text x="{WIDTH - MARGIN - 20}" y="{ky + 13}" text-anchor="end" '
            f'fill="{COLORS["text"]}" font-size="12" class="mono">'
            f'{pct:6.2f}%  (n={m["total"]:,})</text>'
        )
        ky += 26

    # Domains panel
    y += kpi_h + 16
    parts.append(
        f'<rect x="{MARGIN}" y="{y}" width="{CONTENT_W}" height="{domains_h}" rx="8" '
        f'fill="{COLORS["panel"]}" stroke="{COLORS["border"]}"/>'
    )
    parts.append(
        f'<text x="{MARGIN + 20}" y="{y + 26}" fill="{COLORS["text"]}" '
        f'font-size="14" font-weight="600">Domain Shards (10 Parallel Workstreams)</text>'
    )
    hy = y + 50
    parts.append(
        f'<text x="{MARGIN + 20}" y="{hy}" fill="{COLORS["muted"]}" font-size="11">DOMAIN</text>'
    )
    parts.append(
        f'<text x="{MARGIN + 290}" y="{hy}" fill="{COLORS["muted"]}" font-size="11">STAGE</text>'
    )
    parts.append(
        f'<text x="{MARGIN + 360}" y="{hy}" fill="{COLORS["muted"]}" font-size="11">PROGRESS</text>'
    )
    parts.append(
        f'<text x="{MARGIN + 640}" y="{hy}" fill="{COLORS["muted"]}" font-size="11">FUNCS / ITEMS</text>'
    )
    parts.append(
        f'<text x="{WIDTH - MARGIN - 20}" y="{hy}" text-anchor="end" '
        f'fill="{COLORS["muted"]}" font-size="11">MATURITY</text>'
    )

    ry = hy + 16
    for dom in domains_list:
        fn_counts = dom["functions"]
        fn_total = sum(fn_counts.values())
        item_states = {
            "UNMAPPED": 0,
            "LOCATED": 0,
            "DECOMPILED": 0,
            "SPEC_READY": 0,
        }
        for kind_counts in dom["items"].values():
            for st, cnt in kind_counts.items():
                item_states[st] = item_states.get(st, 0) + cnt
        combined_states = {
            st: item_states[st] + fn_counts.get(st, 0)
            for st in ("UNMAPPED", "LOCATED", "DECOMPILED", "SPEC_READY")
        }
        combined_total = sum(combined_states.values())
        item_total = sum(item_states.values())
        dom_pct = dom["maturity"] * 100.0
        title = f"{dom['domain']}  {dom['title']}"
        if len(title) > 34:
            title = title[:31] + "..."
        parts.append(
            f'<text x="{MARGIN + 20}" y="{ry + 13}" fill="{COLORS["body"]}" '
            f'font-size="12">{escape(title)}</text>'
        )
        parts.append(
            f'<text x="{MARGIN + 290}" y="{ry + 13}" fill="{COLORS["muted"]}" '
            f'font-size="11" class="mono">P{dom["phase"]}/{escape(dom["milestone"])}</text>'
        )
        parts.extend(_stacked_bar(MARGIN + 360, ry + 3, 250, 12, combined_states, combined_total))
        parts.append(
            f'<text x="{MARGIN + 640}" y="{ry + 13}" fill="{COLORS["muted"]}" '
            f'font-size="11" class="mono">{fn_total:,} fn / {item_total:,} it</text>'
        )
        parts.append(
            f'<text x="{WIDTH - MARGIN - 20}" y="{ry + 13}" text-anchor="end" '
            f'fill="{COLORS["text"]}" font-size="12" class="mono">{dom_pct:6.2f}%</text>'
        )
        ry += domain_row_h

    # Footer
    y += domains_h + 12
    parts.append(
        f'<text x="{MARGIN}" y="{y + 16}" fill="{COLORS["muted"]}" font-size="11">'
        f'Generated by tools/research/render_avcam_status.py from docs/avcam-map/shards/ '
        f'(formula: 0.15*ABI + 0.20*CMD + 0.15*TAG + 0.30*FUNC + 0.20*MMIO)</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map-dir", type=Path, default=DEFAULT_MAP_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="exit non-zero if committed SVG differs from rendered output")
    mode.add_argument("--write", action="store_true",
                      help="write rendered SVG to --out")
    mode.add_argument("--stdout", action="store_true",
                      help="print rendered SVG to stdout")
    args = parser.parse_args(argv)

    svg = render_svg(args.map_dir)
    if args.stdout:
        sys.stdout.write(svg)
        return 0
    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(svg, encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    # Default: --check
    if not args.out.is_file():
        print(f"missing SVG: {args.out}", file=sys.stderr)
        return 1
    current = args.out.read_text(encoding="utf-8")
    if current != svg:
        print(f"drift detected in {args.out}; run with --write", file=sys.stderr)
        return 1
    print(f"OK: {args.out} matches docs/avcam-map/shards/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
