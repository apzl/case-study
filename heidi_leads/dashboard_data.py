"""Dashboard data for the static dashboard, which lives in its own repository
(case-study-dashboard, expected next to this repository; override with --dashboard-dir or the
HEIDI_DASHBOARD_DIR environment variable).

    python -m heidi_leads.dashboard_data            # full data -> output/dashboard/data.js (local only)
    python -m heidi_leads.dashboard_data --public   # redact it -> <dashboard repo>/data.js (safe to host)

The full build reads the exported CSVs and the database (no network or model calls). It
carries named contacts, so it stays local: output/ is git-ignored. If the dashboard repository
is found, its pages are copied next to the full data so output/dashboard/index.html opens the
complete version.
The public build removes everything that names or contacts a person (see redact())."""
import argparse, csv, json, os, re, shutil, sqlite3, datetime
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'output'
PAGES = Path(os.environ.get('HEIDI_DASHBOARD_DIR', ROOT.parent / 'case-study-dashboard'))  # the dashboard repository
FULL_DATA = OUT / 'dashboard' / 'data.js'


def rows(name):
    with (OUT / name).open(newline='') as f:
        return list(csv.DictReader(f))


def num(v):
    try: return int(v)
    except (TypeError, ValueError): return None


def units_number(text):
    m = re.search(r'([\d.,]+)\s*units', text or '')
    return int(re.sub(r'[.,]', '', m.group(1))) if m else None


def size_band(n):
    if n is None: return 'Not published'
    return '1,000+ (key account)' if n >= 1000 else '100-999' if n >= 100 else 'Under 100'


FACT_LABEL = {'management_type': 'What they manage', 'units': 'Apartments', 'metering': 'Meters and billing',
    'metering_provider': 'Metering company', 'software': 'Software', 'hkvo_2027': 'Meter reading (2027 rule)',
    'metering_contract': 'Metering contract', 'contact': 'Contact person'}


