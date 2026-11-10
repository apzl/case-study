from __future__ import annotations
import asyncio, ipaddress, json, os, re, socket, subprocess, tempfile, shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from parsel import Selector
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.downloadermiddlewares.redirect import RedirectMiddleware
from .core import Store, domain, norm, digest

UA='HeidiCaseStudyResearch/0.1 (public property manager research)'

def public_url(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username: raise ValueError('invalid public URL')
    for address in socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80)):
        if not ipaddress.ip_address(address[4][0]).is_global: raise ValueError('non-public destination')
    return url

class PublicDestinations:
    def process_request(self,request,spider=None): public_url(request.url)

CHANNEL_LINK=re.compile(r'linkedin\.com/(?:company|in)/|xing\.com/(?:pages|profile|companies)/|/kontakt|/contact|kontaktformular',re.I)

def html_blocks(body):
    s=Selector(body.decode('utf-8',errors='replace') if isinstance(body,bytes) else body)
    for bad in s.xpath('//script|//style|//noscript|//svg'): bad.root.getparent().remove(bad.root)
    blocks={}
    # Preserve complete repeated reference cards before isolated paragraphs.
    for node in s.css('.references-list-entry, article'):
        text=norm(' '.join(node.xpath('.//text()').getall()))
        if 3<=len(text)<=6000: blocks[f'b{len(blocks)+1}']=text
    # Keep compact numerical cards together: isolated 820 and its heading lose meaning.
    for node in s.xpath('//h2|//h3|//p'):
        text=norm(' '.join(node.xpath('.//text()').getall()))
        if not re.search(r'verwaltete.*(?:einheiten|wohnungen)|wohneinheiten',text,re.I):continue
        parent=node.xpath('..')
        context=norm(' '.join(parent.xpath('.//text()').getall()))
        if len(context)<=350 and re.search(r'\d',context) and context not in blocks.values():
            blocks[f'b{len(blocks)+1}']=context
    for node in s.xpath('//h1|//h2|//h3|//p|//li[not(.//li)]|//tr|//address|//div[not(.//div) and not(.//p)]'):
        text=norm(' '.join(node.xpath('.//text()').getall()))
        if len(text)>=3 and text not in blocks.values(): blocks[f'b{len(blocks)+1}']=text
    # Page builders may put entire paragraphs in nested spans outside paragraph tags.
    for node in s.xpath('//span[not(.//span) and not(ancestor::p) and not(ancestor::li)]'):
        text=norm(' '.join(node.xpath('.//text()').getall()))
        if len(text)>=80 and not any(text in b for b in blocks.values()):blocks[f'b{len(blocks)+1}']=text
    # Preserve contact-card neighbourhood, mailto and contact roles together.
    # Outbound links to tenant portals, metering services or PM software name the provider
    # even when no sentence does (e.g. "Mieterportal" -> casavi.de). Kept as one literal block.
    from .core import VOCAB
    links=[]
    for a in s.xpath('//a[@href]'):
        href=a.attrib['href'];text=norm(' '.join(a.xpath('.//text()').getall()))
        # Provider/software names, plus the outreach channels Heidi's sales team uses
        # (LinkedIn, XING, contact form): kept as one literal block.
        if href.startswith(('http','/')) and (VOCAB.search(href+' '+text) or CHANNEL_LINK.search(href)):links.append(f'{text} [{href}]'.strip())
    if links:blocks['links_vocab']='Links: '+' | '.join(dict.fromkeys(links))[:3000]
    for node in s.xpath('//a[starts-with(@href,"mailto:")]'):
        text=norm(' '.join(node.xpath('../..//text()').getall())+' '+node.attrib['href'].removeprefix('mailto:'))
        if text and len(text)<5000: blocks[f'b{len(blocks)+1}']=text
    return blocks,s

def hosting_placeholder(selector):
    title=norm(selector.css('title::text').get() or '').casefold()
    text=norm(' '.join(selector.xpath('//body//text()').getall())).casefold()
    return title in ("web server's default page",'default web site page') and 'plesk' in text

