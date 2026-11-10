"""Map-service discovery of Berlin property managers (primary name source; VDIV supplements).

Sources:
- OSM Overpass (free, ODbL): one request per run, raw response cached.
- Google Places API (New) Text Search (paid, opt-in: key from env GOOGLE_MAPS_API_KEY, else google_places.env).

Output: runs/discovery/maps-candidates.json and maps-summary.json. `to_company_rows` yields
(name, url, source_dict) tuples for Store.add_company; ingestion is left to the orchestrator.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit
import requests
import tldextract
from heidi_leads.core import digest, domain, norm, now

UA = 'heidi-case-study-research/0.1'
TERMS = ['Hausverwaltung','Immobilienverwaltung','WEG-Verwaltung','Mietverwaltung','Miethausverwaltung','Grundstücksverwaltung','Objektverwaltung','Liegenschaftsverwaltung','Wohnungsverwaltung','Immobilienmanagement','Property Management','Wohnungsbaugesellschaft','Wohnungsgenossenschaft','Facility Management']
DISTRICTS = ['Mitte','Friedrichshain-Kreuzberg','Pankow','Charlottenburg-Wilmersdorf','Spandau','Steglitz-Zehlendorf','Tempelhof-Schöneberg','Neukölln','Treptow-Köpenick','Marzahn-Hellersdorf','Lichtenberg','Reinickendorf']
OVERPASS = 'https://overpass-api.de/api/interpreter'
OSM_ATTRIBUTION = '© OpenStreetMap contributors. Data available under the Open Database License (ODbL) 1.0, https://www.openstreetmap.org/copyright'
PLACES = 'https://places.googleapis.com/v1/places:searchText'
# places.location added to the requested mask for lat/lon; it does not change the (already Enterprise) SKU.
FIELD_MASK = 'places.id,places.displayName,places.formattedAddress,places.addressComponents,places.websiteUri,places.nationalPhoneNumber,places.types,places.businessStatus,places.rating,places.userRatingCount,places.location,nextPageToken'
BERLIN_RECT = {'rectangle':{'low':{'latitude':52.33,'longitude':13.08},'high':{'latitude':52.68,'longitude':13.77}}}
MAX_PAGES = 3
# Hosts shared by unrelated businesses: never dedup or match on them.
PLATFORM_HOSTS = {'facebook.com','instagram.com','linkedin.com','google.com','business.site','wixsite.com','jimdosite.com','jimdo.com','immobilienscout24.de','immowelt.de','xing.com','t-online.de','gmx.de','web.de','wordpress.com','sites.google.com','g.page','goo.gl'}
LEGAL = r'\b(gmbh|mbh|ag|kg|ohg|gbr|ug|e\s?\.?\s?g|e\s?\.?\s?v|eg|ev|co|haftungsbeschränkt|se|inh|inhaber)\b'
_tld = tldextract.TLDExtract(suffix_list_urls=())  # bundled public-suffix snapshot; no network

def log(*a): print(*a, flush=True)

# ---------- normalisation helpers ----------
def name_key(s):
    s = unicodedata.normalize('NFKC', s or '').casefold().replace('&', ' ')
    s = re.sub(r'[^\w\s]', ' ', s); s = re.sub(LEGAL, ' ', s)
    return ' '.join(s.split())

def website(url):
    """Normalise to scheme://host (lowercase); first of ';'-separated values; adds https:// if missing."""
    url = (url or '').split(';')[0].strip()
    if not url: return ''
    if '://' not in url: url = 'https://' + url
    p = urlsplit(url); host = (p.hostname or '').lower().rstrip('.')
    return f'{p.scheme.lower()}://{host}' if host and '.' in host else ''

def registrable(url):
    host = domain(url) if url else ''
    if not host: return ''
    e = _tld(host); return e.top_domain_under_public_suffix or host

def host_key(url):
    h = domain(url) if url else ''
    return '' if not h or h in PLATFORM_HOSTS or registrable(url) in PLATFORM_HOSTS else h

def postcode_of(s):
    m = re.search(r'\b(\d{5})\b', s or ''); return m.group(1) if m else None

def is_berlin_postcode(pc): return bool(pc) and pc.isdigit() and 10115 <= int(pc) <= 14199

