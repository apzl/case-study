import json
from heidi_leads import discover_maps as dm
from heidi_leads.core import Store

OSM = {'osm3s': {'timestamp_osm_base': '2026-11-09T00:00:00Z'}, 'elements': [
    {'type': 'node', 'id': 1, 'lat': 52.5, 'lon': 13.4, 'tags': {'name': 'Muster Hausverwaltung GmbH', 'office': 'property_management', 'website': 'www.Muster-HV.de/kontakt', 'addr:street': 'Drakestraße', 'addr:housenumber': '48', 'addr:postcode': '12205', 'addr:city': 'Berlin', 'phone': '+49 30 1'}},
    {'type': 'way', 'id': 2, 'center': {'lat': 52.4, 'lon': 13.1}, 'tags': {'name': 'Potsdam Immobilienverwaltung', 'office': 'company', 'addr:postcode': '14467', 'addr:city': 'Potsdam'}},
    {'type': 'node', 'id': 3, 'lat': 52.5, 'lon': 13.4, 'tags': {'name': 'Ohne Adresse WEG-Verwaltung', 'office': 'property_management'}},
    {'type': 'node', 'id': 4, 'lat': 52.5, 'lon': 13.4, 'tags': {'name': 'Nachbar Hausverwaltung', 'addr:street': 'Drakestraße', 'addr:housenumber': '48', 'addr:postcode': '12205', 'addr:city': 'Berlin'}},
]}
PLACES = {'places': [
    {'id': 'g1', 'displayName': {'text': 'Muster Hausverwaltung'}, 'websiteUri': 'https://muster-hv.de/?utm=x', 'nationalPhoneNumber': '030 1',
     'addressComponents': [{'longText': 'Drakestraße', 'types': ['route']}, {'longText': '48', 'types': ['street_number']}, {'longText': '12205', 'types': ['postal_code']}, {'longText': 'Berlin', 'types': ['locality']}],
     'types': ['real_estate_agency'], 'rating': 4.1, 'userRatingCount': 9, 'location': {'latitude': 52.5, 'longitude': 13.4}},
    {'id': 'g2', 'displayName': {'text': 'Kiez Verwaltung UG'}, 'addressComponents': [{'longText': '10405', 'types': ['postal_code']}, {'longText': 'Berlin', 'types': ['locality']}], 'types': ['point_of_interest']},
]}

def cands():
    c = dm.normalise_osm(OSM) + dm.normalise_google([('Hausverwaltung', 'Mitte', 0, PLACES), ('Mietverwaltung', 'Pankow', 0, PLACES)])
    return [x for x in c if dm.berlin_filter(x)]

def test_normalisation():
    o = dm.normalise_osm(OSM)[0]
    assert (o['website'], o['postcode'], o['street'], o['source'], o['source_id']) == ('https://www.muster-hv.de', '12205', 'Drakestraße 48', 'OSM', 'node/1')
    g = dm.normalise_google([('Hausverwaltung', 'Mitte', 0, PLACES), ('Mietverwaltung', 'Pankow', 0, PLACES)])
    assert len(g) == 2 and g[0]['website'] == 'https://muster-hv.de' and g[0]['matched_terms'] == ['Hausverwaltung', 'Mietverwaltung']
    assert g[0]['query_districts'] == ['Mitte', 'Pankow'] and g[0]['hits'] == 2 and g[1]['needs_website_lookup']

def test_berlin_filter():
    names = {c['name']: c for c in cands()}
    assert 'Potsdam Immobilienverwaltung' not in names
    assert names['Ohne Adresse WEG-Verwaltung']['address_precision'] == 'none'
    assert names['Muster Hausverwaltung GmbH']['address_precision'] == 'postcode'
    assert dm.is_berlin_postcode('10115') and dm.is_berlin_postcode('14199') and not dm.is_berlin_postcode('14467')

