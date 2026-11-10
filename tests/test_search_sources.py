import json, sqlite3
from types import SimpleNamespace
from heidi_leads import search_sources as ss

CANNED = '''```json
{"urls":[
 {"url":"https://www.hansen-heinrich.de/leistungen/?utm_source=x#top","kind":"company_page","why":"services"},
 {"url":"https://www.hansen-heinrich.de/leistungen","kind":"company_page","why":"dup"},
 {"url":"https://www.immowelt.de/expose/abc?gclid=1","kind":"listing","why":"listing"},
 {"url":"https://www.kleinanzeigen.de/s-anzeige/1","kind":"listing","why":"l2"},
 {"url":"https://www.immobilienscout24.de/expose/2","kind":"listing","why":"l3"},
 {"url":"https://inberlinwohnen.de/wohnung/4","kind":"listing","why":"l4 over cap"},
 {"url":"https://hansen-heinrich.de/files/broschuere.pdf","kind":"other","why":"pdf"},
 {"url":"https://www.facebook.com/hansenheinrich","kind":"other","why":"social"},
 {"url":"https://de.wikipedia.org/wiki/X","kind":"other","why":"wiki"},
 {"url":"https://www.google.com/maps/place/x","kind":"other","why":"maps"},
 {"url":"https://hh-verwaltung.de","kind":"alias_domain","why":"alias"},
 {"url":"https://hh-other.de","kind":"alias_domain","why":"second alias"},
 {"url":"ftp://bad","kind":"other","why":"bad scheme"},
 {"url":"https://x.de/a","kind":"rumour","why":"malformed kind"},
 {"url":"https://x.de/b","kind":"other","why":"w","extra":1}
]}
```'''


def fake_sdk(calls):
    def run(prompt, model, max_turns=8):
        calls.append(dict(prompt=prompt, model=model))
        return SimpleNamespace(result=CANNED, total_cost_usd=0.0123, num_turns=5, is_error=False, session_id='s')
    return run


def test_parse_result_fenced_and_malformed():
    good, bad = ss.parse_result(CANNED)
    assert len(good) == 13 and len(bad) == 2
    assert ss.parse_result('not json at all') == ([], [{'error': 'unparseable', 'raw': 'not json at all'}])
    good, _ = ss.parse_result('Here you go: {"urls":[{"url":"https://a.de","kind":"listing"}]} done')
    assert good[0]['why'] == ''


def test_canonical_and_blocked():
    assert ss.canonical('HTTPS://WWW.A.de:443/x/?utm_medium=1&b=2&a=1#f') == 'https://www.a.de/x?a=1&b=2'
    assert ss.canonical('mailto:x@y.de') is None
    assert ss.blocked('https://de.linkedin.com/company/x') and ss.blocked('https://maps.google.com/?q=1')
    assert ss.blocked('https://www.stepstone.de/job/1') and not ss.blocked('https://www.immowelt.de/expose/1')


def test_clean_caps_and_kinds():
    good, _ = ss.parse_result(CANNED)
    out = ss.clean(good, 'http://www.hansen-heinrich.de')
    urls = [u['url'] for u in out]
    assert len(urls) == len(set(urls))
    kinds = [u['kind'] for u in out]
    assert kinds.count('listing') == 3 and kinds.count('alias_domain') == 1 and kinds.count('company_page') == 1
    assert {'url': 'https://hansen-heinrich.de/files/broschuere.pdf', 'kind': 'document', 'why': 'pdf'} in out
    assert not any('facebook' in u or 'wikipedia' in u or 'maps' in u for u in urls)
    assert 'https://www.immowelt.de/expose/abc' in urls


def test_search_company_logs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ss, '_run_sdk', fake_sdk(calls))
    urls = ss.search_company('Hansen & Heinrich', 'http://www.hansen-heinrich.de', company_id='c1', log_dir=tmp_path)
    log = json.loads((tmp_path / 'c1.json').read_text())
    assert log['cost_usd'] == 0.0123 and log['num_turns'] == 5 and log['raw'] == CANNED and log['urls'] == urls
    assert len(log['dropped']) == 2 and len(log['queries']) == 4 and '"Hansen & Heinrich" Berlin Hausverwaltung' in calls[0]['prompt']
    assert ss.short_name('SOLANO Immobilienmanagement GmbH') == 'SOLANO Immobilienmanagement'
    assert ss.short_name('Muster GmbH & Co. KG') == 'Muster'