def build():
    slim, details = rows('companies.csv'), {r['Company ID']: r for r in rows('details.csv')}
    from .core import Store, channels, accepted, contact_tier, personal_email, EXCLUDED_ROLE
    store = Store(ROOT)
    evidence = {}
    for e in rows('evidence.csv'):  # every accepted fact with its quote and source page
        if e['Fact'] in ('berlin_office', 'berlin_service', 'berlin_property'): label = 'Berlin presence'
        elif e['Fact'] in FACT_LABEL: label = FACT_LABEL[e['Fact']]
        else: continue  # retired fact types
        evidence.setdefault(e['Company ID'], []).append({'fact': label, 'value': e['Value'], 'number': e['Number'],
            'person': e['Person'], 'phone': e['Phone'], 'quote': e['Quote (from the page)'], 'source': e['Source page']})
    db = sqlite3.connect(ROOT / 'data' / 'leads.sqlite')
    states = dict(db.execute('SELECT state,count(*) FROM companies GROUP BY state').fetchall())
    companies, sources = [], Counter()
    for r in slim:
        if r['Tier'] not in ('A', 'B', 'C'): continue  # tier D (brokers, competitors, no management) is left out
        d = details.get(r['Company ID'], {})
        found = [s.strip() for s in (d.get('Found via') or '').replace(';', ',').split(',') if s.strip()]
        sources.update(set(found))
        companies.append({
            'id': r['Company ID'], 'tier': r['Tier'], 'name': r['Company'], 'segment': r['Segment'],
            'why': r['Why'], 'first_step': r['Suggested first step'],
            'fit': num(r['Fit score']), 'precheck': num(r['First-look score']),
            'access_score': num(r['Access score']), 'access': r['Access'],
            'fit_breakdown': d.get('Fit breakdown', ''),
            'contact_person': r['Contact person'], 'contact_role': r['Contact role'],
            'contact_phone': r['Contact phone'], 'contact_email': r['Contact email'], 'contact_linkedin': r.get('Contact LinkedIn', ''),
            'all_contacts': d.get('All named contacts', ''),
            'phone': r['Office phone'], 'email': r['General email'], 'contact_page': r['Contact page'],
            'linkedin': r['Company LinkedIn'], 'website': r['Website'], 'district': r['District'],
            'address': d.get('Address', ''), 'units': r['Apartments'],
            'manages': d.get('Manages', ''), 'signals': d.get('Meters, 2027 and contract signals', ''),
            'metering_company': d.get('Metering company', ''), 'software': d.get('Software', ''),
            'vdiv': r['VDIV member'] == 'yes', 'manager_check': d.get('Manager check', ''),
            'researched': r['Fit score'] != '', 'sources': found,
        })
        c, fb = companies[-1], d.get('Fit breakdown', '')
        co_row = store.rows('SELECT * FROM companies WHERE id=?', (c['id'],))
        ch = channels(store, co_row[0]) if co_row else {'linkedin_people': ''}
        c['evidence'] = evidence.get(c['id'], [])
        # Named people, apartment managers first. relevant = manages properties or decides
        # (owner/director/board), not HR/job-ad recruiters; the page shows only relevant ones.
        people = {}
        for x in accepted(store, c['id']):
            if x['field'] != 'contact' or not x.get('person') or EXCLUDED_ROLE.search(x.get('role') or ''): continue
            t = contact_tier(x) or 3
            p = people.setdefault(x['person'].casefold(), dict(name=x['person'], role=x.get('role') or '', level=t, phone='', email='', linkedin='', source='', quote=''))
            p['level'] = min(p['level'], t); p['role'] = p['role'] or x.get('role') or ''
            p['phone'] = p['phone'] or x.get('phone') or ''
            p['email'] = p['email'] or (x['email'] if personal_email(x) else '')
            p['linkedin'] = p['linkedin'] or x.get('profile_url') or ''
            if not p['source']: p['source'], p['quote'] = next(((d['url'], x['quote']) for d in store.rows('SELECT url FROM documents WHERE id=?', (x['document_id'],))), ('', x['quote']))
        for p in people.values():
            p['relevant'] = p['level'] <= 2 and 'job ad' not in p['role'].lower()
            p['level'] = {1: 'Property manager', 2: 'Owner/director', 3: 'Other staff'}[p['level']]
        c['contacts'] = sorted(people.values(), key=lambda p: (['Property manager', 'Owner/director', 'Other staff'].index(p['level']), not (p['phone'] or p['email']), not p['linkedin'], p['name']))
        # Hiring signal from public LinkedIn job ads (job_ads step): title, date, link.
        c['hiring'] = [dict(title=json.loads(d['meta_json']).get('title', ''), date=json.loads(d['meta_json']).get('date', ''), url=d['url'])
                       for d in store.rows("SELECT url,meta_json FROM documents WHERE company_id=? AND method='job_ad'", (c['id'],))]
        # Map position from the directory/map record (Google Places / OSM); None when unknown.
        pos = next(((r.get('lat'), r.get('lon')) for r in json.loads(co_row[0]['raw_json'] or '[]') if r.get('lat') and r.get('lon')), (None, None)) if co_row else (None, None)
        c['lat'], c['lon'] = (round(float(pos[0]), 5), round(float(pos[1]), 5)) if pos[0] else (None, None)
        c['linkedin_profiles'] = ch['linkedin_people'].split() if ch['linkedin_people'] else []
        n = units_number(c['units'])
        signals = (c['signals'] or '').lower()
        # The actual inputs behind the fit score, as values a salesperson can read.
        c.update({
            'units_n': n, 'units_berlin': 'Berlin share unknown' not in c['units'] and bool(c['units']),
            'units_approx': (c['units'] or '').startswith(('approx', 'over')), 'size_band': size_band(n),
            'manages_apartments': 'Manages apartments' in fb,
            'not_remote_readable': 'Meters read by hand' in fb,
            'switch_ready': 'Provider change announced' in fb,
            'provider_change_signal': '; '.join(t for k, t in (('contract end known', 'Metering contract end date published'),
                ('plans to change metering provider', 'Says it plans to change metering provider'), ('metering tender', 'Tender for metering services published')) if k in signals),
            'billing_setup': ('Outside provider: ' + c['metering_company']) if c['metering_company']
                else 'Outside metering provider' if 'outside metering provider' in signals
                else 'Bills heating costs in-house' if 'in-house' in signals else '',
            'has_named_contact': bool(c['contact_person']),
            'has_direct_line': bool(c['contact_person'] and (c['contact_phone'] or c['contact_email'])),
        })
    leads = [c for c in companies if c['tier'] in ('A', 'B', 'C')]
    count = lambda pred, rows=companies: sum(1 for c in rows if pred(c))
    stats = {
        'totals': {
            'companies': len(companies), 'priority_leads_AB': count(lambda c: c['tier'] in ('A', 'B')),
            'tier_A': count(lambda c: c['tier'] == 'A'),
            'named_contact': count(lambda c: c['has_named_contact']),
            'named_contact_direct_line': count(lambda c: c['has_direct_line']),
            'property_manager_contact': count(lambda c: c['access'].startswith('Property manager')),
            'contact_on_linkedin': count(lambda c: bool(c['contact_linkedin'])),
            'apartments_in_berlin_published': sum(c['units_n'] or 0 for c in companies if c['units_berlin']),
            'key_accounts_1000plus': count(lambda c: (c['units_n'] or 0) >= 1000),
            'not_remote_readable_2027': count(lambda c: c['not_remote_readable']),
            'switch_ready': count(lambda c: c['switch_ready']),
            'billing_setup_known': count(lambda c: bool(c['billing_setup'])),
            'vdiv_members': count(lambda c: c['vdiv']),
            'hiring_property_staff': count(lambda c: bool(c['hiring'])),
            'contact_on_linkedin_any': count(lambda c: any(p['linkedin'] for p in c['contacts']) or bool(c['contact_linkedin'])),
            'relevant_people': sum(sum(p['relevant'] for p in c['contacts']) for c in companies),
            'facts_with_evidence': sum(len(c['evidence']) for c in companies),
            'info_only': count(lambda c: (c['access'] or '').startswith('Main office')),
        },
        'tiers': dict(sorted(Counter(c['tier'] for c in companies).items())),
        'reachability': dict(Counter((c['access'] or 'No contact found').replace(' + LinkedIn', '') for c in leads).most_common()),
        'segments': dict(Counter(c['segment'] or 'Not stated' for c in leads).most_common()),
        'size_bands': dict(Counter(c['size_band'] for c in leads).most_common()),
        'billing_setup': dict(Counter(('Outside provider' if c['billing_setup'].startswith('Outside') else c['billing_setup'] or 'Not stated') for c in leads).most_common()),
        'districts': dict(Counter(c['district'] or 'Unknown' for c in leads).most_common()),
        # Heidi's three customer segments; a company can belong to more than one.
        'segment_contacts': {seg: {'companies': count(lambda c: seg in (c['segment'] or '')),
                                   'named_contact': count(lambda c: seg in (c['segment'] or '') and c['has_named_contact']),
                                   'direct_line': count(lambda c: seg in (c['segment'] or '') and c['has_direct_line'])}
                             for seg in ('WEG manager', 'Rental manager', 'Housing company')},
        'note': 'Breakdowns (reachability, segments, size, billing, districts) cover tiers A-C; tier D is low priority.',
    }
    data = {'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), 'stats': stats, 'companies': companies}
    write_js(FULL_DATA, data)
    if PAGES.is_dir():
        for page in PAGES.glob('*.html'): shutil.copy2(page, FULL_DATA.parent / page.name)
    return {'companies': len(companies), 'path': str(FULL_DATA)}


# ---------------------------------------------------------------------------
# Public (redacted) data: companies, tiers, scores, business phone, shared inbox, website,
# district, map position and facts stay; people, their roles, direct lines, personal
# e-mails, LinkedIn profiles and any quote naming or contacting a person are removed.

GENERIC_INBOXES = {'info', 'kontakt', 'contact', 'mail', 'email', 'e-mail', 'service', 'office', 'hausverwaltung',
    'verwaltung', 'post', 'berlin', 'buero', 'büro', 'vermietung', 'hv', 'kundenservice', 'zentrale', 'anfrage',
    'anfragen', 'team', 'management', 'property', 'teamassistenz', 'vergabe', 'empfang', 'sekretariat', 'hello',
    'hallo', 'mieterservice', 'kundencenter', 'immobilien', 'wohnen', 'weg', 'mietverwaltung', 'weg-verwaltung',
    'objektverwaltung', 'immobilienverwaltung', 'verwaltungen', 'buchhaltung', 'technik', 'notdienst', 'kunden', 'welcome'}
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}', re.I)
PROFILE_RE = re.compile(r'linkedin\.com/in/', re.I)
# Role words followed by a capitalised name, e.g. "Geschäftsführer: Max Muster", "Inh. Anna Beispiel".
NAMED_ROLE = re.compile(r'(?i:\b(?:gesch(?:ä|ae)ftsf(?:ü|ue)hr\w*|inhaber\w*|inh\.|vorstand\w*|prokurist\w*|vertreten\s+durch|'
                        r'vertretungsberechtigt\w*|ansprechpartner\w*|ansprechperson|kontaktperson|herrn?|frau|dipl\.[\w.-]*|dr\.))'
                        r'[\s:,\-–]*[A-ZÄÖÜ][a-zäöüß]+')
