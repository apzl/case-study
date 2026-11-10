"""Contact step: find the people who manage a lead's buildings, so sales can reach a person
instead of info@. One Claude call per company (Claude Code login, no API key) with two searches:

  1. LinkedIn (web search limited to linkedin.com): current employees, property managers first.
  2. The open web: the company's team / Ansprechpartner page, opened to read personal phone
     numbers and e-mail addresses.

Nothing is invented: a person, profile link, phone or e-mail is kept only if it appears in
what the searches and pages actually returned, and a LinkedIn hit must name the company.
Each kept person becomes a normal contact fact (quote = search result title or page text),
so scoring, workbook and dashboard use it like any other contact.

    python -m heidi_leads.find_contacts --tiers AB [--limit N] [--workers 4]
"""
import argparse, csv, hashlib, json, os, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .core import Store, Claim, VERSION, digits, hard_error, ident, norm, quote_found, accepted, contact_tier, LINKEDIN_IN

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / 'runs' / 'contacts'          # git-ignored: names and phone numbers
MODEL = 'claude-haiku-5-5'
PROMPT_VERSION = 2
PROFILE = re.compile(r'https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+', re.I)
EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
GENERIC = {'gmbh', 'mbh', 'co', 'kg', 'ag', 'eg', 'ug', 'berlin', 'hausverwaltung', 'immobilien', 'verwaltung', 'verwaltungs',
           'immobilienverwaltung', 'gesellschaft', 'management', 'property', 'real', 'estate', 'und', 'the', 'facility',
           'grundstücksverwaltung', 'wohnungsbaugesellschaft', 'wohnungsgenossenschaft', 'service', 'services', 'haftungsbeschränkt'}

SYSTEM = ('You find named contact people at one Berlin property-management company for a sales team. '
          'Use only the tools. Report only people, links, phones and e-mails that appear in tool results. '
          'Answer with JSON only.')
PROMPT = '''Company: {name}{site}
Goal: personal e-mail addresses (not info@/office@), direct phone lines and LinkedIn profiles of the people
who personally manage this company's apartments
(Objektbetreuer, Hausverwalter, WEG-Verwalter, Immobilienverwalter, Property Manager, Kundenbetreuer,
Bestandsmanager, Mietverwaltung, Teamleiter Verwaltung), then their heads (Leiter Verwaltung/Bestand), then
Geschäftsführer/Inhaber/Vorstand. Even in a big firm, the individual property manager matters most.
{known}
Steps (max {calls} tool calls in total):
1. For each known person above (managers first), WebSearch with allowed_domains=["linkedin.com"], query: "<person name>" {short}
2. WebSearch with allowed_domains=["linkedin.com"], query: "{short}" Objektbetreuer OR Hausverwalter OR Immobilienverwalter OR Property Manager OR WEG-Verwalter
3. WebSearch with allowed_domains=["linkedin.com"], query: "{short}"
4. If calls remain: WebSearch "{short}" Berlin Ansprechpartner OR Team OR Objektbetreuer E-Mail, and WebFetch the company's
   own team/Ansprechpartner page if shown: copy each person's personal e-mail and direct phone exactly.
Keep linkedin.com/in/ profiles whose result title names this company (current job). Role: copy the job title exactly
as shown in the result title or page; null if none is shown (do not describe).
Return JSON: {{"people":[{{"name":"...","role":"...","linkedin_url":null,"phone":null,"email":null,
"source_url":"page or profile URL","quote":"exact result title or page line naming the person"}}]}}
Max {max_people} people, apartment managers first. {{"people":[]}} if none.'''


def short_name(name):
    s = re.sub(r'\b(GmbH|mbH|AG|KG|eG|e\.G\.|UG|Co\.|haftungsbeschränkt)\b|[&()]', ' ', name)
    return re.sub(r'\s+', ' ', s).strip(' ,.-') or name


def tokens(name):
    return {t for t in re.findall(r'[a-zäöüß0-9]{4,}', name.casefold()) if t not in GENERIC}