def berlin_filter(c):
    """True=keep. Keeps Berlin postcode or Berlin city/address; keeps no-postcode records flagged."""
    pc, city = c.get('postcode'), (c.get('city') or '') + ' ' + (c.get('raw_address') or '')
    if is_berlin_postcode(pc): c['address_precision'] = 'postcode'; return True
    if pc:
        if 'berlin' in city.casefold(): c['address_precision'] = 'postcode_outside_berlin_range_city_berlin'; return True
        return False
    c['address_precision'] = 'city_only' if 'berlin' in city.casefold() else 'none'
    return True

def candidate(**kw):
    base = dict(name='',website='',phone=None,email=None,street=None,postcode=None,city=None,lat=None,lon=None,source='',source_id='',place_types=[],rating=None,user_rating_count=None,matched_terms=[],query_districts=[],raw=None)
    c = base | kw; c['name'] = norm(c['name']); c['website'] = website(c['website'])
    c['needs_website_lookup'] = not c['website']
    return c

def matched(name, terms): return [t for t in terms if t.casefold() in (name or '').casefold()]

# ---------- OSM ----------
def osm_query(terms):
    rx = '|'.join(re.escape(t).replace('\\-', '-').replace('\\ ', ' ') for t in terms)
    return ('[out:json][timeout:180];\narea["de:amtlicher_gemeindeschluessel"="11000000"]->.b;\n'
            f'(nwr["office"="property_management"](area.b);nwr["name"~"{rx}",i](area.b););\nout tags center;')

def fetch_osm(terms=TERMS, cache_dir='runs/discovery/cache', session=None, sleep=time.sleep):
    """One Overpass request per run (cached by query hash). Retries once after 30 s on 429/502/503/504."""
    q = osm_query(terms); cache = Path(cache_dir) / f'osm-{digest(q)[:16]}.json'
    if cache.exists(): log(f'OSM: using cached {cache}'); return json.loads(cache.read_text())
    s = session or requests.Session()
    for attempt in (1, 2):
        r = s.post(OVERPASS, data={'data': q}, headers={'User-Agent': UA}, timeout=240)
        if r.status_code in (429, 502, 503, 504) and attempt == 1: log(f'OSM: HTTP {r.status_code}, retrying in 30 s'); sleep(30); continue
        r.raise_for_status(); break
    data = r.json(); data['_query'] = q; data['_fetched_at'] = now()
    cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(data, ensure_ascii=False))
    return data

def normalise_osm(data, terms=TERMS):
    out = []
    for e in data.get('elements', []):
        t = e.get('tags', {}); name = t.get('name') or t.get('official_name') or t.get('operator')
        if not name: continue
        center = e.get('center') or {}
        street = ' '.join(x for x in (t.get('addr:street'), t.get('addr:housenumber')) if x) or None
        types = [f'{k}={t[k]}' for k in ('office','shop','amenity','craft','building','government') if k in t]
        mt = matched(name, terms) or (['office=property_management'] if t.get('office') == 'property_management' else [])
        out.append(candidate(name=name, website=t.get('website') or t.get('contact:website') or t.get('url'),
            phone=t.get('phone') or t.get('contact:phone'), email=t.get('email') or t.get('contact:email'),
            street=street, postcode=t.get('addr:postcode'), city=t.get('addr:city'), lat=e.get('lat', center.get('lat')),
            lon=e.get('lon', center.get('lon')), source='OSM', source_id=f"{e['type']}/{e['id']}", place_types=types,
            matched_terms=mt, query_districts=[t['addr:suburb']] if t.get('addr:suburb') else [], raw=e,
            raw_address=' '.join(x for x in (street, t.get('addr:postcode'), t.get('addr:city')) if x)))
    return out

# ---------- Google Places ----------
class GooglePlacesError(RuntimeError): pass

def resolve_key(env_file='google_places.env'):
    """Env GOOGLE_MAPS_API_KEY first, else env_file holding `GOOGLE_MAPS_API_KEY=value` or a bare value. Never logged."""
    k = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
    if k or not env_file or not Path(env_file).is_file(): return k or None
    for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            name, _, val = line.partition('=')
            if name.strip().removeprefix('export ').strip() != 'GOOGLE_MAPS_API_KEY': continue
            line = val
        return line.strip().strip('"\'') or None
    return None

def google_queries(terms=TERMS, districts=DISTRICTS): return [(t, d, f'{t} {d} Berlin') for t in terms for d in districts]

