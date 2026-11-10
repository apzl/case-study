import json
from pathlib import Path
from heidi_leads.core import Store, Claim, check_claim
from heidi_leads.collect import html_blocks
from heidi_leads.pipeline import packet


def test_nested_spans_keep_service_territory():
    text='Auf Wunsch erstellen wir ein unverbindliches Angebot für die Verwaltung Ihrer Immobilie in Berlin bzw. Brandenburg.'
    blocks,_=html_blocks('<div><div>Intro</div><span><span>'+text+'</span></span></div>')
    assert text in blocks.values()


def test_long_block_is_excerpted_without_changing_original_locator(tmp_path):
    s=Store(tmp_path);cid=s.add_company('Manager','https://example.org',{})
    text=('General context. '*400)+' Wir verwalten Wohnhäuser in Berlin. '+('Further context. '*400)
    did=s.document(cid,'https://example.org',b'original',{'long':text})
    docs=packet(s,cid)
    fragments=docs[0]['excerpts']
    assert any('Wohnhäuser' in p['text'] for p in fragments)
    assert all(p['text']==text[p['start']:p['end']] and p['block_id']=='long' for p in fragments)
    manifest=json.loads((tmp_path/'runs/packets'/f'{cid}.json').read_text())
    assert manifest['characters']<=40000
    assert {f['reason'] for f in manifest['fragments']}=={'included'}


def test_budget_omissions_are_explicit_and_service_beats_legal(tmp_path):
    s=Store(tmp_path);cid=s.add_company('Manager','https://example.org',{})
    s.document(cid,'https://example.org/impressum',b'legal',{'b':('Legal metadata. '*4000)})
    service='Unsere Leistungen: WEG-Verwaltung und Miethausverwaltung in Berlin.'
    did=s.document(cid,'https://example.org/leistungen',b'service',{'b':service})
    out=packet(s,cid)
    assert any(d['document_id']==did and service in d['blocks'].values() for d in out)
    m=json.loads((tmp_path/'runs/packets'/f'{cid}.json').read_text())
    assert m['characters']<=40000
    assert any(f['reason'] in ('duplicate','budget') for f in m['fragments'])


def test_targeted_collection_does_not_expand_links(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Manager','https://example.org',{})
    req=Request('https://example.org/one',meta={'cid':cid})
    body=('<p>'+('Relevant service text. '*20)+'</p><a href="/leistungen">Leistungen</a>').encode()
    response=HtmlResponse(req.url,request=req,body=body,encoding='utf8')
    spider=PilotSpider(root=tmp_path,ids=[cid],targets={cid:[req.url]})
    assert list(spider.parse(response))==[]

def test_targeted_collection_still_crawls_homepage_with_links(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Manager','https://example.org',{})
    spider=PilotSpider(root=tmp_path,ids=[cid],targets={cid:['https://portal.example/expose/1']},kinds={'https://portal.example/expose/1':'listing'})
    import asyncio
    async def starts():
        return [r async for r in spider.start()]
    reqs=asyncio.run(starts())
    assert [r.url.rstrip('/') for r in reqs]==['https://example.org','https://portal.example/expose/1']
    assert reqs[0].meta['follow'] is True and reqs[1].meta['follow'] is False
    req=Request('https://example.org',meta={'cid':cid,'follow':True})
    body=('<p>'+('Relevant service text. '*20)+'</p><a href="/leistungen">Leistungen</a>').encode()
    assert len(list(spider.parse(HtmlResponse(req.url,request=req,body=body,encoding='utf8'))))==1
    listing=Request('https://portal.example/expose/1',meta={'cid':cid,'follow':False})
    assert spider.source_class(HtmlResponse(listing.url,request=listing,body=body,encoding='utf8'))=='rental_listing'

def test_portal_and_provider_links_kept_as_literal_block():
    from heidi_leads.collect import html_blocks
    html='<p>Willkommen</p><a href="https://mieter.casavi.de/login">Mieterportal</a><a href="/datenschutz">Datenschutz</a><a href="https://www.techem.de">Ablesung</a>'
    blocks,_=html_blocks(html)
    assert 'casavi.de' in blocks['links_vocab'] and 'techem.de' in blocks['links_vocab'] and 'datenschutz' not in blocks['links_vocab']

def test_tenant_service_page_gets_reserved_slot(tmp_path):
    from scrapy.http import HtmlResponse,Request
    from heidi_leads.collect import PilotSpider
    s=Store(tmp_path);cid=s.add_company('Manager','https://example.org',{})
    links=''.join(f'<a href="/{p}">{p}</a>' for p in ['leistungen','weg-verwaltung','referenzen','portfolio','unternehmen','impressum','kontakt','team','mieterservice'])
    req=Request('https://example.org',meta={'cid':cid,'follow':True})
    body=('<p>'+('Relevant service text. '*20)+'</p>'+links).encode()
    spider=PilotSpider(root=tmp_path,ids=[cid])
    urls=[r.url for r in spider.parse(HtmlResponse(req.url,request=req,body=body,encoding='utf8'))]
    assert 'https://example.org/mieterservice' in urls and len(urls)==6
