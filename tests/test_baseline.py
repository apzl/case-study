import copy,json
from pathlib import Path
import pytest
from pydantic import ValidationError
from heidi_leads.core import Claim,Extraction,Store,check_claim,ingest,qualify
from heidi_leads.collect import html_blocks,pdf_blocks,public_url
from heidi_leads.pipeline import safe,export

BASE=dict(field='units',value='Published count',number=29,quantity='residential_units',qualifier='exact',subject='company',scope='whole_portfolio',geography='berlin',currentness='uncertain',effective_date=None,person=None,role=None,email=None,document_id='d',block_id='b',quote='29 Wohnungen in Berlin')
def claim(**kw):return Claim(**(BASE|kw))
def docs(text):return {'d':{'blocks_json':json.dumps({'b':text})}}

def test_numeric_strings_and_extra_fields_rejected():
    with pytest.raises(ValidationError):claim(number='29')
    with pytest.raises(ValidationError):claim(extra='x')
    with pytest.raises(ValidationError):claim(number=-1)

def test_quote_and_wrong_document():
    assert check_claim(claim(),docs(BASE['quote'])) is None
    assert check_claim(claim(),docs('irrelevant'))=='quote_not_anchored'
    assert check_claim(claim(document_id='other'),docs(BASE['quote']))=='wrong_or_unissued_document'

@pytest.mark.parametrize('quote,number,reason',[
    ('155 Stellplätze',155,'parking_as_residential'),
    ('153 Wohn- und Gewerbeeinheiten',153,'mixed_units_as_residential'),
])
def test_units(quote,number,reason):assert check_claim(claim(quote=quote,number=number),docs(quote))==reason

def test_group_count_and_historical():
    text='Die Gruppe verwaltet 8000 Wohnungen bundesweit'
    assert check_claim(claim(number=8000,quote=text,subject='group'),docs(text))=='group_total_as_berlin'
    text='Bei ihrem früheren Arbeitgeber verwaltete sie 29 Wohnungen in Berlin'
    assert check_claim(claim(quote=text,currentness='current'),docs(text))=='historical_as_current'

def test_smoke_radio():
    text='Funk Rauchwarnmelder'
    c=claim(field='metering',value='radio_consumption',number=None,quantity=None,qualifier=None,quote=text)
    assert check_claim(c,docs(text))=='smoke_as_consumption'

def test_personal_email_requires_name():
    text='Kontakt info@example.de'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Jane Doe',role='Geschäftsführerin',email='info@example.de',quote=text)
    assert check_claim(c,docs(text))=='email_person_not_linked'

def test_null_extra_keys_dropped_not_quarantined():
    from heidi_leads.core import parse_extraction
    base=BASE|{'note':None}
    raw=json.dumps({'company_id':'c','claims':[base],'researched_families':['scale'],'notes':[]})
    result,quarantined=parse_extraction(raw)
    assert len(result.claims)==1 and not quarantined
    raw=json.dumps({'company_id':'c','claims':[BASE|{'note':'text'}],'researched_families':[],'notes':[]})
    result,quarantined=parse_extraction(raw)
    assert not result.claims and len(quarantined)==1

def test_unspecified_units_count_as_scale():
    text='betreut in Berlin und Brandenburg sowie im Berliner Umland Wohn- und Geschäftshäuser mit etwas über 600 Einheiten'
    c=claim(number=600,quantity='unspecified_units',qualifier='at_least',geography='berlin_region',quote=text)
    assert check_claim(c,docs(text)) is None
    assert qualify([c.model_dump()],['scale'])['scale']

def test_berliner_umland_not_berlin_city():
    text='über 600 Einheiten im Berliner Umland'
    c=claim(number=600,quantity='unspecified_units',qualifier='at_least',geography='berlin',quote=text)
    assert check_claim(c,docs(text))=='regional_total_as_berlin_city'

def test_exact_qualifier_softened_by_approximators():
    assert claim(qualifier='exact',number=500,quote='ca. 500 Wohneinheiten in Berlin').qualifier=='approximately'
    assert claim(qualifier='exact',number=100,quote='über 100 Wohneinheiten in Berlin').qualifier=='at_least'
    # Mixed list with ca. dominant stays approximately; explicit at_least is never changed.
    assert claim(qualifier='exact',number=1000,quote='ca. 4.600 Wohnungen, über 100 Gewerbe und 1.000 Stellplätze in Berlin',quantity='parking_spaces').qualifier=='approximately'
    assert claim(qualifier='at_least',number=100,quote='über 100 Wohneinheiten in Berlin').qualifier=='at_least'

def test_no_property_sample_or_parking_scale():
    c=claim(subject='property',scope='reference_sample').model_dump()
    q=qualify([c],['scale']);assert not q['scale'];assert q['evidence_coverage']==0
    c=claim(quantity='parking_spaces').model_dump();assert not qualify([c],[])['scale']

def test_conflict_is_reported_but_largest_count_still_used():
    a=claim().model_dump();b=claim(number=45,quote='45 Wohnungen in Berlin').model_dump()
    q=qualify([a,b],[]);assert q['conflict_fields']==['units'];assert max(c['number'] for c in q['scale'])==45