def estimate(terms=TERMS, districts=DISTRICTS):
    n = len(google_queries(terms, districts))
    return dict(queries=n, min_requests=n, max_requests=n * MAX_PAGES, max_results=n * MAX_PAGES * 20, sku='Text Search Enterprise (websiteUri/nationalPhoneNumber/rating fields)')

def fetch_google_places(terms=TERMS, api_key=None, cache_dir='runs/discovery/cache/google', districts=DISTRICTS, session=None, env_file='google_places.env', stats=None):
    """Google Places API (New) Text Search, one textQuery per term x Bezirk, <=3 pages x 20.

    SKU/quota assumption: requesting websiteUri / nationalPhoneNumber / rating puts every
    searchText call in the *Text Search Enterprise* SKU. 14 terms x 12 districts = 168 queries,
    168..504 requests (pagination). Check current pricing and the per-SKU monthly free allowance
    before running; every response is cached by (query, page) hash so reruns cost nothing.
    Key: argument, else env GOOGLE_MAPS_API_KEY, else env_file; absent -> skip. Any HTTP error stops the
    run immediately (no retry loop) with GooglePlacesError carrying the redacted response text.
    Returns list of (term, district, page, response_json); `stats` dict gets live/cached counts.
    """
    api_key = api_key or resolve_key(env_file)
    cache = Path(cache_dir); out = []; stats = stats if stats is not None else {}; stats.update(live=0, cached=0)
    s = None
    for term, district, text in google_queries(terms, districts):
        body = {'textQuery': text, 'languageCode': 'de', 'regionCode': 'DE', 'locationRestriction': BERLIN_RECT, 'pageSize': 20}
        token = None
        for page in range(MAX_PAGES):
            f = cache / f'{digest(json.dumps([body, page], sort_keys=True))[:20]}.json'
            if f.exists(): resp = json.loads(f.read_text()); stats['cached'] += 1
            else:
                if not api_key:
                    log('Google Places: no key (GOOGLE_MAPS_API_KEY / env file); skipping live requests (paid API, opt-in only).')
                    return out
                s = s or session or requests.Session()
                r = s.post(PLACES, json=body | ({'pageToken': token} if token else {}), timeout=30,
                           headers={'X-Goog-Api-Key': api_key, 'X-Goog-FieldMask': FIELD_MASK, 'User-Agent': UA})
                stats['live'] += 1
                if r.status_code != 200:
                    raise GooglePlacesError(f'HTTP {r.status_code} on {text!r} page {page}: ' + (r.text or '').replace(api_key, '<redacted>')[:2000])
                resp = r.json()
                f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(resp | {'_query': text, '_page': page}, ensure_ascii=False))
                if resp.get('nextPageToken'): time.sleep(2)  # token needs a moment to become valid
            out.append((term, district, page, resp)); token = resp.get('nextPageToken')
            if not token: break
    log(f"Google Places: {stats['live']} live requests, {stats['cached']} cached responses")
    return out

def normalise_google(pages):
    byid = {}
    for term, district, _, resp in pages:
        for p in resp.get('places', []):
            pid = p.get('id'); c = byid.get(pid)
            if c is None:
                comp = {t: x.get('longText') for x in p.get('addressComponents', []) for t in x.get('types', [])}
                street = ' '.join(x for x in (comp.get('route'), comp.get('street_number')) if x) or None
                loc = p.get('location') or {}
                c = byid[pid] = candidate(name=(p.get('displayName') or {}).get('text', ''), website=p.get('websiteUri'),
                    phone=p.get('nationalPhoneNumber'), street=street, postcode=comp.get('postal_code'),
                    city=comp.get('locality'), lat=loc.get('latitude'), lon=loc.get('longitude'), source='GOOGLE_PLACES',
                    source_id=pid, place_types=p.get('types', []), rating=p.get('rating'), user_rating_count=p.get('userRatingCount'),
                    business_status=p.get('businessStatus'), raw=p, raw_address=p.get('formattedAddress'))
            if term not in c['matched_terms']: c['matched_terms'].append(term)
            if district not in c['query_districts']: c['query_districts'].append(district)
            c.setdefault('hits', 0); c['hits'] += 1
    return list(byid.values())

