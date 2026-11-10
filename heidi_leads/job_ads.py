"""Job-ads step: Berlin property-management job ads from LinkedIn's public (logged-out) job
search (guest pages). For each lead that is hiring:

  * a buying signal: the company is hiring property staff (growing or short-handed -> less
    admin work is Heidi's pitch);
  * the ad's Ansprechpartner (name, role, direct phone / personal e-mail) when the ad names
    one who is not just HR.

No login, no cookies, polite pauses. Search and ad pages are plain HTTP; one small Haiku call
reads the contact block of an ad, and a name/phone/e-mail is kept only if it is in the ad text.

    python -m heidi_leads.job_ads search            # free: which leads are hiring
    python -m heidi_leads.job_ads contacts [--limit N]
"""
import argparse, csv, json, os, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

from parsel import Selector

from .core import Store, Claim, VERSION, digits, hard_error, ident, norm, quote_found, EXCLUDED_ROLE, personal_email

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'runs' / 'jobs'                      # git-ignored (names in ads)
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'
KEYWORDS = ['Hausverwaltung', 'Objektbetreuer', 'Immobilienverwalter', 'WEG-Verwalter', 'Mietverwalter',
            'Property Manager', 'Immobilienkaufmann', 'Objektmanager', 'Bestandsmanager', 'Kundenbetreuer Immobilien',
            'Buchhaltung Hausverwaltung', 'Nebenkostenabrechnung', 'Wohnungsgenossenschaft', 'Wohnungsbaugesellschaft']
PAUSE = 1.5
GENERIC = {'gmbh', 'mbh', 'co', 'kg', 'ag', 'eg', 'ug', 'berlin', 'hausverwaltung', 'immobilien', 'verwaltung', 'verwaltungs',
           'immobilienverwaltung', 'gesellschaft', 'management', 'property', 'real', 'estate', 'und', 'service', 'services',
           'haftungsbeschränkt', 'gruppe', 'group', 'germany', 'deutschland', 'hausverwaltungen', 'verwaltungen',
           'immobilienmanagement', 'immobilienservice', 'holding', 'partners', 'berliner', 'die', 'der', 'das', 'für'}


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'de,en;q=0.8'})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (429, 999): time.sleep(20 * (i + 1)); continue
            return e.code, ''
        except Exception:
            time.sleep(5 * (i + 1))
    return 0, ''


def key(name):
    """Distinctive name words in order."""
    return tuple(dict.fromkeys(t for t in re.findall(r'[a-zäöüß0-9]{3,}', (name or '').casefold()) if t not in GENERIC))


def same_company(a, b):
    """Equal distinctive words; or one name's words inside the other's when that is 2+ words,
    ("Hansen & Heinrich AG" ~ "Hansen & Heinrich Immobilienservice"). One shared word alone is not enough."""
    sa, sb = set(a), set(b)
    if not sa or not sb: return False
    if sa == sb: return True
    small = a if len(sa) <= len(sb) else b
    if not set(small) <= (sb if small is a else sa): return False
    return len(set(small)) >= 2


def search(pages=8):
    """All Berlin job cards for the keywords: {job_id: {company, title, url, date}}."""
    cards = {}
    for kw in KEYWORDS:
        for page in range(pages):
            q = urllib.parse.urlencode({'keywords': kw, 'location': 'Berlin, Germany', 'start': page * 10})
            status, html = get('https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?' + q)
            time.sleep(PAUSE)
            sel = Selector(text=html or '<html/>')
            found = sel.css('div.base-card, li div.base-search-card')
            if status != 200 or not found: break
            new = 0
            for c in found:
                urn = (c.attrib.get('data-entity-urn') or '').rsplit(':', 1)[-1]
                if not urn or urn in cards: continue
                cards[urn] = dict(id=urn, keyword=kw, company=norm(' '.join(c.css('.base-search-card__subtitle *::text').getall())),
                                  title=norm(' '.join(c.css('.base-search-card__title::text').getall())),
                                  location=norm(' '.join(c.css('.job-search-card__location::text').getall())),
                                  date=c.css('time::attr(datetime)').get(''),
                                  url=(c.css('a.base-card__full-link::attr(href)').get('') or '').split('?')[0])
                new += 1
            if not new: break
        print(f'{kw}: {len(cards)} ads so far', flush=True)
    return cards


def leads():
    with (ROOT / 'output' / 'companies.csv').open(newline='') as f:
        return [r for r in csv.DictReader(f) if r['Tier'] in 'ABC']


def match(cards, rows):
    """Job ad -> lead by distinctive name words (all ad-company words found in the lead name, or vice versa)."""
    index = [(key(r['Company']), r) for r in rows]
    out = {}
    for c in cards.values():
        k = key(c['company'])
        if not k: continue
        hit = [r for lk, r in index if same_company(k, lk)]
        if len(hit) == 1: out.setdefault(hit[0]['Company ID'], []).append(c)
    return out