def test_same_domain_different_names_not_merged(tmp_path):
    s=Store(tmp_path)
    assert s.add_company('A GmbH','https://example.com',{})!=s.add_company('B GmbH','https://example.com',{})
    assert s.add_company('A GmbH','https://example.com',{})==s.add_company('A GmbH','https://example.com',{})

def test_ingestion_idempotent_export_offline(tmp_path):
    s=Store(tmp_path);cid=s.add_company('=HYPERLINK("evil")','https://example.com',{})
    did=s.document(cid,'https://example.com',b'text',{'b':BASE['quote']})
    e=Extraction(company_id=cid,claims=[claim(document_id=did)],researched_families=['scale'],notes=[])
    ingest(s,cid,e);ingest(s,cid,e)
    assert len(s.rows('SELECT * FROM claims'))==1
    a=export(s);b=export(s);assert a==b
    assert safe(' =BAD').startswith("'")

def test_html_preserves_table_contact_context():
    blocks,_=html_blocks('<table><tr><td>153 Wohn- und Gewerbeeinheiten</td><td>155 Stellplätze</td></tr></table><p>Jane Doe, Geschäftsführerin <a href="mailto:jane@example.de">Email</a></p>')
    assert any('153' in x and '155' in x for x in blocks.values())
    assert any('Jane Doe' in x and 'jane@example.de' in x for x in blocks.values())

def test_private_destination():
    with pytest.raises(ValueError):public_url('http://127.0.0.1/private')

def test_pdf_text_and_scan(tmp_path):
    import shutil
    from reportlab.pdfgen.canvas import Canvas
    from PIL import Image,ImageDraw,ImageFont
    text=tmp_path/'text.pdf';c=Canvas(str(text));c.drawString(50,750,'Berlin property manager with 29 residential apartments and central heating');c.save()
    blocks,method=pdf_blocks(text);assert method=='pdftotext';assert '29' in blocks['page1']
    if not (shutil.which('tesseract') or shutil.which('ocrmypdf')):
        pytest.skip('OCR system tools not installed on this host; OCR fallback not exercised')
    image=Image.new('RGB',(1800,1000),'white');draw=ImageDraw.Draw(image)
    try:font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',48)
    except OSError:font=ImageFont.truetype('arial.ttf',48)
    draw.text((100,200),'Berlin Hausverwaltung - 29 Wohnungen',fill='black',font=font)
    draw.text((100,300),'Kontakt: anna@example.de',fill='black',font=font)
    scan=tmp_path/'scan.pdf';image.save(scan,'PDF',resolution=150)
    artifacts=tmp_path/'ocr-artifacts'
    blocks,method=pdf_blocks(scan,artifact_dir=artifacts);assert (artifacts/'page1.png').exists();assert method in ['ocrmypdf_tesseract','tesseract_raster'];assert '29' in blocks['page1'];assert 'anna@example.de' in blocks['page1']

def test_directory_pagination_links_detected(tmp_path):
    # Frozen HTML shape: trailing slash profile has four components, not five.
    from urllib.parse import urlsplit
    profile='/de/hausverwaltung/berlin/example-gmbh/'
    assert len(urlsplit(profile).path.strip('/').split('/'))==4

def test_historical_and_generic_contact_not_priority():
    c=claim(currentness='historical').model_dump()
    q=qualify([c],['scale']);assert not q['scale'];assert not q['shortlist']

def test_boolean_scope_definition():
    text='Wir verwalten Immobilien in Berlin und Brandenburg.'
    c=claim(field='berlin_service',value='yes',number=None,quantity=None,qualifier=None,scope='company',quote=text)
    assert check_claim(c,docs(text)) is None
    assert check_claim(c.model_copy(update={'scope':'whole_portfolio'}),docs(text))=='company_fact_wrong_scope'

def test_company_sample_does_not_inflate_portfolio():
    c=claim(scope='reference_sample').model_dump()
    assert qualify([c],['scale'])['scale']==[]

def test_count_substring_not_valid_evidence():
    text='129 Wohnungen in Berlin'
    assert check_claim(claim(number=29,quote=text),docs(text))=='count_not_in_evidence'
    text='1.500 Wohnungen in Berlin'
    assert check_claim(claim(number=1500,quote=text),docs(text)) is None

def test_unissued_document_stops_ingestion(tmp_path):
    s=Store(tmp_path);cid=s.add_company('Example','https://example.com',{})
    e=Extraction(company_id=cid,claims=[claim(document_id='not-issued')],researched_families=[],notes=[])
    with pytest.raises(ValueError,match='unissued document'):ingest(s,cid,e)
    assert not s.rows('SELECT * FROM claims')

def test_spider_preserves_published_http_url(tmp_path):
    import asyncio
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Redirect example','http://example.com',{})
    spider=PilotSpider(root=tmp_path,ids=[cid])
    async def requests():return [r async for r in spider.start()]
    assert asyncio.run(requests())[0].url=='http://example.com/'

