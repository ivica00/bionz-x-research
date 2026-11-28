# Tracked `av-cam.bin` Mapping Shards

This directory holds the Git-tracked domain shards (`shards/S*.json`) and lightweight
`denominators.json` (IDs, counts, and metadata only—no proprietary firmware bytes)
for the ILCE-6400 `av-cam.bin` deep-analysis effort.

- **Validate locally:** `python -m tools.research.avcam_dashboard --check --out docs/avcam-map`
- **Render SVG locally:** `python -m tools.research.render_avcam_status --write`
- **CI refresh:** `.github/workflows/avcam-status.yml` automatically re-renders
  `docs/images/avcam-status.svg` on `main` after every 3 shard-updating merges (or on
  manual `workflow_dispatch`).
