from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION='simple-1.0'  # stored on every claim; bump when extraction rules change
FIELDS = Literal['berlin_office','berlin_service','berlin_property','management_type','units','metering','metering_provider','software','hkvo_2027','metering_contract','contact']
# Fields outside this list (legal_identity, devices, inactive, existing_customer) were
# retired: rare or unreliable on company pages. Old claims for them stay in the database
# but are ignored by accepted().
FAMILIES = {'berlin_office':'berlin','berlin_service':'berlin','berlin_property':'berlin','management_type':'management','units':'scale','metering':'metering','metering_provider':'metering','software':'metering','hkvo_2027':'metering','metering_contract':'metering','contact':'contact'}
# Only values that change the sales conversation: which meters they have, and whether an
# outside company reads them.
METERING_ENUM = {'radio_consumption','nonradio_consumption','external_provider','internal_provider'}
# HKVO remote-reading deadline (existing non-remote devices replaced by 31.12.2026).
HKVO_ENUM = {'manual_reading','remote_reading_installed','retrofit_planned'}
# Buying-timing signals from Heidi's own switching guidance (heidisystems.com/messdienst-wechseln):
# a known contract end, a stated intent/decision to switch, or a tender for metering services.
CONTRACT_ENUM = {'end_date_known','switch_intent','tender'}
HKVO_COMPLIANT = {'remote_reading_installed'}
MANAGEMENT_ENUM = ('weg','whole_building_rental','owned_stock')  # sev/commercial dropped: not a Heidi target

CURRENT_FIELDS=set(FIELDS.__args__)

def digits(s): return re.sub(r'\D','',s or '')

def now(): return datetime.now(timezone.utc).isoformat()
def digest(s): return hashlib.sha256(s.encode() if isinstance(s,str) else s).hexdigest()
def norm(s): return ' '.join(unicodedata.normalize('NFKC',s).split())
def ident(*s): return digest('|'.join(s))[:20]
def domain(url): return (urlsplit(url).hostname or '').lower().removeprefix('www.')

class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    field: FIELDS
    value: str
    number: int | None
    quantity: Literal['residential_units','mixed_units','unspecified_units','parking_spaces','buildings'] | None
    qualifier: Literal['exact','approximately','at_least','at_most'] | None
    subject: Literal['company','group','property','person']
    scope: Literal['whole_portfolio','segment','reference_sample','individual_property','company','person']
    geography: Literal['berlin','berlin_region','nationwide','other','unspecified']
    currentness: Literal['current','historical','uncertain']
    effective_date: str | None
    person: str | None
    role: str | None
    email: str | None
    # Contact-only: a phone number printed next to the person (or the company line).
    phone: str | None = None
    # Contact-only: office/branch city the quoted block or its heading ties the
    # contact to (e.g. 'Berlin', 'Potsdam'). Older payloads omit it.
    location: str | None = None
    # Contact-only: the person's LinkedIn profile (find_contacts step); the profile's search
    # result is the evidence document.
    profile_url: str | None = None
    document_id: str
    block_id: str
    quote: str = Field(min_length=3,max_length=1200)

    @model_validator(mode='after')
    def measurement(self):
        if self.field=='units':
            if self.number is None or self.number<0 or self.quantity is None or self.qualifier is None:
                raise ValueError('units require a nonnegative count, quantity and qualifier')
            # Deterministic qualifier softening: a count published with an approximator
            # is never 'exact'. Softening only; never upgraded toward exactness.
            if self.qualifier=='exact' and re.search(r'\bca\.|\bcirca\b|\betwa\b|\brund\b|ungefähr|\büber\b|mehr als',self.quote.casefold()):
                self.qualifier='at_least' if re.search(r'\büber\b|mehr als',self.quote.casefold()) and not re.search(r'\bca\.|\bcirca\b|\betwa\b|\brund\b|ungefähr',self.quote.casefold()) else 'approximately'
        elif any(x is not None for x in (self.number,self.quantity,self.qualifier)):
            raise ValueError('measurement metadata belongs only to units')
        if self.effective_date:
            from datetime import date
            if re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{4}',self.effective_date):
                self.effective_date=datetime.strptime(self.effective_date,'%d.%m.%Y').date().isoformat()
            else: date.fromisoformat(self.effective_date)
        if self.phone is not None and self.field!='contact':
            raise ValueError('phone belongs only to contact claims')
        if self.location is not None and self.field!='contact':
            raise ValueError('location belongs only to contact claims')
        if self.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',self.email):
            raise ValueError('invalid email syntax')
        return self