def run_sdk(prompt, max_turns=8):
    import anyio
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, UserMessage
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'): os.environ.pop(key, None)
    options = ClaudeAgentOptions(model=MODEL, system_prompt=SYSTEM, allowed_tools=['WebSearch', 'WebFetch'], max_turns=max_turns)

    async def go():
        result, seen = None, []
        async for m in query(prompt=prompt, options=options):
            if isinstance(m, ResultMessage): result = m
            elif isinstance(m, UserMessage):  # tool results = what the searches really returned
                for b in m.content if isinstance(m.content, list) else []:
                    c = getattr(b, 'content', None)
                    seen.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str))
        return result, '\n'.join(seen)
    return anyio.run(go)


def norm_profile(url):
    m = PROFILE.search(url or '')
    return re.sub(r'https?://(?:[a-z]{2,3}\.)?', 'https://www.', m.group(0).rstrip('/')) if m else ''


def titles(seen):
    """url -> result title from the search tool's link lists."""
    out = {}
    for m in re.finditer(r'"title":\s*"((?:[^"\\]|\\.)*)",\s*"url":\s*"([^"]+)"', seen):
        out[m.group(2).rstrip('/')] = json.loads('"' + m.group(1) + '"')
    return out


def verify(company, people, seen):
    """Keep only what the tools returned. Returns (kept, dropped)."""
    seen_cf, seen_digits = seen.casefold(), digits(seen)
    links = {norm_profile(u).casefold(): t for u, t in titles(seen).items() if PROFILE.search(u)}
    want = tokens(company['name'])
    kept, dropped = [], []
    for p in people if isinstance(people, list) else []:
        if not isinstance(p, dict): continue
        name = norm(str(p.get('name') or ''))[:120]
        why = None
        if len(name) < 4 or name.casefold() not in seen_cf: why = 'name_not_in_results'
        url = norm_profile(p.get('linkedin_url'))
        title = links.get(url.casefold(), '') if url else ''
        if url and not title: why = why or 'profile_not_in_results'
        if url and want and not (want & tokens(title + ' ' + str(p.get('quote') or ''))): why = why or 'profile_other_company'
        phone = p.get('phone') or None
        if phone and (len(digits(phone)) < 6 or digits(phone) not in seen_digits): phone = None
        email = (p.get('email') or '').strip() or None
        if email and (not EMAIL.fullmatch(email) or email.casefold() not in seen_cf): email = None
        if why: dropped.append(dict(name=name, reason=why)); continue
        quote = norm(str(p.get('quote') or ''))
        # The evidence text: the LinkedIn result title, or the model's quote with the contact
        # details appended exactly as found.
        if title: quote = title
        elif not quote_found(quote, seen): dropped.append(dict(name=name, reason='quote_not_in_results')); continue
        if name.casefold() not in quote.casefold(): quote = f'{name} – {quote}'
        extra = ' '.join(x for x in (phone, email) if x and x not in quote)
        quote = (quote + (' ' + extra if extra else ''))[:1100]
        source = url or str(p.get('source_url') or '')
        if not source.startswith('http'): dropped.append(dict(name=name, reason='no_source')); continue
        role = norm(str(p.get('role') or ''))[:160]
        if role and role.casefold() not in seen_cf: role = ''  # only a title the results show
        known_role = next((k['role'] for k in company.get('known') or [] if k['person'].casefold() == name.casefold()), None)
        role = role or known_role or ''
        kept.append(dict(name=name, role=role or None, linkedin=url or None,
                         phone=phone, email=email, source=source, quote=quote))
    return kept, dropped


