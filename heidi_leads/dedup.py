"""Global de-duplication across all discovery sources; run after discovery, before collection/extraction.

Two companies match on (rule order = reported rule):
- domain: same registrable website domain (platform/shared hosts ignored),
- phone: same phone digits (>= 8 digits, +49/0049 normalised to 0),
- name: same normalised clean name (legal forms stripped) and a shared postcode, or one side without postcode.
Matches are transitive (union-find). Survivor per cluster: furthest state (reviewed > extracted > collected),
then has website, then most source records. Duplicates' raw records are appended to the survivor (each tagged
with its original name and merged_from id); duplicates get state='merged', error='duplicate_of:<id>'. Rows and
claims are never deleted or moved. Names of active companies are cleaned with clean_name().

`python -m heidi_leads.dedup [--dry-run]` -> counts by rule; runs/discovery/dedup-report.json (dry run: dedup-report.dry-run.json;
no emails or person fields).
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from heidi_leads.core import Store, digits, now
from heidi_leads.discover_maps import host_key, name_key, postcode_of, registrable

# Legal-form tokens (case-sensitive so words like 'Tag' or 'weg' never match); optional '(haftungsbeschränkt)'.
LEGAL_FORM = re.compile(r'(?<![\w.])(GmbH|mbH|AG|KG|OHG|eG|e\.\s?G\.|e\.\s?K\.|UG|GbR|SE)(?![\w])(\s*\(haftungsbeschränkt\))?')
SEPARATORS = re.compile(r'\s+[|–—-]\s+|\s*\|\s*')
GENERIC = set('''hausverwaltung immobilienverwaltung verwaltung weg-verwaltung mietverwaltung immobilien immobilienmanagement
property management facility service services gebäudereinigung berlin in und für fuer der die das ihr ihre ihren partner ihrem
mitte nord süd ost west kundencenter kundenzentrum büro buero geschäftsstelle geschaeftsstelle niederlassung zentrale
standort filiale office mitgliedertreffen vermietung vermietungsbüro verkauf makler & + - wohnungsverwaltung
grundstücksverwaltung objektverwaltung hausmeisterservice hausmeister sev weg verwalter immobilienmakler'''.split())
GENERIC |= {d.casefold() for d in ('Charlottenburg','Wilmersdorf','Friedrichshain','Kreuzberg','Pankow','Spandau','Steglitz','Zehlendorf',
    'Tempelhof','Schöneberg','Neukölln','Treptow','Köpenick','Marzahn','Hellersdorf','Lichtenberg','Reinickendorf','Prenzlauer','Berg','Moabit','Wedding')}
STATE_RANK = {'reviewed': 3, 'extracted': 2, 'collected': 1}

def generic(s): return all(w.strip('.,()/:') in GENERIC or not w.strip('.,()/:') for w in s.casefold().split())

def tail_generic(t):
    """A cuttable tail: has a word, every word generic, and is not a continuation ('- und Immobilien GmbH')."""
    return bool(re.search(r'\w', t)) and generic(t) and t.split()[0].casefold() not in ('und', '&', '+')

def clean_name(name):
    """Drop a generic tail after ' | ' / ' - ' separators or after the last legal form (GmbH, eG, ...)."""
    s = ' '.join((name or '').split())
    while (m := list(SEPARATORS.finditer(s))) and tail_generic(s[m[-1].end():]): s = s[:m[-1].start()]
    m = list(LEGAL_FORM.finditer(s))
    if m and m[-1].start() > 0 and tail_generic(s[m[-1].end():]): s = s[:m[-1].end()]
    return s.strip(' ,;-|') or ' '.join((name or '').split())

def phone_key(p):
    d = digits(p)
    if d.startswith('0049'): d = '0' + d[4:]
    elif d.startswith('49') and len(d) > 10: d = '0' + d[2:]
    return d if len(d) >= 8 else ''

def nkey(name):
    k = name_key(clean_name(name)); k = re.sub(r'\b(e\s?k|mbh)\b', ' ', k)
    k = ' '.join(k.split())
    return '' if not k or generic(k) else k

def keys(c):
    """c: dict(name, url, raw=list of source records). Returns domain, phones, name key, postcodes."""
    recs = [r for r in c['raw'] if isinstance(r, dict)]
    dom = registrable(c['url']) if c.get('url') and host_key(c['url']) else ''
    phones = {phone_key(r.get('phone')) for r in recs} - {''}
    pcs = {r.get('postcode') or postcode_of(r.get('address')) for r in recs} - {None, ''}
    return dom, phones, nkey(c['name']), pcs

def match(companies):
    """Edges (a_id, b_id, rule) between companies; bucketed so it stays linear-ish."""
    edges, by = [], defaultdict(lambda: defaultdict(list))
    for c in companies:
        dom, phones, nk, pcs = c['_keys'] = keys(c)
        if dom: by['domain'][dom].append(c['id'])
        for p in phones: by['phone'][p].append(c['id'])
        if nk: by['name'][nk].append(c['id'])
    pcs = {c['id']: c['_keys'][3] for c in companies}
    for rule in ('domain', 'phone', 'name'):
        for ids in by[rule].values():
            ids = list(dict.fromkeys(ids))
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    if rule == 'name' and pcs[a] and pcs[b] and not pcs[a] & pcs[b]: continue
                    edges.append((a, b, rule))
    return edges

DIRECTORY_SOURCES = ('LANDESEIGENE', 'BBU', 'WBGD', 'BVI', 'VDIV')  # official names, best first

def display_name(s, members):
    """Survivor keeps its name if it comes from a directory; else the best directory name in the cluster;
    else its own name unless generic (e.g. Google 'Geschäftsstelle'), then the longest member name."""
    srcs = lambda c: {r.get('source') for r in c['raw'] if isinstance(r, dict)}
    if srcs(s) & set(DIRECTORY_SOURCES): return s['name']
    for src in DIRECTORY_SOURCES:
        for c in members:
            if src in srcs(c): return c['name']
    return s['name'] if nkey(s['name']) else max(members, key=lambda c: len(nkey(c['name'])))['name']

def survivor_key(c): return (STATE_RANK.get(c['state'], 0), bool(c.get('url')), len(c['raw']), c['id'])

def clusters(companies, edges):
    """Union-find; returns list of (survivor, [(duplicate, rule_by_which_reached)])."""
    parent = {c['id']: c['id'] for c in companies}
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    adj = defaultdict(list)
    for a, b, r in edges: parent[find(a)] = find(b); adj[a].append((b, r)); adj[b].append((a, r))
    groups, byid = defaultdict(list), {c['id']: c for c in companies}
    for c in companies: groups[find(c['id'])].append(c)
    out = []
    for g in groups.values():
        if len(g) < 2: continue
        s = max(g, key=survivor_key); seen, queue, dups = {s['id']}, [s['id']], []
        while queue:  # BFS from survivor: rule = edge through which each member was reached
            x = queue.pop(0)
            for y, r in adj[x]:
                if y not in seen: seen.add(y); queue.append(y); dups.append((byid[y], r))
        s['new_name'] = display_name(s, [d for d, _ in dups])
        out.append((s, dups))
    return out

def plan(companies):
    return clusters(companies, match(companies))

def run(root='.', dry_run=False, report='runs/discovery/dedup-report.json'):
    store = Store(root)
    rows = store.rows("SELECT id,name,url,state,raw_json FROM companies WHERE state!='merged'")
    companies = [dict(r, raw=json.loads(r['raw_json'])) for r in rows]
    groups = plan(companies)
    by_rule = Counter(r for _, d in groups for _, r in d)
    for s, _ in groups: s['name_before'] = s['name']; s['name'] = s.pop('new_name')
    renamed = [(c['id'], c.get('name_before', c['name']), clean_name(c['name'])) for c in companies
               if clean_name(c['name']) != c.get('name_before', c['name'])]
    rep = dict(generated_at=now(), dry_run=dry_run, companies_considered=len(companies), clusters=len(groups),
               duplicates=sum(len(d) for _, d in groups), by_rule=dict(by_rule),
               reviewed_duplicates=sum(d['state'] == 'reviewed' for _, ds in groups for d, _ in ds),
               largest_clusters=sorted([dict(survivor=s['name'], size=len(d) + 1) for s, d in groups], key=lambda x: -x['size'])[:15],
               renamed=[dict(id=i, old=o, new=n) for i, o, n in renamed],
               pairs=[dict(survivor_id=s['id'], survivor=s['name'], survivor_state=s['state'], duplicate_id=d['id'], duplicate=d['name'],
                           duplicate_state=d['state'], rule=r) for s, ds in groups for d, r in ds])
    if dry_run: report = Path(report).with_suffix('.dry-run.json')  # never overwrite the record of the real merge
    Path(report).parent.mkdir(parents=True, exist_ok=True); Path(report).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    if dry_run: return rep
    db = store.db
    with db:
        for s, ds in groups:
            raw = [dict(r, name=r.get('name', s['name_before'])) if isinstance(r, dict) else r for r in s['raw']]
            url = s['url']
            for d, _ in ds:
                raw += [dict(r, name=r.get('name', d['name']), merged_from=d['id']) if isinstance(r, dict) else r for r in d['raw']]
                url = url or d['url']
                db.execute("UPDATE companies SET state='merged',error=? WHERE id=?", (f"duplicate_of:{s['id']}", d['id']))
            db.execute('UPDATE companies SET raw_json=?,url=? WHERE id=?', (json.dumps(raw, ensure_ascii=False), url or '', s['id']))
        merged = {d['id'] for _, ds in groups for d, _ in ds}
        for i, old, new in renamed:
            if i in merged: continue
            c = store.company(i); raw = [dict(r, name=r.get('name', old)) if isinstance(r, dict) else r for r in json.loads(c['raw_json'])]
            db.execute('UPDATE companies SET name=?,raw_json=? WHERE id=?', (new, json.dumps(raw, ensure_ascii=False), i))
    return rep

def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m heidi_leads.dedup')
    ap.add_argument('--dry-run', action='store_true'); ap.add_argument('--root', default='.')
    a = ap.parse_args(argv)
    r = run(a.root, a.dry_run)
    print(json.dumps({k: r[k] for k in ('dry_run', 'companies_considered', 'clusters', 'duplicates', 'by_rule', 'reviewed_duplicates')}, indent=2))
    print(f"renamed: {len(r['renamed'])}; largest clusters: {r['largest_clusters'][:5]}")
    return 0

if __name__ == '__main__': raise SystemExit(main())
