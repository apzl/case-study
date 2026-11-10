"""Resumable full run: maps discovery -> dedup -> collect own website -> extract facts
-> website search for firms without a site -> collect/extract those -> export -> contact step.

The company's own website and its documents are the primary source. A web search runs only
(a) to find the website when none is known and (b) with --gap-search, to fill values still
missing after the website pass (metering/heating, scale, named contact), aimed at third-party
listing platforms (Kleinanzeigen, ImmoScout24, Immowelt).

Usage:
  python -m heidi_leads.run_full --source google|osm|both|none [--batch 25] [--limit N]
                                  [--gap-search] [--only-new] [--shard i/n] [--dry-run]

Every stage is idempotent: discovery dedups against the store, search reuses saved
logs, collect skips nothing but is cheap, enrich skips reviewed companies, export
rebuilds from the database. A progress file (runs/full/progress.json) records each
batch so an interrupted run continues where it stopped. Model calls go through the
Claude Code login only (see Gateway). No API keys.
"""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
from .core import Store,now,domain,norm,digest

ROOT=Path.cwd()
FULL=ROOT/'runs/full'

def log(*a):print(*a,flush=True)

def ids_for_run(store,only_new=False):
    rows=store.rows("SELECT id,url,raw_json,state FROM companies WHERE state IN ('discovered','collected','extracted') ORDER BY id")
    if only_new:rows=[r for r in rows if any(s.get('source') in ('GOOGLE_PLACES','OSM') for s in json.loads(r['raw_json']))]
    return [r['id'] for r in rows]

THIRD_PARTY_MAX=4
def resolve_websites(store,ids,model='claude-haiku-5-5',suffix=''):
    """One WebSearch per website-less company; accept a company_page/alias URL whose host
    contains a distinctive name token. If none is found, one deeper search (4 queries) gathers
    up to 4 third-party pages about the firm as its collection targets. Returns
    {cid:[url,...]} for companies without an own site. Logged under runs/search/website-lookup*/."""
    from .search_sources import search_company,deep_queries
    out=FULL/f'website-lookup{suffix}.json';done=json.loads(out.read_text()) if out.exists() else {}
    # Shard membership shifts when a run restarts; reuse lookups saved by any shard
    # (an entry that already has the deep-search record wins).
    for f in FULL.glob('website-lookup*.json'):
        if f!=out:
            for cid,v in json.loads(f.read_text()).items():
                if cid in ids and (cid not in done or ('deep' in v and 'deep' not in done[cid])):done[cid]=v
    for cid in ids:
        co=store.company(cid);v=done.get(cid)
        # Entries with a single `profile` (older lookup format) count as resolved.
        if co['url'] or (v and (v.get('picked') or v.get('profile') or 'deep' in v)):continue
        tokens=[t.replace('-','') for t in norm(co['name']).casefold().replace('-',' ').split() if len(t)>=4 and t not in {'gmbh','hausverwaltung','immobilien','verwaltung','immobilienverwaltung','berlin','grundstücksverwaltung'}]
        own=lambda urls:next((u['url'] for u in urls if u['kind'] in ('company_page','alias_domain') and any(t in domain(u['url']).replace('-','') for t in tokens)),None)
        short=co['name'].split('|')[0].strip()
        # One search call (3 queries) finds the own website or, failing that, up to 4 third-
        # party pages naming the firm (directories, Verwalter portals, listings). One combined
        # call is cheaper than separate website and third-party searches.
        # Extraction must still quote a management role, so brokers do not qualify on the name.
        urls=search_company(co['name'],None,model=model,max_queries=3,company_id=cid,log_dir=ROOT/'runs/search/website-lookup-deep',
                            queries=deep_queries(short))
        prior=(v or {}).get('candidates',[])
        v=done[cid]=dict(name=co['name'],picked=own(urls),deep=[u['url'] for u in urls])
        found=[u['url'] for u in urls if u['kind'] in ('other','company_page','listing','document')]+prior
        v['profiles']=[] if v['picked'] else list(dict.fromkeys(found))[:THIRD_PARTY_MAX]
        v.pop('third_party',None)
        if v['picked']:
            store.db.execute('UPDATE companies SET url=? WHERE id=?',(v['picked'],cid));store.db.commit()
        out.write_text(json.dumps(done,ensure_ascii=False,indent=1))
    return {cid:urls for cid,v in done.items() if not v.get('picked') and (urls:=v.get('profiles') or ([v['profile']] if v.get('profile') else []))}

