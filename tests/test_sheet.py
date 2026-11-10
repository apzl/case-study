import json
import pytest
from openpyxl import load_workbook
from heidi_leads.core import Store,berlin_district,directory_profile,general_email,lead_tier,provisional_score
from heidi_leads.pipeline import COMPANY_HEADERS,export

def company(name,url='https://example.de',**src):
    base=dict(source='GOOGLE_PLACES',address='Musterstraße 1 10115 Berlin',postcode='10115',phone='030 123456',place_types=['real_estate_agency'])
    return dict(name=name,url=url,raw_json=json.dumps([base|src]))

def test_provisional_full_marks():
    assert provisional_score(company('Muster Hausverwaltung GmbH'))==(30,{'Name says property manager':15,'Listed as property office on the map':5,'Has a website':5,'Phone listed':5})

def test_provisional_partial_and_osm():
    c=company('Muster GmbH',url=None,phone=None,place_types=['office=property_management'])
    assert provisional_score(c)[0]==5  # map category only; Berlin is a filter, not points
    c=company('WEG Service Schmidt',address='Hauptstr. 1 14467 Potsdam',postcode='14467',place_types=[])
    assert provisional_score(c)==(25,{'Name says property manager':15,'Has a website':5,'Phone listed':5})

def test_provisional_broker_zero():
    assert provisional_score(company('Immobilienmakler Schulz'))[0]==0
    assert provisional_score(company('Makler & Hausverwaltung Schulz'))[0]==30

@pytest.mark.parametrize('score,checked,excluded,tier',[
    (50,True,False,'A'),(49,True,False,'B'),(25,True,False,'B'),(24,True,False,'C'),(1,True,False,'C'),(0,True,False,'D'),
    (30,False,False,'C'),(20,False,False,'C'),(19,False,False,'D'),(90,True,True,'D')])
def test_tier_boundaries(score,checked,excluded,tier):assert lead_tier(score,checked,excluded)==tier

@pytest.mark.parametrize('postcode,district',[
    ('10115','Mitte'),('10997','Friedrichshain-Kreuzberg'),('12043','Neukölln'),('13353','Mitte'),
    ('14059','Charlottenburg-Wilmersdorf'),('12619','Marzahn-Hellersdorf'),('12529',''),('14467',''),('',None)])
def test_district(postcode,district):assert berlin_district(postcode)==(district or '')

def test_district_from_vdiv_card_address():
    c=dict(name='X',url='',raw_json=json.dumps([{'source':'VDIV','address':'Adresse Saatwinkler Damm 70 13627 Berlin'}]))
    assert directory_profile(None,c)['district']=='Charlottenburg-Wilmersdorf'

def contact(**kw):
    return dict(field='contact',value='Kontakt',person=None,role=None,email=None,location=None,currentness='current',id='c1')|kw

def test_general_email_fallbacks():
    profile=dict(emails=['datenschutz@a.de'],cards=['Muster GmbH info@muster.de Telefon 030 1'])
    assert general_email([contact(email='kontakt@firma.de')],profile)=='kontakt@firma.de'
    assert general_email([contact(email='anna@firma.de',person='Anna',role='Verwalterin')],profile)=='info@muster.de'
    assert general_email([],dict(emails=['welcome@x.de'],cards=[]))=='welcome@x.de'
    assert general_email([],{})==''

def test_export_slim_header(tmp_path):
    s=Store(tmp_path)
    s.add_company('Muster Hausverwaltung GmbH','https://muster.de',json.loads(company('x')['raw_json'])[0])
    s.add_company('Immobilienmakler Schulz','',{'source':'OSM','address':'Weg 2 12043 Berlin','postcode':'12043'})
    export(s)
    wb=load_workbook(tmp_path/'output/Heidi_Berlin_Pilot.xlsx')
    assert wb.sheetnames==['Leads','Contacts','Evidence','Details','Guide']
    rows=list(wb['Leads'].values)
    from heidi_leads.workbook import LEADS
    assert list(rows[0])==[h for _,h,_,_ in LEADS] and len(COMPANY_HEADERS)==33
    assert [(r[0],r[1],r[13]) for r in rows[1:]]==[('C','Muster Hausverwaltung GmbH','Mitte')]  # tier D stays on Details only
    details=load_workbook(tmp_path/'output/Heidi_Berlin_Pilot.xlsx')['Details']
    assert details.max_row==3 and details['A1'].value=='Company ID'


def test_sales_opener_is_specific_and_never_says_check_website():
    from heidi_leads.core import sales_opener
    q=dict(management_types=['weg'],scale=[],providers=[],metering=[])
    o=sales_opener(q,{'Manages apartments':25},True,'Anna Beispiel','Objektbetreuerin',True,'',"WEG-Verwalter (m/w/d)",True)
    assert o.startswith('Ask for Anna Beispiel (Objektbetreuerin).') and 'WEG owners' in o and 'hiring' in o
    o=sales_opener(dict(management_types=[],scale=[],providers=[],metering=[]),{},False,'','',True)
    assert 'website' not in o.lower() and o.startswith('Call the office')