PERSON_FIELDS = ('contact_person', 'contact_role', 'contact_phone', 'contact_email', 'contact_linkedin', 'all_contacts')


def generic_email(e):
    return bool(e) and e.split('@')[0].casefold() in GENERIC_INBOXES


def personal_trace(text, names=()):
    """True when text carries a personal e-mail, a LinkedIn profile, a known person's name
    or a role word followed by a name."""
    t = str(text or '')
    if any(not generic_email(e) for e in EMAIL_RE.findall(t)) or PROFILE_RE.search(t): return True
    folded = t.casefold()
    return bool(NAMED_ROLE.search(t)) or any(n in folded for n in names)


def known_names(c):
    names = {c.get('contact_person') or ''} | {p.get('name') or '' for p in c.get('contacts') or []}
    names |= {e.get('person') or '' for e in c.get('evidence') or []}
    names |= {re.sub(r'\s*\(.*', '', x).strip() for x in (c.get('all_contacts') or '').split(';')}
    out = set()
    for n in names:
        n = ' '.join(n.split()).casefold()
        if len(n) >= 5: out.add(n)
        parts = [w for w in re.findall(r'[\wäöüß-]+', n) if len(w) >= 4]
        if len(parts) >= 2: out.add(parts[-1])  # surname alone
    return out