GAP_FIELDS=('provider','metering','scale','contact')
def gaps(store,cid):
    """Missing value families after the website pass, for reviewed companies only."""
    from .core import accepted,qualify
    co=store.company(cid)
    if co['state']!='reviewed':return set()
    q=qualify(accepted(store,cid),json.loads(co['research_json']))
    # Paid gap search only where it can change a lead: relevant targets.
    if q['target_status']!='relevant_residential_manager':return set()
    missing=set()
    if not (q['providers'] or q['software']):missing.add('provider')
    if not (q['metering'] or q['hkvo']):missing.add('metering')
    if not q['scale']:missing.add('scale')
    if q['contact_tier'] not in (1,2):missing.add('contact')
    return missing

def gap_fill(store,batch,tag):
    """Targeted listing-platform search for companies with gaps; collect the finds; re-enrich
    only companies that gained documents. Returns a summary dict."""
    from .search_sources import build_targets,gap_queries
    missing={cid:gaps(store,cid) for cid in batch};todo=[cid for cid,m in missing.items() if m]
    if not todo:return dict(gap_companies=0)
    ids_file=FULL/f'{tag}-gap-ids.json';ids_file.write_text(json.dumps(todo,indent=1))
    targets=FULL/f'{tag}-gap-targets.json'
    build_targets(ROOT/'data/leads.sqlite',todo,targets,log_dir=ROOT/'runs/search/gap',queries_for=lambda row:gap_queries(row['name'],missing[row['id']]))
    before={cid:len(store.docs(cid)) for cid in todo}
    run_cmd(['collect','--ids',str(ids_file),'--targets',str(targets)])
    gained=[cid for cid in todo if len(store.docs(cid))>before[cid]]
    if gained:
        q=','.join('?'*len(gained))
        store.db.execute(f"UPDATE companies SET state='collected' WHERE id IN ({q})",gained);store.db.commit()
        redo=FULL/f'{tag}-gap-redo.json';redo.write_text(json.dumps(gained,indent=1))
        run_cmd(['enrich','--ids',str(redo)])
    filled={f:sum(1 for cid in gained if f not in gaps(store,cid) and f in missing[cid]) for f in GAP_FIELDS}
    return dict(gap_companies=len(todo),gained_documents=len(gained),filled=filled)

def run_cmd(args):
    cmd=[sys.executable,'-m','heidi_leads.pipeline',*args]
    log('>',' '.join(cmd));subprocess.run(cmd,check=True,cwd=ROOT)