SYSTEM = 'You read one German job ad and copy its contact person. JSON only.'
PROMPT = '''Job ad text:
"""{text}"""
Copy the contact person(s) named in this ad (Ansprechpartner/in, Kontakt, "Ihre Fragen beantwortet"),
exactly as written: name, role/title if stated, direct phone, e-mail. Skip general addresses like
info@/jobs@/bewerbung@ unless a person is named with them. Return
{{"people":[{{"name":"...","role":null,"phone":null,"email":null,"quote":"the exact line(s) naming them"}}]}}
or {{"people":[]}}.'''


def read_contacts(text):
    import anyio
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
    for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'): os.environ.pop(k, None)
    opts = ClaudeAgentOptions(model='claude-haiku-5-5', system_prompt=SYSTEM, allowed_tools=[], max_turns=1)

    async def go():
        res = None
        async for m in query(prompt=PROMPT.format(text=text[:6000]), options=opts):
            if isinstance(m, ResultMessage): res = m
        return res
    res = anyio.run(go)
    raw = getattr(res, 'result', '') or ''
    m = re.search(r'\{.*\}', raw, re.S)
    return (json.loads(m.group(0)).get('people', []) if m else []), getattr(res, 'total_cost_usd', 0) or 0


def ad_text(job_id):
    status, html = get(f'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}')
    time.sleep(PAUSE)
    if status != 200: return ''
    sel = Selector(text=html)
    return norm(' '.join(sel.css('.show-more-less-html__markup ::text, .description__text ::text').getall()))


def save(store, cid, ad, text, people):
    """Ad text -> evidence document; each verified, non-HR person -> contact fact."""
    doc_id = ident(cid, 'job-ad', ad['id'])
    store.db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?)',
                     (doc_id, cid, ad['url'] or f"https://www.linkedin.com/jobs/view/{ad['id']}", datetime.now(timezone.utc).isoformat(),
                      ident(text), '', 'job_ad', json.dumps({'title': ad['title'], 'b1': text}, ensure_ascii=False),
                      json.dumps({'step': 'job_ads', 'title': ad['title'], 'date': ad['date']})))
    kept = 0
    for p in people if isinstance(people, list) else []:
        name = norm(str(p.get('name') or ''))
        quote = norm(str(p.get('quote') or ''))
        role = norm(str(p.get('role') or '')) or None
        if len(name) < 4 or name.casefold() not in text.casefold() or not quote_found(quote, text): continue
        if role and (EXCLUDED_ROLE.search(role) or re.search(r'\bHR\b|personal|human resources|talent', role, re.I)): continue
        if name.casefold() not in quote.casefold(): quote = f'{name}: {quote}'
        phone = p.get('phone') if p.get('phone') and digits(p['phone']) in digits(quote) and len(digits(p['phone'])) >= 6 else None
        email = (p.get('email') or '').strip() or None
        if email and email.casefold() not in quote.casefold(): email = None
        claim = Claim(field='contact', value=email or phone or 'contact', number=None, quantity=None, qualifier=None,
                      subject='person', scope='person', geography='unspecified', currentness='current', effective_date=None,
                      person=name, role=role or 'Contact named in job ad', email=email, phone=phone,
                      document_id=doc_id, block_id='b1', quote=quote[:1100])
        docs = {d['id']: d for d in store.docs(cid)}
        error = hard_error(claim, docs); payload = claim.model_dump_json()
        store.db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                         (ident(cid, payload), cid, doc_id, payload, 'rejected' if error else 'accepted', error or '', VERSION))
        kept += not error
    store.db.commit()
    return kept


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('step', choices=['search', 'contacts']); ap.add_argument('--limit', type=int)
    a = ap.parse_args(); OUT.mkdir(parents=True, exist_ok=True)
    if a.step == 'search':
        cards = search()
        (OUT / 'ads.json').write_text(json.dumps(cards, ensure_ascii=False, indent=1))
        hits = match(cards, leads())
        (OUT / 'matches.json').write_text(json.dumps(hits, ensure_ascii=False, indent=1))
        print(json.dumps(dict(ads=len(cards), companies_in_ads=len({c['company'] for c in cards.values()}), leads_hiring=len(hits),
                              ads_matched=sum(map(len, hits.values())))))
        return
    hits = json.loads((OUT / 'matches.json').read_text()); store = Store(ROOT)
    done = json.loads((OUT / 'done.json').read_text()) if (OUT / 'done.json').exists() else {}
    cost = people = 0
    for cid, ads in list(hits.items())[:a.limit] if a.limit else hits.items():
        for ad in ads[:3]:
            if ad['id'] in done: continue
            text = ad_text(ad['id'])
            found, c = (read_contacts(text) if re.search(r'ansprechpart|kontakt|fragen|@|tel', text, re.I) else ([], 0)) if text else ([], 0)
            cost += c
            n = save(store, cid, ad, text, found) if text else 0
            people += n; done[ad['id']] = dict(company_id=cid, people=n, has_text=bool(text))
            (OUT / 'done.json').write_text(json.dumps(done))
            print(f"{ad['company'][:40]:40} | {ad['title'][:40]:40} | people={n}", flush=True)
    print(json.dumps(dict(ads_read=len(done), people=people, cost_usd=round(cost, 3))))


if __name__ == '__main__':
    main()
