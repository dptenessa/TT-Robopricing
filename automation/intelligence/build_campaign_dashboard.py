#!/usr/bin/env python3
"""Build the agency-facing T-Travel opportunity dashboard from events.db."""
from __future__ import annotations
import argparse, csv, json, re, sqlite3
from datetime import date, timedelta
from pathlib import Path

from event_matching import classify, destination_matches, origin_matches, overlaps

DEFAULT_INPUT_DIR = Path(__file__).resolve().parent / "inputs" / "travel_demand"
EVENT_FILTER_VERSION = "2026-10-06-filters-kpi-aliases-v4"
SALES_SIGNAL_VERSION = "2026-10-06-observed-sales-60d"
SCORE_SNAPSHOT_VERSION = "2026-10-08-ignore-partial-score-snapshots-v1"

def s(v): return "" if v is None else str(v).strip()
def n(v):
    m = re.search(r"-?\d+(?:\.\d+)?", s(v).replace(",", ""))
    return float(m.group()) if m else None
def d(v):
    v=s(v); return v[:10] if re.match(r"^\d{4}-\d{2}-\d{2}",v) else ""


def load_country_names(input_dir=None):
    """Load ISO-2 names from the collector's local input CSVs."""
    candidates = []
    if input_dir:
        candidates.append(Path(input_dir))
    candidates.extend([
        DEFAULT_INPUT_DIR,
        Path(__file__).resolve().parent / "travel_demand_inputs",
    ])
    names = {}
    for folder in candidates:
        for filename, code_key, name_key in (
            ("origins.csv", "origin_iso2", "country_name"),
            ("destination_aliases.csv", "destination_iso2", "country_name"),
        ):
            path = folder / filename
            if not path.exists():
                continue
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    code = s(row.get(code_key)).upper()
                    name = s(row.get(name_key))
                    if code and name:
                        names[code] = name
    return names
ALIASES={"JP":["japan","tokyo","osaka","kyoto"],"TR":["turkey","türkiye","turkiye","istanbul","antalya"],"ES":["spain","madrid","barcelona","mallorca","ibiza","valencia"],"MA":["morocco","marrakech","casablanca"],"US":["united states","usa","u.s.","new york","miami","los angeles","las vegas"],"MX":["mexico","cancun","mexico city"],"CA":["canada","toronto","vancouver","montreal"],"TH":["thailand","bangkok","phuket"],"AE":["united arab emirates","uae","dubai","abu dhabi"],"EG":["egypt","cairo","hurghada","sharm"]}
EU_ISO2={"AT","BE","BG","HR","CY","CZ","DE","DK","EE","ES","FI","FR","GR","HU","IE","IT","LT","LU","LV","MT","NL","PL","PT","RO","SE","SI","SK"}
INTRA_EU_SIM_PENALTY=0.75
ORIGIN_MARKET_WEIGHTS={}
ORIGIN_ALIASES={"DE":["de","germany","deutschland"],"FR":["fr","france"],"NL":["nl","netherlands","holland"],"GB":["gb","uk","united kingdom","great britain"],"IT":["it","italy"],"ES":["es","spain"],"HR":["hr","croatia"],"AT":["at","austria"],"CH":["ch","switzerland"]}

def load_origin_weights(input_dir=None):
    """Load the full origin market-weight file used by the demand model."""
    global ORIGIN_MARKET_WEIGHTS
    folders = []
    if input_dir:
        folders.append(Path(input_dir))
    folders.append(DEFAULT_INPUT_DIR)
    for folder in folders:
        path = folder / "origin_market_weights.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        weights = {}
        for row in rows:
            code = s(row.get("origin_iso2") or row.get("iso2")).upper()
            raw = row.get("weight") or row.get("market_weight") or row.get("origin_weight")
            value = n(raw)
            if code and value is not None and value > 0:
                weights[code] = value
        if weights:
            ORIGIN_MARKET_WEIGHTS = weights
            return
    # Safe fallback for older installations without the input file.
    ORIGIN_MARKET_WEIGHTS = {"DE":84,"FR":68,"NL":18,"GB":68,"IT":59,"ES":48,"HR":4,"AT":9,"CH":9}
def matching(e,dest):
    return destination_matches(e, dest)
def listed_origins(e):
    if not s(e.get("Origin_Markets")): return set()
    return {code for code in ORIGIN_MARKET_WEIGHTS if origin_matches(e, code)}
def origin_relevant(e,origin):
    return not listed_origins(e) or origin_matches(e, origin)
def event_audience(e):
    for key in ("Estimated_Addressable_Travellers","Estimated_Foreign_Visitors","Estimated_Total_Participants"):
        value=n(e.get(key))
        if value is not None: return value
    return None
def corridor_event_audience(e,origin):
    total=event_audience(e)
    if total is None: return None
    listed=listed_origins(e)
    pool=listed or set(ORIGIN_MARKET_WEIGHTS)
    weights={code:ORIGIN_MARKET_WEIGHTS.get(code,1) for code in pool}
    return total*weights.get(origin,0)/sum(weights.values()) if origin in weights else 0
def product(events):
    h=" ".join(s(e.get(k)).lower() for e in events for k in ("Connectivity_Requirements","Interests_Motivation","Recommended_Product"))
    if "video" in h and "social" not in h: return "L"
    if "social" in h: return "M"
    if any(x in h for x in ("maps","navigation","emergency","booking","continuous","translation")): return "Unlimited"
    return "S"