def redact(data):
    """Copy of the dashboard data without personal data; counts and stats are unchanged."""
    data = json.loads(json.dumps(data))
    for c in data['companies']:
        names = known_names(c)
        # The opener starts "Ask for <person> (<role>)." when a person is known (core.sales_opener).
        step, person, role = c.get('first_step') or '', c.get('contact_person') or '', c.get('contact_role') or ''
        ask = f'Ask for {person}' + (f' ({role})' if role else '') + '.'
        if person and step.startswith(ask): step = 'Ask for the named contact (withheld in this public version).' + step[len(ask):]
        c['first_step'] = '' if personal_trace(step, names) else step
        for f in PERSON_FIELDS: c[f] = ''
        c['contacts'] = []; c['linkedin_profiles'] = []
        if not generic_email(c.get('email')): c['email'] = ''
        evidence = []
        for e in c.get('evidence') or []:
            if e.get('fact') == 'Contact person': continue
            e['person'] = ''; e['phone'] = ''
            if personal_trace(e.get('quote'), names): e['quote'] = ''   # keep the fact and its source page
            if personal_trace(e.get('source')): e['source'] = ''
            evidence.append(e)
        c['evidence'] = evidence
        for k, v in c.items():  # last safety net over the remaining text fields
            if isinstance(v, str) and k not in ('name', 'id') and (PROFILE_RE.search(v) or any(not generic_email(x) for x in EMAIL_RE.findall(v))):
                c[k] = ''
    data['redacted'] = True
    return data


def read_js(path):
    text = Path(path).read_text()
    return json.loads(text[text.index('=') + 1:].rstrip().rstrip(';'))


def write_js(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text('window.HEIDI_DATA = ' + json.dumps(data, ensure_ascii=False) + ';\n')


def main():
    ap = argparse.ArgumentParser(description='Build dashboard data (full, local) or its redacted public copy.')
    ap.add_argument('--public', action='store_true', help='redact output/dashboard/data.js into the dashboard repository')
    ap.add_argument('--dashboard-dir', default=str(PAGES), help='path of the dashboard repository (default: ../case-study-dashboard)')
    a = ap.parse_args()
    if a.public:
        target = Path(a.dashboard_dir) / 'data.js'
        target.parent.mkdir(parents=True, exist_ok=True)
        data = redact(read_js(FULL_DATA)); write_js(target, data)
        print({'companies': len(data['companies']), 'path': str(target)})
    else:
        print(build())


if __name__ == '__main__':
    main()
