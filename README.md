# Berlin property-manager leads for Heidi Systems

Builds a ranked call list of Berlin property managers and housing companies for Heidi Systems
(heat-cost billing and metering): who manages residential buildings, how big they are, whether their
meters must be upgraded for the 2027 remote-reading rule, and whom to call. Every fact carries a quote
from a saved source page. Prototype for a case study: no outreach, no paid data sets.

- Results (aggregates): [`docs/results.md`](docs/results.md)
- Design decisions, sources, costs and limits: [`docs/design-notes.md`](docs/design-notes.md)
- Case-study deck: [`docs/Heidi_Lead_Pipeline_Case_Study.pptx`](docs/Heidi_Lead_Pipeline_Case_Study.pptx)
- Static sales dashboard (hostable, redacted data): separate repository `case-study-dashboard`

## Architecture

| Stage | What happens | Module |
|---|---|---|
| 1. Discovery | Google Places (Text Search, 14 terms x 12 districts) and OpenStreetMap Overpass; association directories: VDIV, BBU, BVI, WBGD, the state-owned housing companies | `discover_maps.py`, `discover_directories.py`, `collect.discover` (VDIV) |
| 2. Relevance filter + dedup | Name/place-type filter; global dedup by registrable domain, phone, name + postcode (union-find) | `pipeline.py discover`, `dedup.py` |
| 3. Crawl | Company website: home plus up to 6 relevant pages (Impressum, Kontakt, Team, Leistungen, tenant service, PDFs); robots.txt obeyed, public IPs only; Playwright fallback for JS-only pages; PDFs via pdftotext, OCR fallback | `collect.py` |
| 4. Website search | Firms without a website: one Claude WebSearch call finds the own site or up to 4 third-party pages about the firm | `run_full.resolve_websites`, `search_sources.py` |
| 5. Fact extraction | Claude Haiku (Claude Agent SDK) reads a bounded page packet and returns typed claims, each with an exact quote; a gap pass asks only for missing families | `pipeline.enrich`, `rules.py` |
| 6. Checks | Code confirms each quote is on the page (80% word match), that numbers, phones and e-mails are in the quote, and catches known traps (parking spaces, regional totals, smoke detectors) | `core.check_claim`, `core.autofix_claims` |
| 7. Scoring | Rule-based: Fit score 0-100, separate Access score 0-100, tiers A-D, segment, opener | `core.qualify`, `core.fit_score`, `core.access`, `core.sales_opener` |
| 8. Contacts | Per A-C lead, one Claude call with WebSearch/WebFetch: LinkedIn results and team pages; a person is kept only if the tools returned them | `find_contacts.py` |
| 9. Job ads | LinkedIn public (logged-out) job search: which leads are hiring property staff; one small Haiku call reads an ad's named contact | `job_ads.py` |
| 10. Export | Sales workbook (Leads, Contacts, Evidence, Details, Guide) and CSVs, rebuilt from the database without network or model calls | `pipeline.export`, `workbook.py` |
| 11. Dashboard data | `data.js` for the static dashboard; `--public` writes a redacted copy | `dashboard_data.py` |

`run_full.py` orchestrates stages 1-8 in resumable batches (it calls `pipeline` subcommands, `dedup`
and `find_contacts` as subprocesses). `job_ads` and `dashboard_data` are run separately.

### Data flow

```
discovery ──> data/leads.sqlite  (companies, documents = saved page blocks, claims, runs)
                 │      data/snapshots/  (raw page bytes)
                 ▼
       pipeline export ──> output/Heidi_Berlin_Pilot.xlsx, output/companies.csv,
                           output/details.csv, output/evidence.csv
                 ▼
       dashboard_data  ──> output/dashboard/data.js        (full, local only)
       dashboard_data --public ──> ../case-study-dashboard/data.js (redacted, hostable)
```

`find_contacts` and `job_ads` read the lead list from `output/companies.csv` and write their findings
back into the database as ordinary contact claims (with the search result or ad text as the evidence
document), so the next export picks them up.

## Setup

Python 3.12; system tools for PDFs: Poppler (`pdftotext`, `pdftoppm`), Tesseract (deu, eng), OCRmyPDF.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp google_places.env.example google_places.env   # add the key (only needed for Google discovery)
```

Model calls use the **Claude Agent SDK on the local Claude Code login**: install Claude Code and sign
in (`claude`, then `/login`). No API key is used; `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are
removed from the process environment before each call so the login is always used.

## How to run

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

## Outputs

| Path | Content | In git |
|---|---|---|
| `data/leads.sqlite`, `data/snapshots/` | Database and raw page snapshots | no |
| `output/Heidi_Berlin_Pilot.xlsx` | Sales workbook: Leads (tiers A-C), Contacts, Evidence, Details (all companies), Guide | no |
| `output/companies.csv`, `details.csv`, `evidence.csv` | Same tables as CSV | no |
| `output/run-report.json` | Row counts, company states, yield per fact type | no |
| `output/dashboard/` | Full dashboard (pages + data with named contacts) | no |
| `runs/` | Model, search, contact and job-ad logs; batch files; packets | no |
| `../case-study-dashboard/data.js` | Hostable dashboard data, redacted (separate repository) | yes, in that repo |

## Configuration

| Variable | Default | Used by |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | read from `google_places.env` if unset | `discover_maps` (Google Places API (New)) |
| `HEIDI_EXTRACT_MODEL` | `claude-haiku-5-5` | fact extraction and gap pass |
| `HEIDI_EFFORT` | `low` | effort setting for extraction calls |

Website search, contact step and job-ad reading use `claude-haiku-5-5` (constants in
`search_sources.py`, `find_contacts.py`, `job_ads.py`; `run_full.resolve_websites` takes a `model` argument).

## Costs

Model cost is reported by the Claude Agent SDK (`total_cost_usd`; notional when billed to a Claude
subscription). Measured on the full Berlin run:

| Step | Cost |
|---|---|
| Fact extraction + gap pass | about $0.018 per company read |
| Website search (firms without a site) | about $0.043 per search |
| Contact step | about $0.05 per lead |
| Job ads (reading an ad's contact) | about $0.003 per ad; the search itself is free |
| Overall | about $0.09 per lead (about $95 for 1,115 leads) |

Google Places stayed within the monthly free allowance (responses are cached by query and page, so
reruns cost nothing); OpenStreetMap and the directories are free.

## Data protection

Only business information is collected: company names, listed phone numbers, office addresses and
professional contacts published on company websites, search results and job ads. Everything that
names or contacts a person (database, workbook, CSVs, run report, full dashboard data, model and
search logs) stays on the local machine: `data/`, `output/` and `runs/` are git-ignored. The only
published data file is the dashboard repository's `data.js`, produced by `dashboard_data --public`, which drops
people, roles, direct lines, personal e-mails, LinkedIn profiles and quotes that name a person.
Use of the contact data for outreach needs its own legal basis (B2B, legitimate interest) and an
opt-out process.

## Limits

- Company websites rarely state meters, heating or metering contracts: the 2027 signal and provider
  changes are found for only a few percent of companies; apartment counts for under a fifth.
- Facts are checked by code against the saved page, not by a second model or a person; categories
  are deliberately loose (utility over proof).
- Score weights are judgment calls based on Heidi's own public material, not fitted to sales outcomes.
- LinkedIn search results rarely show job titles, so many named people count as "other staff";
  personal e-mails are rarely published.
- Google Places caps results per query; directories change their markup; LinkedIn guest pages may
  rate-limit. Small firms without a website are often not findable at all.
- Single-city prototype: Berlin postcodes, districts and terms are hard-coded.