def make_route(score, events, holidays=None, country_names=None):
    dest=s(score.get("destination_iso2")); today=date.today()
    origin=s(score.get("origin_iso2")); holidays=holidays or {}; country_names=country_names or {}
    origin_name=country_names.get(origin, origin)
    destination_name=country_names.get(dest, dest)
    upcoming_holidays=[h for h in holidays.get(origin, [])
                       if h.get("date") and date.fromisoformat(h["date"]) >= today
                       and date.fromisoformat(h["date"]) <= today+timedelta(days=120)]
    # Ignore historical catalogue rows when building a current/next-month brief.
    # They can remain in the database for audit/history, but must not determine
    # the campaign timing shown to the agency.
    matched=[e for e in events if matching(e,dest) and origin_relevant(e,origin)
             and overlaps(e, today-timedelta(days=7), 120)]
    # Concrete events support the scored event component. Broad occasions
    # remain visible as context but must not be presented as a specific event.
    ev=[e for e in matched if classify(e) == "specific"]
    broader_ev=[e for e in matched if classify(e) != "specific"]
    ev.sort(key=lambda x:d(x.get("Start_Date")) or "9999-99-99")
    seg=sorted({s(e.get("Main_Traveller_Segment")) for e in ev if s(e.get("Main_Traveller_Segment"))})
    interests=sorted({s(e.get("Interests_Motivation")) for e in ev if s(e.get("Interests_Motivation"))})
    origins=sorted({s(e.get("Origin_Markets")) for e in ev if s(e.get("Origin_Markets"))})
    names=[s(e.get("Occasion_Event")) for e in ev if s(e.get("Occasion_Event"))]
    addressable=[n(e.get("Estimated_Addressable_Travellers")) for e in ev if n(e.get("Estimated_Addressable_Travellers")) is not None]
    foreign=[n(e.get("Estimated_Foreign_Visitors")) for e in ev if n(e.get("Estimated_Foreign_Visitors")) is not None]
    participants=[n(e.get("Estimated_Total_Participants")) for e in ev if n(e.get("Estimated_Total_Participants")) is not None]
    # Use the largest matching event rather than summing events, which would
    # double-count overlapping audiences.
    corridor_audiences=[corridor_event_audience(e,origin) for e in ev]
    corridor_audiences=[x for x in corridor_audiences if x is not None and x>0]
    audience=max(corridor_audiences) if corridor_audiences else None
    raw_score=n(score.get("opportunity_score")) or 0
    if origin == dest:
        return None
    intra_eu=origin in EU_ISO2 and dest in EU_ISO2
    score_value=raw_score*INTRA_EU_SIM_PENALTY if intra_eu else raw_score
    weighted_opportunity=(audience*score_value/100.0) if audience is not None else None
    try:
        components=json.loads(s(score.get("components_json")) or "{}")
    except json.JSONDecodeError:
        components={}
    first=d(ev[0].get("Start_Date")) if ev else ""
    # Do not recommend immediate activation when the route is supported by
    # only a small subset of the scoring signals.
    coverage=n(score.get("data_coverage")) or 0
    action="Activate now" if score_value>=75 and coverage>=80 else "Prepare campaign" if score_value>=60 else "Monitor"
    urgency="Immediate" if first and (date.fromisoformat(first)-date.today()).days<=30 else "Plan ahead"
    angle=f"Stay connected in {destination_name} for {', '.join(interests[:2]) or 'travel, navigation and sharing'}"
    content=s(ev[0].get("Landing_Page_Recommendation")) if ev else f"Existing {destination_name} destination page with seasonal content"
    route=f"{origin} {origin_name} → {dest} {destination_name}"
    relevance="Lower SIM relevance: intra-EU travel" if intra_eu else "Normal SIM relevance"
    brief=(f"{route}\n\nRecommendation: {action}. SIM opportunity score: {score_value:.0f} (raw demand score: {raw_score:.0f}).\n"
           f"Segments: {', '.join(seg) or 'Validate traveller segments'}\nInterests: {', '.join(interests) or 'Validate motivations'}\n"
           f"Product: {product(ev)}\nRelevance: {relevance}\nCampaign timing: {first or 'To be validated'} ({urgency})\nMessage: {angle}.\nContent: {content}.")
    event_cards=[{"name":s(e.get("Occasion_Event")),"type":"Specific destination event · "+s(e.get("Occasion_Type")),
                  "start":d(e.get("Start_Date")),"end":d(e.get("End_Date")),
                  "markets":s(e.get("Origin_Markets")),"source":s(e.get("Source_Type")),
                  "url":s(e.get("Source_URL")),"scope":"specific",
                  "corridor_audience":corridor_event_audience(e,origin)} for e in ev if s(e.get("Occasion_Event"))]
    # Broad/multi-destination occasions are deliberately not placed in the
    # route's relevant-event cards. They are not concrete evidence for this
    # destination and would otherwise appear identical to real events.
    missing=components.get("missing",[])
    sales_units=n(score.get("observed_sales_units"))
    sales_source="T-Travel sales export · " + (f"{sales_units:.0f} units in last 60d" if sales_units is not None else "60-day window")
    signal_sources=[
      {"name":"Baseline demand","value":n(score.get("baseline_search_score")),"source":"Google Ads","status":"Not collected" if "baseline_search" in missing else "Available"},
      {"name":"Demand momentum","value":n(score.get("search_momentum_score")),"source":"Google Trends","status":"No corridor match / not available" if "search_momentum" in missing else "Available"},
      {"name":"Observed sales (60d)","value":n(score.get("observed_sales_score")),"source":sales_source,"status":"No sales in last 60 days" if "observed_sales" in missing else "Available"},
      {"name":"Seasonality","value":n(score.get("seasonality_score")),"source":"Tourism history / seasonal model","status":"Available" if n(score.get("seasonality_score")) is not None else "Missing"},
      {"name":"Events","value":n(score.get("events_score")),"source":"Event Radar","status":"Available" if n(score.get("events_score")) is not None else "Missing"},
      {"name":"Holiday timing","value":n(score.get("holiday_score")),"source":"Holiday calendar","status":"Available" if n(score.get("holiday_score")) is not None else "Missing"},
      {"name":"Travel distance","value":n(score.get("distance_score")),"source":"Approximate country-centre distance","status":"Available" if n(score.get("distance_km")) is not None else "Unavailable; neutral factor"},
    ]
    return {"origin":origin,"destination":dest,"score_date":s(score.get("score_date")),"score":score_value,"raw_score":raw_score,
      "baseline":n(score.get("baseline_search_score")),"momentum":n(score.get("search_momentum_score")),"sales_score":n(score.get("observed_sales_score")),"sales_units":n(score.get("observed_sales_units")),"sales_revenue":n(score.get("observed_sales_revenue")),"seasonality":n(score.get("seasonality_score")),
      "events_score":n(score.get("events_score")),"holiday":n(score.get("holiday_score")),"distance_score":n(score.get("distance_score")),"distance_km":n(score.get("distance_km")),"coverage":coverage,
      "change_7d":n(score.get("change_7d")),"action":action,"urgency":urgency,"start_date":first,"product":product(ev),
      "segments":seg,"interests":interests,"origins":origins,"events":names,"event_count":len(names),
      "addressable_audience":audience,"foreign_visitors":max(foreign) if foreign else None,
      "participants":max(participants) if participants else None,"content":content,"angle":angle,"brief":brief,
      "weighted_opportunity":weighted_opportunity,
      "route":route,"origin_name":origin_name,"destination_name":destination_name,
      "signal_sources":signal_sources,"event_cards":event_cards,
      "route_type":"Event-supported route" if ev else "Always-on demand route",
      "broader_event_count":len(broader_ev),
      "event_markets":sorted({m for e in event_cards for m in e["markets"].split(";") if m.strip()}),
      "upcoming_holidays":upcoming_holidays,"sim_relevance":"Lower" if intra_eu else "Normal"}
def build(database, output, input_dir=None):
    country_names=load_country_names(input_dir)
    load_origin_weights(input_dir)
    db=sqlite3.connect(database); db.row_factory=sqlite3.Row
    # A failed/interrupted opportunity build can leave a newer date with only
    # one or a few rows.  Treating that partial snapshot as authoritative makes
    # valid events appear to disappear from the dashboard.  Use the newest
    # reasonably complete snapshot instead; a fully rebuilt snapshot is always
    # preferred when available.
    snapshot_counts = db.execute(
        "SELECT score_date, COUNT(*) FROM opportunity_scores "
        "GROUP BY score_date ORDER BY score_date"
    ).fetchall()
    latest = None
    if snapshot_counts:
        max_rows = max(int(row[1]) for row in snapshot_counts)
        # A deliberately small explicit test run (for example selected
        # origins/destinations) can be valid, while a one-row interrupted run
        # is not.  Accept snapshots with at least 20% of the largest snapshot
        # or two rows; this keeps targeted rebuilds usable and rejects the
        # common one-row failure case.
        minimum_complete = max(2, int(max_rows * 0.20))
        complete = [
            str(row[0]) for row in snapshot_counts
            if int(row[1]) >= minimum_complete
        ]
        latest = complete[-1] if complete else str(snapshot_counts[-1][0])
    scores=[dict(x) for x in db.execute("SELECT * FROM opportunity_scores WHERE score_date=?",(latest,))]
    events=[dict(x) for x in db.execute('SELECT * FROM events WHERE "Start_Date" GLOB "[0-9][0-9][0-9][0-9]-*"')]
    holiday_rows=[dict(x) for x in db.execute("SELECT origin_iso2, holiday_date, holiday_name, local_name, source FROM holiday_periods")]
    holidays={}
    for h in holiday_rows:
        holidays.setdefault(h["origin_iso2"], []).append({"date":h["holiday_date"],"name":h["holiday_name"],"local_name":h["local_name"],"source":h["source"]})
    db.close(); routes=[make_route(x,events,holidays,country_names) for x in scores]; routes=[x for x in routes if x]; routes.sort(key=lambda x:(-x["score"],x["origin"],x["destination"]))
    Path(output).write_text(HTML.replace("__DATA__",json.dumps(routes,ensure_ascii=False)),encoding="utf-8")
    print(
        f"Built {output} with {len(routes)} scored routes from {latest} "
        f"| event filter: {EVENT_FILTER_VERSION} "
        f"| snapshot policy: {SCORE_SNAPSHOT_VERSION}"
    )
HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T-Travel Campaign Opportunity Dashboard</title><style>
:root{--p:#e20074;--i:#191919;--m:#6d6d6d;--bg:#f4f4f4;--c:#fff;--l:#ddd;--g:#16845f;--o:#bf7e16;--b:#5474d5}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--i);font:15px Arial;line-height:1.4}.top{background:var(--i);color:white;padding:20px 4vw}.topin,.main{max-width:1500px;margin:auto}.eyebrow{color:#ff91bf;text-transform:uppercase;letter-spacing:.12em;font-size:11px;font-weight:bold}.top h1{margin:5px 0 2px;font-size:clamp(30px,4vw,55px);letter-spacing:-.05em}.top p{margin:0;color:#ccc}.main{padding:28px 4vw 60px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:17px}.toolbar button,.toolbar select,.toolbar input{min-height:40px;border:1px solid #bbb;border-radius:7px;background:#fff;padding:8px 12px;font:inherit}.toolbar button.active{background:var(--p);color:#fff;border-color:var(--p)}.toolbar input{min-width:220px}.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:17px}.kpi{background:var(--c);border:1px solid var(--l);border-top:4px solid var(--p);border-radius:10px;padding:13px 15px}.kpi:nth-child(2){border-top-color:var(--g)}.kpi:nth-child(3){border-top-color:var(--b)}.kpi:nth-child(4){border-top-color:var(--o)}.kpi:nth-child(5){border-top-color:#8060bd}.label{font-size:11px;color:var(--m);font-weight:bold;text-transform:uppercase;letter-spacing:.06em}.value{font-size:27px;font-weight:bold;margin-top:4px}.layout{display:grid;grid-template-columns:minmax(390px,1fr) minmax(480px,1.5fr);gap:16px}.panel{background:var(--c);border:1px solid var(--l);border-radius:11px;padding:17px}.panel h2{font-size:19px;margin:0}.sub{font-size:12px;color:var(--m);margin:3px 0 14px}.route-list{max-height:720px;overflow:auto;border-top:1px solid var(--l)}.route{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--l);padding:13px 5px;background:transparent;color:inherit;cursor:pointer}.route:hover,.route.selected{background:#fff0f7}.head{display:flex;justify-content:space-between;gap:8px;font-size:17px;font-weight:bold}.meta{font-size:12px;color:var(--m);margin-top:4px}.pill{display:inline-block;padding:2px 8px;border-radius:11px;color:#fff;font-size:11px;font-weight:bold}.activate{background:var(--g)}.prepare{background:var(--o)}.monitor{background:var(--b)}.route-score{color:var(--p);font-size:22px}.title{display:flex;justify-content:space-between;gap:10px}.title h2{font-size:28px;letter-spacing:-.04em}.bigscore{font-size:43px;color:var(--p);font-weight:bold;text-align:right}.bigscore small{display:block;font-size:10px;color:var(--m);text-transform:uppercase;letter-spacing:.08em}.scores{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:14px 0}.sr{display:grid;grid-template-columns:130px 1fr 35px;gap:7px;align-items:center;font-size:12px}.track{height:9px;background:#eee;border-radius:5px;overflow:hidden}.fill{height:100%;background:var(--p)}.na{color:var(--m);font-style:italic}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:15px}.info{border-top:1px solid var(--l);padding-top:8px}.info strong{display:block;color:var(--m);font-size:10px;text-transform:uppercase;letter-spacing:.06em}.info div{margin-top:3px}.chip{display:inline-block;background:#f1f1f1;padding:4px 7px;margin:2px;border-radius:5px;font-size:12px}.section{margin-top:15px;border-top:1px solid var(--l);padding-top:12px}.copy{margin-top:13px;border:1px solid var(--i);border-radius:7px;background:#fff;padding:9px 12px;font-weight:bold;cursor:pointer}.matrix{margin-top:15px;height:300px;position:relative;border-left:1px solid var(--i);border-bottom:1px solid var(--i);background:linear-gradient(transparent 49.8%,#eee 50%,transparent 50.2%),linear-gradient(90deg,transparent 49.8%,#eee 50%,transparent 50.2%)}.dot{position:absolute;transform:translate(-50%,50%);border-radius:50%;width:15px;height:15px;border:2px solid white;box-shadow:0 0 0 1px #777;background:var(--p);cursor:pointer}.dot:hover{transform:translate(-50%,50%) scale(1.5);z-index:2}.axisx,.axisy{position:absolute;color:var(--m);font-size:11px}.axisx{bottom:-25px;left:50%;transform:translateX(-50%)}.axisy{left:-37px;top:50%;transform:rotate(-90deg)}.note{font-size:12px;color:var(--m);margin-top:13px}@media(max-width:1000px){.layout{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,1fr)}}@media(max-width:600px){.kpis{grid-template-columns:repeat(2,1fr)}.grid,.scores{grid-template-columns:1fr}.layout{display:block}.panel+.panel{margin-top:16px}}
</style></head><body><header class="top"><div class="topin"><div class="eyebrow">T-Travel campaign intelligence</div><h1>Which routes should we activate?</h1><p>Ranked demand opportunities with the evidence behind every score.</p></div></header><main class="main"><div class="toolbar"><button class="period active" data-p="current">Current month</button><button class="period" data-p="next">Next month</button><button class="period" data-p="all">All scored routes</button><select id="action"><option value="all">All actions</option><option>Activate now</option><option>Prepare campaign</option><option>Monitor</option></select><input id="search" placeholder="Search origin or destination"></div><section class="kpis"><div class="kpi"><div class="label">Routes shown</div><div class="value" id="kr">—</div></div><div class="kpi"><div class="label">Strong opportunities</div><div class="value" id="ks">—</div></div><div class="kpi"><div class="label">Average score</div><div class="value" id="ka">—</div></div><div class="kpi"><div class="label">Accelerating</div><div class="value" id="ku">—</div></div><div class="kpi"><div class="label">Average coverage</div><div class="value" id="kc">—</div></div></section><div class="layout"><section class="panel"><h2>Preferred routes</h2><p class="sub" id="note">—</p><div class="route-list" id="routes"></div></section><section class="panel"><div id="detail"><p class="sub">Select a route to see the full campaign brief.</p></div><h2>Opportunity matrix</h2><p class="sub">Momentum vertically; overall opportunity score horizontally.</p><div class="matrix" id="matrix"><span class="axisx">Opportunity score →</span><span class="axisy">Momentum →</span></div><p class="note">Weights: baseline 20%, momentum 25%, seasonality 20%, events 20%, holiday timing 15%. Missing signals reduce coverage, not the component score.</p></section></div></main><script>
const DATA=__DATA__,Q=x=>document.getElementById(x),N=x=>x==null||x===""?null:Number(x),E=x=>String(x??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));let period="current",selected="";function win(p){let z=new Date(),m=z.getMonth()+(p==="next"?1:0);return[new Date(z.getFullYear(),m,1),new Date(z.getFullYear(),m+1,0)]}function inside(r){if(period==="all")return true;if(!r.start_date)return true;let w=win(period),x=new Date(r.start_date);return x>=w[0]&&x<=w[1]}function data(){let q=Q("search").value.toLowerCase(),a=Q("action").value;return DATA.filter(r=>inside(r)&&(a==="all"||r.action===a)&&(!q||(r.origin+" "+r.destination+" "+r.segments.join(" ")+" "+r.interests.join(" ")).toLowerCase().includes(q)))}function row(label,v){return `<div class="sr"><span>${label}</span>${v==null?'<span class="na">N/A</span><span></span>':`<span class="track"><span class="fill" style="width:${Math.max(0,Math.min(100,v))}%"></span></span><b>${Math.round(v)}</b>`}</div>`}function chips(a){return a.length?a.map(x=>`<span class="chip">${E(x)}</span>`).join(""):"<span class='na'>Not available yet</span>"}function show(r){if(!r)return;Q("detail").innerHTML=`<div class="title"><div><div class="eyebrow">Agency campaign brief</div><h2>${E(r.origin)} → ${E(r.destination)}</h2><span class="pill ${r.action[0]==="A"?"activate":r.action[0]==="P"?"prepare":"monitor"}">${E(r.action)}</span> <span class="pill monitor">Product ${E(r.product)}</span></div><div class="bigscore">${Math.round(r.score)}<small>opportunity</small></div></div><div class="scores">${row("Baseline demand",r.baseline)}${row("Demand momentum",r.momentum)}${row("Seasonality",r.seasonality)}${row("Events",r.events_score)}${row("Holiday timing",r.holiday)}</div><div class="grid"><div class="info"><strong>Origins / audience</strong><div>${chips(r.origins)}</div></div><div class="info"><strong>Campaign start / urgency</strong><div>${E(r.start_date||"To be validated")} · ${E(r.urgency)}</div></div><div class="info"><strong>Traveller segments</strong><div>${chips(r.segments)}</div></div><div class="info"><strong>Interests / motivations</strong><div>${chips(r.interests)}</div></div><div class="info"><strong>Destination content</strong><div>${E(r.content)}</div></div><div class="info"><strong>Message angle</strong><div>${E(r.angle)}</div></div></div><div class="section"><strong>Relevant events</strong><div>${chips(r.events)}</div></div><button class="copy" id="copy">Copy agency brief</button>`;Q("copy").onclick=()=>navigator.clipboard?.writeText(r.brief)}function render(){let z=data();Q("kr").textContent=z.length;Q("ks").textContent=z.filter(r=>r.score>=75).length;Q("ka").textContent=z.length?Math.round(z.reduce((a,r)=>a+r.score,0)/z.length):"—";Q("ku").textContent=z.filter(r=>(r.change_7d||0)>=5).length;Q("kc").textContent=z.length?Math.round(z.reduce((a,r)=>a+r.coverage,0)/z.length)+"%":"—";Q("note").textContent=z.length+" routes · sorted by opportunity score";Q("routes").innerHTML=z.map(r=>`<button class="route ${selected===r.origin+r.destination?"selected":""}" data-id="${E(r.origin+r.destination)}"><div class="head"><span>${E(r.origin)} → ${E(r.destination)}</span><span class="route-score">${Math.round(r.score)}</span></div><div class="meta"><span class="pill ${r.action[0]==="A"?"activate":r.action[0]==="P"?"prepare":"monitor"}">${E(r.action)}</span> ${E(r.product)} · momentum ${r.momentum==null?"N/A":Math.round(r.momentum)} · coverage ${Math.round(r.coverage)}%</div></button>`).join("");document.querySelectorAll(".route").forEach(b=>b.onclick=()=>{selected=b.dataset.id;show(z.find(r=>r.origin+r.destination===selected));render()});Q("matrix").querySelectorAll(".dot").forEach(x=>x.remove());z.forEach(r=>{let b=document.createElement("button");b.className="dot";b.style.left=Math.max(2,Math.min(98,r.score))+"%";b.style.bottom=Math.max(2,Math.min(98,r.momentum||0))+"%";b.ariaLabel=`${r.origin} to ${r.destination}, score ${Math.round(r.score)}`;b.onclick=()=>{selected=r.origin+r.destination;show(r);render()};Q("matrix").appendChild(b)})}document.querySelectorAll(".period").forEach(b=>b.onclick=()=>{document.querySelectorAll(".period").forEach(x=>x.classList.remove("active"));b.classList.add("active");period=b.dataset.p;selected="";render()});["action","search"].forEach(x=>Q(x).addEventListener("input",()=>{selected="";render()}));render();
</script></body></html>'''
# Keep the template compact while adding the numeric audience evidence to each
# route brief. This is deliberately event-derived and labelled as such; the
# opportunity database does not contain a full corridor population estimate.
HTML=HTML.replace(
    '<div class="info"><strong>Origins / audience</strong><div>${chips(r.origins)}</div></div>',
    '<div class="info"><strong>Origins / audience</strong><div>${chips(r.origins)}<br><b>Event addressable audience:</b> ${r.addressable_audience==null?"Not available":Number(r.addressable_audience).toLocaleString()}</div></div>'
).replace(
    '<div class="info"><strong>Campaign start / urgency</strong><div>${E(r.start_date||"To be validated")} · ${E(r.urgency)}</div></div>',
    '<div class="info"><strong>Campaign start / urgency</strong><div>${E(r.start_date||"To be validated")} · ${E(r.urgency)}<br><b>Matched events:</b> ${r.event_count}</div></div>'
)
HTML=HTML.replace(
    '<div class="info"><strong>Origins / audience</strong><div>${chips(r.origins)}</div></div>',
    '<div class="info"><strong>Scored corridor</strong><div><b>${E(r.route)}</b><br>Event-derived audience: ${r.addressable_audience==null?"Not available":Number(r.addressable_audience).toLocaleString()}</div></div>'
).replace(
    '<div class="info"><strong>Origins / audience</strong><div>${chips(r.origins)}<br><b>Event addressable audience:</b> ${r.addressable_audience==null?"Not available":Number(r.addressable_audience).toLocaleString()}</div></div>',
    '<div class="info"><strong>Scored corridor</strong><div><b>${E(r.route)}</b><br>Event-derived audience: ${r.addressable_audience==null?"Not available":Number(r.addressable_audience).toLocaleString()}</div></div>'
).replace(
    '<div class="info"><strong>Campaign start / urgency</strong><div>${E(r.start_date||"To be validated")} · ${E(r.urgency)}<br><b>Matched events:</b> ${r.event_count}</div></div>',
    '<div class="info"><strong>Campaign start / urgency</strong><div>${E(r.start_date||"To be validated")} · ${E(r.urgency)}<br><b>Matched events:</b> ${r.event_count}</div></div><div class="info"><strong>Upcoming origin holidays</strong><div>${holidayCards(r)}</div></div>'
).replace(
    '<div class="section"><strong>Relevant events</strong><div>${chips(r.events)}</div></div>',
    '<div class="section"><strong>Destination events supporting this route</strong><p class="sub">These are destination events. Their listed international markets are event context, not proof of Germany→Japan demand.</p><div class="eventcards">${eventCards(r)}</div></div>'
).replace(
    '<div class="scores">${row("Baseline demand",r.baseline)}${row("Demand momentum",r.momentum)}${row("Seasonality",r.seasonality)}${row("Events",r.events_score)}${row("Holiday timing",r.holiday)}</div>',
    '<div class="section"><strong>Why is this corridor recommended?</strong><div class="signaltable">${signalTable(r)}</div></div><div class="scores">${row("Baseline demand",r.baseline)}${row("Demand momentum",r.momentum)}${row("Seasonality",r.seasonality)}${row("Events",r.events_score)}${row("Holiday timing",r.holiday)}</div>'
).replace(
    '.copy{margin-top:13px;',
    '.signal{display:grid;grid-template-columns:1.2fr 45px 1.5fr;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--l);font-size:12px}.signal small,.eventcard small{color:var(--m)}.eventcards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.eventcard{border:1px solid var(--l);border-radius:7px;padding:9px;font-size:12px}.eventcard b,.eventcard small{display:block}.eventcard a{color:var(--p)}.copy{margin-top:13px;'
).replace(
    'let period="current",selected="";',
    'const signalTable=r=>r.signal_sources.map(x=>`<div class="signal"><b>${E(x.name)}</b><span>${x.value==null?"N/A":Math.round(x.value)}</span><small>${E(x.source)} · ${E(x.status)}</small></div>`).join("");const eventCards=r=>r.event_cards.length?r.event_cards.map(x=>`<div class="eventcard"><b>${E(x.name)}</b><small>${E(x.type||"Event")} · ${E(x.start||"Date to validate")}${x.end?" to "+E(x.end):""}</small><div><b>Event markets:</b> ${E(x.markets||"Not specified")}</div><small>${E(x.source||"Source not specified")} ${x.url?`· <a href="${E(x.url)}" target="_blank" rel="noreferrer">source</a>`:""}</small></div>`).join(""):"<span class=\'na\'>No dated destination events matched.</span>";const holidayCards=r=>r.upcoming_holidays.length?r.upcoming_holidays.map(x=>`<span class="chip">${E(x.date)} · ${E(x.name)}</span>`).join(""):"<span class=\'na\'>No upcoming public holidays in the next 120 days.</span>";let period="current",selected="";'
).replace(
    'Q("copy").onclick=()=>navigator.clipboard?.writeText(r.brief)',
    'Q("copy").onclick=async()=>{try{await navigator.clipboard.writeText(r.brief);Q("copy").textContent="Copied"}catch(e){let a=document.createElement("a");a.href="data:text/plain;charset=utf-8,"+encodeURIComponent(r.brief);a.download="agency-brief-"+r.origin+"-"+r.destination+".txt";a.click();Q("copy").textContent="Downloaded brief"}}'
).replace(
    'b.ariaLabel=`${r.origin} to ${r.destination}, score ${Math.round(r.score)}`;',
    'b.ariaLabel=`${r.origin} to ${r.destination}, score ${Math.round(r.score)}`;b.title=`${r.origin} → ${r.destination} · score ${Math.round(r.score)} · momentum ${r.momentum==null?"N/A":Math.round(r.momentum)} · coverage ${Math.round(r.coverage)}%`;'
)
HTML=HTML.replace('${holidayCards(r)}','${holidayCardsForPeriod(r)}').replace(
    'let period="current",selected="";',
    'const holidayCardsForPeriod=r=>{let xs=r.upcoming_holidays;if(period!=="all"){let w=win(period);xs=xs.filter(x=>{let d=new Date(x.date);return d>=w[0]&&d<=w[1]})}return xs.length?xs.map(x=>`<span class="chip">${E(x.date)} · ${E(x.name)}</span>`).join(""):"<span class=\'na\'>No public holidays in the selected period.</span>"};let period="current",selected="";'
).replace('Upcoming origin holidays','Origin holidays in selected period')
HTML=HTML.replace(
    '<div class="info"><strong>Origin holidays in selected period</strong><div>${holidayCardsForPeriod(r)}</div></div>',
    ''
).replace(
    '<div class="section"><strong>Why is this corridor recommended?</strong><div class="signaltable">${signalTable(r)}</div></div>',
    '<div class="section"><strong>Why is this corridor recommended?</strong><div class="signaltable">${signalTable(r)}</div><div class="holidayline"><b>Origin holidays in selected period:</b> ${holidayCardsForPeriod(r)}</div></div>'
)
HTML=HTML.replace('<small>opportunity</small>','<small>SIM opportunity</small>').replace('Opportunity score →','SIM opportunity score →').replace(
    '<span class="pill monitor">Product ${E(r.product)}</span>',
    '<span class="pill monitor">Product ${E(r.product)}</span> ${r.sim_relevance==="Lower"?"<span class=\'pill monitor\'>Lower SIM relevance</span>":""}'
).replace('Event-derived audience: ${r.addressable_audience==null?"Not available":Number(r.addressable_audience).toLocaleString()}','Estimated event audience for this origin: ${r.addressable_audience==null?"Not available":Math.round(r.addressable_audience).toLocaleString("de-DE")}').replace('Number(r.addressable_audience).toLocaleString()','Math.round(r.addressable_audience).toLocaleString("de-DE")').replace(
    '<div><b>Event markets:</b> ${E(x.markets||"Not specified")}</div>',
    '<div><b>Event markets:</b> ${E(x.markets||"Not specified")}</div><div><b>Estimated for this corridor:</b> ${x.corridor_audience==null?"Not available":Math.round(x.corridor_audience).toLocaleString("de-DE")}</div>'
)

# Keep the matrix readable by default while retaining access to every route.
# The chart is deliberately implemented without an external JavaScript
# dependency so the exported HTML remains standalone and agency-shareable.
HTML=HTML.replace(
    '.matrix{margin-top:15px;height:300px;position:relative;border-left:1px solid var(--i);border-bottom:1px solid var(--i);background:linear-gradient(transparent 49.8%,#eee 50%,transparent 50.2%),linear-gradient(90deg,transparent 49.8%,#eee 50%,transparent 50.2%)}',
    '.matrix-controls{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:8px 0 4px}.matrix-controls select,.matrix-controls button{border:1px solid #bbb;border-radius:6px;background:#fff;padding:5px 8px;font:inherit}.matrix-controls button{cursor:pointer}.matrix-controls button:hover{background:#fff0f7}.matrix{margin-top:8px;height:340px;overflow:auto;position:relative;border-left:1px solid var(--i);border-bottom:1px solid var(--i);background:#fff}.matrix-plot{position:relative;width:100%;min-width:100%;height:300px;background:linear-gradient(transparent 49.8%,#eee 50%,transparent 50.2%),linear-gradient(90deg,transparent 49.8%,#eee 50%,transparent 50.2%)}'
).replace(
    '<h2>Opportunity matrix</h2><p class="sub">Momentum vertically; overall opportunity score horizontally.</p><div class="matrix" id="matrix"><span class="axisx">SIM opportunity score →</span><span class="axisy">Momentum →</span></div>',
    '<h2>Opportunity matrix</h2><p class="sub">Top routes are shown first. Use All and zoom for the complete corridor population.</p><div class="matrix-controls"><label for="matrixLimit">Show:</label><select id="matrixLimit"><option value="20" selected>Top 20</option><option value="50">Top 50</option><option value="all">All routes</option></select><button id="zoomOut" type="button">−</button><span id="zoomValue">100%</span><button id="zoomIn" type="button">+</button><button id="zoomReset" type="button">Reset zoom</button></div><div class="matrix" id="matrix"><div class="matrix-plot" id="matrixPlot"></div><span class="axisx">SIM opportunity score →</span><span class="axisy">Momentum →</span></div>'
).replace(
    'const DATA=__DATA__,Q=x=>document.getElementById(x),N=x=>x==null||x===""?null:Number(x),E=x=>String(x??"").replace(/[&<>"\']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\'":"&#39;"}));',
    'const DATA=__DATA__,Q=x=>document.getElementById(x),N=x=>x==null||x===""?null:Number(x),E=x=>String(x??"").replace(/[&<>"\']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","\'":"&#39;"}));let matrixLimit="20",matrixZoom=1;'
).replace(
    'Q("matrix").querySelectorAll(".dot").forEach(x=>x.remove());z.forEach(r=>{',
    'Q("matrixPlot").innerHTML="";let plotted=matrixLimit==="all"?z:z.slice(0,Number(matrixLimit));plotted.forEach(r=>{'
).replace(
    'Q("matrix").appendChild(b)',
    'Q("matrixPlot").appendChild(b)'
).replace(
    'document.querySelectorAll("[class]")',
    'document.querySelectorAll("[class]")'
)
HTML=HTML.replace(
    'document.querySelectorAll("[class]")',
    'document.querySelectorAll("[class]")'
)
# Add matrix controls immediately before the initial render call.
HTML=HTML.replace(
    'render();\n</script>',
    'function applyMatrixZoom(){let p=Q("matrixPlot");p.style.width=(100*matrixZoom)+"%";p.style.height=(300*matrixZoom)+"px";Q("zoomValue").textContent=Math.round(matrixZoom*100)+"%"}Q("matrixLimit").onchange=e=>{matrixLimit=e.target.value;render()};Q("zoomIn").onclick=()=>{matrixZoom=Math.min(3,matrixZoom+.25);applyMatrixZoom()};Q("zoomOut").onclick=()=>{matrixZoom=Math.max(1,matrixZoom-.25);applyMatrixZoom()};Q("zoomReset").onclick=()=>{matrixZoom=1;applyMatrixZoom()};applyMatrixZoom();render();\n</script>'
)
# The earlier HTML transformations intentionally preserve the standalone
# template; this short replacement works for both the original and enriched
# dashboard variants.
HTML=HTML.replace(
    'const DATA=__DATA__,Q=',
    'let matrixLimit="20",matrixZoom=1;const DATA=__DATA__,Q=',
    1,
)
# Keep axis labels inside the visible plotting area. The matrix container is
# scrollable, so labels positioned outside it would otherwise be clipped.
HTML=HTML.replace(
    '.axisx{bottom:-25px;left:50%;transform:translateX(-50%)}.axisy{left:-37px;top:50%;transform:rotate(-90deg)}',
    '.axisx{bottom:5px;left:50%;transform:translateX(-50%);background:#fff;padding:2px 6px;z-index:3}.axisy{left:4px;top:50%;transform:rotate(-90deg);background:#fff;padding:2px 6px;z-index:3}'
).replace(
    '.note{font-size:12px;color:var(--m);margin-top:13px}',
    '.matrix-meta{font-size:11px;color:var(--m);margin-top:5px}.note{font-size:12px;color:var(--m);margin-top:13px}'
)
HTML=HTML.replace(
    '<span class="axisy">Momentum →</span></div><p class="note">',
    '<span class="axisy">Momentum →</span></div><div class="matrix-meta" id="matrixMeta"></div><p class="note">'
)
# A missing momentum value is not a zero. Do not place those routes on the
# bottom edge; report them separately below the chart instead.
HTML=HTML.replace(
    'Q("matrixPlot").innerHTML="";let plotted=matrixLimit==="all"?z:z.slice(0,Number(matrixLimit));plotted.forEach(r=>{',
    'Q("matrixPlot").innerHTML="";let candidates=z.filter(r=>r.momentum!=null);let plotted=matrixLimit==="all"?candidates:candidates.slice(0,Number(matrixLimit));Q("matrixMeta").textContent=plotted.length+" plotted · "+(z.length-candidates.length)+" omitted because momentum is unavailable.";plotted.forEach(r=>{'
)
HTML=HTML.replace('if(!r.start_date)return true','if(!r.start_date)return (r.score>=60||r.sales_units!=null||r.momentum!=null)')
# The primary chart is a ranked commercial view: audience multiplied by the
# explainable SIM opportunity score. Momentum remains visible in each route's
# evidence panel, but is not the chart axis because it is often unavailable.
HTML=HTML.replace(
    '.matrix{margin-top:8px;height:340px;overflow:auto;position:relative;border-left:1px solid var(--i);border-bottom:1px solid var(--i);background:#fff}.matrix-plot{position:relative;width:100%;min-width:100%;height:300px;background:linear-gradient(transparent 49.8%,#eee 50%,transparent 50.2%),linear-gradient(90deg,transparent 49.8%,#eee 50%,transparent 50.2%)}',
    '.matrix{margin-top:8px;height:340px;overflow:auto;position:relative;border:1px solid var(--l);border-radius:7px;background:#fff}.matrix-plot{position:relative;width:100%;min-width:620px;min-height:300px;padding:8px 12px}.bar-row{display:grid;grid-template-columns:105px minmax(180px,1fr) 110px;gap:9px;align-items:center;width:100%;min-height:30px;margin:2px 0;border:0;background:transparent;text-align:left;cursor:pointer;color:inherit;font:inherit}.bar-row:hover{background:#fff0f7}.bar-label{font-weight:bold;font-size:12px}.bar-track{height:17px;background:#eee;border-radius:4px;overflow:hidden}.bar-fill{display:block;height:100%;background:var(--p);border-radius:4px}.bar-value{font-size:12px;color:var(--m);text-align:right;white-space:nowrap}.momentum-section{margin-top:24px;padding-top:16px;border-top:1px solid var(--l)}.momentum-chart{max-height:420px;overflow:auto;border:1px solid var(--l);border-radius:7px;padding:8px 12px}.momentum-row{display:grid;grid-template-columns:105px minmax(180px,1fr) 110px;gap:9px;align-items:center;width:100%;min-height:30px;margin:2px 0;border:0;background:transparent;text-align:left;cursor:pointer;color:inherit;font:inherit}.momentum-row:hover{background:#eef2ff}.momentum-fill{display:block;height:100%;background:var(--b);border-radius:4px}.scatter{height:340px;position:relative;overflow:hidden}.scatter .dot{z-index:2}'
).replace(
    '<h2>Opportunity matrix</h2><p class="sub">Top routes are shown first. Use All and zoom for the complete corridor population.</p><div class="matrix-controls"><label for="matrixLimit">Show:</label><select id="matrixLimit"><option value="20" selected>Top 20</option><option value="50">Top 50</option><option value="all">All routes</option></select><button id="zoomOut" type="button">−</button><span id="zoomValue">100%</span><button id="zoomIn" type="button">+</button><button id="zoomReset" type="button">Reset zoom</button></div><div class="matrix" id="matrix"><div class="matrix-plot" id="matrixPlot"></div><span class="axisx">SIM opportunity score →</span><span class="axisy">Momentum →</span></div>',
    '<h2>Opportunity-weighted audience</h2><p class="sub">Estimated addressable travellers × SIM opportunity score. This is a prioritisation index, not forecast revenue. The route list is sorted by SIM opportunity score; this chart is sorted by weighted audience.</p><div class="matrix-controls"><label for="matrixLimit">Show:</label><select id="matrixLimit"><option value="20" selected>Top 20</option><option value="50">Top 50</option><option value="all">All routes</option></select><button id="zoomOut" type="button">−</button><span id="zoomValue">100%</span><button id="zoomIn" type="button">+</button><button id="zoomReset" type="button">Reset size</button></div><div class="matrix" id="matrix"><div class="matrix-plot" id="matrixPlot"></div></div>'
)
old_plot='Q("matrixPlot").innerHTML="";let candidates=z.filter(r=>r.momentum!=null);let plotted=matrixLimit==="all"?candidates:candidates.slice(0,Number(matrixLimit));Q("matrixMeta").textContent=plotted.length+" plotted · "+(z.length-candidates.length)+" omitted because momentum is unavailable.";plotted.forEach(r=>{let b=document.createElement("button");b.className="dot";b.style.left=Math.max(2,Math.min(98,r.score))+"%";b.style.bottom=Math.max(2,Math.min(98,r.momentum||0))+"%";b.ariaLabel=`${r.origin} to ${r.destination}, score ${Math.round(r.score)}`;b.title=`${r.origin} → ${r.destination} · score ${Math.round(r.score)} · momentum ${r.momentum==null?"N/A":Math.round(r.momentum)} · coverage ${Math.round(r.coverage)}%`;b.onclick=()=>{selected=r.origin+r.destination;show(r);render()};Q("matrixPlot").appendChild(b)})'
new_plot='Q("matrixPlot").innerHTML="";let candidates=z.filter(r=>r.weighted_opportunity!=null).sort((a,b)=>b.weighted_opportunity-a.weighted_opportunity);let plotted=matrixLimit==="all"?candidates:candidates.slice(0,Number(matrixLimit));let maxOpportunity=plotted.length?Math.max(...plotted.map(r=>r.weighted_opportunity)):0;Q("matrixMeta").textContent=plotted.length+" plotted · "+(z.length-candidates.length)+" omitted because no event audience estimate is available.";plotted.forEach(r=>{let b=document.createElement("button");b.className="bar-row";let width=maxOpportunity?Math.max(2,100*r.weighted_opportunity/maxOpportunity):0;b.innerHTML=`<span class="bar-label">${E(r.route)}</span><span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span><span class="bar-value">${Math.round(r.weighted_opportunity).toLocaleString("de-DE")} · ${Math.round(r.score)} score</span>`;b.title=`${r.route} · weighted audience ${Math.round(r.weighted_opportunity).toLocaleString("de-DE")} · audience ${r.addressable_audience==null?"N/A":Math.round(r.addressable_audience).toLocaleString("de-DE")} · score ${Math.round(r.score)} · momentum ${r.momentum==null?"N/A":Math.round(r.momentum)}`;b.onclick=()=>{selected=r.origin+r.destination;show(r);render()};Q("matrixPlot").appendChild(b)});renderMomentum(z);renderScatter(z)'
HTML=HTML.replace(old_plot,new_plot)
HTML=HTML.replace(
    '<h2>${E(r.origin)} → ${E(r.destination)}</h2>',
    '<h2>${E(r.route)}</h2>'
).replace(
    '<span>${E(r.origin)} → ${E(r.destination)}</span>',
    '<span>${E(r.route)}</span>'
).replace(
    'r.origin+" "+r.destination+" "+r.segments.join(" ")+" "+r.interests.join(" ")',
    'r.route+" "+r.segments.join(" ")+" "+r.interests.join(" ")'
)
# Add a separate momentum diagnostic below the commercial audience ranking.
HTML=HTML.replace(
    '</section></div></main><script>',
    '<div class="momentum-section"><h2>Demand momentum diagnostic</h2><p class="sub">Search momentum shows acceleration in interest; it is a signal, not an audience-size estimate. It uses the same month/filter selection as the route list.</p><div class="momentum-chart" id="momentumPlot"></div><div class="matrix-meta" id="momentumMeta"></div><h2 style="margin-top:22px">Opportunity / momentum scatter</h2><p class="sub">Each dot is a route. Further right means higher SIM opportunity; higher means stronger demand momentum. Hover for details.</p><div class="matrix scatter" id="scatterPlot"><span class="axisx">SIM opportunity score →</span><span class="axisy">Demand momentum →</span></div><div class="matrix-meta" id="scatterMeta"></div></div></section></div></main><script>'
)
HTML=HTML.replace(
    '<script>\nlet matrixLimit="20",matrixZoom=1;const DATA=',
    '<script>\nfunction renderMomentum(z){let box=Q("momentumPlot");if(!box)return;box.innerHTML="";let candidates=z.filter(r=>r.momentum!=null).sort((a,b)=>b.momentum-a.momentum);let plotted=matrixLimit==="all"?candidates:candidates.slice(0,Number(matrixLimit));Q("momentumMeta").textContent=plotted.length+" plotted · "+(z.length-candidates.length)+" without momentum data.";plotted.forEach(r=>{let b=document.createElement("button");b.className="momentum-row";let width=Math.max(2,Math.min(100,r.momentum));b.innerHTML=`<span class="bar-label">${E(r.route)}</span><span class="bar-track"><span class="momentum-fill" style="width:${width}%"></span></span><span class="bar-value">${Math.round(r.momentum)} momentum · ${Math.round(r.score)} score</span>`;b.title=`${r.route} · momentum ${Math.round(r.momentum)} · score ${Math.round(r.score)} · coverage ${Math.round(r.coverage)}%`;b.onclick=()=>{selected=r.origin+r.destination;show(r);render()};box.appendChild(b)})} function renderScatter(z){let box=Q("scatterPlot");if(!box)return;box.querySelectorAll(".dot").forEach(x=>x.remove());let candidates=z.filter(r=>r.momentum!=null);let plotted=matrixLimit==="all"?candidates:candidates.slice(0,Number(matrixLimit));Q("scatterMeta").textContent=plotted.length+" plotted · "+(z.length-candidates.length)+" without momentum data.";plotted.forEach(r=>{let b=document.createElement("button");b.className="dot";b.style.left=Math.max(2,Math.min(98,r.score))+"%";b.style.bottom=Math.max(2,Math.min(98,r.momentum))+"%";b.ariaLabel=`${r.route}, score ${Math.round(r.score)}, momentum ${Math.round(r.momentum)}`;b.title=`${r.route} · score ${Math.round(r.score)} · momentum ${Math.round(r.momentum)} · coverage ${Math.round(r.coverage)}%`;b.onclick=()=>{selected=r.origin+r.destination;show(r);render()};box.appendChild(b)})} let matrixLimit="20",matrixZoom=1;const DATA='
)
# Add first-party sales to the visible evidence panel without changing the
# existing standalone dashboard structure.
HTML=HTML.replace(
    '${row("Baseline demand",r.baseline)}${row("Demand momentum",r.momentum)}${row("Seasonality",r.seasonality)}${row("Events",r.events_score)}${row("Holiday timing",r.holiday)}',
    '${row("Baseline demand",r.baseline)}${row("Demand momentum",r.momentum)}${row("Observed sales (60d)",r.sales_score)}${row("Seasonality",r.seasonality)}${row("Events",r.events_score)}${row("Holiday timing",r.holiday)}${row("Travel distance",r.distance_score)}'
)
HTML=HTML.replace(
    '<div class="eyebrow">Agency campaign brief</div>',
    '<div class="eyebrow">Agency campaign brief · ${E(r.route_type)}</div>'
)
HTML=HTML.replace(
    'Weights: baseline 20%, momentum 25%, seasonality 20%, events 20%, holiday timing 15%. Missing signals reduce coverage, not the component score.',
    'Weights: baseline 18%, momentum 18%, observed sales 25%, seasonality 10%, events 15%, holiday timing 9%, distance 5%. Distance is a modest travel-friction factor, not a demand estimate.'
)

# Independent origin/destination filters and expandable KPI summaries.
HTML=HTML.replace(
    '<input id="search" placeholder="Search origin or destination">',
    '<select id="originFilter"><option value="all">All origins</option></select><select id="destinationFilter"><option value="all">All destinations</option></select><input id="search" placeholder="Search route or segment">'
)
HTML=HTML.replace(
    '<div class="layout">',
    '<section class="kpi-detail" id="kpiDetail" hidden></section><div class="layout">',
    1,
)
HTML=HTML.replace(
    '</style>',
    '.toolbar select{min-width:145px}.kpi{cursor:pointer}.kpi:hover{box-shadow:0 2px 8px #0002}.kpi-detail{background:#fff;border:1px solid var(--l);border-radius:10px;padding:13px 15px;margin-bottom:17px}.kpi-detail h3{margin:0 0 8px;font-size:16px}.kpi-detail-list{display:grid;grid-template-columns:repeat(2,minmax(220px,1fr));gap:5px 20px}.kpi-detail-list button{border:0;border-bottom:1px solid var(--l);background:transparent;text-align:left;padding:6px 2px;cursor:pointer;font:inherit}.kpi-detail-list button:hover{background:#fff0f7}@media(max-width:700px){.kpi-detail-list{grid-template-columns:1fr}}\n</style>',
    1,
)
HTML=HTML.replace(
    'return DATA.filter(r=>inside(r)&&(a==="all"||r.action===a)&&',
    'return DATA.filter(r=>inside(r)&&(originFilter==="all"||r.origin===originFilter)&&(destinationFilter==="all"||r.destination===destinationFilter)&&(a==="all"||r.action===a)&&',
    1,
)
HTML=HTML.replace(
    'function render(){let z=data();Q("kr")',
    'function render(){let z=data();Q("kr")',
    1,
)
HTML=HTML.replace(
    'const DATA=__DATA__,Q=',
    'let originFilter="all",destinationFilter="all",kpiOpen="";const DATA=__DATA__,Q=',
    1,
)
HTML=HTML.replace(
    '</script></body></html>',
    '''</script><script>