def discover(store):
    path=store.root/'data/bootstrap/vdiv.html'
    body=path.read_bytes(); sel=Selector(body.decode())
    blocks={}; raw=[]
    for card in sel.css('.contact-list-main-elements'):
        name=norm(' '.join(card.css('.contact-list-contact-name::text').getall()))
        address=norm(' '.join(card.css('.contact-list-address ::text').getall()))
        url=card.css('.contact-list-custom-field-value a::attr(href)').get()
        if not name or not url: continue
        text=norm(' '.join(card.xpath('.//text()').getall()))
        bid=f'card{len(blocks)+1}'; blocks[bid]=text
        raw.append((name,url,address,bid))
    did=store.document(None,'https://vdiv-bb.de/mitglieder/',body,blocks,meta={'bootstrap':True,'file_mtime':path.stat().st_mtime})
    seen=set(); ids=[]
    for name,url,address,bid in raw:
        key=(norm(name).casefold(),domain(url),address)
        if key in seen: continue
        seen.add(key)
        cid=store.add_company(name,url,dict(document_id=did,block_id=bid,address=address,source='VDIV'))
        ids.append(cid)
    # Independently inspect the second discovery source without pretending to paginate it.
    portal=Selector((store.root/'data/bootstrap/portal.html').read_text())
    links=sorted(set(urljoin('https://deutsches-verwalterportal.de',u) for u in portal.css('a[href*="/hausverwaltung/berlin/"]::attr(href)').getall() if len(urlsplit(u).path.strip('/').split('/'))==4 and u.startswith('/de/')))
    report=dict(source='VDIV',all_name_cards=len(sel.css('.contact-list-contact-name')),cards_without_website=len(sel.css('.contact-list-contact-name'))-len(raw),raw_cards=len(raw),unique_candidates=len(ids),duplicate_cards=len(raw)-len(ids),berlin_address_candidates=sum('Berlin' in x[2] for x in raw)//2,coverage='single public members page; Brandenburg included',portal_profile_links=len(links),portal_coverage='listing inspected; profiles not crawled in this pilot')
    (store.root/'data/discovery-report.json').write_text(json.dumps(report,indent=2))
    (store.root/'data/portal-profile-links.json').write_text(json.dumps(links,indent=2))
    return ids

class PilotSpider(scrapy.Spider):
    name='heidi_pilot'
    custom_settings={'USER_AGENT':UA,'ROBOTSTXT_OBEY':True,'CONCURRENT_REQUESTS':6,'CONCURRENT_REQUESTS_PER_DOMAIN':1,'DOWNLOAD_DELAY':2,'DOWNLOAD_DELAY_JITTER':0,'DOWNLOAD_TIMEOUT':25,'DOWNLOAD_MAXSIZE':30*1024*1024,'RETRY_TIMES':2,'HTTPCACHE_ENABLED':True,'HTTPCACHE_DIR':'data/httpcache','LOG_LEVEL':'WARNING','TELNETCONSOLE_ENABLED':False,'DOWNLOADER_MIDDLEWARES':{'heidi_leads.collect.PublicDestinations':50},'REDIRECT_MAX_TIMES':4}
    # Search-discovered URL kinds (heidi_leads.search_sources) -> document source_class.
    KIND_CLASS={'listing':'rental_listing','document':'company_document','tenant_notice':'tenant_notice','provider_mention':'provider_mention','company_page':'company_page','alias_domain':'company_page','other':'external_page'}
    def __init__(self,root,ids,targets=None,kinds=None,**kw):
        super().__init__(**kw); self.store=Store(root); self.ids=ids;self.targets=targets;self.kinds=kinds or {};self.counts={};self.pdfs={};self.failures=[];self.fallback=[];self.queued={}
    def source_class(self,response):
        for url in [response.url]+list(response.meta.get('redirect_urls',[])):
            if url in self.kinds:return self.KIND_CLASS.get(self.kinds[url],'external_page')
        return 'company_page'
    async def start(self):
        for cid in self.ids:
            company=self.store.company(cid)
            # Preserve the published scheme; follow server redirects instead of guessing HTTPS.
            # The homepage is always crawled (with link following); search-discovered
            # targets are fetched as single pages so an empty search never blanks a company.
            home=[company['url']] if company['url'] else []
            extra=[u for u in (self.targets.get(cid,[]) if self.targets is not None else []) if u not in home]
            self.queued[cid]=set(home+extra)
            for url in home:
                yield scrapy.Request(url,callback=self.parse,errback=self.failed,meta={'cid':cid,'follow':True},dont_filter=True)
            for url in extra:
                yield scrapy.Request(url,callback=self.parse,errback=self.failed,meta={'cid':cid,'follow':False},dont_filter=True)
    def failed(self,failure):
        req=failure.request; reason=str(failure.value)
        response=getattr(failure.value,'response',None)
        status=response.status if response is not None else None
        self.failures.append(dict(company_id=req.meta['cid'],url=req.url,status=status,error=reason))
        # Missing pages and explicit access denials cannot be repaired by rendering.
        if 'robots' not in reason.casefold() and status not in (400,401,403,404,410,423):
            self.fallback.append({'cid':req.meta['cid'],'url':req.url})
    def parse(self,response):
        cid=response.meta['cid']; self.counts[cid]=self.counts.get(cid,0)+1
        ctype=response.headers.get('Content-Type',b'').decode()
        if 'pdf' in ctype or response.body.startswith(b'%PDF'):
            try:
                # delete=False then unlink: Windows blocks reopening a file that is still held open.
                with tempfile.NamedTemporaryFile(suffix='.pdf',delete=False) as f:
                    f.write(response.body);name=f.name
                try: blocks,method=pdf_blocks(Path(name),artifact_dir=self.store.root/'data/snapshots'/digest(response.body))
                finally: os.unlink(name)
                self.store.document(cid,response.url,response.body,blocks,method,meta={'source_class':self.source_class(response),'final_url':response.url})
            except Exception as e:self.failures.append(dict(company_id=cid,url=response.url,error=str(e)))
            return
        if len(response.body)>5*1024*1024:
            self.failures.append(dict(company_id=cid,url=response.url,error='html_too_large'));return
        blocks,sel=html_blocks(response.text)
        placeholder=hosting_placeholder(sel)
        self.store.document(cid,response.url,response.body,blocks,meta={'source_class':self.source_class(response),'status':response.status,'requested_url':response.meta.get('redirect_urls',[response.url])[0],'redirect_chain':response.meta.get('redirect_urls',[]),'final_url':response.url,'transport':urlsplit(response.url).scheme,'quality':'hosting_placeholder' if placeholder else 'unclassified'})
        if placeholder:
            self.failures.append(dict(company_id=cid,url=response.url,error='hosting_placeholder_no_company_evidence'))
            self.store.db.execute("UPDATE companies SET state='blocked',error='hosting_placeholder_no_company_evidence' WHERE id=?",(cid,));self.store.db.commit()
            return
        self.store.db.execute("UPDATE companies SET state='collected',error=NULL WHERE id=? AND state IN ('blocked','discovered')",(cid,));self.store.db.commit()
        if sum(map(len,blocks.values()))<150:
            self.fallback.append({'cid':cid,'url':response.url});return
        if not response.meta.get('follow',self.targets is None):return
        candidates=[]
        link_base=urljoin(response.url,sel.css('base::attr(href)').get() or response.url)
        for a in sel.css('a[href]'):
            url=urljoin(link_base,a.attrib['href']);p=urlsplit(url)
            if domain(url)!=domain(response.url) or p.scheme not in ['http','https'] or p.fragment:continue
            text=norm(' '.join(a.xpath('.//text()').getall())+' '+p.path).casefold()
            score=sum(weight for word,weight in [('impressum',1),('kontakt',4),('referenz',9),('portfolio',9),('bestand',9),('team',4),('unternehmen',5),('über-uns',5),('leistung',10),('weg',7),('verwaltung',6),('heiz',8),('mieter',8),('service',5),('download',8),('formular',6),('faq',6),('ablese',10),('abrechnung',9),('zähler',9),('messdienst',10),('portal',7),('verbrauch',8),('nebenkosten',7),('betriebskosten',7)] if word in text)
            if p.path.lower().endswith('.pdf') and (re.search(r'zertifikat|datenschutz|agb|widerruf',text) or not re.search(r'portfolio|bestand|geschäftsbericht|expos|referenz|projekt|messgerät|zähler|ablese|abrechnung|heiz|mieter|verbrauch|merkblatt|information',text)):continue
            if score and url!=response.url:candidates.append((score,url))
        queued=self.queued.setdefault(cid,{response.url})
        remaining=max(0,7-len(queued))
        # Follow relevant links to depth two within a shared six-HTML-page budget.
        if response.meta.get('depth',0)<2:
            html=[x for x in candidates if x[1] not in queued and not urlsplit(x[1]).path.lower().endswith('.pdf')]
            pdf=[x for x in candidates if urlsplit(x[1]).path.lower().endswith('.pdf')]
            # Give service, portfolio and contact categories a slot before redundant pages.
            ranked=sorted(set(html),reverse=True);reserved=[]
            # The legal notice gets its own slot: it carries the named director that
            # contact access depends on, and losing it left 4/20 companies generic-only.
            # Tenant-service pages carry reading dates, metering-service notices and portal
            # software; they get a reserved slot so service/about pages cannot crowd them out.
            for pattern in (r'leistung|verwaltung|weg',r'referenz|portfolio|bestand|unternehmen|über-uns',r'impressum',r'kontakt|team',r'mieter|ablese|abrechnung|zähler|messdienst|download|portal|faq|service'):
                match=next((x for x in ranked if x not in reserved and re.search(pattern,x[1],re.I)),None)
                if match:reserved.append(match)
            selected=(reserved+[x for x in ranked if x not in reserved])[:remaining]+sorted(set(pdf),reverse=True)[:2]
            for _,url in selected:
                if not urlsplit(url).path.lower().endswith('.pdf'):queued.add(url)
                if urlsplit(url).path.lower().endswith('.pdf'):
                    if self.pdfs.get(cid,0)>=2:continue
                    self.pdfs[cid]=self.pdfs.get(cid,0)+1
                yield scrapy.Request(url,callback=self.parse,errback=self.failed,meta={'cid':cid,'depth':response.meta.get('depth',0)+1})
    def closed(self,reason):
        report=dict(reason=reason,counts=self.counts,failures=self.failures,fallback=self.fallback)
        (self.store.root/'data/collection-report.json').write_text(json.dumps(report,indent=2))

def crawl(root,ids,targets=None,kinds=None):
    # A killed crawl leaves a corrupt persisted queue (queuelib struct.error); each run
    # starts a fresh job directory. Resume happens at company level, not request level.
    # One job dir per process: concurrent shards must not share or delete each other's queue.
    job=Path(root)/f'data/crawl-job-{os.getpid()}';shutil.rmtree(job,ignore_errors=True)
    process=CrawlerProcess(settings={'JOBDIR':str(job)})
    process.crawl(PilotSpider,root=str(root),ids=ids,targets=targets,kinds=kinds);process.start()
    shutil.rmtree(job,ignore_errors=True)

async def render(url):
    from playwright.async_api import async_playwright
    public_url(url)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page(user_agent=UA)
        async def route(r):
            try: public_url(r.request.url); await r.continue_()
            except Exception: await r.abort()
        await page.route('**/*',route)
        response=await page.goto(url,wait_until='domcontentloaded',timeout=45000)
        if response and response.status>=400: raise ValueError(f'browser HTTP {response.status}')
        await page.wait_for_timeout(1000)
        html=await page.content();final=page.url
        await browser.close();return html,final

def browser_fallbacks(store):
    from urllib.robotparser import RobotFileParser
    from urllib.request import urlopen,Request
    from urllib.error import HTTPError
    report=json.loads((store.root/'data/collection-report.json').read_text())
    for job in report['fallback']:
        try:
            # Check permission again; transport failures must not bypass robots.
            p=urlsplit(job['url']); robots=f'{p.scheme}://{p.netloc}/robots.txt'
            parser=RobotFileParser();parser.set_url(robots)
            try:
                with urlopen(Request(public_url(robots),headers={'User-Agent':UA}),timeout=25) as r: parser.parse(r.read().decode().splitlines())
            except HTTPError as e:
                if e.code in (404,410): parser.parse([])  # No robots policy published.
                elif e.code in (401,403): raise ValueError('robots_disallowed')
                else: raise ValueError(f'robots_unavailable_http_{e.code}')
            except Exception: raise ValueError('robots_unavailable_for_browser')
            if not parser.can_fetch(UA,job['url']):raise ValueError('robots_disallowed')
            # Scrapy/Twisted installs a selector event loop; on Windows that loop cannot
            # spawn the browser subprocess, so run Playwright on a proactor loop.
            if os.name=='nt':
                loop=asyncio.ProactorEventLoop()
                try: html,url=loop.run_until_complete(render(job['url']))
                finally: loop.close()
            else: html,url=asyncio.run(render(job['url']))
            blocks,_=html_blocks(html)
            store.document(job['cid'],url,html.encode(),blocks,'playwright')
            job['result']='saved'
        except Exception as e: job['result']=str(e)
    (store.root/'data/collection-report.json').write_text(json.dumps(report,indent=2))

def pdf_blocks(path,artifact_dir=None):
    if artifact_dir is not None: Path(artifact_dir).mkdir(parents=True,exist_ok=True)
    result=subprocess.run(['pdftotext','-f','1','-l','20','-layout','-enc','UTF-8',str(path),'-'],capture_output=True,timeout=90)
    pages=result.stdout.decode('utf-8',errors='replace').split('\f')
    if pages and not pages[-1].strip():pages.pop()
    method='pdftotext'
    bad=[i+1 for i,t in enumerate(pages) if len(re.findall(r'\w',t))<40 or t.count('\ufffd')>max(1,len(t)*.05)]
    with tempfile.TemporaryDirectory() as tmp:
        if result.returncode or bad:
            if not pages:bad=[1];pages=['']
            derived=Path(tmp)/'ocr.pdf'
            ocr=subprocess.run(['ocrmypdf','-l','deu+eng','--skip-text','--rotate-pages','--deskew','--output-type','pdf','--optimize','0','--pages',','.join(map(str,bad)),str(path),str(derived)],capture_output=True,timeout=600)
            if ocr.returncode==0:
                text=subprocess.run(['pdftotext','-f','1','-l','20','-layout',str(derived),'-'],capture_output=True,check=True).stdout.decode()
                pages=text.split('\f');method='ocrmypdf_tesseract'
                if not pages[-1].strip():pages.pop()
            for n in bad:
                ocr_worked=n<=len(pages) and len(re.findall(r'\w',pages[n-1]))>=40
                prefix=Path(tmp)/f'page{n}'
                subprocess.run(['pdftoppm','-f',str(n),'-l',str(n),'-r','300','-singlefile','-png',str(path),str(prefix)],capture_output=True,check=True,timeout=90)
                image=prefix.with_suffix('.png')
                if artifact_dir is not None: shutil.copy2(image,Path(artifact_dir)/f'page{n}.png')
                if ocr_worked: continue
                from PIL import Image,ImageStat
                if ImageStat.Stat(Image.open(image).convert('L')).stddev[0]<1:continue
                text=subprocess.run(['tesseract',str(image),'stdout','-l','deu+eng','--psm','3'],capture_output=True,check=True,timeout=90).stdout.decode()
                pages[n-1]=text;method='tesseract_raster'
        return {f'page{i+1}':t for i,t in enumerate(pages) if t.strip()},method

class SeedSpider(scrapy.Spider):
    name='heidi_directory_seeds'
    custom_settings=PilotSpider.custom_settings | {'HTTPCACHE_ENABLED':False}
    def __init__(self,root,**kwargs):super().__init__(**kwargs);self.root=Path(root)
    async def start(self):
        for name,url in [('vdiv','https://vdiv-bb.de/mitglieder/'),('portal','https://deutsches-verwalterportal.de/de/hausverwaltung/berlin/')]:
            yield scrapy.Request(url,callback=self.parse,meta={'seed':name})
    def parse(self,response):
        if response.status!=200:raise ValueError('directory fetch failed')
        directory=self.root/'data/bootstrap';directory.mkdir(parents=True,exist_ok=True)
        (directory/(response.meta['seed']+'.html')).write_bytes(response.body)

def fetch_seeds(root):
    process=CrawlerProcess();process.crawl(SeedSpider,root=str(root));process.start()
