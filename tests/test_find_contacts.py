import json
from heidi_leads.core import Store, accepted, contact_route, access
from heidi_leads.find_contacts import verify, save

CO = dict(id='c1', name='Muster Hausverwaltung GmbH', website='https://muster.de')
SEEN = json.dumps({'results': [{'title': 'Anna Beispiel - Muster Hausverwaltung GmbH', 'url': 'https://de.linkedin.com/in/anna-beispiel-1'},
                               {'title': 'Bernd Fremd - Andere Firma AG', 'url': 'https://www.linkedin.com/in/bernd-fremd'}]}) + \
    '\nTeam: Carla Muster, Objektbetreuerin, Tel. 030 1234567, c.muster@muster.de'


def test_verify_keeps_only_what_tools_returned():
    people = [
        dict(name='Anna Beispiel', role=None, linkedin_url='https://de.linkedin.com/in/anna-beispiel-1'),
        dict(name='Bernd Fremd', linkedin_url='https://www.linkedin.com/in/bernd-fremd'),          # other company
        dict(name='Erfunden Person', linkedin_url='https://www.linkedin.com/in/erfunden'),         # not in results
        dict(name='Carla Muster', role='Objektbetreuerin', phone='030 1234567', email='c.muster@muster.de',
             source_url='https://muster.de/team', quote='Carla Muster, Objektbetreuerin, Tel. 030 1234567'),
        dict(name='Carla Muster', phone='030 9999999', source_url='https://muster.de/team', quote='Carla Muster, Objektbetreuerin'),
    ]
    kept, dropped = verify(CO, people, SEEN)
    assert [p['name'] for p in kept] == ['Anna Beispiel', 'Carla Muster', 'Carla Muster']
    assert kept[0]['linkedin'] == 'https://www.linkedin.com/in/anna-beispiel-1' and kept[0]['quote'].startswith('Anna Beispiel - Muster')
    assert kept[1]['phone'] == '030 1234567' and kept[1]['email'] == 'c.muster@muster.de'
    assert kept[2]['phone'] is None  # invented number dropped
    assert {d['reason'] for d in dropped} == {'profile_other_company', 'name_not_in_results'}


def test_saved_linkedin_contact_raises_access(tmp_path):
    s = Store(tmp_path)
    cid = s.add_company('Muster Hausverwaltung GmbH', 'https://muster.de', {'source': 'OSM', 'postcode': '10115'})
    kept, _ = verify(CO, [dict(name='Anna Beispiel', role='Objektbetreuerin', linkedin_url='https://de.linkedin.com/in/anna-beispiel-1')],
                     SEEN + ' Objektbetreuerin')
    assert save(s, cid, kept) == 1
    route = contact_route(accepted(s, cid))
    assert route['person'] == 'Anna Beispiel' and route['linkedin'].endswith('anna-beispiel-1')
    assert access(route) == (90, 'Property manager, name only + LinkedIn')


def test_likely_email_from_company_pattern():
    from heidi_leads.core import email_pattern, likely_email
    contacts = [dict(person='Anna Beispiel', email='anna.beispiel@muster.de', role='Objektbetreuerin'),
                dict(person='Jörg Müller', email=None, role='Hausverwalter'),
                dict(person=None, email='info@muster.de', role=None)]
    assert email_pattern(contacts)[:2] == ('first.last', 'muster.de')
    assert likely_email('Herr Jörg Müller', contacts) == 'joerg.mueller@muster.de'
    assert likely_email('Jörg Müller', [contacts[2]]) == ''  # no personal pattern published


def test_website_profile_linked_to_named_person(tmp_path):
    from heidi_leads.core import Claim, ingest, Extraction
    from heidi_leads.find_contacts import website_profiles
    s = Store(tmp_path)
    cid = s.add_company('Muster Hausverwaltung GmbH', 'https://muster.de', {'source': 'OSM', 'postcode': '10115'})
    import hashlib, json as j
    s.db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)', ('d1', cid, 'https://muster.de/team', 'now', 'h', '', 'html',
                 j.dumps({'b1': 'Anna Beispiel, Objektbetreuerin', 'links': 'Linkedin [https://de.linkedin.com/in/anna-beispiel-12ab?trk=x]'}), '{}'))
    claim = Claim(field='contact', value='contact', number=None, quantity=None, qualifier=None, subject='person', scope='person',
                  geography='berlin', currentness='current', effective_date=None, person='Anna Beispiel', role='Objektbetreuerin',
                  email=None, document_id='d1', block_id='b1', quote='Anna Beispiel, Objektbetreuerin')
    s.db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?)', ('c1', cid, 'd1', claim.model_dump_json(), 'accepted', '', 'x')); s.db.commit()
    assert website_profiles(s, [cid]) == 1
    route = contact_route(accepted(s, cid))
    assert route['person'] == 'Anna Beispiel' and route['linkedin'] == 'https://www.linkedin.com/in/anna-beispiel-12ab'
    assert access(route)[0] == 90


def test_service_firm_rule():
    from heidi_leads.core import service_or_broker
    mt = lambda q: [dict(field='management_type', quote=q)]
    assert service_or_broker({'name': 'Muster Facility GmbH'}, mt('Für Hausverwaltungen übernehmen wir die Reinigung von Treppenhäusern'))
    assert service_or_broker({'name': 'Muster Immobilien'}, mt('Verkauf und Vermietung von Eigentumswohnungen'))
    assert not service_or_broker({'name': 'Muster Facility GmbH'}, mt('WEG-Verwaltung und Mietverwaltung in Berlin'))
    assert not service_or_broker({'name': 'Muster Hausverwaltung'}, mt('Vermietung von Wohnungen'))