class Extraction(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    company_id: str
    claims: list[Claim]
    researched_families: list[Literal['berlin','management','scale','metering','contact']]
    notes: list[str]

class Store:
    def __init__(self,root):
        self.root=Path(root); (self.root/'data/snapshots').mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'data/leads.sqlite',timeout=120); self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA busy_timeout=120000')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT,raw_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'discovered',research_json TEXT NOT NULL DEFAULT '[]',error TEXT);
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,company_id TEXT REFERENCES companies(id),url TEXT NOT NULL,fetched_at TEXT NOT NULL,hash TEXT NOT NULL,path TEXT NOT NULL,method TEXT NOT NULL,blocks_json TEXT NOT NULL,meta_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),document_id TEXT NOT NULL REFERENCES documents(id),payload_json TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,claim_id TEXT NOT NULL REFERENCES claims(id),reviewer TEXT NOT NULL,verdict TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,report_json TEXT NOT NULL);
        '''); self.db.commit()
    def add_company(self,name,url,source):
        # Exact name/domain identity only. Shared domains are intentionally separate.
        cid=ident(norm(name).casefold(),domain(url))
        old=self.db.execute('SELECT raw_json FROM companies WHERE id=?',(cid,)).fetchone()
        raw=json.loads(old[0]) if old else []
        if source not in raw: raw.append(source)
        self.db.execute('INSERT INTO companies(id,name,url,raw_json) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET raw_json=excluded.raw_json',(cid,name,url,json.dumps(raw,ensure_ascii=False))); self.db.commit()
        return cid
    def document(self,cid,url,body,blocks,method='html',meta=None):
        h=digest(body); did=ident(cid or 'directory',url,h,method)
        path=self.root/'data/snapshots'/f'{h}.bin'
        if not path.exists(): path.write_bytes(body)
        self.db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?,?)',(did,cid,url,now(),h,str(path.relative_to(self.root)),method,json.dumps(blocks,ensure_ascii=False),json.dumps(meta or {}))); self.db.commit()
        return did
    def rows(self,sql,args=()): return [dict(x) for x in self.db.execute(sql,args)]
    def docs(self,cid): return self.rows('SELECT * FROM documents WHERE company_id=?',(cid,))
    def company(self,cid): return self.rows('SELECT * FROM companies WHERE id=?',(cid,))[0]

def check_claim(c:Claim,documents):
    d=documents.get(c.document_id)
    if not d: return 'wrong_or_unissued_document'
    block=json.loads(d['blocks_json']).get(c.block_id)
    if not block or not quote_found(c.quote,block): return 'quote_not_anchored'
    q=norm(c.quote).casefold()
    if c.field in ['management_type','berlin_office','berlin_service','berlin_property'] and c.scope!='company': return 'company_fact_wrong_scope'
    if c.field=='units':
        if c.geography=='berlin' and re.search(r'grossraum\s+berlin|berlin\s*(?:und|&)\s*brandenburg|berliner\s+umland|in\s+und\s+um\s+berlin',q): return 'regional_total_as_berlin_city'
        if json.loads(d.get('meta_json','{}')).get('source_class')=='directory_report': return 'directory_scale_requires_primary_claim'
        numbers={int(re.sub(r'[.\s\u00a0]','',v)) for v in re.findall(r'(?<!\d)\d+(?:[. \u00a0]\d{3})*(?!\d)',q)}
        if c.number not in numbers: return 'count_not_in_evidence'
        if c.quantity=='residential_units' and re.search(r'wohn.{0,15}(?:und|&|/).{0,15}gewerbe',q): return 'mixed_units_as_residential'
        if c.quantity=='residential_units' and re.search(r'stellpl|parkpl|garagen',q) and not re.search(r'wohnungen|wohneinheiten',q): return 'parking_as_residential'
    if c.field=='metering' and c.value not in METERING_ENUM: return 'invalid_metering_enum'
    if c.field=='hkvo_2027' and c.value not in HKVO_ENUM: return 'invalid_hkvo_enum'
    if c.field=='metering_contract' and c.value not in CONTRACT_ENUM: return 'invalid_contract_enum'
    if c.field=='metering_contract' and not re.search(r'vertrag|kündig|ausschreib|wechsel|laufzeit|messdienst|abrechnungsdienst|wärmedienst',q): return 'contract_signal_not_in_evidence'
    smoke_only=re.search(r'rauch|smoke',q) and not re.search(r'wasser|heizkostenverteiler|wärme|heiz|verbrauch',q)
    if smoke_only and ((c.field=='metering' and c.value in ['radio_consumption','nonradio_consumption']) or (c.field=='hkvo_2027' and c.value in HKVO_COMPLIANT|{'manual_reading'})): return 'smoke_as_consumption'
    # Provider/software names are recorded as written: the value must be literal in the quote.
    if c.field in ('metering_provider','software') and (len(norm(c.value))<2 or norm(c.value).casefold() not in q): return 'name_not_in_quote'
    if c.field=='hkvo_2027' and c.value=='remote_reading_installed' and not re.search(r'funk|fernables|remote',q): return 'remote_reading_not_in_evidence'
    if c.field=='hkvo_2027' and c.value=='manual_reading' and not re.search(r'ablese|ableser|selbstables|zutritt|betreten',q): return 'manual_reading_not_in_evidence'
    if c.field=='management_type' and c.value not in MANAGEMENT_ENUM: return 'invalid_management_enum'
    if c.field in ['berlin_office','berlin_service','berlin_property'] and c.value not in ['yes','no']: return 'invalid_boolean_enum'
    if c.field.startswith('berlin_') and c.value=='yes' and not re.search(r'\bberlin\b|\bberliner\s+(?:stadtteil|bezirk)\w*\b',q): return 'berlin_not_in_evidence'
    if c.email:
        if c.field!='contact' or c.email.casefold() not in q: return 'email_not_in_evidence'
        # Name tokens must appear outside the email string: an email local-part
        # spelling the name does not link that person (or a role) to the address.
        if c.person and not all(n.casefold() in q.replace(c.email.casefold(),' ') for n in c.person.split()): return 'email_person_not_linked'
    elif c.field=='contact' and c.person and not all(n.casefold() in q for n in c.person.split()): return 'person_not_in_quote'
    if c.field=='contact' and c.person and not c.role: return 'missing_role'
    if c.phone and (len(digits(c.phone))<6 or digits(c.phone) not in digits(c.quote)): return 'phone_not_in_evidence'
    if c.location and norm(c.location).casefold() not in norm(block).casefold(): return 'location_not_in_block'
    if c.currentness=='current' and re.search(r'früheren arbeitgeber|vorherigen arbeitgeber|damaligen arbeitgeber',q): return 'historical_as_current'
    if c.field=='units' and c.subject=='group' and c.geography=='berlin' and 'berlin' not in q: return 'group_total_as_berlin'
    return None

def quote_found(quote,block):
    """Loose anchor (slight wording errors are tolerated): the exact quote, or at least 80%
    of its words, appear in the block."""
    nq,nb=norm(quote).casefold(),norm(block).casefold()
    if nq in nb:return True
    words=re.findall(r'\w+',nq);have=set(re.findall(r'\w+',nb))
    return len(words)>=3 and sum(w in have for w in words)/len(words)>=0.8

# Only these failures reject a claim: the fact is not on the page or is not a category we
# use. Everything else (role wording, scope labels, regional vs city ...) is accepted:
# the goal is collecting the most information, not proving every label.
HARD_ERRORS={'wrong_or_unissued_document','quote_not_anchored','count_not_in_evidence','name_not_in_quote',
    'phone_not_in_evidence','email_not_in_evidence','berlin_not_in_evidence','invalid_metering_enum','invalid_hkvo_enum',
    'invalid_contract_enum','invalid_management_enum','invalid_boolean_enum','smoke_as_consumption'}

def hard_error(c,documents):
    e=check_claim(c,documents)
    return e if e in HARD_ERRORS else None

def ingest(store,cid,result,replace=True):
    if result.company_id!=cid: raise ValueError('wrong company output')
    docs={d['id']:d for d in store.docs(cid)}
    if any(c.document_id not in docs for c in result.claims): raise ValueError('unissued document in extraction')
    if replace:store.db.execute("UPDATE claims SET status='superseded' WHERE company_id=?",(cid,))
    for c in result.claims:
        error=hard_error(c,docs); payload=c.model_dump_json()
        claim_id=ident(cid,payload)
        # No model review: a claim whose quote is on the page and whose value fits a category is accepted.
        store.db.execute('INSERT INTO claims VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',(claim_id,cid,c.document_id,payload,'rejected' if error else 'accepted',error or '',VERSION))
    store.db.execute('UPDATE companies SET state=?,research_json=?,error=NULL WHERE id=?',('extracted',json.dumps(result.researched_families),cid));store.db.commit()

def accepted(store,cid):
    out=[dict(id=r['id'],**json.loads(r['payload_json'])) for r in store.rows("SELECT * FROM claims WHERE company_id=? AND status='accepted'",(cid,))]
    retired=lambda c:(c['field'] not in CURRENT_FIELDS or (c['field']=='management_type' and c['value'] not in MANAGEMENT_ENUM)
        or (c['field']=='metering' and c['value'] not in METERING_ENUM) or (c['field']=='hkvo_2027' and c['value'] not in HKVO_ENUM))
    return [c for c in out if not retired(c)]

ROLE_INBOXES={'info','kontakt','office','verwaltung','service','mail','team','buero','büro','post'}

EXCLUDED_ROLE=re.compile(r'datenschutz|privacy|bewerbung|recruit|marketing|presse',re.I)
# Tier 1 directly manages properties; tier 2 decides; tier 3 is other named staff.
TIER1=re.compile(r'objektbetreu|objektmanag|hausverwalter|verwalter|weg-verwalt|bestandsmanag|immobilienverwalt|kundenbetreu|property\s*manag|technische[rn]?\s+leit|technik|betriebskosten|abrechnung|vermietung|\bverwaltung\b|mietverwalt|wohnungsverwalt',re.I)
TIER2=re.compile(r'geschäftsführ|inhaber|inh\.|prokur|vorstand|leitung|leiter|director|managing|owner|vertreten durch|vertretungsberechtigt|gesetzlich vertreten',re.I)

def contact_tier(c):
    """1/2/3 for a usable named contact, None for unnamed or excluded roles."""
    role=c.get('role') or ''
    if not c.get('person') or EXCLUDED_ROLE.search(role): return None
    return 1 if TIER1.search(role) else 2 if TIER2.search(role) else 3

def relevant_contact(c):
    return bool(c['role'] and contact_tier(c) in (1,2))

def personal_email(c): return bool(c.get('email') and c['email'].split('@')[0].casefold() not in ROLE_INBOXES)

def reachable(c): return bool(c.get('phone') or personal_email(c))

NEAR_BERLIN=re.compile(r'brandenburg|potsdam|teltow|kleinmachnow|stahnsdorf|falkensee|oranienburg|bernau|königs\s*wusterhausen|schönefeld|hennigsdorf|ludwigsfelde|strausberg|erkner|eberswalde|fürstenwalde|nauen|werder|zossen|wildau|blankenfelde|hoppegarten|panketal|cottbus|frankfurt\s*\(oder\)',re.I)

def berlin_rank(c):
    """0 Berlin, 1 Brandenburg/Berlin commuter belt, 2 location unstated, 3 elsewhere."""
    loc=c.get('location') or ''
    if not loc: return 2
    if re.search(r'\bberlin',loc,re.I): return 0
    return 1 if NEAR_BERLIN.search(loc) else 3

def contact_route(claims):
    """Choose a published route; shared email remains separate from person attribution."""
    contacts=[c for c in claims if c['field']=='contact' and c['currentness']!='historical'
              and not EXCLUDED_ROLE.search(c.get('role') or '')
              and not re.match(r'(?:datenschutz|privacy|bewerbung|jobs|careers|marketing|presse)@',c.get('email') or '',re.I)]
    # People who look after properties come first, then directors; within a level, a person
    # with a direct phone or email comes first.
    named=sorted([c for c in contacts if c['person']],key=lambda c:(contact_tier(c),not reachable(c),not c.get('profile_url'),berlin_rank(c),c.get('id','')))
    company=sorted([c for c in contacts if not c['person']],key=lambda c:(berlin_rank(c),not bool(c['email']),c.get('id','')))
    person=named[0] if named else None
    channel=person if person and (person['email'] or person.get('phone')) else company[0] if company else None
    if channel is None:channel=next((c for c in named if c['email']),None)
    direct=bool(person and channel is person and personal_email(person))
    basis=('Published personal email; '+('relevant role' if relevant_contact(person) else 'role relevance unconfirmed')) if direct else 'Company contact; personal email unavailable' if person and channel else 'Published company contact' if channel else 'Named contact; contact channel unavailable' if person else 'Unknown'
    if person and channel and channel is not person and channel['person']:
        basis=f"Fallback via {channel['person']} ({channel['role'] or 'role unknown'}); preferred person's email unavailable"
    used=[c for c in (person,channel) if c]
    return dict(person=person['person'] if person else '',role=person['role'] or '' if person else '',location=(person or channel or {}).get('location') or '',channel=(channel['email'] or channel['value']) if channel else '',basis=basis,evidence_ids='; '.join(dict.fromkeys(c.get('id','') for c in used if c.get('id'))),
        contact_tier=contact_tier(person) if person else None,direct_email=direct,
        phone=(person.get('phone') if person else None) or (channel.get('phone') if channel else None) or '',
        reachable=bool(person and reachable(person)),linkedin=(person.get('profile_url') or '') if person else '')

def fit_score(claims,q,route):
    """Additive client-fit score 0-100; every point is a named component."""
    current=[c for c in claims if c['currentness']!='historical']
    vals=lambda f:{c['value'] for c in current if c['field']==f}
    met,hkvo,types=vals('metering'),vals('hkvo_2027'),set(q['management_types'])
    s={}
    # Five plain questions about the company, answered from accepted evidence. Max 100.
    # Who to call is a separate Access score (access()) and never changes fit.
    # Labels follow Heidi's own sales language (heidisystems.com).
    if types:s['Manages apartments']=25
    if q['scale']:
        n=best_units(q['scale'])['number']
        # 1,000+ apartments is Heidi's key-account threshold (custom quote, cluster rollout).
        s['Portfolio size']=15 if n>=1000 else 10 if n>=100 else 5
    if hkvo&{'manual_reading','retrofit_planned'} or 'nonradio_consumption' in met:
        s['Meters read by hand (upgrade due 2027)']=25
    if vals('metering_contract'):s['Provider change announced']=25
    # Known setup = a named provider, an outside service, or in-house billing (Heidi has an
    # offer for self-billing managers too).
    if vals('metering_provider') or met&{'external_provider','internal_provider'}:s['Current metering setup known']=10
    # Berlin is a pass/fail filter, applied in qualify/export; it earns no points here.
    raw=sum(s.values());score=max(0,min(100,raw))
    return score,s

SHORTLIST_SCORE=50

ACCESS={(1,True):(100,'Property manager, direct line'),(1,False):(80,'Property manager, name only'),
        (2,True):(60,'Owner/director, direct line'),(2,False):(40,'Owner/director, name only'),
        (3,True):(30,'Other staff, direct line'),(3,False):(20,'Other staff, name only')}

def access(route,company_line=False):
    """Separate 0-100 Access score: how directly sales can reach someone who manages the
    buildings. Property managers beat directors; a direct phone/email beats a name alone."""
    if route.get('contact_tier'):
        score,label=ACCESS[(route['contact_tier'],bool(route.get('reachable')))]
        # A personal LinkedIn profile is an extra, personal channel: +10 (max 100).
        return (min(100,score+10),label+' + LinkedIn') if route.get('linkedin') else (score,label)
    if company_line or route.get('channel'):return 10,'Main office line / info@ only'
    return 0,'No contact found'

def fit_text(components): return '; '.join(f'{k} {v:+d}' for k,v in components.items())

def qualify(claims,researched,berlin_address=False):
    """berlin_address: the company's own directory/map address has a Berlin postcode.
    Berlin is a requirement: that address, or one accepted Berlin claim, passes it."""
    # Historical positive statements never establish current eligibility.
    current=[c for c in claims if c['currentness']!='historical']
    berlin=[c for c in current if c['field'].startswith('berlin_') and c['value']=='yes' and c['subject']=='company']
    types={c['value'] for c in current if c['field']=='management_type' and c['subject']=='company'}
    conflict_fields=set()
    groups={}
    for c in current:
        key=tuple(c[k] for k in ('field','subject','scope','geography','effective_date'))
        if c['field']=='units': key+=(c['quantity'],)
        if c['field'] in ('units','berlin_office','berlin_service','berlin_property'):
            groups.setdefault(key,set()).add((c['number'],c['qualifier']) if c['field']=='units' else (c['value'],))
    for key,values in groups.items():
        if key[0]=='units':
            # Several published counts of the same stock (8,027 / 7,951 / 7,950 across report
            # years) are not a conflict; only counts differing by more than 10% are.
            nums=[n for n,_ in values if n is not None]
            if nums and max(nums)>0 and (max(nums)-min(nums))/max(nums)>0.10: conflict_fields.add('units')
        elif len(values)>1: conflict_fields.add(key[0])
    # Conflicts are reported but never block a fact (loose categories): the largest
    # published count is used and any Berlin 'yes' counts.
    # Berlin relevance needs ONE accepted current claim: a Berlin office, Berlin service,
    # Berlin property, or any Berlin-city unit count (no minimum).
    berlin_apartments=[c for c in current if c['field']=='units' and c['geography']=='berlin' and c['subject'] in ('company','property')
        and c['quantity'] in ('residential_units','mixed_units','unspecified_units') and (c['number'] or 0)>0]
    present={c['field'] for c in berlin}
    berlin_basis=next((f for f in ('berlin_office','berlin_service','berlin_property') if f in present),'berlin_units' if berlin_apartments else 'berlin_address' if berlin_address else 'none')
    # Owner-operators (housing companies, cooperatives) are an explicit Heidi segment
    # (heidisystems.com/kunden/wohnungsunternehmen), so owned_stock qualifies like WEG.
    target='relevant_residential_manager' if berlin_basis!='none' and types & {'weg','whole_building_rental','owned_stock'} else 'other_segment' if berlin_basis!='none' and types else 'needs_research'
    contacts=[c for c in current if c['field']=='contact']
    named=[c for c in contacts if relevant_contact(c)]
    direct=[c for c in named if personal_email(c)]
    tier='named_with_direct_email' if direct else 'named_relevant' if named else 'generic_only' if any(not c['person'] for c in contacts) else 'unknown'
    scale=[c for c in current if c['field']=='units' and c['subject'] in ('company','group') and c['scope'] not in ('reference_sample','individual_property') and c['quantity'] in ['residential_units','mixed_units','unspecified_units'] and (c['number'] or 0)>0 and not RANGE.search(c['quote'])]
    # Smoke detectors never count toward metering KPIs.
    families={FAMILIES[c['field']] for c in current if c['field'] in FAMILIES}
    # Scale coverage needs a usable scoped company measurement, not parking or samples.
    if not scale: families.discard('scale')
    if tier=='unknown': families.discard('contact')
    pick=lambda *f:[c for c in current if c['field'] in f]
    q=dict(target_status=target,management_types=sorted(types),contact_access=tier,
        berlin_basis=berlin_basis,berlin_apartments=max((c['number'] for c in berlin_apartments),default=None),
        scale=scale,metering=pick('metering'),providers=pick('metering_provider'),software=pick('software'),
        hkvo=pick('hkvo_2027'),contracts=pick('metering_contract'),
        evidence_coverage=len(families)/5,
        research_completion=len(set(researched))/5,conflict_fields=sorted(conflict_fields))
    route=contact_route(claims);q['contact_tier']=route['contact_tier']
    q['fit_score'],q['fit_components']=fit_score(claims,q,route)
    q['access_score'],q['access_text']=access(route)
    q['shortlist']=target=='relevant_residential_manager' and q['fit_score']>=SHORTLIST_SCORE
    return q

BERLIN_POSTCODE=re.compile(r'\b1(?:0[1-9]\d{2}|[1-3]\d{3}|4[01]\d{2})\s+Berlin\b')
NAME_STOP={'gmbh','mbh','kg','ag','ug','e.k.','gbr','und','&','hausverwaltung','immobilien','immobilienverwaltung','verwaltung','grundstücksverwaltung','haus','der','die','für','berlin','co.','haftungsbeschränkt'}

def office_from_address(company_name,documents):
    """Propose berlin_office=yes from the company's own Berlin address block.

    Extraction models often note the address but omit the claim. This proposes it
    deterministically when a block on the company's own collected pages carries a
    distinctive company-name token and a Berlin postcode. The proposal still passes
    check_claim like any model-proposed claim."""
    tokens=[t for t in re.findall(r'[\wäöüß.-]+',company_name.casefold()) if len(t)>=3 and t not in NAME_STOP]
    if not tokens:return None
    best=None
    for d in documents:
        for bid,text in json.loads(d['blocks_json']).items():
            t=norm(text);m=BERLIN_POSTCODE.search(t)
            if not m or not any(tok in t.casefold() for tok in tokens):continue
            start=max(0,m.start()-160);quote=t[start:m.end()].strip()
            if best is None or len(quote)<len(best[2]):best=(d['id'],bid,quote)
    if best is None:return None
    return Claim(field='berlin_office',value='yes',number=None,quantity=None,qualifier=None,subject='company',scope='company',geography='berlin',currentness='current',effective_date=None,person=None,role=None,email=None,document_id=best[0],block_id=best[1],quote=best[2])

OWNER_NAME=re.compile(r'genossenschaft|\beG\b|wohnungsbau|wohnungsunternehmen|wohnungsgesellschaft|wohnstätten|baugesellschaft',re.I)

def owner_stock_claim(company_name,claims):
    """A firm whose name says what it does and that publishes a unit count: a cooperative or
    housing company owns its homes (owned_stock); a Hausverwaltung/Immobilienverwaltung
    manages rented homes. Recorded on the unit-count quote. Returns a Claim or None."""
    name=company_name or ''
    if any(c['field']=='management_type' for c in claims):return None
    value='owned_stock' if OWNER_NAME.search(name) else 'whole_building_rental' if re.search(r'verwalt',name,re.I) else None
    if not value:return None
    units=[c for c in claims if c['field']=='units' and (c['number'] or 0)>0]
    if not units:return None
    u=max(units,key=lambda c:c['number'])
    return Claim(field='management_type',value=value,number=None,quantity=None,qualifier=None,subject='company',scope='company',geography=u['geography'],currentness=u['currentness'],effective_date=None,person=None,role=None,email=None,document_id=u['document_id'],block_id=u['block_id'],quote=u['quote'])

def autofix_claims(claims,documents):
    """Cheap code-side repairs that replace a second extraction call (measured top causes of
    repair rounds: location_not_in_block, email_not_in_evidence, berlin_not_in_evidence).
    Attributes that are not in the source are dropped; a Berlin flag whose quote lacks
    'Berlin' is removed. Returns (claims, fixes)."""
    fixed=[];fixes=[]
    for c in claims:
        d=documents.get(c.document_id);block=json.loads(d['blocks_json']).get(c.block_id,'') if d else ''
        nb=norm(block).casefold();q=norm(c.quote).casefold()
        if c.location and norm(c.location).casefold() not in nb:
            c=c.model_copy(update={'location':None});fixes.append(dict(field=c.field,fix='location_dropped'))
        if c.phone and digits(c.phone) not in digits(c.quote):
            c=c.model_copy(update={'phone':None});fixes.append(dict(field=c.field,fix='phone_dropped_not_in_quote'))
        if c.email and c.email.casefold() not in q:
            if c.person and c.role:c=c.model_copy(update={'email':None});fixes.append(dict(field=c.field,fix='email_dropped_not_in_quote'))
        if c.field=='contact' and c.person and not c.role:
            fixes.append(dict(field=c.field,fix='nameless_role_contact_dropped'));continue
        if c.field.startswith('berlin_') and c.value=='yes' and not re.search(r'\bberlin\b|\bberliner\s+(?:stadtteil|bezirk)\w*\b',q):
            fixes.append(dict(field=c.field,fix='berlin_flag_dropped'));continue
        fixed.append(c)
    return fixed,fixes

# Metering-service and property-management-software vocabulary, shared by the crawler
# (page choice, portal links), the packet builder (what reaches the model) and gap search.
METERING_PROVIDERS=['techem','ista','brunata','metrona','minol','kalo','qundis','noventic','zenner','wärmemessdienst','messdienst','heidi systems']
PM_SOFTWARE=['domus','haufe','powerhaus','axera','karthago','immoware','wodis','yuneo','aareon','casavi','facilioo','gfad','flowfact','datev','rentec','hausperfekt','immotech','gesys','wincasa','win-casa','immosolve','promos','hausbank','mieterportal','eigentümerportal','kundenportal']
METERING_WORDS=['ablesung','ablesetermin','ableser','ablesebogen','zählerstand','heizkostenverteiler','wärmezähler','wasserzähler','wasseruhr','funkzähler','fernablesbar','fernablesung','verbrauchsinformation','heizkostenabrechnung','betriebskostenabrechnung','nebenkostenabrechnung','rauchwarnmelder','eichfrist','zählerwechsel']
# Short names (ista, kalo) need word boundaries; longer ones also match inside domains
# such as solano-immobilien.mycasavi.com.
_names=METERING_PROVIDERS+PM_SOFTWARE
VOCAB=re.compile('|'.join([r'\b'+re.escape(w)+r'\b' for w in _names if len(w)<=4]+[re.escape(w) for w in _names if len(w)>4]+[re.escape(w) for w in METERING_WORDS]),re.I)

BROKER_NAME=re.compile(r'makler',re.I)
# Metering/billing service firms are Heidi's competitors, not leads.
COMPETITOR_NAME=re.compile(r'techem|\bista\b|minol|brunata|\bkalo\b|qundis|metrona|objektus|messdienst|wärmemess|abrechnungsdienst|submetering|zählerdienst|ableseservice',re.I)
MANAGER_NAME=re.compile(r'verwalt|property\s*manag|wohnungsbau|wohnungsgenossenschaft|baugenossenschaft|genossenschaft|\bwbg\b|liegenschaft|\bweg\b',re.I)
PHONE=re.compile(r'(?:Telefon|Tel\.?|Fon|Phone)[:\s]*((?:\+49|0)[\d\s/()\-]{6,20}\d)',re.I)

def directory_contact(store,company):
    """Phone, address and discovery sources from directory/map records (not claims):
    Google Places and OSM carry a phone field; VDIV cards carry 'Telefon ...' in the card text."""
    phones=[];addresses=[];sources=[]
    for src in json.loads(company['raw_json']):
        sources.append(src.get('source') or '?')
        if src.get('phone'):phones.append((src['phone'],src.get('source')))
        if src.get('address'):addresses.append(re.sub(r'^Adresse\s+','',src['address']))
        if src.get('source')=='VDIV' and src.get('document_id'):
            row=store.db.execute('SELECT blocks_json FROM documents WHERE id=?',(src['document_id'],)).fetchone()
            text=json.loads(row[0]).get(src.get('block_id'),'') if row else ''
            m=PHONE.search(text)
            if m:phones.append((' '.join(m.group(1).split()),'VDIV'))
    phone,phone_source=phones[0] if phones else ('','')
    return dict(phone=phone,phone_source=phone_source,address=addresses[0] if addresses else '',sources=', '.join(dict.fromkeys(sources)))

# Service firms and brokers whose "management" quote only describes cleaning, caretaking,
# repairs or selling/letting flats (hand-checked exceptions in KEEP_DESPITE_SERVICE).
STRONG_MANAGEMENT=re.compile(r'verwalt|\bWEG\b|eigentümergemeinschaft|wohnungseigentum|woeigg|bestandshalter|genossenschaft|wohnungsbaugesellschaft|mietwohnungen|bewirtschaft|eigenen bestand|unsere wohnungen|wohnungsbestand|property management|nebenkostenabrechnung|betriebskostenabrechnung|hausgeld|eigentümerversammlung',re.I)
SERVICE_OR_SALES=re.compile(r'reinigung|hausmeister|winterdienst|gartenpflege|gebäudeservice|objektpflege|technische betreuung|instandhaltung|wartung|kleinreparatur|verkauf|makler|vermittlung|exposé|besichtigung|inserierung|vermarktung',re.I)
KEEP_DESPITE_SERVICE={'a95a67d075370ee84e57','2e87f527f7ef02b2cb0c','985cbaca8694c5d995c9','40e011d01d3cc5b5996f','8fda9e94e6472ab2bec9',
    '558d16838d6bd28c6cbb','622ae371211ae609ece4','17f26a616575eb8a02cc','92f8c579196a3a41b073','0c76375b23448cdcc0b7'}  # hand-checked: real managers
# Property managers as *clients* ("für Hausverwaltungen") are not a management claim.
CLIENT_MANAGERS=re.compile(r'(?:für|unsere[rn]?|von|mit)\s+(?:\w+\s+){0,3}?(?:hausverwaltungen|verwaltungen|wohnungsbaugesellschaften|verwaltern)\b',re.I)
SERVICE_NAME=re.compile(r'facility|clean|reinigung|hausmeister|gebäudeservice|gebäudedienst',re.I)

def service_or_broker(company,claims):
    """True when nothing on the pages says the firm manages property, and the quotes (or the
    name) describe cleaning/caretaking/repairs or selling/letting instead."""
    if company.get('id') in KEEP_DESPITE_SERVICE:return False
    name=company.get('name') or ''
    quotes=[CLIENT_MANAGERS.sub(' ',c['quote']) for c in claims if c['field']=='management_type']
    if STRONG_MANAGEMENT.search(SERVICE_NAME.sub(' ',name)) or any(STRONG_MANAGEMENT.search(q) for q in quotes):return False
    return bool(SERVICE_OR_SALES.search(name) or SERVICE_NAME.search(name) or any(SERVICE_OR_SALES.search(q) for q in quotes))

def manager_verification(company,q,claims=None):
    """How a firm's property-management role is established, strongest first."""
    types=set(q['management_types'])
    if COMPETITOR_NAME.search(company.get('name') or ''):return 'excluded_metering_provider'
    if claims is not None and types and service_or_broker(company,claims):return 'excluded_service_or_broker'
    if types&{'weg','whole_building_rental','owned_stock'}:return 'verified_property_manager'
    if types:return 'verified_other_management'
    if COMPETITOR_NAME.search(company.get('name') or ''):return 'excluded_metering_provider'
    if (company.get('error') or '').startswith(('broker_name','junk_name_filter','no_management_signal_on_site')):return 'excluded_'+company['error'].split(':')[0]
    name=company['name'] or ''
    if BROKER_NAME.search(name) and not MANAGER_NAME.search(name):return 'likely_broker'
    if MANAGER_NAME.search(name):return 'name_indicates_manager_unverified'
    return 'unverified'

def parse_extraction(raw):
    """Quarantine malformed individual claims without losing valid sibling facts."""
    data=json.loads(raw)
    if not isinstance(data,dict) or not isinstance(data.get('claims'),list):
        return Extraction.model_validate(data),[]
    # Unknown envelope keys (e.g. an invented 'company_id_check') carry no claim
    # content; drop them so one stray key cannot fail the whole extraction.
    # Claims stay strict; known envelope fields keep their type checks.
    data={k:v for k,v in data.items() if k in Extraction.model_fields}
    valid=[];quarantined=[]
    for index,item in enumerate(data['claims']):
        if isinstance(item,dict):
            # A null-valued unknown key carries no information; drop it instead of
            # quarantining the claim. Non-null extra keys still fail extra='forbid'.
            item={k:v for k,v in item.items() if k in Claim.model_fields or v is not None}
        try:valid.append(Claim.model_validate(item))
        except ValueError as e:quarantined.append({'index':index,'field':item.get('field') if isinstance(item,dict) else None,'error':str(e),'raw_claim':item})
    data['claims']=valid
    if isinstance(data.get('notes'),list) and quarantined:
        data['notes'].append(f'{len(quarantined)} malformed claim(s) quarantined; original output retained, not accepted as facts.')
    return Extraction.model_validate(data),quarantined

# ---------------------------------------------------------------------------
# Sales-sheet helpers: provisional score, tier, district, general email, plain text.

# Berlin postcode ranges -> Bezirk. Postcodes are delivery areas, not district borders;
# where one postcode spans two districts the range carries the district holding most of it
# (e.g. 10119 Mitte/Prenzlauer Berg -> Mitte, 10965 Kreuzberg/Tempelhof -> Friedrichshain-Kreuzberg,
# 10787 Tiergarten/Schöneberg -> Mitte, 12157 Friedenau/Steglitz -> Tempelhof-Schöneberg).
BEZIRK_RANGES=[
    (10115,10179,'Mitte'),(10243,10249,'Friedrichshain-Kreuzberg'),(10315,10369,'Lichtenberg'),
    (10405,10439,'Pankow'),(10551,10559,'Mitte'),(10585,10719,'Charlottenburg-Wilmersdorf'),
    (10777,10783,'Tempelhof-Schöneberg'),(10785,10787,'Mitte'),(10789,10829,'Tempelhof-Schöneberg'),
    (10961,10999,'Friedrichshain-Kreuzberg'),(12043,12059,'Neukölln'),(12099,12109,'Tempelhof-Schöneberg'),
    (12157,12161,'Tempelhof-Schöneberg'),(12163,12249,'Steglitz-Zehlendorf'),(12277,12309,'Tempelhof-Schöneberg'),
    (12347,12359,'Neukölln'),(12435,12589,'Treptow-Köpenick'),(12619,12689,'Marzahn-Hellersdorf'),
    (13051,13059,'Lichtenberg'),(13086,13189,'Pankow'),(13347,13359,'Mitte'),(13403,13509,'Reinickendorf'),
    (13581,13599,'Spandau'),(13627,13627,'Charlottenburg-Wilmersdorf'),(13629,13629,'Spandau'),
    (14050,14059,'Charlottenburg-Wilmersdorf'),(14089,14089,'Spandau'),(14109,14169,'Steglitz-Zehlendorf'),
    (14193,14193,'Charlottenburg-Wilmersdorf'),(14195,14195,'Steglitz-Zehlendorf'),(14197,14199,'Charlottenburg-Wilmersdorf')]
NOT_BERLIN_POSTCODES={'12529'}  # Schönefeld (Brandenburg) sits inside the Treptow-Köpenick range
EMAIL=re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}',re.I)
NON_SALES_INBOX=re.compile(r'^(?:datenschutz|privacy|dsb|bewerbung|jobs|karriere|careers|marketing|presse|noreply|no-reply)@',re.I)

def berlin_district(postcode):
    """Berlin Bezirk for a postcode, '' when not a Berlin postcode."""
    p=str(postcode or '').strip()
    if not re.fullmatch(r'\d{5}',p) or p in NOT_BERLIN_POSTCODES:return ''
    n=int(p)
    return next((d for lo,hi,d in BEZIRK_RANGES if lo<=n<=hi),'')

def directory_profile(store,company):
    """Directory/map facts beyond directory_contact: postcode, district, map categories,
    listed emails and VDIV card texts. Store may be None (raw records only)."""
    postcodes=[];types=[];emails=[];cards=[]
    for src in json.loads(company['raw_json']):
        address=src.get('address') or ''
        # A postcode counts only when the address names Berlin (or names no town).
        town=re.search(r'\b(\d{5})\s+([^\d,]+)',address)
        if not town or re.match(r'berlin\b',town.group(2).strip(),re.I):
            if src.get('postcode'):postcodes.append(str(src['postcode']))
            if town:postcodes.append(town.group(1))
        types+=src.get('place_types') or []
        if src.get('email'):emails.append(src['email'])
        if store is not None and src.get('source')=='VDIV' and src.get('document_id'):
            row=store.db.execute('SELECT blocks_json FROM documents WHERE id=?',(src['document_id'],)).fetchone()
            text=json.loads(row[0]).get(src.get('block_id'),'') if row else ''
            if text:cards.append(text)
    district=next((berlin_district(p) for p in postcodes if berlin_district(p)),'')
    return dict(postcode=postcodes[0] if postcodes else '',district=district,place_types=list(dict.fromkeys(types)),emails=emails,cards=cards)

MAP_MANAGER_TYPES={'real_estate_agency','office=property_management'}

def provisional_score(company,profile=None,phone=''):
    """0-30 from name and map data only (never checked against the company's own pages).
    Returns (score, components). A broker-only name scores 0."""
    profile=profile or directory_profile(None,company)
    name=company.get('name') or ''
    if BROKER_NAME.search(name) and not MANAGER_NAME.search(name):return 0,{'Broker name (no management word)':0}
    s={}
    if MANAGER_NAME.search(name):s['Name says property manager']=15
    if MAP_MANAGER_TYPES&set(profile.get('place_types') or []):s['Listed as property office on the map']=5
    if company.get('url'):s['Has a website']=5
    if phone or any(src.get('phone') for src in json.loads(company.get('raw_json') or '[]')):s['Phone listed']=5
    return sum(s.values()),s

def lead_tier(score,checked,excluded=False):
    """Only Berlin companies get a tier (callers pass excluded=True otherwise).
    A >=50 checked (manager plus a size or buying signal); B 25-49 checked; C 1-24 checked or
    provisional >=20; D otherwise."""
    if excluded:return 'D'
    if checked:return 'A' if score>=50 else 'B' if score>=25 else 'C' if score>=1 else 'D'
    return 'C' if score>=20 else 'D'

def general_email(claims,profile=None):
    """Shared company inbox: accepted person-less contact claim, else a listed map email,
    else an email in the VDIV directory card. Privacy/jobs/press inboxes are skipped."""
    ok=lambda e:bool(e) and not NON_SALES_INBOX.match(e)
    company=sorted([c for c in claims if c['field']=='contact' and not c.get('person') and c['currentness']!='historical' and ok(c.get('email'))],key=lambda c:(berlin_rank(c),c.get('id','')))
    if company:return company[0]['email']
    profile=profile or {}
    for e in profile.get('emails') or []:
        if ok(e):return e
    for text in profile.get('cards') or []:
        for e in EMAIL.findall(text):
            if ok(e):return e
    return ''

MANAGER_CHECK_TEXT={'verified_property_manager':'Checked: property manager','verified_other_management':'Checked: other management (not residential)',
    'likely_broker':'Likely broker','name_indicates_manager_unverified':'Name suggests manager'}

def manager_check_text(verification,checked=False):
    if verification.startswith('excluded_'):return 'Excluded'
    if verification=='unverified':return 'Checked: no management evidence' if checked else 'Not checked: name gives no hint'
    return MANAGER_CHECK_TEXT.get(verification,verification)

PLAIN={'weg':'WEG','whole_building_rental':'rental','owned_stock':'own housing stock',
    'radio_consumption':'radio meters (read without entering flats)','nonradio_consumption':'meters read on site','external_provider':'outside metering provider','internal_provider':'bills heating costs in-house',
    'manual_reading':'meters read by hand','remote_reading_installed':'remote reading installed','retrofit_planned':'meter upgrade planned',
    'end_date_known':'contract end known','switch_intent':'plans to change metering provider','tender':'metering tender'}

RANGE=re.compile(r'\d[\d.]*\s*(?:-|–|bis)\s*\d')

def best_units(scale):
    """The one unit count to show and score: ranges are ignored; exact
    counts beat approximate ones (approximate only when nothing else exists, labelled);
    a Berlin count beats a company-wide total, which is never presented as Berlin."""
    usable=[c for c in scale if not RANGE.search(c['quote'])] or []
    if not usable:return None
    return max(usable,key=lambda c:(c['qualifier']!='approximately',c['geography'] in ('berlin','berlin_region'),c['number'] or 0))

def units_text(q):
    """The best published unit count in plain words, e.g. 'approx. 600 units (Berlin region)'."""
    c=best_units(q['scale'])
    if not c:return f"{q['berlin_apartments']:,} units in Berlin" if q.get('berlin_apartments') else ''
    prefix={'approximately':'approx. ','at_least':'over '}.get(c['qualifier'],'')
    where={'berlin':' in Berlin','berlin_region':' (Berlin region)'}.get(c['geography'],' (company total, Berlin share unknown)')
    kind=' incl. commercial' if c['quantity']=='mixed_units' else ''
    return f"{prefix}{c['number']:,} units{kind}{where}"

def why_text(company,q,route,checked,verification,components,email=''):
    """One short sentence for a sales reader: what they manage, size, buying signal, who to call."""
    if verification.startswith('excluded_') or verification=='likely_broker':
        return 'Excluded: '+(company.get('error') or 'broker name').split(':')[0].replace('_',' ')
    if not checked:
        parts=[k.lower() for k,v in components.items() if v>0]
        return 'First look: '+(', '.join(parts) if parts else 'no manager signal in name or map data')+'.'
    order=['weg','whole_building_rental','owned_stock']
    types=[PLAIN.get(t,t) for t in sorted(q['management_types'],key=lambda t:order.index(t) if t in order else 9)][:2]
    who=(' / '.join(types)+' manager') if types else 'No management evidence'
    parts=[who+(' in Berlin' if q['berlin_basis']!='none' else ', Berlin link not confirmed')]
    units=units_text(q)
    if units:parts.append(units)
    sig=[k.lower() for k in components if k in ('Meters read by hand (upgrade due 2027)','Provider change announced','Current metering setup known')]
    if sig:parts.append(', '.join(sig))
    contact='named contact with phone/email' if route.get('reachable') else 'named contact' if route.get('person') else 'company inbox only' if email or route.get('channel') else ''
    if contact:parts.append(contact)
    return ', '.join(parts)


SEGMENT={'weg':'WEG manager','whole_building_rental':'Rental manager','owned_stock':'Housing company'}

def segment(q):
    """Heidi's own customer segments (heidisystems.com/kunden)."""
    return ' + '.join(SEGMENT[t] for t in ('weg','whole_building_rental','owned_stock') if t in q['management_types'])

def sales_opener(q,components,checked,person='',role='',has_line=False,billing='',hiring='',vdiv=False,has_email=False):
    """A short, lead-specific opener for the first call: who to ask for, the strongest hook
    from the company's own facts (Heidi offers), and one extra angle."""
    who=(f"Ask for {person}"+(f" ({role})" if role else '')+'.') if person else \
        'Call the office and ask who manages the buildings.' if has_line else \
        'Write to the general inbox and ask for the property manager.' if has_email else 'No phone or e-mail found yet: look the company up before reaching out.'
    units=units_text(q)
    if 'Berlin share unknown' in units:units=''  # company-wide totals are not a Berlin pitch
    seg=segment(q)
    if 'Meters read by hand (upgrade due 2027)' in components:
        hook=f"Their pages say meters are still read by hand; under the Heating Cost Ordinance (HKVO) they must become remote-readable. Offer a free check of {('their '+units) if units else 'their buildings'} and a retrofit plan."
    elif 'Provider change announced' in components:
        hook='They have announced a change of metering provider. Offer a side-by-side quote; Heidi can switch even mid-contract.'
    elif billing.startswith('Outside provider:'):
        hook=f"They bill heating costs with {billing.split(':',1)[1].strip()}. Offer a cost and service comparison."
    elif billing=='In-house':
        hook='They bill heating costs in-house. Show how Heidi takes that work off the team.'
    elif components.get('Portfolio size')==15:
        hook=f"With {units or '1,000+ apartments'} they are a key account: propose a portfolio-wide offer."
    elif 'WEG' in seg:
        hook='Pitch clear, on-time heating-cost billing for their WEG owners and offer a short demo.'
    elif 'Rental' in seg:
        hook='Pitch clear, on-time heating-cost statements for their tenants and offer a short demo.'
    elif 'Housing' in seg:
        hook='Pitch heating-cost billing across their own housing stock and offer a short demo.'
    else:
        hook='Confirm they manage residential buildings, then offer a short demo.'
    extra=f"They are hiring ({hiring.split(';')[0].strip()}): lead with less admin work." if hiring else \
          "Mention Heidi's VDIV partnership." if vdiv else ''
    return ' '.join(x for x in (who,hook,extra) if x)

LINKEDIN_CO=re.compile(r'https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[\w%.-]+',re.I)
LINKEDIN_IN=re.compile(r'https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%.-]+',re.I)

def channels(store,company):
    """Outreach channels from the company's saved pages: contact-form page, LinkedIn company
    page, LinkedIn profiles. Read from saved text, no extra calls."""
    out=dict(contact_page='',linkedin='',linkedin_people='')
    if store is None:return out
    people=[]
    for d in store.docs(company['id']):
        if not out['contact_page'] and re.search(r'kontakt|contact',d['url'],re.I):out['contact_page']=d['url']
        text=' '.join(json.loads(d['blocks_json']).values())
        if not out['linkedin']:
            m=LINKEDIN_CO.search(text);out['linkedin']=m.group(0) if m else ''
        people+=LINKEDIN_IN.findall(text)
    out['linkedin_people']=' '.join(dict.fromkeys(people))
    return out

def vdiv_member(company):
    return any(r.get('source')=='VDIV' for r in json.loads(company.get('raw_json') or '[]'))

# Likely personal e-mail from the company's own published pattern (a personal, relevant
# address instead of info@). Labelled "likely": built from a pattern, not found.
def _fold(s):
    s=unicodedata.normalize('NFKC',s).casefold()
    for a,b in (('ä','ae'),('ö','oe'),('ü','ue'),('ß','ss'),('é','e'),('è','e'),('á','a')):s=s.replace(a,b)
    return re.sub(r'[^a-z]','',s)

def _name_parts(person):
    words=[w for w in re.sub(r'\b(?:herr|frau|dr|dipl|ing|prof|mba|ll\.?m|b\.?a|m\.?sc)\b\.?','',person,flags=re.I).replace('-',' ').split() if len(_fold(w))>1]
    return (_fold(words[0]),_fold(words[-1])) if len(words)>=2 else None

EMAIL_PATTERNS={'first.last':lambda f,l:f'{f}.{l}','f.last':lambda f,l:f'{f[0]}.{l}','firstlast':lambda f,l:f+l,'flast':lambda f,l:f[0]+l,
    'last':lambda f,l:l,'first':lambda f,l:f,'first_last':lambda f,l:f'{f}_{l}','first-last':lambda f,l:f'{f}-{l}','last.first':lambda f,l:f'{l}.{f}'}

def email_pattern(contacts):
    """(pattern, domain, example) when the company's published personal e-mails of named people
    all follow one pattern on one domain; else None."""
    found=set();example=''
    for c in contacts:
        parts=_name_parts(c.get('person') or '');email=(c.get('email') or '').casefold()
        if not parts or not personal_email(c) or '@' not in email:continue
        local,dom=email.split('@',1)
        hits={k for k,f in EMAIL_PATTERNS.items() if f(*parts)==re.sub(r'[^a-z._-]','',local)}
        if hits and hits!={'first'} and hits!={'last'}:found.add((min(hits,key=list(EMAIL_PATTERNS).index),dom));example=example or email
    return (*found.pop(),example) if len(found)==1 else None

def likely_email(person,contacts):
    pat=email_pattern(contacts);parts=_name_parts(person or '')
    if not pat or not parts:return ''
    return EMAIL_PATTERNS[pat[0]](*parts)+'@'+pat[1]
