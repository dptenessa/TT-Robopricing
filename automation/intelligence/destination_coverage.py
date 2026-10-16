"""Observed publisher coverage, separate from event dates and commercial scoring."""
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from pathlib import Path
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from functools import lru_cache
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata

VERSION = '2026-10-11-country-grounding-v2'
# Explicit creative hypotheses, not inferred demographics or proven travel intent.
TOPICS = {
 'food': ('Food-focused travellers','Food and local dining'),
 'culture': ('Culture-focused travellers','Museums, heritage and culture'),
 'nature': ('Nature-focused travellers','Landscapes and outdoor activities'),
 'beach': ('Beach holiday planners','Coastal trips and beaches'),
 'sport': ('Sports travel audiences','Sporting occasions and active trips'),
 'music': ('Music and festival audiences','Concerts and music festivals'),
 'family': ('Family trip planners','Family activities'),
 'wellness': ('Wellness trip planners','Spas and relaxation'),
 'city_break': ('City-break planners','Urban sightseeing'),
 'adventure': ('Adventure travellers','Active and adventure trips'),
 'winter_sports': ('Winter-sports travellers','Skiing and winter activities'),
 'shopping': ('Shopping-focused travellers','Shopping and markets'),
 'business': ('Business travellers','Business travel'),
 'practical': ('Destination trip planners','Practical trip planning'),
 'transport': ('Destination trip planners','Transport access and journeys'),
 'accommodation': ('Accommodation-focused trip planners','Hotels and distinctive places to stay'),
}

def norm(value):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(value or '')).casefold()).strip()

def publication_date(value):
    raw=str(value or '').strip()
    if not raw:return ''
    try:
        return datetime.fromisoformat(raw.replace('Z','+00:00')).date().isoformat()
    except ValueError:
        try:return parsedate_to_datetime(raw).date().isoformat()
        except (ValueError,TypeError,OverflowError):return ''