def test_search_company_sdk_error(tmp_path, monkeypatch):
    def boom(*a, **k): raise RuntimeError('denied')
    monkeypatch.setattr(ss, '_run_sdk', boom)
    assert ss.search_company('X', None, company_id='c2', log_dir=tmp_path) == []
    assert 'denied' in json.loads((tmp_path / 'c2.json').read_text())['error']


def test_build_targets_and_resume(tmp_path, monkeypatch):
    db = tmp_path / 'leads.sqlite'
    con = sqlite3.connect(db); con.execute('CREATE TABLE companies(id TEXT PRIMARY KEY,name TEXT,url TEXT)')
    con.execute("INSERT INTO companies VALUES('c1','Hansen & Heinrich','http://www.hansen-heinrich.de')"); con.commit(); con.close()
    calls = []
    monkeypatch.setattr(ss, '_run_sdk', fake_sdk(calls))
    out = tmp_path / 'search/targets.json'
    targets = ss.build_targets(db, ['c1', 'missing'], out, log_dir=tmp_path / 'search')
    assert json.loads(out.read_text()) == targets and list(targets) == ['c1']
    assert all(isinstance(u, str) for u in targets['c1'])
    kinds = json.loads((tmp_path / 'search/targets-kinds.json').read_text())
    assert set(kinds) == set(targets['c1']) and set(kinds.values()) <= set(ss.KINDS)
    summary = json.loads((tmp_path / 'search/summary.json').read_text())
    assert summary['totals']['cost_usd'] == 0.0123 and summary['companies']['missing']['error']
    ss.build_targets(db, ['c1'], out, log_dir=tmp_path / 'search')
    assert len(calls) == 1  # resumed from log
    ss.build_targets(db, ['c1'], out, log_dir=tmp_path / 'search', force=True)
    assert len(calls) == 2


def test_resolve_websites_deep_search(tmp_path, monkeypatch):
    from heidi_leads import run_full as rf
    con = sqlite3.connect(':memory:'); con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE companies(id TEXT PRIMARY KEY,name TEXT,url TEXT)')
    con.executemany('INSERT INTO companies VALUES(?,?,NULL)', [('a', 'Zander Hausverwaltung GmbH'), ('b', 'Quorra Verwaltung GmbH'),
                                                             ('c', 'Old Profile GmbH'), ('d', 'Nothing Found GmbH')])
    store = SimpleNamespace(db=con, company=lambda cid: con.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone())
    monkeypatch.setattr(rf, 'FULL', tmp_path); monkeypatch.setattr(rf, 'ROOT', tmp_path)
    (tmp_path / 'website-lookup-s9.json').write_text(json.dumps({'c': dict(name='Old Profile GmbH', picked=None, profile='https://dir.de/old')}))
    calls = []
    def fake(name, website, max_queries=4, queries=None, **k):
        calls.append((name, len(queries)))
        if name.startswith('Zander'):
            return [dict(url=f'https://p{i}.de/zander', kind='listing' if i % 2 else 'other', why='') for i in range(5)]
        if name.startswith('Quorra'): return [dict(url='https://www.quorra-verwaltung.de', kind='company_page', why='')]
        return []
    monkeypatch.setattr(ss, 'search_company', fake)
    out = rf.resolve_websites(store, ['a', 'b', 'c', 'd'])
    assert out == {'a': ['https://p0.de/zander', 'https://p1.de/zander', 'https://p2.de/zander', 'https://p3.de/zander'],
                   'c': ['https://dir.de/old']}
    assert store.company('b')['url'] == 'https://www.quorra-verwaltung.de'
    # One search call (3 queries) per firm without a site; the old cached profile is not searched.
    assert sorted(calls) == [('Nothing Found GmbH', 3), ('Quorra Verwaltung GmbH', 3), ('Zander Hausverwaltung GmbH', 3)]
    cache = json.loads((tmp_path / 'website-lookup.json').read_text())
    assert cache['d']['deep'] == [] and cache['d']['profiles'] == []
    n = len(calls); assert rf.resolve_websites(store, ['a', 'b', 'c', 'd']) == out and len(calls) == n  # cached, no repeat
    assert ss.deep_queries('Zander GmbH')[1] == 'Zander Hausverwaltung Berlin Impressum Telefon'