def save(store, cid, people):
    """Each person -> one evidence document (the result/page text) + one contact fact."""
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for p in people:
        doc_id = ident(cid, 'contact-step', p['source'], p['name'])
        blocks = {'b1': p['quote']}
        store.db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?)',
                         (doc_id, cid, p['source'], now, hashlib.sha256(p['quote'].encode()).hexdigest(), '',
                          'contact_search', json.dumps(blocks, ensure_ascii=False), json.dumps({'step': 'find_contacts'})))
        claim = Claim(field='contact', value=p['email'] or p['phone'] or 'contact', number=None, quantity=None, qualifier=None,
                      subject='person', scope='person', geography='unspecified', currentness='current', effective_date=None,
                      person=p['name'], role=p['role'], email=p['email'], phone=p['phone'], profile_url=p['linkedin'],
                      document_id=doc_id, block_id='b1', quote=p['quote'])
        docs = {d['id']: d for d in store.docs(cid)}
        error = hard_error(claim, docs)
        payload = claim.model_dump_json()
        store.db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                         (ident(cid, payload), cid, doc_id, payload, 'rejected' if error else 'accepted', error or '', VERSION))
        added += not error
    store.db.commit()
    return added


def find(company):
    path = LOG / f"{company['id']}.json"
    if path.exists():
        old = json.loads(path.read_text())
        # v1 logs are kept unless v2 has something new to do: known people to look up, or a big firm.
        if old.get('v') == PROMPT_VERSION or not (company.get('known') or company.get('big')): return old
    site = f" (website {company['website']})" if company.get('website') else ''
    known = company.get('known') or []
    known_text = ('Known people from their website (find their LinkedIn profiles first):\n' +
                  '\n'.join(f"- {k['person']} ({k['role'] or 'role not stated'})" for k in known[:6])) if known else 'No named people known yet.'
    calls, max_people = (8, 10) if company.get('big') else (5, 6)
    log = dict(company_id=company['id'], name=company['name'], people=[], dropped=[], v=PROMPT_VERSION)
    start = time.monotonic()
    try:
        result, seen = run_sdk(PROMPT.format(name=company['name'], site=site, short=short_name(company['name']),
                                             known=known_text, calls=calls, max_people=max_people), max_turns=calls + 4)
        raw = getattr(result, 'result', '') or ''
        log['cost_usd'] = getattr(result, 'total_cost_usd', None)
        m = re.search(r'\{.*\}', raw, re.S)
        people = json.loads(m.group(0)).get('people', []) if m else []
        log['people'], log['dropped'] = verify(company, people, seen)
    except Exception as e:
        log['error'] = f'{type(e).__name__}: {str(e)[:300]}'
    log['seconds'] = round(time.monotonic() - start, 1)
    if 'error' not in log:
        LOG.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    return log


def known_people(store, cid):
    """Named people from the company's own pages, apartment managers first."""
    out, seen = [], set()
    for c in sorted((c for c in accepted(store, cid) if c['field'] == 'contact' and c['person']), key=lambda c: contact_tier(c) or 9):
        if c['person'].casefold() in seen or c.get('profile_url'): continue
        seen.add(c['person'].casefold()); out.append(dict(person=c['person'], role=c.get('role') or ''))
    return out


def targets(tiers, store=None):
    """Every lead in the given tiers (contact people for all of them), best leads first."""
    with (ROOT / 'output' / 'companies.csv').open(newline='') as f:
        rows = [r for r in csv.DictReader(f) if r['Tier'] in tiers]
    rows.sort(key=lambda r: (r['Tier'], -int(r['Fit score'] or r['First-look score'] or 0)))
    out = []
    for r in rows:
        units = re.search(r'([\d.,]+)\s*units', r['Apartments'] or '')
        n = int(re.sub(r'[.,]', '', units.group(1))) if units else 0
        out.append(dict(id=r['Company ID'], name=r['Company'], website=r['Website'],
                        known=known_people(store, r['Company ID']) if store else [],
                        big=n >= 1000 or 'Housing company' in (r['Segment'] or '')))
    return out


