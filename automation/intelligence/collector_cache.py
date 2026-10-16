"""Local collector caches. Failed requests never advance successful refresh markers."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from collections import defaultdict
import base64
import xml.etree.ElementTree as ET
import hashlib
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

CACHE_VERSION = '2026-10-08-incremental-cache-v1'

def ensure_cache_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS collector_refresh (
        source TEXT NOT NULL, origin TEXT NOT NULL, period TEXT NOT NULL,
        checked_at REAL NOT NULL, PRIMARY KEY(source,origin,period))''')

def fresh(db, source, origin, period, ttl, now=None):
    now = time.time() if now is None else now
    row=db.execute('SELECT checked_at FROM collector_refresh WHERE source=? AND origin=? AND period=?',(source,origin,period)).fetchone()
    return bool(row and 0<=now-row[0]<ttl)

def mark(db, source, origin, period, now=None):
    db.execute('INSERT INTO collector_refresh VALUES(?,?,?,?) ON CONFLICT(source,origin,period) DO UPDATE SET checked_at=excluded.checked_at',
               (source,origin,period,time.time() if now is None else now))

def cached_trends(db, observed, origins, history_days, query, force=False, offline=False, now=None):
    """Reuse historical dates; refresh the trailing three dates every six hours.

    query(start_iso,end_iso,origins) returns named BigQuery-like rows. Replace each
    fetched date/origin partition atomically, including successfully empty ones.
    Old partitions are revalidated weekly to catch late source corrections.
    """
    ensure_cache_schema(db)
    now=time.time() if now is None else now
    end=date.fromisoformat(observed);start=end-timedelta(days=history_days-1)
    dates=[(start+timedelta(days=i)).isoformat() for i in range(history_days)]
    origins=sorted(set(origins))
    groups=defaultdict(list)
    for origin in origins:
        stamps=dict(db.execute("SELECT period,checked_at FROM collector_refresh WHERE source='trends-v1' AND origin=? AND period BETWEEN ? AND ?",(origin,dates[0],dates[-1])))
        missing=[]
        for day in dates:
            ttl=6*3600 if (end-date.fromisoformat(day)).days<3 else 7*86400
            if force or day not in stamps or not 0<=now-stamps[day]<ttl:
                missing.append(day)
        if missing:
            groups[(missing[0],missing[-1])].append(origin)
    calls=0;downloaded=0;failed=set()
    for (lo,hi),members in groups.items():
        if offline:
            continue
        try:
            rows=list(query(lo,hi,members));calls+=1
            # Source can contain repeated history points for the same term.
            # Canonical aggregation ensures cached and fresh runs score identically.
            canonical={}
            for r in rows:
                key=(str(r.refresh_date),str(r.country_code),str(r.term),str(r.kind))
                if key[1] not in members or not lo<=key[0]<=hi:
                    raise ValueError('Trends response outside requested partitions')
                rank=int(r.rank) if r.rank is not None else None
                score=float(r.score) if r.score is not None else None
                if key in canonical:
                    previous=canonical[key]
                    ranks=[x for x in [previous[0],rank] if x is not None]
                    scores=[x for x in [previous[1],score] if x is not None]
                    rank=min(ranks) if ranks else None;score=max(scores) if scores else None
                canonical[key]=(rank,score,str(getattr(r,'week','') or ''))
            stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()
            period_days=[(date.fromisoformat(lo)+timedelta(days=i)).isoformat() for i in range((date.fromisoformat(hi)-date.fromisoformat(lo)).days+1)]
            with db:
                db.executemany('DELETE FROM google_trends_terms WHERE origin_iso2=? AND refresh_date BETWEEN ? AND ?',[(o,lo,hi) for o in members])
                db.executemany('''INSERT INTO google_trends_terms(refresh_date,origin_iso2,term,kind,rank,dataset_score,week,captured_at)
                    VALUES(?,?,?,?,?,?,?,?)''',[(*key,*value,stamp) for key,value in canonical.items()])
                db.executemany('INSERT INTO collector_refresh VALUES(?,?,?,?) ON CONFLICT(source,origin,period) DO UPDATE SET checked_at=excluded.checked_at',
                    [('trends-v1',o,day,now) for o in members for day in period_days])
            downloaded+=len(rows)
        except Exception as exc:
            failed.update(members)
            print(f'GOOGLE TRENDS ERR {lo}..{hi}: {exc}; refresh not marked successful')
    output=[]
    for origin in origins:
        coverage=db.execute("SELECT COUNT(*) FROM collector_refresh WHERE source='trends-v1' AND origin=? AND period BETWEEN ? AND ?",(origin,dates[0],dates[-1])).fetchone()[0]
        if coverage!=history_days or origin in failed:
            print(f'GOOGLE TRENDS {origin}: incomplete/failed refresh; no new momentum score')
            continue
        for r in db.execute('SELECT refresh_date,origin_iso2,term,kind,rank,dataset_score,week FROM google_trends_terms WHERE origin_iso2=? AND refresh_date BETWEEN ? AND ? ORDER BY refresh_date,kind,rank,term',(origin,dates[0],dates[-1])):
            output.append(SimpleNamespace(refresh_date=r[0],country_code=r[1],term=r[2],kind=r[3],rank=r[4],score=r[5],week=r[6]))
    print(f'GOOGLE TRENDS CACHE: {calls} queries; {downloaded} downloaded rows; {len(output)} canonical rows reused for scoring')
    return output

def cached_feed(url, timeout, cache_dir, validator, force=False, now=None):
    """One-hour RSS cache, conditional requests thereafter; validate before saving."""
    now=time.time() if now is None else now
    cache_dir=Path(cache_dir);cache_dir.mkdir(parents=True,exist_ok=True)
    path=cache_dir/(hashlib.sha256(url.encode()).hexdigest()+'.json')
    cached={}
    try:
        cached=json.loads(path.read_text(encoding='utf-8'))
        body=base64.b64decode(cached['body_b64'],validate=True);validator(body)
        cached['checked_at']=float(cached['checked_at'])
    except (OSError,ValueError,KeyError,TypeError,ET.ParseError):
        cached={}
    if cached and not force and 0<=now-cached.get('checked_at',0)<3600:
        return body,'cached'
    headers={'User-Agent':'T-Travel Event Radar/3.1'}
    if cached and not force:
        if cached.get('etag'):headers['If-None-Match']=cached['etag']
        if cached.get('modified'):headers['If-Modified-Since']=cached['modified']
    req=urllib.request.Request(url,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            body=response.read();validator(body)
            # Preserve source XML bytes exactly, including its declared encoding.
            entry={'body_b64':base64.b64encode(body).decode(),'checked_at':now,
                   'etag':response.headers.get('ETag',''),'modified':response.headers.get('Last-Modified','')}
            status='downloaded'
    except urllib.error.HTTPError as exc:
        if exc.code!=304 or not cached:raise
        entry=dict(cached,checked_at=now);status='not modified'
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(entry),encoding='utf-8');temporary.replace(path)
    return body,status