# ---------- dedup ----------
def existing_companies(db_path):
    if not Path(db_path).exists(): return []
    db = sqlite3.connect(f'file:{Path(db_path).resolve()}?mode=ro', uri=True)
    rows = db.execute('SELECT id,name,url,raw_json FROM companies').fetchall(); db.close()
    return [dict(id=i, name=n, url=u, postcodes={postcode_of(s.get('address')) for s in json.loads(r) if isinstance(s, dict)} - {None}) for i, n, u, r in rows]

def ckey(c): return f"{c['source']}:{c['source_id']}"

def dedup(cands, existing=()):
    """(a) within maps results: website host, else (name_key, postcode); (b) vs existing: registrable
    domain, then name_key + postcode. Shared address alone never merges."""
    cands = sorted(cands, key=lambda c: (not c['website'], c['source'] != 'OSM'))  # richest record represents
    seen = {}
    for c in cands:
        keys = [k for k in (('host', host_key(c['website'])), ('np', name_key(c['name']), c['postcode'])) if k[1]]
        rep = next((seen[k] for k in keys if k in seen), None)
        if rep:
            c['dedup_status'] = f'duplicate_of:{ckey(rep)}'
            for f in ('website', 'phone', 'email'):  # fill gaps on the representative
                if not rep.get(f) and c.get(f): rep[f] = c[f]; rep['needs_website_lookup'] = not rep['website']
            rep.setdefault('also_seen_in', []).append(ckey(c))
            continue
        c['dedup_status'] = 'new'
        for k in keys: seen[k] = c
    by_dom, by_np, by_name = {}, {}, defaultdict(list)
    for e in existing:
        d = registrable(e['url']) if host_key(e['url'] or '') else ''
        if d: by_dom.setdefault(d, e['id'])
        for pc in e['postcodes']: by_np.setdefault((name_key(e['name']), pc), e['id'])
        by_name[name_key(e['name'])].append(e['id'])
    for c in cands:
        if c['dedup_status'] != 'new': continue
        d = registrable(c['website']) if host_key(c['website']) else ''
        hit = by_dom.get(d) if d else None
        hit = hit or by_np.get((name_key(c['name']), c['postcode']))
        if hit: c['dedup_status'] = f'matches_existing:{hit}'
        elif by_name.get(name_key(c['name'])): c['possible_existing_by_name'] = by_name[name_key(c['name'])]
    return cands

def to_company_rows(candidates, statuses=('new',)):
    """(name, url, source_dict) for Store.add_company. url is '' when unknown (add_company needs a str)."""
    rows = []
    for c in candidates:
        if c.get('dedup_status', 'new') not in statuses: continue
        addr = ' '.join(x for x in (c['street'], c['postcode'], c['city']) if x)
        src = dict(source=c['source'], source_id=c['source_id'], address=addr, postcode=c['postcode'], phone=c['phone'],
                   email=c['email'], lat=c['lat'], lon=c['lon'], place_types=c['place_types'], matched_terms=c['matched_terms'],
                   rating=c['rating'], user_rating_count=c['user_rating_count'], address_precision=c.get('address_precision'),
                   needs_website_lookup=c['needs_website_lookup'])
        if c['source'] == 'OSM': src['attribution'] = OSM_ATTRIBUTION
        rows.append((c['name'], c['website'] or '', src))
    return rows

# ---------- run ----------
def summarise(cands, rejected, meta):
    s = dict(meta, by_source={}, by_term={}, by_district={}, by_postcode={})
    for src in sorted({c['source'] for c in cands} | {c['source'] for c in rejected}):
        cs = [c for c in cands if c['source'] == src]; u = [c for c in cs if not c['dedup_status'].startswith('duplicate_of')]
        s['by_source'][src] = dict(hits=sum(c.get('hits', 1) for c in cs), raw_unique_places=len(cs), rejected_non_berlin=sum(c['source'] == src for c in rejected),
            unique=len(u), new=sum(c['dedup_status'] == 'new' for c in u), matches_existing=sum(c['dedup_status'].startswith('matches') for c in u),
            with_website=sum(bool(c['website']) for c in u), with_phone=sum(bool(c['phone']) for c in u),
            needs_website_lookup=sum(c['needs_website_lookup'] for c in u), no_postcode=sum(not c['postcode'] for c in u))
    def group(field, key):
        g = defaultdict(lambda: Counter())
        for c in cands:
            for k in key(c) or ['(none)']:
                x = g[k]; x['hits'] += 1
                if not c['dedup_status'].startswith('duplicate_of'):
                    x['unique'] += 1; x['new' if c['dedup_status'] == 'new' else 'existing'] += 1
                    x['with_website'] += bool(c['website']); x['with_phone'] += bool(c['phone'])
        s[field] = {k: dict(v) for k, v in sorted(g.items())}
    group('by_term', lambda c: c['matched_terms']); group('by_district', lambda c: c['query_districts']); group('by_postcode', lambda c: [c['postcode']] if c['postcode'] else [])
    tot = [c for c in cands if not c['dedup_status'].startswith('duplicate_of')]
    s['total'] = dict(candidates=len(cands), unique=len(tot), new=sum(c['dedup_status'] == 'new' for c in tot),
                      matches_existing=len(tot) - sum(c['dedup_status'] == 'new' for c in tot), with_website=sum(bool(c['website']) for c in tot))
    return s

