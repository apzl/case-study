# Running individual steps

The README covers the normal run. This page lists every step on its own, configuration, and system tools.

## Full setup

Python 3.12; system tools for PDFs: Poppler (`pdftotext`, `pdftoppm`), Tesseract (deu, eng), OCRmyPDF.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp google_places.env.example google_places.env   # add the key (only needed for Google discovery)
```

Model calls use the **Claude Agent SDK**: install Claude Code and sign in (`claude`, then `/login`).

## All commands

All commands run from the repository root (paths are relative to it).

```bash
# Cost check for Google Places (no requests are sent)
python -m heidi_leads.discover_maps --source google --estimate

# Full run: discovery, dedup, crawl, extraction, website search, export, contact step
python -m heidi_leads.run_full --source both
python -m heidi_leads.run_full --source none --only-new --shard 0/4   # four parallel runners, i = 0..3
python -m heidi_leads.run_full --source none --dry-run                # count what would run

# Individual steps
python -m heidi_leads.discover_directories --ingest         # BBU, WBGD, BVI, state-owned list
python -m heidi_leads.pipeline fetch-seeds                  # save the VDIV directory page
python -m heidi_leads.pipeline discover                     # VDIV -> database (default source)
python -m heidi_leads.pipeline discover --source both       # Google + OSM -> database
python -m heidi_leads.dedup [--dry-run]
python -m heidi_leads.pipeline collect --ids ids.json [--targets targets.json]
python -m heidi_leads.pipeline enrich  --ids ids.json
python -m heidi_leads.find_contacts --tiers ABC [--limit N] [--workers 4]
python -m heidi_leads.job_ads search                        # free: which leads are hiring
python -m heidi_leads.job_ads contacts [--limit N]

# Rebuild outputs from the database, no network or model calls
python -m heidi_leads.pipeline export                       # workbook + CSVs
python -m heidi_leads.pipeline evaluate                     # export + output/run-report.json
python -m heidi_leads.dashboard_data                        # output/dashboard/data.js (full)
python -m heidi_leads.dashboard_data --public               # ../case-study-dashboard/data.js (redacted)

# Tests (offline; model calls are faked)
python -m pytest -q
```

Every stage is idempotent. `run_full` records batches in `runs/full/progress*.json`; companies that
are already researched are skipped, and search logs are reused, so an interrupted run continues
where it stopped.

## Configuration

| Variable | Default | Used by |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | read from `google_places.env` if unset | `discover_maps` (Google Places API (New)) |
| `HEIDI_EXTRACT_MODEL` | `claude-haiku-5-5` | fact extraction and gap pass |
| `HEIDI_EFFORT` | `low` | effort setting for extraction calls |

Website search, contact step and job-ad reading use `claude-haiku-5-5` (constants in
`search_sources.py`, `find_contacts.py`, `job_ads.py`; `run_full.resolve_websites` takes a `model` argument).
