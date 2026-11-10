import json
from heidi_leads import dedup as dd
from heidi_leads.core import Store

def test_clean_name():
    c = dd.clean_name
    assert c('Erste Marzahner Wohnungsgenossenschaft eG Mitgliedertreffen') == 'Erste Marzahner Wohnungsgenossenschaft eG'
    assert c('Müller Immobilien GmbH | Hausverwaltung Berlin') == 'Müller Immobilien GmbH'
    assert c('Hausverwaltung Schulze - Berlin') == 'Hausverwaltung Schulze'
    assert c('Treubau Verwaltung GmbH Niederlassung Berlin') == 'Treubau Verwaltung GmbH'
    # kept: real name after legal form, legal-form chains, continuations, hyphenated places, bare generic names
    for keep in ('Berliner Wohnungsgenossenschaft eG Nord Ost 77', 'X GmbH & Co. KG', 'Y UG (haftungsbeschränkt)',
                 'Plura Haus – und Immobilien GmbH', 'Melzer Verwaltungs GmbH (MDV GmbH)', 'WBM Wohnungsbaugesellschaft Berlin-Mitte mbH', 'Hausverwaltung'):
        assert c(keep) == keep

def test_keys():
    assert dd.phone_key('+49 30 1234567') == dd.phone_key('030 / 123 45 67') == '0301234567'
    assert dd.phone_key('030 1') == ''
    assert dd.nkey('Muster Hausverwaltung GmbH') == dd.nkey('Muster Hausverwaltung mbH | Berlin') == 'muster hausverwaltung'
    assert dd.nkey('Hausverwaltung GmbH') == ''  # generic names never match by name

def co(i, name, url='', state='discovered', **rec):
    return dict(id=i, name=name, url=url, state=state, raw=[dict(source=rec.pop('source', 'GOOGLE_PLACES'), **rec)])

def test_matching_rules():
    cs = [co('a', 'Alpha Verwaltung GmbH', 'https://www.alpha.de', phone='030 1111111'),
          co('b', 'Alpha HV', 'http://alpha.de/kontakt'),                                      # domain
          co('c', 'Beta Immobilien', phone='+49 30 2222222', postcode='10115'),
          co('d', 'Gamma GmbH', phone='030 2222222', postcode='12000'),                          # phone
          co('e', 'Delta Wohnen eG', postcode='10115'), co('f', 'Delta Wohnen e.G.', source='BBU'),  # name, one side no postcode
          co('g', 'Epsilon Hausverwaltung', postcode='10115'), co('h', 'Epsilon Hausverwaltung GmbH', postcode='13000'),  # different postcodes
          co('i', 'One', 'https://facebook.com/x'), co('j', 'Two', 'https://facebook.com/y')]   # shared host ignored
    rules = {frozenset((a, b)): r for a, b, r in dd.match(cs)}
    assert rules == {frozenset('ab'): 'domain', frozenset('cd'): 'phone', frozenset('ef'): 'name'}

def test_survivor_rules():
    cs = [co('a', 'Alpha', 'https://alpha.de', source='OSM'), co('b', 'Alpha GmbH', 'https://alpha.de', state='reviewed'),
          co('c', 'Alpha GmbH', '', postcode='10115', source='VDIV')]
    (s, dups), = dd.plan(cs)
    assert s['id'] == 'b' and {d['id'] for d, _ in dups} == {'a', 'c'}
    cs = [co('x', 'Kappa', '', phone='030 3333333'), co('y', 'Kappa GmbH', 'https://kappa.de', phone='030 3333333')]
    (s, _), = dd.plan(cs); assert s['id'] == 'y'  # website beats none

def test_run_merges_without_deleting(tmp_path):
    st = Store(tmp_path)
    a = st.add_company('Geschäftsstelle', 'https://www.zeta-eg.de', dict(source='GOOGLE_PLACES', phone='030 4444444', postcode='10115'))
    b = st.add_company('Zeta Wohnungsgenossenschaft eG', '', dict(source='BBU', address='Berlin'))
    c = st.add_company('Zeta Wohnungsgenossenschaft eG', 'https://zeta-eg.de/', dict(source='WBGD', phone='+49 30 4444444', postcode='10115'))
    st.db.execute("UPDATE companies SET state='collected' WHERE id=?", (a,)); st.db.commit()
    rep = dd.run(tmp_path, dry_run=True, report=tmp_path / 'r.json')
    assert rep['duplicates'] == 2 and st.company(b)['state'] == 'discovered'
    dd.run(tmp_path, report=tmp_path / 'r.json')
    rows = {r['id']: r for r in st.rows('SELECT * FROM companies')}
    assert len(rows) == 3 and rows[a]['state'] == 'collected' and rows[a]['name'] == 'Zeta Wohnungsgenossenschaft eG'
    assert rows[b]['state'] == rows[c]['state'] == 'merged' and rows[b]['error'] == f'duplicate_of:{a}'
    raw = json.loads(rows[a]['raw_json'])
    assert sorted(r['source'] for r in raw) == ['BBU', 'GOOGLE_PLACES', 'WBGD'] and raw[0]['name'] == 'Geschäftsstelle'
    assert dd.run(tmp_path, report=tmp_path / 'r.json')['duplicates'] == 0  # idempotent
