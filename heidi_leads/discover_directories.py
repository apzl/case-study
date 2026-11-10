"""Free public directories of Berlin property managers / housing companies (one discovery pass).

Sources (robots.txt checked per host, HTML cached under runs/discovery/cache/<source>/):
- BBU: Verband Berlin-Brandenburgischer Wohnungsunternehmen member list, region facet Berlin.
- WBGD: wohnungsbaugenossenschaften.de 'alle Genossenschaften' (Verbund Berlin; Berlin postcodes kept).
- BVI: Bundesfachverband der Immobilienverwalter Verwaltersuche (whole list on one page; Berlin kept).
- LANDESEIGENE: the six state-owned Berlin housing companies + berlinovo (hard-coded, websites checked Nov 2026).
Skipped (see report): IVD (search is an iframe on immobilie1.de, whose robots.txt disallows ClaudeBot/anthropic-ai),
Haus & Grund Berlin (lists only owner associations and partners, no Verwalter), Berliner Genossenschaftsforum (no member list).

`python -m heidi_leads.discover_directories [--ingest]` writes runs/discovery/directories-candidates.json;
--ingest adds Berlin-filtered candidates via Store.add_company (state discovered). Run heidi_leads.dedup afterwards.
"""
from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import requests
from parsel import Selector
from heidi_leads.core import Store, digest, now
from heidi_leads.discover_maps import berlin_filter, candidate, log, postcode_of, to_company_rows

UA = 'HeidiCaseStudyResearch/0.1 (public property manager research)'  # same as collect.py
DELAY = 1.5
_robots, _last = {}, [0.0]

def fetch(url, source, cache_dir='runs/discovery/cache'):
    """GET with per-host robots.txt check, DELAY between live requests, cache by URL hash."""
    f = Path(cache_dir) / source / f'{digest(url)[:20]}.html'
    if f.exists(): return f.read_text()
    p = urlsplit(url); root = f'{p.scheme}://{p.netloc}'
    if root not in _robots:
        rp = RobotFileParser(); r = requests.get(root + '/robots.txt', headers={'User-Agent': UA}, timeout=25)
        rp.parse(r.text.splitlines() if r.status_code == 200 else []); _robots[root] = rp
    if not _robots[root].can_fetch(UA, url): raise PermissionError(f'robots.txt disallows {url}')
    time.sleep(max(0, DELAY - (time.time() - _last[0])))
    r = requests.get(url, headers={'User-Agent': UA}, timeout=40); _last[0] = time.time(); r.raise_for_status()
    f.parent.mkdir(parents=True, exist_ok=True); f.write_text(r.text)
    return r.text

def text(sel): return ' '.join(' '.join(sel.getall()).split())

# ---------- BBU ----------
BBU = 'https://bbu.de/mitgliedsunternehmen?f%5B0%5D=r%3A130'  # r:130 = region facet 'Berlin'
# BBU also lists supporting members that hold no housing: banks, utilities, telecoms, software, metering (Techem = competitor).
BBU_NOT_HOUSING = re.compile(r'bank|\bhyp\b|sparkasse|volksbank|gasag|energie und wärme|techem|vodafone|tele columbus|mih net|aareon|gig technology', re.I)

def bbu(max_pages=20):
    out = []
    for page in range(max_pages):
        rows = Selector(fetch(f'{BBU}&page={page}', 'bbu')).css('article.node--type-company')
        if not rows: break
        for a in rows:
            name = text(a.css('.company-name h4 ::text')); form = text(a.css('.company-form ::text'))
            if BBU_NOT_HOUSING.search(name): continue
            region = text(a.css('.company-region ::text'))
            out.append(candidate(name=name, website=a.css('.company-website a::attr(href)').get(), city=region or None,
                source='BBU', source_id=a.attrib.get('data-history-node-id', ''), place_types=[f'legal_form={form}'] if form else [],
                matched_terms=['BBU member'], raw_address=region))
    return out

# ---------- Wohnungsbaugenossenschaften Deutschland ----------
WBGD = 'https://www.wohnungsbaugenossenschaften.de/genossenschaften/alle-genossenschaften'

def wbgd():
    out = []
    for p in Selector(fetch(WBGD, 'wbgd')).css('div.address p'):
        name = text(p.css('strong ::text'))
        lines = [l.strip() for l in p.xpath('text()').getall() if l.strip()]
        addr = [l for l in lines if not re.match(r'(Tel|Fax)\b', l)]
        pc_line = next((l for l in addr if postcode_of(l)), '')
        street = next((l for l in addr if l != pc_line), None)
        phone = next((l[3:].strip() for l in lines if l.startswith('Tel')), None)
        email = next((h[7:] for h in p.css('a::attr(href)').getall() if h.startswith('mailto:')), None)
        site = next((h for h in p.css('a::attr(href)').getall() if h.startswith('http')), None)
        if not name: continue
        out.append(candidate(name=name, website=site, phone=phone, email=email, street=street, postcode=postcode_of(pc_line),
            city=re.sub(r'^\s*\d{5}\s*', '', pc_line) or None, source='WBGD', source_id=digest(name + pc_line)[:16],
            matched_terms=['Wohnungsbaugenossenschaft'], raw_address=' '.join(addr)))
    return out