def website_profiles(store, ids):
    """Free step: LinkedIn profiles a company links on its own website. Linked to the named
    person whose name is in the profile address; otherwise added under the name the address
    spells (role unknown). Evidence: the website page that carries the link."""
    added = 0
    for cid in ids:
        people = [c for c in accepted(store, cid) if c['field'] == 'contact' and c['person']]
        have = {c.get('profile_url') for c in people if c.get('profile_url')}
        for d in store.docs(cid):
            if d['method'] == 'contact_search': continue
            for bid, text in json.loads(d['blocks_json']).items():
                for raw in dict.fromkeys(LINKEDIN_IN.findall(text)):
                    url = norm_profile(raw)
                    if not url or url in have: continue
                    have.add(url)
                    slug = re.sub(r'%[0-9a-f]{2}', '', url.rsplit('/', 1)[-1].casefold())
                    words = [w for w in re.split(r'[-_]', slug) if w.isalpha() and len(w) > 1]
                    fold = lambda x: x.casefold().replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue').replace('ß', 'ss')
                    match = next((c for c in people if all(fold(n) in fold(slug) or n.casefold() in slug
                                                           for n in re.findall(r'[A-Za-zÄÖÜäöüß]{3,}', c['person'])[-2:])), None)
                    if match:  # same person, same evidence, plus the profile link
                        claim = Claim.model_validate({**{k: v for k, v in match.items() if k != 'id'}, 'profile_url': url})
                    elif len(words) >= 2:
                        name = ' '.join(w.capitalize() for w in words[:3])
                        quote = f'LinkedIn profile linked on the company website ({d["url"]}): {url} – {name}'
                        doc_id = ident(cid, 'website-linkedin', url)
                        store.db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?)',
                                         (doc_id, cid, d['url'], datetime.now(timezone.utc).isoformat(), hashlib.sha256(quote.encode()).hexdigest(), '',
                                          'website_linkedin', json.dumps({'b1': quote}), json.dumps({'step': 'find_contacts', 'from_document': d['id']})))
                        claim = Claim(field='contact', value='linkedin', number=None, quantity=None, qualifier=None, subject='person',
                                      scope='person', geography='unspecified', currentness='current', effective_date=None, person=name,
                                      role=None, email=None, phone=None, profile_url=url, document_id=doc_id, block_id='b1', quote=quote)
                    else: continue
                    docs = {x['id']: x for x in store.docs(cid)}
                    error = hard_error(claim, docs); payload = claim.model_dump_json()
                    store.db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                                     (ident(cid, payload), cid, claim.document_id, payload, 'rejected' if error else 'accepted', error or '', VERSION))
                    added += not error
    store.db.commit()
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tiers', default='AB'); ap.add_argument('--limit', type=int); ap.add_argument('--workers', type=int, default=4)
    a = ap.parse_args()
    store = Store(ROOT)
    rows = targets(a.tiers, store)
    rows = rows[:a.limit] if a.limit else rows
    print('website LinkedIn profiles added:', website_profiles(store, [r['id'] for r in rows]), flush=True)
    stats = dict(companies=len(rows), with_people=0, people=0, linkedin=0, direct=0, cost_usd=0.0, errors=0)
    with ThreadPoolExecutor(a.workers) as ex:
        for log in ex.map(find, rows):
            if log.get('error'): stats['errors'] += 1
            people = log.get('people', [])
            if people and not log.get('saved'):
                save(store, log['company_id'], people)
                log['saved'] = True; (LOG / f"{log['company_id']}.json").write_text(json.dumps(log, ensure_ascii=False, indent=2))
            stats['with_people'] += bool(people); stats['people'] += len(people)
            stats['linkedin'] += sum(bool(p['linkedin']) for p in people)
            stats['direct'] += sum(bool(p['phone'] or p['email']) for p in people)
            stats['cost_usd'] += log.get('cost_usd') or 0
            print(f"{log['name'][:50]:50} people={len(people)} li={sum(bool(p['linkedin']) for p in people)} dropped={len(log.get('dropped', []))} {log.get('error', '')}", flush=True)
    stats['cost_usd'] = round(stats['cost_usd'], 2)
    print(json.dumps(stats))


if __name__ == '__main__':
    main()
