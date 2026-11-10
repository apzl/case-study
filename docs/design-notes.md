# Design notes

Condensed from the development log: the decisions that shaped the pipeline, the sources, what each
step costs, and where it stops scaling.

## Decisions

1. **Maps first, directories as supplement.** The VDIV member directory alone gave a few hundred
   firms and almost no technical facts. Google Places and OpenStreetMap give the broad Berlin list
   (with office phone and map position); association directories (VDIV, BBU, BVI, WBGD, the
   state-owned list) add official names and a warm-introduction signal (VDIV membership).
2. **Own website first, search only for gaps.** The company's own pages and documents are the
   primary source. A web search runs only to find a missing website (or third-party pages about a
   firm without one) and, optionally (`--gap-search`), to fill values still missing.
3. **Evidence on every fact.** The model must return an exact quote from a saved page block for
   each claim. Code checks the quote (80% word match tolerated), that numbers, phones and e-mails
   are inside it, and known traps: parking spaces counted as flats, Berlin-and-Brandenburg totals
   as Berlin, group totals as company totals, smoke detectors as consumption meters, past jobs as
   current. Search snippets are discovery only, never evidence.
4. **No second-model review.** An earlier version had a reviewer model and repair rounds; it cost
   about 7x more per company for little sales value. Now: one Haiku extraction, cheap code-side
   repairs, and one gap pass that asks only for the missing fact families. Conflicting counts are
   reported but never block a fact (the largest published count is used).
5. **Only fields that change the sales conversation.** Register identity, device types, inactivity
   and "existing customer" were dropped after measuring them: rare, misread or not useful.
6. **Berlin is a filter, not a score.** A Berlin address in the map/directory record or one accepted
   Berlin fact puts a company on the list; it earns no points.
7. **Two separate scores.** Fit (0-100) answers "do they need Heidi": manages apartments +25;
   portfolio size up to +15 (1,000+ apartments = key account); meters read by hand +25 (2027
   remote-reading deadline); provider change announced +25; current billing setup known +10.
   Access (0-100) answers "can sales reach the person who runs the buildings": property manager
   before owner/director before other staff; a direct line beats a name; a personal LinkedIn
   profile adds 10. Tiers come from fit (A 50+, B 25-49, C 1-24, or an unresearched first look of 20+).
8. **Exclude before paying.** Brokers (Makler without management wording), metering competitors and
   sites with no management wording are blocked before any model call; service firms (cleaning,
   caretaking, repairs, letting) are removed by a keyword rule with a hand-checked exception list.
9. **Login-only model access.** All model calls go through the Claude Agent SDK on the local Claude
   Code login; API-key variables are removed from the environment.
10. **Rebuildable outputs.** Workbook, CSVs and dashboard data are rebuilt from the database without
    network or model calls, so rules can change and exports refresh for free.

## Sources

| Source | Gives | Notes |
|---|---|---|
| Google Places API (New), Text Search | Names, address, office phone, website, map position | 14 terms x 12 districts, up to 3 pages each; cached |
| OpenStreetMap Overpass | Names, address, website | Free, ODbL; one cached request per run |
| VDIV Berlin-Brandenburg members | Official names, warm intro | Single public members page |
| BBU, WBGD, BVI, state-owned housing companies | Housing companies, cooperatives, managers | robots.txt checked per host |
| Company websites (Scrapy, Playwright fallback, PDF text/OCR) | All facts and most contacts | Impressum supplies most managing-director contacts |
| Claude WebSearch | Websites and third-party pages for firms without a site; LinkedIn result titles; team pages | Discovery only; LinkedIn results need `allowed_domains=["linkedin.com"]` |
| LinkedIn public job search (guest pages) | Hiring signal, ad contact person | No login or cookies, polite pauses |

Skipped: IVD (search hosted on a site whose robots.txt disallows AI crawlers), Haus & Grund Berlin
(no manager list), Berliner Genossenschaftsforum (no member list).

## Cost per step

| Step | Cost |
|---|---|
| Discovery | Google Places within free allowance; others free |
| Crawl | Free (bandwidth) |
| Extraction + gap pass (Haiku) | about $0.018 per company |
| Website search | about $0.043 per firm; finds a page for a minority of website-less firms |
| Contact step | about $0.05 per lead |
| Job ads | search free; about $0.003 per ad read |
| Scoring, export, dashboard | Free (no model calls) |

## Limits at larger scale

- Places caps results per query; more cities need finer tiling (postcodes) and a cost check first.
- Directory pages change markup; the scrapers are small and need maintenance.
- About 130 page downloads failed (timeouts, TLS errors); JS-only sites need the browser fallback.
- The website search is the most expensive step per useful result: small firms are often not online.
- Metering provider, 2027 status and contract timing are published by only 1-2% of companies.
  Better yield would need other sources (tenant notices, rental listings, tenders) or calls.
- Contact step: LinkedIn titles are often missing, profile pages cannot be fetched logged out, and
  personal e-mails are rare. Personal data must stay out of git and be used only with a legal basis.
- The service-firm rule still needs a hand check of flagged leads; name matching for job ads needs at
  least two distinctive words to avoid false matches.