def run_batches(store,ids,profiles,a,progress,progress_path,prefix):
    """Collect pages, extract facts and rebuild the workbook, batch by batch."""
    for index,start in enumerate(range(0,len(ids),a.batch),1):
        batch=[cid for cid in ids[start:start+a.batch] if store.company(cid)['state']!='reviewed']
        if not batch:continue
        tag=f'batch-{prefix}-{index:03d}'
        ids_file=FULL/f'{tag}.json';ids_file.write_text(json.dumps(batch,indent=1))
        t=time.monotonic();entry=dict(tag=tag,size=len(batch),started_at=now())
        prof={cid:profiles[cid] for cid in batch if cid in profiles}
        if prof:
            pt=FULL/f'{tag}-profiles.json';pt.write_text(json.dumps(prof,indent=1))
            pt.with_name(pt.stem+'-kinds.json').write_text(json.dumps({u:'other' for us in prof.values() for u in us},indent=1))
            run_cmd(['collect','--ids',str(ids_file),'--targets',str(pt)])
        else:run_cmd(['collect','--ids',str(ids_file)])
        run_cmd(['enrich','--ids',str(ids_file)])
        if a.gap_search:entry['gap_fill']=gap_fill(store,batch,tag)
        states=store.rows(f"SELECT state,count(*) n FROM companies WHERE id IN ({','.join('?'*len(batch))}) GROUP BY state",batch)
        entry.update(finished_at=now(),seconds=round(time.monotonic()-t),states={r['state']:r['n'] for r in states})
        progress['batches'].append(entry);progress_path.write_text(json.dumps(progress,indent=1))
        # One part rebuilds the workbook per batch; parallel rebuilds would race on the file.
        if not a.shard or a.shard.startswith('0/'):
            try:run_cmd(['export'])
            except subprocess.CalledProcessError as e:log(f'export failed (will retry next batch): {e}')
        log(json.dumps(entry))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',default='google',help='google|osm|both|none (none = skip discovery)')
    ap.add_argument('--batch',type=int,default=25);ap.add_argument('--limit',type=int)
    ap.add_argument('--gap-search',action='store_true',help='enable listing/provider gap search for missing values (off by default)');ap.add_argument('--only-new',action='store_true',help='map sources only (default: every source)')
    ap.add_argument('--no-dedup',action='store_true',help='skip dedup (parallel shards: run it once before starting them)')
    ap.add_argument('--redo-checked',action='store_true',help='re-extract companies whose facts come from an older pipeline VERSION (pages are re-read)')
    ap.add_argument('--no-contacts',action='store_true',help='skip the contact step (LinkedIn + team pages for tiers A-C)')
    ap.add_argument('--dry-run',action='store_true')
    ap.add_argument('--shard',default=None,help='i/n: process companies with index %% n == i (disjoint shards run concurrently; one progress file per shard)')
    a=ap.parse_args();FULL.mkdir(parents=True,exist_ok=True)
    shard=tuple(int(x) for x in a.shard.split('/')) if a.shard else None
    progress_path=FULL/(f'progress-shard-{shard[0]}-of-{shard[1]}.json' if shard else 'progress.json');progress=json.loads(progress_path.read_text()) if progress_path.exists() else dict(started_at=now(),batches=[])
    store=Store(ROOT)
    if a.source!='none':run_cmd(['discover','--source',a.source])
    # One de-duplication pass over every source before any paid step (safe to repeat).
    if not a.dry_run and not a.no_dedup:subprocess.run([sys.executable,'-m','heidi_leads.dedup'],check=True,cwd=ROOT)
    if a.redo_checked and not a.dry_run:
        # Only companies with no claim from the current pipeline version, so restarts never redo work.
        from .core import VERSION
        store.db.execute("UPDATE companies SET state='collected' WHERE state='reviewed' AND id NOT IN (SELECT company_id FROM claims WHERE version=?)",(VERSION,));store.db.commit()
    ids=ids_for_run(store,a.only_new)
    # Stable split by company id (not list position), so parts can restart or be re-split
    # at any time without overlapping. Finished companies drop out by state.
    if shard:ids=[cid for cid in ids if int(digest(cid)[:8],16)%shard[1]==shard[0]]
    if a.limit:ids=ids[:a.limit]
    log(f'{len(ids)} companies to process')
    if a.dry_run:print(json.dumps(dict(companies=len(ids),without_website=sum(1 for i in ids if not store.company(i)['url']))));return
    # Broker-only names (Makler without any management word) and metering competitors are
    # excluded before any paid call.
    from .core import BROKER_NAME,MANAGER_NAME,COMPETITOR_NAME
    for i in ids:
        name=store.company(i)['name'] or ''
        if BROKER_NAME.search(name) and not MANAGER_NAME.search(name):store.db.execute("UPDATE companies SET state='blocked',error='broker_name: Makler without management wording' WHERE id=?",(i,))
        elif COMPETITOR_NAME.search(name):store.db.execute("UPDATE companies SET state='blocked',error='metering_provider: competitor, not a lead' WHERE id=?",(i,))
    store.db.commit();before=len(ids);ids=[i for i in ids if store.company(i)['state']!='blocked'];log(f'brokers/competitors excluded: {before-len(ids)}')
    tag_base='s'+'-'.join(map(str,shard)) if shard else 'all'
    # Phase 1: companies with a website are researched right away.
    with_site=[i for i in ids if store.company(i)['url']]
    run_batches(store,with_site,{},a,progress,progress_path,tag_base+'-site')
    # Phase 2: website search for the rest, then research of whatever pages were found.
    rest=[i for i in ids if not store.company(i)['url']]
    profiles=resolve_websites(store,rest,suffix=f'-s{shard[0]}' if shard else '')
    found=[i for i in rest if store.company(i)['url'] or i in profiles]
    log(f'website search: {len(rest)} firms, {sum(1 for i in rest if store.company(i)["url"])} own site, {sum(1 for i in rest if i in profiles)} via third-party pages')
    (FULL/f'skipped-no-website-{tag_base}.json').write_text(json.dumps([i for i in rest if i not in found],indent=1))
    run_batches(store,found,profiles,a,progress,progress_path,tag_base+'-search')
    run_cmd(['evaluate'])
    # Contact step: named people (LinkedIn profiles, team pages) for the tier A-C leads, then
    # rebuild the workbook so the better contacts show. Shards: only shard 0 runs it, at the end.
    if not a.no_contacts and (not shard or shard[0]==0):
        subprocess.run([sys.executable,'-m','heidi_leads.find_contacts','--tiers','ABC'],check=False,cwd=ROOT)
        run_cmd(['evaluate'])

if __name__=='__main__':main()