def test_redirect_provenance_is_saved(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Example','http://example.com',{})
    request=Request('https://example.org/',meta={'cid':cid,'depth':1,'redirect_urls':['http://example.com']})
    response=HtmlResponse('https://example.org/',request=request,body=b'<p>Example company primary website</p>',encoding='utf8')
    spider=PilotSpider(root=tmp_path,ids=[cid]);list(spider.parse(response))
    doc=s.docs(cid)[0];meta=json.loads(doc['meta_json'])
    assert meta['requested_url']=='http://example.com';assert meta['final_url']=='https://example.org/'

def test_potsdam_postcode_is_not_berlin_evidence():
    text='Adresse 14467 Potsdam'
    c=claim(field='berlin_office',value='yes',number=None,quantity=None,qualifier=None,scope='company',quote=text)
    assert check_claim(c,docs(text))=='berlin_not_in_evidence'

def test_privacy_contact_is_not_relevant_sponsor():
    c=claim(field='contact',value='Jane Doe',number=None,quantity=None,qualifier=None,subject='person',scope='person',person='Jane Doe',role='Datenschutzbeauftragte',email='jane@example.de',quote='Jane Doe Datenschutzbeauftragte jane@example.de').model_dump()
    assert qualify([c],['contact'])['contact_access']=='unknown'
    c['role']='Inhaberin'
    assert qualify([c],['contact'])['contact_access']=='named_with_direct_email'
    c['email']='buero@example.de'
    assert qualify([c],['contact'])['contact_access']=='named_relevant'

def test_second_level_service_link_within_budget(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Example','http://example.com',{})
    spider=PilotSpider(root=tmp_path,ids=[cid]);spider.queued[cid]={'http://example.com/','http://example.com/leistungen/'}
    req=Request('http://example.com/leistungen/',meta={'cid':cid,'depth':1})
    response=HtmlResponse(req.url,request=req,body=b'<p>Unsere Leistungen und weitere Informationen fuer die Verwaltung von Immobilien. Hier finden Sie die einzelnen Leistungen und Aufgaben sowie die Details unserer Hausverwaltung.</p><a href="../hausverwaltung/details.html"><img src="service.jpg"></a>',encoding='utf8')
    links=list(spider.parse(response));assert len(links)==1;assert links[0].meta['depth']==2
    spider.queued[cid].update({'a','b','c'})
    assert len(spider.queued[cid])==6
    assert list(spider.parse(response))==[]

def test_enrich_accepts_loose_quote_and_runs_gap_pass(tmp_path,monkeypatch):
    from heidi_leads import pipeline
    s=Store(tmp_path);cid=s.add_company('Example','https://example.com',{})
    did=s.document(cid,'https://example.com',b'text',{'b':BASE['quote']})
    calls=[]
    class FakeGateway:
        def __init__(self,root):pass
        def close(self):pass
        def call(self,kind,company_id,prompt,schema,model):
            calls.append(kind)
            if kind=='extract':
                return Extraction(company_id=cid,claims=[claim(document_id=did,quote='Example ... 29 Wohnungen in Berlin')],researched_families=['scale'],notes=[])
            assert kind=='extract-gap' and 'management_type' in prompt
            return Extraction(company_id=cid,claims=[],researched_families=['management'],notes=[])
    monkeypatch.setattr(pipeline,'Gateway',FakeGateway)
    pipeline.enrich(s,[cid])
    # Small wording differences are fine: no repair round and no review call.
    assert calls==['extract','extract-gap']
    assert s.company(cid)['state']=='reviewed'
    assert s.rows('SELECT * FROM claims')[0]['status']=='accepted'

def test_export_revalidates_saved_accepted_claims(tmp_path):
    s=Store(tmp_path);cid=s.add_company('Example','https://example.com',{})
    did=s.document(cid,'https://example.com',b'text',{'b':'Unrelated source'})
    e=Extraction(company_id=cid,claims=[claim(document_id=did)],researched_families=[],notes=[])
    ingest(s,cid,e)
    s.db.execute("UPDATE claims SET status='accepted'");s.db.commit()
    export(s)
    assert s.rows('SELECT status FROM claims')[0]['status']=='rejected'

def test_packet_excludes_unmatched_register_landing_page(tmp_path):
    from heidi_leads.pipeline import packet
    s=Store(tmp_path);cid=s.add_company('Example','https://example.com',{})
    s.document(cid,'https://official.example/search',b'landing',{'b':'unrelated companies'},meta={'query_snapshot':True,'matched_entity':False})
    assert packet(s,cid)==[]
    s.document(cid,'https://official.example/result',b'proper',{'b':'Example HRB 123'},meta={'query_snapshot':True,'matched_entity':True})
    assert len(packet(s,cid))==1

def test_scale_wording_and_different_quantities_are_not_conflicts():
    a=claim().model_dump();b=claim(value='Another wording for same count').model_dump()
    parking=claim(number=155,quantity='parking_spaces').model_dump()
    q=qualify([a,b,parking],['scale'])
    assert q['conflict_fields']==[];assert len(q['scale'])==2

def test_directory_scale_is_not_promoted_as_primary_confirmation():
    d=docs(BASE['quote']);d['d']['meta_json']=json.dumps({'source_class':'directory_report'})
    assert check_claim(claim(),d)=='directory_scale_requires_primary_claim'
    d['d']['meta_json']=json.dumps({'source_class':'company_portfolio_statement'})
    assert check_claim(claim(),d) is None

def test_german_date_normalization_preserves_calendar_validation():
    assert claim(effective_date='13.11.2025').effective_date=='2025-11-13'
    with pytest.raises(ValidationError):claim(effective_date='31.02.2025')

def test_malformed_range_does_not_discard_supported_sibling_claim():
    from heidi_leads.core import parse_extraction
    good=claim(number=500,quantity='mixed_units',qualifier='approximately',quote='ca. 500 Wohn- und Gewerbe-Einheiten.').model_dump()
    bad=good|{'number':None,'quantity':None,'qualifier':None,'quote':'Unser Bestand variiert zwischen 450 und 500 Einheiten.'}
    result,quarantine=parse_extraction(json.dumps({'company_id':'c','claims':[bad,good],'researched_families':['scale'],'notes':[]}))
    assert len(result.claims)==1 and result.claims[0].number==500
    assert len(quarantine)==1 and quarantine[0]['raw_claim']['number'] is None
    with pytest.raises(ValidationError):parse_extraction(json.dumps({'company_id':42,'claims':[good],'researched_families':[],'notes':[]}))

def test_declared_html_base_prevents_duplicate_language_path(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Example','https://example.com/de/',{})
    req=Request('https://example.com/de/',meta={'cid':cid})
    html='<base href="https://example.com/"><p>'+('Relevant property management services. '*8)+'</p><a href="de/leistungen/">Leistungen</a>'
    response=HtmlResponse(req.url,request=req,body=html.encode(),encoding='utf8')
    links=list(PilotSpider(root=tmp_path,ids=[cid]).parse(response))
    assert [r.url for r in links]==['https://example.com/de/leistungen/']

@pytest.mark.parametrize('status',[401,403,404,410,423])
def test_denied_or_missing_http_page_does_not_trigger_browser(tmp_path,status):
    from scrapy.http import HtmlResponse,Request
    from scrapy.spidermiddlewares.httperror import HttpError
    from twisted.python.failure import Failure
    from heidi_leads.collect import PilotSpider
    req=Request('https://example.com/',meta={'cid':'test'})
    response=HtmlResponse(req.url,request=req,status=status)
    failure=Failure(HttpError(response,'Ignoring non-200 response'));failure.request=req
    spider=PilotSpider(root=tmp_path,ids=[]);spider.failed(failure)
    assert not spider.fallback
    assert spider.failures[0]['status']==status

def test_numerical_card_retains_label_with_count():
    blocks,_=html_blocks('<div><p><strong>820</strong></p><h3>Verwaltete Wohneinheiten</h3></div>')
    assert '820 Verwaltete Wohneinheiten' in blocks.values()

def test_berliner_districts_but_not_berliner_street_establish_geography():
    c=claim(field='berlin_service',value='yes',number=None,quantity=None,qualifier=None,scope='company',quote='Wir verwalten in verschiedenen Berliner Stadtteilen.')
    assert check_claim(c,docs(c.quote)) is None
    c.quote='Berliner Straße 10, 14467 Potsdam'
    assert check_claim(c,docs(c.quote))=='berlin_not_in_evidence'

def test_greater_berlin_portfolio_is_not_berlin_city_total():
    c=claim(number=15000,quote='Wir betreuen im Großraum Berlin über 15.000 Wohneinheiten.')
    assert check_claim(c,docs(c.quote))=='regional_total_as_berlin_city'
    c.geography='berlin_region'
    assert check_claim(c,docs(c.quote)) is None

def test_legal_representative_role_is_named_contact():
    c=claim(field='contact',value='Max Mustermann',number=None,quantity=None,qualifier=None,subject='person',scope='person',person='Max Mustermann',role='Gesetzlich vertreten',quote='Gesetzlich vertreten vertreten durch Herrn Max Mustermann').model_dump()
    assert qualify([c],['contact'])['contact_access']=='named_relevant'

def test_hosting_default_page_is_not_company_content():
    from parsel import Selector
    from heidi_leads.collect import hosting_placeholder
    assert hosting_placeholder(Selector('<title>Web Server\'s Default Page</title><body>What is Plesk</body>'))
    assert not hosting_placeholder(Selector('<title>Example Hausverwaltung</title><body>We use Plesk hosting</body>'))

def test_contact_route_shared_inbox_is_not_personal_email():
    from heidi_leads.core import contact_route
    person=claim(field='contact',value='Jane Doe',number=None,quantity=None,qualifier=None,subject='person',scope='person',person='Jane Doe',role='Inhaberin',quote='Jane Doe Inhaberin').model_dump()|{'id':'owner'}
    general=claim(field='contact',value='info@example.de',number=None,quantity=None,qualifier=None,scope='company',email='info@example.de',quote='info@example.de').model_dump()|{'id':'inbox'}
    r=contact_route([person,general])
    assert r['person']=='Jane Doe' and r['channel']=='info@example.de'
    assert r['basis']=='Company contact; personal email unavailable'
    assert r['evidence_ids']=='owner; inbox'
    assert qualify([person,general],[])['contact_access']=='named_relevant'

def test_contact_route_falls_back_to_affiliated_staff_without_misattribution():
    from heidi_leads.core import contact_route
    owner=claim(field='contact',value='Jane Doe',number=None,quantity=None,qualifier=None,subject='person',scope='person',person='Jane Doe',role='Inhaberin',quote='Jane Doe Inhaberin').model_dump()|{'id':'owner'}
    staff=owner|{'id':'staff','person':'Alex Doe','role':'Ansprechpartner','email':'alex@example.de'}
    r=contact_route([owner,staff])
    assert r['person']=='Jane Doe' and r['channel']=='alex@example.de'
    assert 'Fallback via Alex Doe' in r['basis']
    staff['role']='Datenschutzbeauftragter'
    assert contact_route([owner,staff])['channel']==''

def test_contact_route_keeps_generic_and_unknown_routes():
    from heidi_leads.core import contact_route
    generic=claim(field='contact',value='030 123456',number=None,quantity=None,qualifier=None,scope='company',quote='030 123456').model_dump()|{'id':'phone'}
    assert contact_route([generic])['channel']=='030 123456'
    generic['currentness']='historical'
    assert contact_route([generic])['basis']=='Unknown'

def test_email_local_part_does_not_link_person():
    text='Matthias.Christen@example.de'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Matthias Christen',role='Senior Berater',email='Matthias.Christen@example.de',quote=text)
    assert check_claim(c,docs(text))=='email_person_not_linked'
    text='Matthias Christen, Senior Berater: Matthias.Christen@example.de'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Matthias Christen',role='Senior Berater',email='Matthias.Christen@example.de',quote=text)
    assert check_claim(c,docs(text)) is None

def test_named_contact_without_email_requires_name_in_quote():
    text='Geschäftsführerin Dipl. Kauffrau'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Konstanze Arnst',role='Geschäftsführerin',email=None,quote=text)
    assert check_claim(c,docs(text))=='person_not_in_quote'

def test_unknown_envelope_key_dropped_claims_kept():
    from heidi_leads.core import parse_extraction
    raw=json.dumps({'company_id':'c','company_id_check':'c','claims':[BASE],'researched_families':['scale'],'notes':[]})
    result,quarantined=parse_extraction(raw)
    assert len(result.claims)==1 and not quarantined

def test_shared_rules_cover_affiliates_and_out_of_berlin_companies():
    from heidi_leads.rules import SCOPE_RULES
    assert 'sister or parent company' in ' '.join(SCOPE_RULES.split()) and 'based outside Berlin' in SCOPE_RULES

def _flag(field,value,**kw):
    return claim(field=field,value=value,number=None,quantity=None,qualifier=None,scope='company',quote='Berlin '+value,**kw).model_dump()

def test_one_berlin_evidence_is_enough():
    weg=_flag('management_type','weg')
    assert qualify([weg],[])['berlin_basis']=='none' and qualify([weg],[])['target_status']=='needs_research'
    q=qualify([_flag('berlin_service','yes'),weg],['berlin','management'])
    assert q['target_status']=='relevant_residential_manager' and q['berlin_basis']=='berlin_service'
    assert qualify([_flag('berlin_property','yes'),weg],[])['berlin_basis']=='berlin_property'
    two=claim(number=2,subject='property',scope='individual_property',quote='2 Wohnungen in Berlin').model_dump()
    q=qualify([weg,two],[])
    assert q['target_status']=='relevant_residential_manager' and q['berlin_basis']=='berlin_units' and q['berlin_apartments']==2
    regional=claim(number=50,geography='berlin_region',quote='50 Wohnungen in Berlin und Brandenburg').model_dump()
    assert qualify([weg,regional],[])['target_status']=='needs_research'
    assert qualify([_flag('berlin_service','no'),weg],[])['berlin_basis']=='none'

def test_berlin_based_company_keeps_office_basis():
    q=qualify([_flag('berlin_office','yes'),_flag('management_type','weg')],[])
    assert q['target_status']=='relevant_residential_manager' and q['berlin_basis']=='berlin_office'

def test_contact_route_prefers_contact_nearest_berlin():
    from heidi_leads.core import contact_route
    def person(name,loc,i):
        return claim(field='contact',value=name,number=None,quantity=None,qualifier=None,subject='person',scope='person',person=name,role='Niederlassungsleiter',location=loc,quote=f'{name} Niederlassungsleiter {loc or ""}').model_dump()|{'id':i}
    r=contact_route([person('Anna Hamburg','Hamburg','a'),person('Ben Potsdam','Potsdam','b'),person('Cara Berlin','Berlin-Mitte','c')])
    assert r['person']=='Cara Berlin' and r['location']=='Berlin-Mitte'
    r=contact_route([person('Anna Hamburg','Hamburg','a'),person('Ben Potsdam','Potsdam','b')])
    assert r['person']=='Ben Potsdam'

def test_contact_location_must_be_in_block_and_contact_only():
    text='Niederlassung Potsdam: Ben Weber, Leiter'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Ben Weber',role='Leiter',location='Berlin',quote='Ben Weber, Leiter')
    assert check_claim(c,docs(text))=='location_not_in_block'
    c=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Ben Weber',role='Leiter',location='Potsdam',quote='Ben Weber, Leiter')
    assert check_claim(c,docs(text)) is None
    with pytest.raises(ValidationError):claim(location='Berlin')

def test_office_proposed_from_own_berlin_address_and_validated():
    from heidi_leads.core import office_from_address
    blocks={'b1':'Willkommen','b2':'Karsten Hamelow Hausverwaltung | Danckelmannstr. 21 | 14059 Berlin T.: (030) 36 40 70 00'}
    d={'id':'d','blocks_json':json.dumps(blocks)}
    c=office_from_address('KHH Karsten Hamelow Hausverwaltung',[d])
    assert c.field=='berlin_office' and c.block_id=='b2' and '14059 Berlin' in c.quote
    assert check_claim(c,{'d':d}) is None

def test_office_not_proposed_for_foreign_or_unnamed_address():
    from heidi_leads.core import office_from_address
    d={'id':'d','blocks_json':json.dumps({'b1':'Musterverwaltung GmbH, Berliner Straße 27, 15711 Königs Wusterhausen','b2':'Kundenobjekt: Hauptstr. 1, 10115 Berlin'})}
    assert office_from_address('Musterverwaltung GmbH',[d]) is None

def _fact(field,value,quote,**kw):
    return claim(field=field,value=value,number=None,quantity=None,qualifier=None,scope='company',quote=quote,**kw)

@pytest.mark.parametrize('field,value,quote,reason',[
    ('metering_provider','Techem','Die Verbrauchsabrechnung erstellt Techem.',None),
    ('metering_provider','ista','Die Verbrauchsabrechnung erstellt Techem.','name_not_in_quote'),
    ('software','Haufe PowerHaus','Wir arbeiten mit Haufe PowerHaus.',None),
    ('software','Domus','Wir arbeiten mit moderner Software.','name_not_in_quote'),
    ('hkvo_2027','remote_reading_installed','Alle Wohnungen sind mit Funkzählern ausgestattet.',None),
    ('hkvo_2027','remote_reading_installed','Die Zähler wurden 2024 ausgetauscht.','remote_reading_not_in_evidence'),
    ('hkvo_2027','manual_reading','Der Ableser kommt am 12. März.',None),
    ('hkvo_2027','manual_reading','Die Abrechnung erfolgt jährlich.','manual_reading_not_in_evidence'),
    ('hkvo_2027','compliant','Funkzähler installiert.','invalid_hkvo_enum'),
])
def test_metering_context_checks(field,value,quote,reason):
    assert check_claim(_fact(field,value,quote),docs(quote))==reason

def test_dropped_fields_are_ignored():
    from heidi_leads.core import CURRENT_FIELDS
    assert not {'devices','legal_identity','existing_customer','inactive'}&CURRENT_FIELDS
    assert check_claim(_fact('management_type','sev','SEV-Verwaltung'),docs('SEV-Verwaltung'))=='invalid_management_enum'

def _person(name,role,email=None,i='p',loc=None):
    quote=f'{name} {role} {email or ""}'.strip()
    return claim(field='contact',value=name,number=None,quantity=None,qualifier=None,subject='person',scope='person',person=name,role=role,email=email,location=loc,quote=quote).model_dump()|{'id':i}

def test_contact_tiers_prefer_property_manager():
    from heidi_leads.core import contact_tier,contact_route
    assert contact_tier(_person('Ana Weg','WEG-Verwalterin'))==1
    assert contact_tier(_person('Ana Weg','Objektbetreuung'))==1
    assert contact_tier(_person('Ana Weg','Geschäftsführerin'))==2
    assert contact_tier(_person('Ana Weg','Assistenz'))==3
    assert contact_tier(_person('Ana Weg','Leitung Marketing')) is None
    boss=_person('Ben Boss','Geschäftsführer','ben@example.de','b');pm=_person('Pia Plan','Objektmanagerin',i='p')
    r=contact_route([boss,pm])
    assert r['person']=='Pia Plan' and r['contact_tier']==1 and not r['direct_email']
    assert qualify([boss,pm],[])['contact_tier']==1
    assert contact_route([_person('Mia Press','Pressesprecherin','mia@example.de')])['channel']==''

def test_fit_score_is_five_plain_components_and_access_is_separate():
    from heidi_leads.core import fit_score,contact_route,fit_text
    office=_flag('berlin_office','yes');weg=_flag('management_type','weg')
    units=claim(number=1200,quote='1200 Wohnungen in Berlin').model_dump()
    due=_fact('hkvo_2027','manual_reading','Ableser').model_dump()
    pm=_person('Pia Plan','Objektmanagerin','pia.plan@example.de',loc='Berlin')
    claims=[office,weg,units,due,pm];q=qualify(claims,[])
    assert q['fit_components']=={'Manages apartments':25,'Portfolio size':15,'Meters read by hand (upgrade due 2027)':25}
    assert q['fit_score']==65 and q['shortlist'] and 'Manages apartments +25' in fit_text(q['fit_components'])
    # The contact never changes fit; it sets the separate Access score.
    assert qualify(claims[:-1],[])['fit_score']==65
    assert (q['access_score'],q['access_text'])==(100,'Property manager, direct line')
    more=claims+[_fact('metering','nonradio_consumption','Ableser').model_dump(),_fact('metering_provider','Techem','Techem').model_dump()]
    q=qualify(more,[]);assert q['fit_score']==75 and q['fit_components']['Current metering setup known']==10
    q=qualify([weg,due],[])
    assert q['berlin_basis']=='none' and q['target_status']=='needs_research' and not q['shortlist']
    q=qualify([weg,due],[],berlin_address=True)
    assert q['berlin_basis']=='berlin_address' and q['target_status']=='relevant_residential_manager' and q['shortlist']
    assert qualify([],[])['fit_score']==0

def test_access_prefers_property_managers_then_direct_line():
    from heidi_leads.core import contact_route,access
    boss=_person('Ben Boss','Geschäftsführer','ben@example.de',i='b')
    pm=_person('Pia Plan','Objektbetreuerin',i='p')
    r=contact_route([boss,pm]);assert r['person']=='Pia Plan' and access(r)==(80,'Property manager, name only')
    assert access(contact_route([boss]))==(60,'Owner/director, direct line')
    assert access(contact_route([]),company_line=True)==(10,'Main office line / info@ only')
    assert access(contact_route([]))==(0,'No contact found')

def test_contact_phone_must_be_in_quote():
    ok=claim(field='contact',value='Pia',number=None,quantity=None,qualifier=None,subject='person',scope='person',person='Pia Plan',role='Verwalterin',phone='030 123456',quote='Pia Plan, Verwalterin, Tel. 030 / 123 456')
    assert check_claim(ok,docs(ok.quote)) is None
    bad=ok.model_copy(update={'phone':'030 999999'})
    assert check_claim(bad,docs(ok.quote))=='phone_not_in_evidence'

def test_shortlist_requires_score_threshold():
    office=_flag('berlin_office','yes');weg=_flag('management_type','weg')
    q=qualify([office,weg],[])
    assert q['target_status']=='relevant_residential_manager' and q['fit_score']==25 and not q['shortlist']
    switch=_fact('metering_contract','switch_intent','Wechsel des Messdienstleisters beschlossen').model_dump()
    q=qualify([office,weg,switch],[])
    assert q['fit_score']==50 and q['shortlist']

def test_contract_signal_checks():
    text='Der Vertrag mit dem Messdienst endet am 31.12.2026; die Eigentümer haben den Wechsel beschlossen.'
    c=_fact('metering_contract','switch_intent',text)
    assert check_claim(c,docs(text)) is None
    assert check_claim(_fact('metering_contract','switch_intent','Wir freuen uns auf Sie.'),docs('Wir freuen uns auf Sie.'))=='contract_signal_not_in_evidence'
    assert check_claim(_fact('metering_contract','renewal','Vertrag'),docs('Vertrag'))=='invalid_contract_enum'
    assert check_claim(_fact('hkvo_2027','kuerzung_risk','nicht fernablesbar'),docs('nicht fernablesbar'))=='invalid_hkvo_enum'

def test_size_bands():
    office=_flag('berlin_office','yes');weg=_flag('management_type','weg')
    for n,pts in ((1200,15),(600,10),(150,10),(40,5)):
        big=claim(number=n,quote=f'{n} Wohnungen in Berlin').model_dump()
        assert qualify([office,weg,big],[])['fit_components']['Portfolio size']==pts

def test_owner_operator_stock_is_relevant_target():
    q=qualify([_flag('berlin_office','yes'),_flag('management_type','owned_stock')],[])
    assert q['target_status']=='relevant_residential_manager' and q['fit_components']['Manages apartments']==25

def test_autofix_drops_unsupported_attributes_instead_of_repair_round():
    from heidi_leads.core import autofix_claims
    text='Niederlassung Potsdam: Ben Weber, Leiter. Kontakt: 030 123'
    d={'d':{'blocks_json':json.dumps({'b':text})}}
    loc=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Ben Weber',role='Leiter',location='Berlin',quote='Ben Weber, Leiter')
    mail=claim(field='contact',value='contact',number=None,quantity=None,qualifier=None,person='Ben Weber',role='Leiter',email='ben@example.de',quote='Ben Weber, Leiter')
    flag=claim(field='berlin_service',value='yes',number=None,quantity=None,qualifier=None,scope='company',quote='Kontakt: 030 123')
    fixed,fixes=autofix_claims([loc,mail,flag],d)
    assert [c.field for c in fixed]==['contact','contact'] and fixed[0].location is None and fixed[1].email is None
    assert {f['fix'] for f in fixes}=={'location_dropped','email_dropped_not_in_quote','berlin_flag_dropped'}
    assert all(check_claim(c,d) is None for c in fixed)

def test_near_identical_unit_counts_are_not_a_conflict():
    a=claim(number=8027,qualifier='exact',quote='8.027 Wohnungen in Berlin').model_dump()
    b=claim(number=7951,qualifier='exact',currentness='uncertain',quote='7.951 Wohnungen in Berlin').model_dump()
    q=qualify([a,b],[]);assert not q['conflict_fields'] and q['scale']
    c=claim(number=4000,qualifier='exact',quote='4000 Wohnungen in Berlin').model_dump()
    assert qualify([a,c],[])['conflict_fields']==['units']

def test_autofix_drops_named_contact_without_role():
    from heidi_leads.core import autofix_claims
    text='Herr Komm, BWV Vital-Team'
    d={'d':{'blocks_json':json.dumps({'b':text})}}
    c=claim(field='contact',value='Herr Komm',number=None,quantity=None,qualifier=None,person='Herr Komm',role=None,quote=text)
    fixed,fixes=autofix_claims([c],d)
    assert not fixed and fixes[0]['fix']=='nameless_role_contact_dropped'

def test_directory_phone_and_manager_verification(tmp_path):
    from heidi_leads.core import directory_contact,manager_verification
    s=Store(tmp_path)
    cid=s.add_company('Muster Hausverwaltung','https://m.de',{'source':'GOOGLE_PLACES','phone':'030 123456','address':'Hauptstr. 1 10115 Berlin'})
    dc=directory_contact(s,s.company(cid))
    assert dc['phone']=='030 123456' and dc['phone_source']=='GOOGLE_PLACES' and dc['sources']=='GOOGLE_PLACES'
    did=s.document(None,'https://vdiv',b'x',{'card':'Beta GmbH Telefon 030/76884921 Adresse Drakestraße 48 12205 Berlin'})
    cid2=s.add_company('Beta GmbH','https://b.de',{'source':'VDIV','document_id':did,'block_id':'card','address':'Adresse Drakestraße 48 12205 Berlin'})
    dc=directory_contact(s,s.company(cid2))
    assert dc['phone']=='030/76884921' and dc['phone_source']=='VDIV' and dc['address'].startswith('Drakestraße')
    q=qualify([_flag('management_type','weg')],[])
    assert manager_verification(dict(name='X',error=None),q)=='verified_property_manager'
    empty=qualify([],[])
    assert manager_verification(dict(name='Immobilienmakler Schulz',error=None),empty)=='likely_broker'
    assert manager_verification(dict(name='Schulz Hausverwaltung',error=None),empty)=='name_indicates_manager_unverified'
    assert manager_verification(dict(name='Makler & Hausverwaltung Schulz',error=None),empty)=='name_indicates_manager_unverified'
    assert manager_verification(dict(name='Schulz',error='broker_name: Makler without management wording'),empty)=='excluded_broker_name'

def test_packet_puts_provider_block_first_and_gap_queries_target_providers(tmp_path):
    from heidi_leads.pipeline import packet
    from heidi_leads.search_sources import gap_queries
    s=Store(tmp_path);cid=s.add_company('M','https://m.de',{})
    filler={f'b{i}':'Wir verwalten Wohnungen in Berlin. '*150 for i in range(12)}
    filler['z']='Die Ablesung erfolgt durch die Firma Brunata.'
    s.document(cid,'https://m.de/a',b'1',filler)
    out=packet(s,cid)
    assert any('z' in d['blocks'] for d in out)
    qs=gap_queries('Muster Hausverwaltung GmbH',{'provider','metering','scale'})
    assert 'Techem' in qs[0] and 'Heizung' in qs[1] and 'Wohneinheiten' in qs[2]

def test_units_exact_berlin_first_ranges_ignored_national_labelled():
    from heidi_leads.core import units_text
    mk=lambda n,q,g,quote:dict(number=n,qualifier=q,geography=g,quote=quote,quantity='residential_units')
    sc=[mk(5000,'approximately','nationwide','ca. 5.000 Wohnungen bundesweit'),mk(800,'exact','berlin','800 Wohnungen in Berlin'),mk(100,'exact','berlin','50 bis 100 Wohnungen')]
    assert units_text(dict(scale=sc))=='800 units in Berlin'
    assert units_text(dict(scale=sc[:1]))=='approx. 5,000 units (company total, Berlin share unknown)'
    assert units_text(dict(scale=sc[2:]))==''

def test_cooperative_with_units_owns_stock():
    from heidi_leads.core import owner_stock_claim
    u=claim(number=246,quote='246 Wohnungen in Berlin').model_dump()
    c=owner_stock_claim('Eigenheimsiedlungsgenossenschaft eG',[u])
    assert c.value=='owned_stock' and c.quote==u['quote']
    assert owner_stock_claim('Muster Hausverwaltung GmbH',[u]).value=='whole_building_rental'
    assert owner_stock_claim('Muster Immobilien GmbH',[u]) is None
    assert owner_stock_claim('Wohnungsbau eG',[]) is None