function fillSelect(id,values,labeler){let el=Q(id);values.sort((a,b)=>a.localeCompare(b)).forEach(x=>{let o=document.createElement("option");o.value=x;o.textContent=labeler(x);el.appendChild(o)})}
function renderKpiDetail(z){let box=Q("kpiDetail");if(!kpiOpen){box.hidden=true;return}box.hidden=false;let title="",rows=[];if(kpiOpen==="routes"){title="All routes shown";rows=z.slice(0,30)}else if(kpiOpen==="strong"){title="Strong opportunities";rows=z.filter(r=>r.score>=75)}else if(kpiOpen==="accelerating"){title="Accelerating routes";rows=z.filter(r=>(r.change_7d||0)>=5).sort((a,b)=>(b.change_7d||0)-(a.change_7d||0))}else if(kpiOpen==="coverage"){title="Routes by evidence coverage";rows=[...z].sort((a,b)=>b.coverage-a.coverage).slice(0,30)}else{title="Top routes by score";rows=z.slice(0,15)}box.innerHTML=`<h3>${E(title)} <span class="na">· click a route to open its brief</span></h3><div class="kpi-detail-list">${rows.length?rows.map(r=>`<button data-kpi-route="${E(r.origin+r.destination)}"><b>${E(r.route)}</b> · score ${Math.round(r.score)} · ${r.change_7d==null?"change N/A":(r.change_7d>=0?"+":"")+Math.round(r.change_7d)+" 7d"}</button>`).join(""):"<span class='na'>No routes match this KPI and filter.</span>"}</div>`;box.querySelectorAll("[data-kpi-route]").forEach(b=>b.onclick=()=>{selected=b.dataset.kpiRoute;show(z.find(r=>r.origin+r.destination===selected));window.scrollTo({top:0,behavior:"smooth"});})}
function initKpiAndFilters(){let origins=[...new Set(DATA.map(r=>r.origin))],destinations=[...new Set(DATA.map(r=>r.destination))];fillSelect("originFilter",origins,x=>{let r=DATA.find(y=>y.origin===x);return x+(r&&r.origin_name?" "+r.origin_name:"")});fillSelect("destinationFilter",destinations,x=>{let r=DATA.find(y=>y.destination===x);return x+(r&&r.destination_name?" "+r.destination_name:"")});Q("originFilter").onchange=e=>{originFilter=e.target.value;selected="";render()};Q("destinationFilter").onchange=e=>{destinationFilter=e.target.value;selected="";render()};let keys=["routes","strong","average","accelerating","coverage"];document.querySelectorAll(".kpi").forEach((el,i)=>{el.tabIndex=0;el.onclick=()=>{kpiOpen=kpiOpen===keys[i]?"":keys[i];render()};el.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();el.click()}}})}
initKpiAndFilters();render();
</script></body></html>''',
)
HTML=HTML.replace(
    'kpiOpen=kpiOpen===keys[i]?"":keys[i];render()',
    'kpiOpen=kpiOpen===keys[i]?"":keys[i];renderKpiDetail(data());render()',
)
HTML=HTML.replace(
    'function render(){let z=data();Q("kr")',
    'function render(){let z=data();if(typeof renderKpiDetail==="function")renderKpiDetail(z);Q("kr")',
    1,
)
if __name__ == "__main__":
    p=argparse.ArgumentParser(description="Build T-Travel agency opportunity dashboard")
    p.add_argument("--database",default="events.db"); p.add_argument("--output",default="campaign_opportunity_dashboard.html")
    p.add_argument("--input-dir",default=None,help="Folder containing origins.csv and destination_aliases.csv")
    a=p.parse_args(); build(a.database,a.output,a.input_dir)