# ---------- BVI Verwaltersuche ----------
BVI = 'https://bvi-verwalter.de/verwaltersuche/'

def bvi():
    out = []
    for c in Selector(fetch(BVI, 'bvi')).css('div[data-markerid]'):
        name = text(c.css('p.h-xsmall ::text'))
        addr = [l.strip() for l in c.xpath('.//p[contains(@class,"txt-blau-100") and not(contains(@class,"h-xsmall"))]/text()').getall() if l.strip()]
        addr = [' '.join(l.split()) for l in addr]
        pc_line = next((l for l in addr if postcode_of(l)), '')
        hrefs = c.css('a::attr(href)').getall()
        phone = next((h[4:] for h in hrefs if h.startswith('tel:')), None)
        site = next((h for h in hrefs if h.startswith('http') and 'maps.apple.com' not in h), None)
        out.append(candidate(name=name, website=site, phone=phone, street=addr[0] if addr and addr[0] != pc_line else None,
            postcode=postcode_of(pc_line), city=re.sub(r'^\d{5}\s*', '', pc_line) or None,
            lat=float(c.attrib['data-latitude']) if c.attrib.get('data-latitude') else None,
            lon=float(c.attrib['data-longitude']) if c.attrib.get('data-longitude') else None,
            source='BVI', source_id=c.attrib.get('data-markerid', ''), matched_terms=['BVI member'], raw_address=' '.join(addr)))
    return out

# ---------- state-owned housing companies ----------
LANDESEIGENE = [
    ('degewo AG', 'https://www.degewo.de', 'Potsdamer Straße 60', '10785'),
    ('GESOBAU AG', 'https://www.gesobau.de', 'Stiftsweg 1', '13187'),
    ('Gewobag Wohnungsbau-Aktiengesellschaft Berlin', 'https://www.gewobag.de', 'Alt-Moabit 101 A', '10559'),
    ('HOWOGE Wohnungsbaugesellschaft mbH', 'https://www.howoge.de', 'Ferdinand-Schultze-Straße 71', '13055'),
    ('STADT UND LAND Wohnbauten-Gesellschaft mbH', 'https://www.stadtundland.de', 'Werbellinstraße 12', '12053'),
    ('WBM Wohnungsbaugesellschaft Berlin-Mitte mbH', 'https://www.wbm.de', 'Dircksenstraße 38', '10178'),
    ('berlinovo Immobilien Gesellschaft mbH', 'https://www.berlinovo.de', 'Hallerstraße 6', '10587'),
]

def landeseigene():
    return [candidate(name=n, website=w, street=s, postcode=pc, city='Berlin', source='LANDESEIGENE', source_id=w.split('//')[1],
                      matched_terms=['landeseigene Wohnungsbaugesellschaft'], raw_address=f'{s} {pc} Berlin') for n, w, s, pc in LANDESEIGENE]

SOURCES = {'BBU': bbu, 'WBGD': wbgd, 'BVI': bvi, 'LANDESEIGENE': landeseigene}

def run(out='runs/discovery', ingest=False, root='.'):
    found, kept, errors = {}, [], {}
    for name, fn in SOURCES.items():
        try: cs = fn()
        except Exception as e: errors[name] = f'{type(e).__name__}: {e}'; log(f'{name}: {errors[name]}'); continue
        k = [c for c in cs if c['name'] and berlin_filter(c)]
        found[name] = dict(raw=len(cs), berlin=len(k)); kept += k; log(f'{name}: {len(cs)} raw, {len(k)} Berlin')
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / 'directories-candidates.json').write_text(json.dumps(dict(generated_at=now(), found=found, errors=errors,
        candidates=[{k: v for k, v in c.items() if k != 'raw'} for c in kept]), ensure_ascii=False, indent=1))
    ids = {}
    if ingest:
        store = Store(root)
        for (name, url, src), c in zip(to_company_rows(kept), kept):
            ids.setdefault(c['source'], []).append(store.add_company(name, url, src))
        (Path(out) / 'directories-ingested-ids.json').write_text(json.dumps(ids, indent=1))
    return found, ids, errors

def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m heidi_leads.discover_directories')
    ap.add_argument('--ingest', action='store_true', help='add Berlin candidates to data/leads.sqlite (state discovered)')
    a = ap.parse_args(argv)
    found, ids, errors = run(ingest=a.ingest)
    log(json.dumps(dict(found=found, ingested={k: len(set(v)) for k, v in ids.items()}, errors=errors), indent=2))
    return 1 if errors else 0

if __name__ == '__main__': raise SystemExit(main())