def canonical_url(value):
    u=urlsplit(str(value or '').strip())
    if u.scheme not in ('http','https') or not u.hostname:return ''
    query=[(k,v) for k,v in parse_qsl(u.query,keep_blank_values=True)
           if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit((u.scheme.lower(),u.netloc.lower(),u.path or '/',urlencode(sorted(query)),''))

@lru_cache(maxsize=1)
def country_codes():
    path=Path(__file__).resolve().parent/'inputs'/'travel_demand'/'destination_aliases.csv'
    codes=set()
    if path.exists():
        with path.open(encoding='utf-8-sig',newline='') as f:
            codes={str(r.get('destination_iso2') or '').strip().upper() for r in csv.DictReader(f)}
    try:
        import pycountry
        codes.update(c.alpha_2 for c in pycountry.countries)
    except ImportError:pass
    codes.add('XK')
    return {c for c in codes if len(c)==2}

@lru_cache(maxsize=1)
def coverage_place_aliases():
    # Hints only: a mention is NOT automatically a destination attribution.
    aliases = {}
    path = Path(__file__).resolve().parent / "inputs" / "travel_demand" / "destination_aliases.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = str(row.get("destination_iso2", "")).strip().upper()
                values = str(row.get("aliases", "")).split("|") + [row.get("country_name", "")]
                for value in values:
                    if len(str(value).strip()) > 2:
                        aliases[str(value)] = code
    try:
        import pycountry
        for country in pycountry.countries:
            aliases.setdefault(country.name, country.alpha_2)
    except ImportError:
        pass
    aliases.update({"Paris":"FR", "London":"GB", "Hong Kong":"HK", "Milan":"IT",
        "Athens":"GR", "Monaco":"MC", "Monte Carlo":"MC", "Interlaken":"CH",
        "Innsbruck":"AT", "Helsinki":"FI", "French Riviera":"FR", "Verbier":"CH",
        "Cortina d'Ampezzo":"IT", "Cortina d’Ampezzo":"IT", "UK":"GB",
        "Santorini":"GR", "Cape Town":"ZA", "Mombasa":"KE",
        "Colombo":"LK", "Algarve":"PT", "Costa del Sol":"ES",
        "Tenerife":"ES", "Canary Islands":"ES", "Punta Cana":"DO",
        "Penang":"MY", "Funchal":"PT", "Madeira":"PT"})
    return aliases


def _place_fold(text):
    text = unicodedata.normalize("NFKD", text.casefold()).replace("’", "'")
    return "".join(c for c in text if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def _place_patterns():
    # Ambiguous aliases assigned to multiple countries cannot establish grounding.
    owners = defaultdict(set)
    for place, code in coverage_place_aliases().items():
        owners[_place_fold(place)].add(code)
    return [(re.compile(r"(?<!\w)"+re.escape(place)+r"(?!\w)"), place, next(iter(codes)))
            for place,codes in owners.items() if len(codes)==1]


@lru_cache(maxsize=4096)
def _mentioned_text(text):
    found = {}
    for pattern, place, code in _place_patterns():
        if pattern.search(text):
            found.setdefault(code, []).append(place)
    return found


def mentioned_destinations(item):
    text = _place_fold(str(item.get("event_name", ""))+" "+str(item.get("description", "")))
    return _mentioned_text(text)


def country_supported(item, code, evidence):
    """Require a mapped place in both the source and the supporting quotation."""
    code = str(code).strip().upper()
    source_codes = mentioned_destinations(item)
    quote_codes = mentioned_destinations({"event_name": evidence, "description": ""})
    source = norm(str(item.get("event_name", "")) + " " + str(item.get("description", "")))
    return (code in source_codes and code in quote_codes
            and len(norm(evidence)) >= 12 and norm(evidence) in source)


def audit_country_links(db):
    """Quarantine unsupported links, preserving evidence for review/recovery."""
    db.execute("""CREATE TABLE IF NOT EXISTS coverage_rejected_links(
        url TEXT NOT NULL, destination_iso2 TEXT NOT NULL, topics_json TEXT,
        evidence TEXT, confidence TEXT, title TEXT, description TEXT,
        rejected_at TEXT, policy TEXT, reason TEXT,
        PRIMARY KEY(url,destination_iso2,policy))""")
    rows = db.execute("""SELECT t.url,t.destination_iso2,t.topics_json,t.evidence,
        t.confidence,a.title,a.description
        FROM destination_article_topics t JOIN destination_articles a USING(url)""").fetchall()
    rejected = []
    for row in rows:
        if not country_supported({"event_name": row[5], "description": row[6]}, row[1], row[3]):
            rejected.append(row)
    for row in rejected:
        db.execute("INSERT OR REPLACE INTO coverage_rejected_links VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*row,date.today().isoformat(),VERSION,"Country/place not grounded in source and quote"))
        db.execute("DELETE FROM destination_article_topics WHERE url=? AND destination_iso2=?",row[:2])
    cutoff=(date.today()-timedelta(days=59)).isoformat()
    db.execute("DELETE FROM coverage_rejected_links WHERE rejected_at<?",(cutoff,))
    print(f"COVERAGE AUDIT: {len(rejected)} unsupported country links quarantined")
    return len(rejected)


def ensure_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS coverage_first_seen(url_hash TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS destination_articles(
      url TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
      publisher TEXT NOT NULL, published_date TEXT, first_seen TEXT NOT NULL,
      last_seen TEXT NOT NULL, evaluated_at TEXT NOT NULL, model TEXT NOT NULL,
      policy TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS destination_article_topics(
      url TEXT NOT NULL, destination_iso2 TEXT NOT NULL, topics_json TEXT NOT NULL,
      evidence TEXT NOT NULL, confidence TEXT NOT NULL,
      PRIMARY KEY(url,destination_iso2),
      FOREIGN KEY(url) REFERENCES destination_articles(url));
    CREATE INDEX IF NOT EXISTS idx_coverage_date ON destination_articles(published_date);
    CREATE INDEX IF NOT EXISTS idx_coverage_country ON destination_article_topics(destination_iso2,url);
    ''')

def store_coverage(db,item,extractions,model,today=None):
    """Store only supported country/topic extractions; never use event date as publication."""
    today=today or date.today().isoformat()
    url=canonical_url(item.get('source_url'))
    if not url:return 0
    digest=hashlib.sha256(url.encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO coverage_first_seen VALUES(?,?)',(digest,today))
    first_seen=db.execute('SELECT first_seen FROM coverage_first_seen WHERE url_hash=?',(digest,)).fetchone()[0]
    source=norm(str(item.get('event_name',''))+' '+str(item.get('description','')))
    kept={}
    for entry in extractions if isinstance(extractions,list) else []:
        if not isinstance(entry,dict):continue
        code=str(entry.get('country_iso2','')).strip().upper()
        evidence=str(entry.get('evidence','')).strip()
        topics=entry.get('topics',[])
        topics=sorted({str(x).lower().strip() for x in topics if str(x).lower().strip() in TOPICS}) if isinstance(topics,list) else []
        # The quote makes the model's country/topic attribution inspectable. It is
        # not independent proof that the country attribution is correct.
        if code not in country_codes() or not topics or not country_supported(item, code, evidence):continue
        previous=kept.get(code)
        if previous:topics=sorted(set(topics)|set(previous[0]))
        kept[code]=(topics,evidence,'medium' if str(entry.get('confidence')).lower()=='medium' else 'low')
    published=publication_date(item.get('published_date'))
    if published and published>today:published='' # future publication is not a current signal
    cutoff=(date.fromisoformat(today)-timedelta(days=59)).isoformat()
    if (published or first_seen)<cutoff:
        db.execute('DELETE FROM destination_article_topics WHERE url=?',(url,))
        db.execute('DELETE FROM destination_articles WHERE url=?',(url,))
        return 0
    # Refresh existing extraction even when it now has no supported country/topic.
    db.execute('DELETE FROM destination_article_topics WHERE url=?',(url,))
    if not kept:
        db.execute('DELETE FROM destination_articles WHERE url=?',(url,))
        return 0
    db.execute('''INSERT INTO destination_articles VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(url) DO UPDATE SET title=excluded.title,description=excluded.description,
      publisher=excluded.publisher,published_date=COALESCE(excluded.published_date,destination_articles.published_date),
      last_seen=excluded.last_seen,evaluated_at=excluded.evaluated_at,model=excluded.model,policy=excluded.policy''',
      (url,str(item.get('event_name','')),str(item.get('description','')),urlsplit(url).hostname or '',
       published or None,first_seen,today,today,model,VERSION))
    db.executemany('INSERT INTO destination_article_topics VALUES(?,?,?,?,?)',
      [(url,c,json.dumps(v[0]),v[1],v[2]) for c,v in kept.items()])
    return len(kept)

def briefings(database,as_of=None):
    """Rolling 60-day summaries from publication-dated stories; unknown dates separate."""
    as_of=as_of or date.today();current=(as_of-timedelta(days=59)).isoformat();previous=current
    path=Path(database)
    if not path.is_file():return {}
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='destination_article_topics'").fetchone():return {}
            rows=[dict(r) for r in db.execute('''SELECT a.*,t.destination_iso2,t.topics_json,t.evidence,t.confidence
                FROM destination_articles a JOIN destination_article_topics t USING(url)
                WHERE (a.published_date BETWEEN ? AND ?) OR
                      (a.published_date IS NULL AND a.first_seen BETWEEN ? AND ?)
                ORDER BY a.published_date,a.url''',(previous,as_of.isoformat(),current,as_of.isoformat()))]
    except sqlite3.Error as exc:
        print(f'DESTINATION COVERAGE: unavailable ({exc})');return {}
    countries=defaultdict(list)
    for r in rows:
        # Also protect reports built before Radar has revalidated the database.
        if country_supported({"event_name":r["title"],"description":r["description"]},
                             r["destination_iso2"],r["evidence"]):
            countries[r['destination_iso2']].append(r)
    result={}
    for country,articles in countries.items():
        groups=[];undated=[]
        for article in articles:
            article['topics']=json.loads(article['topics_json'])
            if not article['published_date']:
                undated.append(article);continue
            # Cluster exact/near identical titles or descriptions within seven days.
            # Different-source counts remain visible but copies do not multiply topics.
            group=None
            for g in groups:
                ref=g[0]
                if abs((date.fromisoformat(article['published_date'])-date.fromisoformat(ref['published_date'])).days)>7:continue
                title_a,title_b=norm(article['title']),norm(ref['title'])
                desc_a,desc_b=norm(article['description']),norm(ref['description'])
                same_title=len(title_a)>=20 and SequenceMatcher(None,title_a,title_b).ratio()>=.90
                same_body=min(len(desc_a),len(desc_b))>=100 and SequenceMatcher(None,desc_a,desc_b).ratio()>=.94
                if same_title or same_body:group=g;break
            if group is None:groups.append([article])
            else:group.append(article)
        now_groups=[g for g in groups if g[0]['published_date']>=current]
        topics=Counter();evidence=defaultdict(list)
        for g in now_groups:
            for topic in set(t for r in g for t in r['topics']):
                topics[topic]+=1
                for r in g:
                    if topic in r['topics']:evidence[topic].append({k:r[k] for k in ['title','url','publisher','published_date','evidence','confidence']})
        ranked=[{'topic':t,'stories':n,'segment':TOPICS[t][0],'interest':TOPICS[t][1],
                 'sources':evidence[t][:5]} for t,n in topics.most_common(5)]
        publishers={r['publisher'] for g in now_groups for r in g}
        result[country]={'as_of':as_of.isoformat(),'start':current,'stories':len(now_groups),
            'articles':sum(len(g) for g in now_groups),'publishers':len(publishers),
            'undated':len(undated),'topics':ranked,
            'confidence':'medium' if len(now_groups)>=3 and len(publishers)>=2 else 'low',
            'note':'Observed coverage in selected RSS feeds, based on titles/summaries; not search volume, impressions or proven traveller demographics. Near-duplicate clustering is heuristic. Country/topic assignments need review.'}
    return result

def prune_coverage(db,as_of=None):
    """Keep 60 calendar days, inclusive, based on publication or first discovery."""
    as_of=as_of or date.today();cutoff=(as_of-timedelta(days=59)).isoformat()
    with db:
        db.execute('''DELETE FROM destination_article_topics WHERE url IN
          (SELECT url FROM destination_articles WHERE COALESCE(published_date,first_seen)<?)''',(cutoff,))
        count=db.execute('DELETE FROM destination_articles WHERE COALESCE(published_date,first_seen)<?',(cutoff,)).rowcount
    return count


def prune_ai_cache(cache_dir,as_of=None):
    as_of=as_of or date.today()
    cutoff=datetime.combine(as_of-timedelta(days=59),datetime.min.time(),tzinfo=timezone.utc).timestamp()
    count=0
    day_cutoff=(as_of-timedelta(days=59)).isoformat()
    for file in Path(cache_dir).glob('*.json'):
        published=''
        try:
            value=json.loads(file.read_text(encoding='utf-8'))
            if isinstance(value,dict):published=publication_date(value.get('_coverage_published_date'))
        except (OSError,ValueError):pass
        if (published and published<day_cutoff) or file.stat().st_mtime<cutoff:
            file.unlink();count+=1
    return count

def briefing_html(brief):
    from html import escape
    if not brief:
        return '<p>No destination coverage collected yet. This does not mean there is no interest.</p>'
    body=(f'<p><b>{brief["stories"]} unique observed stories</b> from {brief["publishers"]} publishers '
          f'({brief["articles"]} articles before clustering), {brief["start"]} → {brief["as_of"]}. '
          f'{brief["undated"]} undated articles kept separately and excluded from topic ranking. '
          f'Brief confidence: {brief["confidence"]}.</p>')
    for topic in brief['topics']:
        body+=f'<details><summary>{escape(topic["interest"])} · {topic["stories"]} stories</summary>'
        body+=f'<p>Audience to test: {escape(topic["segment"])}. Hypothesis from publisher coverage, not measured origin-specific audience.</p>'
        for source in topic['sources']:
            body+=f'<p><a href="{escape(source["url"],quote=True)}" target="_blank" rel="noopener noreferrer">{escape(source["title"])}</a> · {escape(source["publisher"])} · {source["published_date"]}<br><small>RSS evidence: “{escape(source["evidence"])}”</small></p>'
        body+='</details>'
    return body+'<p class="note">'+escape(brief['note'])+' Coverage does not change the opportunity score.</p>'


def briefing_text(brief):
    if not brief:return 'No recent destination coverage available.'
    lines=[f'Destination coverage: {brief["stories"]} observed stories / {brief["publishers"]} publishers in the latest 60 days.']
    for topic in brief['topics']:
        lines.append(f'Test angle: {topic["interest"]}; audience hypothesis: {topic["segment"]}.')
        for source in topic['sources'][:2]:lines.append(f'Source: {source["published_date"]} {source["title"]} {source["url"]}')
    return '\n'.join(lines)+ '\n'+brief['note']

def overview_html(coverage,names=None):
    from html import escape
    names=names or {}
    if not coverage:return '<p>No destination briefings collected yet. Run daily_event_radar.py with Qwen available.</p>'
    rows=[]
    for code,b in sorted(coverage.items(),key=lambda x:(-x[1]['stories'],x[0])):
        topics='; '.join(t['interest'] for t in b['topics']) or 'No publication-dated topic evidence'
        rows.append('<tr><td>'+escape(names.get(code,code))+' ('+escape(code)+')</td><td>'+str(b['stories'])+'</td><td>'+str(b['publishers'])+'</td><td>'+str(b['undated'])+'</td><td>'+escape(topics)+'</td></tr>')
    return '<p>Observed publisher coverage over 60 days. Counts are stories, not impressions or travellers; they do not affect route scores.</p><table><thead><tr><th>Destination</th><th>Stories</th><th>Publishers</th><th>Undated articles</th><th>Leading topics</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'

def coverage_diagnostics(item, extractions):
    """Explain the existing validation rules without relaxing them."""
    if not isinstance(extractions,list):
        return ['MODEL FORMAT: destination_coverage is not a list.']
    if not extractions:
        return ['MODEL EMPTY: Qwen returned no country/topic entries; validation had nothing to accept.']
    source=norm(str(item.get('event_name',''))+' '+str(item.get('description','')))
    messages=[]
    for index, entry in enumerate(extractions,1):
        if not isinstance(entry,dict):
            messages.append(f'ENTRY {index}: rejected; not an object.');continue
        code=str(entry.get('country_iso2','')).strip().upper()
        evidence=str(entry.get('evidence','')).strip()
        raw_topics=entry.get('topics',[])
        topics=[str(x).lower().strip() for x in raw_topics if str(x).lower().strip() in TOPICS] if isinstance(raw_topics,list) else []
        reasons=[]
        if code not in country_codes():reasons.append('country not recognised as ISO2')
        if not topics:reasons.append('no allowed topic names')
        if not country_supported(item,code,evidence):reasons.append('country/place not grounded in source and supporting quote')
        if len(norm(evidence))<12:reasons.append('evidence shorter than 12 characters')
        elif norm(evidence) not in source:reasons.append('evidence is not an exact quote from the supplied title/summary')
        messages.append(f'ENTRY {index}: country={code!r}; topics={raw_topics!r}; '+('REJECT: '+ '; '.join(reasons) if reasons else 'PASS extraction validation'))
        messages.append('EVIDENCE: '+json.dumps(evidence,ensure_ascii=False))
        if entry.get('_original_evidence'):
            messages.append('CONTEXT: short quote expanded from '+json.dumps(entry['_original_evidence'],ensure_ascii=False)+' using the supplied source text.')
    return messages