def run(source='osm', out='runs/discovery', db='data/leads.sqlite', terms=TERMS, districts=DISTRICTS, env_file='google_places.env'):
    out = Path(out); out.mkdir(parents=True, exist_ok=True); cands = []; meta = dict(generated_at=now(), sources=[], attribution={})
    if source in ('osm', 'both'):
        data = fetch_osm(terms, out / 'cache'); cands += normalise_osm(data, terms)
        meta['sources'].append('OSM'); meta['attribution']['OSM'] = OSM_ATTRIBUTION
        meta['osm'] = dict(timestamp_osm_base=data.get('osm3s', {}).get('timestamp_osm_base'), elements=len(data.get('elements', [])), query=data.get('_query'))
    if source in ('google', 'both'):
        st, err = {}, None
        try: pages = fetch_google_places(terms, None, out / 'cache/google', districts, env_file=env_file, stats=st)
        except GooglePlacesError as e: err = str(e); log(f'Google Places stopped: {err}'); return [], {'google_error': err, **st}
        if pages: cands += normalise_google(pages); meta['sources'].append('GOOGLE_PLACES'); meta['attribution']['GOOGLE_PLACES'] = 'Google Maps Platform; subject to Google Maps Platform Terms (caching limits apply)'
        meta['google'] = dict(estimate(terms, districts), responses=len(pages), live_requests=st.get('live', 0), cached_responses=st.get('cached', 0), skipped=not pages)
        if source == 'google' and not pages: log('No Google results; outputs left untouched.'); return [], {}
    kept, rejected = [], []
    for c in cands: (kept if berlin_filter(c) else rejected).append(c)
    existing = existing_companies(db); meta['existing_companies'] = len(existing)
    kept = dedup(kept, existing)
    summary = summarise(kept, rejected, meta)
    (out / 'maps-candidates.json').write_text(json.dumps(dict(meta, candidates=kept, rejected_non_berlin=rejected), ensure_ascii=False, indent=1))
    (out / 'maps-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return kept, summary

def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m heidi_leads.discover_maps')
    ap.add_argument('--source', choices=['osm', 'google', 'both'], default='osm')
    ap.add_argument('--estimate', action='store_true', help='print Google request count and exit')
    ap.add_argument('--out', default='runs/discovery'); ap.add_argument('--db', default='data/leads.sqlite')
    ap.add_argument('--env-file', default='google_places.env', help='file holding the Google key (bare or GOOGLE_MAPS_API_KEY=...)')
    a = ap.parse_args(argv)
    if a.estimate:
        e = estimate(); log(f"Google Places estimate: {len(TERMS)} terms x {len(DISTRICTS)} districts = {e['queries']} queries; "
                            f"{e['min_requests']}-{e['max_requests']} requests (<= {MAX_PAGES} pages x 20); SKU: {e['sku']}")
        log('Google key found (env or env file).' if resolve_key(a.env_file) else 'No Google key (GOOGLE_MAPS_API_KEY / env file): a live Google run would be skipped.')
        if a.source == 'osm': log('(OSM is free; the estimate applies to Google only)')
        return 0
    _, s = run(a.source, a.out, a.db, env_file=a.env_file)
    if not s or 'google_error' in s: return 1 if s else 0
    log(json.dumps(dict(by_source=s['by_source'], total=s['total']), indent=2))
    return 0

if __name__ == '__main__': raise SystemExit(main())