def test_dedup_within_and_against_store(tmp_path):
    s = Store(tmp_path)
    cid = s.add_company('Kiez Verwaltung', 'http://kiez.example', dict(source='VDIV', address='Adresse Teststr. 1 10405 Berlin'))
    s.db.close()
    out = dm.dedup(cands(), dm.existing_companies(tmp_path / 'data/leads.sqlite'))
    st = {(c['source'], c['name']): c['dedup_status'] for c in out}
    assert st[('OSM', 'Muster Hausverwaltung GmbH')] == 'new'
    assert st[('GOOGLE_PLACES', 'Muster Hausverwaltung')] == 'duplicate_of:OSM:node/1'  # same host
    assert st[('OSM', 'Nachbar Hausverwaltung')] == 'new'  # shared address alone never merges
    assert st[('GOOGLE_PLACES', 'Kiez Verwaltung UG')] == f'matches_existing:{cid}'  # name + postcode
    rows = dm.to_company_rows(out)
    assert {r[0] for r in rows} == {'Muster Hausverwaltung GmbH', 'Nachbar Hausverwaltung', 'Ohne Adresse WEG-Verwaltung'}
    noweb = [r for r in rows if r[0] == 'Ohne Adresse WEG-Verwaltung'][0]
    assert noweb[1] == '' and noweb[2]['needs_website_lookup']

def test_existing_match_by_registrable_domain(tmp_path):
    s = Store(tmp_path); cid = s.add_company('Other Name', 'https://shop.muster-hv.de/x', dict(source='VDIV', address='')); s.db.close()
    out = dm.dedup(cands(), dm.existing_companies(tmp_path / 'data/leads.sqlite'))
    assert [c for c in out if c['source_id'] == 'node/1'][0]['dedup_status'] == f'matches_existing:{cid}'

def test_google_key_absent_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    class Boom:
        def post(self, *a, **k): raise AssertionError('network used')
    assert dm.fetch_google_places(api_key=None, cache_dir=tmp_path, session=Boom(), env_file=tmp_path / 'missing.env') == []
    assert 'no key' in capsys.readouterr().out
    assert dm.main(['--source', 'google', '--estimate', '--env-file', str(tmp_path / 'missing.env')]) == 0
    assert '168 queries; 168-504 requests' in capsys.readouterr().out

def test_google_cache_reused(tmp_path, monkeypatch):
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    calls = []
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return PLACES
    class S:
        def post(self, *a, **k): calls.append(k['json']['textQuery']); return R()
    one = dict(terms=['Hausverwaltung'], districts=['Mitte'], cache_dir=tmp_path, env_file=None)
    assert len(dm.fetch_google_places(api_key='k', session=S(), **one)) == 1
    assert len(dm.fetch_google_places(api_key=None, session=S(), **one)) == 1 and calls == ['Hausverwaltung Mitte Berlin']

def test_osm_cache_and_retry(tmp_path):
    codes = [504, 200]; slept = []
    class R:
        def __init__(self, c): self.status_code = c
        def raise_for_status(self): pass
        def json(self): return json.loads(json.dumps(OSM))
    class S:
        def post(self, *a, **k): return R(codes.pop(0))
    d = dm.fetch_osm(dm.TERMS, tmp_path, session=S(), sleep=slept.append)
    assert slept == [30] and len(d['elements']) == 4
    assert dm.fetch_osm(dm.TERMS, tmp_path, session=None)['_query'] == dm.osm_query(dm.TERMS)

def test_key_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    f = tmp_path / 'k.env'; f.write_text('  bareKEY123'); assert dm.resolve_key(f) == 'bareKEY123'
    f.write_text('# c\nGOOGLE_MAPS_API_KEY=abc \n'); assert dm.resolve_key(f) == 'abc'
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'fromenv'); assert dm.resolve_key(f) == 'fromenv'

def test_google_error_stops_and_redacts(tmp_path):
    class R:
        status_code = 403; text = '{"error": "PERMISSION_DENIED for key SECRET"}'
    class S:
        n = 0
        def post(self, *a, **k): S.n += 1; return R()
    import pytest
    with pytest.raises(dm.GooglePlacesError) as e:
        dm.fetch_google_places(api_key='SECRET', session=S(), cache_dir=tmp_path, env_file=None)
    assert S.n == 1 and 'SECRET' not in str(e.value) and 'PERMISSION_DENIED' in str(e.value)
