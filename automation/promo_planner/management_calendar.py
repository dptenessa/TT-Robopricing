from __future__ import annotations

import html
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from .store import Campaign, PromoStore


def _parse(d: str) -> date | None:
    if not d:
        return None
    return datetime.strptime(d, "%Y-%m-%d").date()


def _state(store: PromoStore, c: Campaign, today: date) -> tuple[str, str]:
    started = store.has_execution(c.id, "START")
    ended = store.has_execution(c.id, "END")
    imported = c.status == "ACTIVE_IMPORTED"
    start = _parse(c.start_date)
    end = _parse(c.end_date)
    if ended:
        return "Expired", "expired"
    if imported and end is not None and end < today:
        return "Removal overdue", "overdue"
    if imported:
        return "Published Â· imported manually", "published"
    if started and end is not None and end < today:
        return "Removal overdue", "overdue"
    if started:
        return "Published", "published"
    if end is not None and end < today:
        return "Expired Â· never published", "missed"
    if start is not None and start <= today:
        return "Start due", "due"
    return "Planned", "planned"


def _campaign_payload(store: PromoStore, campaigns: list[Campaign], today: date) -> list[dict]:
    out: list[dict] = []
    for c in campaigns:
        state, key = _state(store, c, today)
        visual_start = c.display_start_date
        visual_end = c.end_date or (today + timedelta(days=180)).isoformat()
        out.append({
            "id": c.id,
            "name": c.name,
            "start": visual_start,
            "end": visual_end,
            "actual_start": c.start_date,
            "actual_end": c.end_date,
            "imported": c.status == "ACTIVE_IMPORTED",
            "promo": c.promo_name or c.promo_code,
            "promo_code": c.promo_code,
            "destinations": c.destinations,
            "plans": c.plans,
            "days": c.days,
            "state": state,
            "state_key": key,
            "notes": c.notes or "",
        })
    return out


def _key_dates(payload: list[dict], today: date) -> list[dict]:
    items: list[dict] = []
    for c in payload:
        start = _parse(c.get("actual_start", ""))
        end = _parse(c.get("actual_end", ""))
        if not c.get("imported") and start is not None:
            items.append({"date": start.isoformat(), "kind": "PUBLISH", "label": f"Publish {c['name']} to current prices", "campaign": c["name"]})
        if end is not None:
            items.append({"date": end.isoformat(), "kind": "REMOVE", "label": f"Remove {c['name']} from current prices", "campaign": c["name"]})
    items.sort(key=lambda x: (x["date"], x["kind"], x["campaign"]))
    return [x for x in items if x["date"] >= (today - timedelta(days=7)).isoformat()]


def generate_management_calendar(store: PromoStore, out_path: Path, open_after: bool = True) -> Path:
    today = date.today()
    campaigns = store.list_campaigns()
    payload = _campaign_payload(store, campaigns, today)
    key_dates = _key_dates(payload, today)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data_json = json.dumps(payload, ensure_ascii=False)
    key_json = json.dumps(key_dates, ensure_ascii=False)
    generated = datetime.now().strftime("%d %b %Y %H:%M")

    doc = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>T-Travel Promotion Plan</title>
<style>
:root{{--magenta:#e20074;--ink:#202124;--muted:#68707b;--line:#e7e9ee;--paper:#fff;--bg:#f5f6f8;--planned:#6b8fdc;--published:#27885a;--overdue:#d97706;--expired:#a4a9b1;--missed:#8c6f9e;--due:#e20074}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
.wrap{{max-width:1600px;margin:0 auto;padding:28px}} .head{{display:flex;gap:20px;align-items:flex-end;justify-content:space-between;margin-bottom:18px;flex-wrap:wrap}}
h1{{margin:0;font-size:30px}} .sub{{color:var(--muted);font-size:13px;margin-top:5px}} .controls{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
button{{border:1px solid #d8dbe2;background:white;border-radius:8px;padding:8px 12px;font-weight:600;cursor:pointer}} button.active{{background:var(--magenta);color:white;border-color:var(--magenta)}}
.cards{{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px;margin:16px 0}} .card{{background:white;border:1px solid var(--line);border-radius:10px;padding:14px 16px}} .num{{font-size:26px;font-weight:700}} .lbl{{font-size:12px;color:var(--muted);margin-top:3px}}
.legend{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:8px 0 12px}} .dot{{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}}
.panel{{background:white;border:1px solid var(--line);border-radius:12px;overflow:hidden}} .timeline-scroll{{overflow-x:auto}} .timeline{{min-width:1100px;position:relative}}
.months{{display:grid;margin-left:230px;border-bottom:1px solid var(--line);height:48px}} .month{{border-left:1px solid var(--line);padding:8px 10px;font-size:12px;font-weight:700;color:#4d5560}}
.row{{display:grid;grid-template-columns:230px 1fr;min-height:54px;border-bottom:1px solid #f0f1f4}} .row:last-child{{border-bottom:0}} .name{{padding:10px 12px;position:sticky;left:0;background:white;z-index:4;border-right:1px solid var(--line)}} .name strong{{display:block;font-size:13px}} .name span{{font-size:11px;color:var(--muted)}}
.track{{position:relative;background-image:linear-gradient(to right, transparent calc(100% - 1px), var(--line) calc(100% - 1px));background-size:var(--month-w,16.666%) 100%}}
.bar{{position:absolute;top:13px;height:28px;border-radius:7px;padding:6px 9px;color:white;font-size:11px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:14px}}
.bar.planned{{background:var(--planned)}} .bar.published{{background:var(--published)}} .bar.overdue{{background:var(--overdue)}} .bar.expired{{background:var(--expired)}} .bar.missed{{background:var(--missed)}} .bar.due{{background:var(--due)}}
.today{{position:absolute;top:48px;bottom:0;width:2px;background:var(--magenta);z-index:3;pointer-events:none}} .today:before{{content:'TODAY';position:absolute;top:-20px;left:4px;color:var(--magenta);font-size:10px;font-weight:800}}
.empty{{padding:35px;color:var(--muted)}} .section-title{{display:flex;justify-content:space-between;align-items:center;margin:26px 0 10px}} .section-title h2{{font-size:20px;margin:0}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid var(--line);border-radius:12px;overflow:hidden}} th,td{{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:12px}} th{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}} tr:last-child td{{border-bottom:0}} .pill{{font-size:10px;font-weight:800;border-radius:999px;padding:4px 8px;background:#f0f1f4}} .pill.publish{{background:#e9f7ef;color:#147a45}} .pill.remove{{background:#fff1e3;color:#a65300}} .pill.reminder{{background:#fce7f2;color:#b0005a}}
@media(max-width:900px){{.wrap{{padding:14px}}.cards{{grid-template-columns:repeat(2,1fr)}}.row{{grid-template-columns:180px 1fr}}.months{{margin-left:180px}}}}
@media print{{body{{background:white}}.controls{{display:none}}.wrap{{max-width:none;padding:10px}}.timeline-scroll{{overflow:visible}}.timeline{{min-width:0}}.panel{{break-inside:avoid}}}}
</style>
</head>
<body>
<div class=\"wrap\">
  <div class=\"head\"><div><h1>T-Travel Promotion Plan</h1><div class=\"sub\">Management timeline Â· generated {html.escape(generated)} Â· current status is based on actual publication to manual prices</div></div>
  <div class=\"controls\"><span class=\"sub\">Horizon</span><button data-months=\"3\">3 months</button><button class=\"active\" data-months=\"6\">6 months</button><button data-months=\"12\">12 months</button><button id=\"expiredToggle\">Hide expired</button></div></div>
  <div class=\"cards\" id=\"cards\"></div>
  <div class=\"legend\"><span><i class=\"dot\" style=\"background:var(--planned)\"></i>Planned</span><span><i class=\"dot\" style=\"background:var(--published)\"></i>Published</span><span><i class=\"dot\" style=\"background:var(--overdue)\"></i>Removal overdue</span><span><i class=\"dot\" style=\"background:var(--expired)\"></i>Expired</span></div>
  <div class=\"panel timeline-scroll\"><div class=\"timeline\" id=\"timeline\"></div></div>
  <div class=\"section-title\"><h2>Next key dates</h2><div class=\"sub\">Campaign changes applied at midnight on each date</div></div>
  <table><thead><tr><th>Date</th><th>Action</th><th>Campaign</th><th>What happens</th></tr></thead><tbody id=\"keyDates\"></tbody></table>
</div>
<script>
const campaigns={data_json};
const keyDates={key_json};
const today=new Date('{today.isoformat()}T00:00:00');
let months=6, hideExpired=false;
const parse=s=>new Date(s+'T00:00:00');
const fmt=d=>d.toLocaleDateString('en-GB',{{day:'2-digit',month:'short',year:'numeric'}});
const esc=s=>String(s).replace(/[&<>\"]/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[m]));
function monthStart(d){{return new Date(d.getFullYear(),d.getMonth(),1)}}
function addMonths(d,n){{return new Date(d.getFullYear(),d.getMonth()+n,1)}}
function render(){{
  document.querySelectorAll('[data-months]').forEach(b=>b.classList.toggle('active',Number(b.dataset.months)===months));
  const start=monthStart(today), end=addMonths(start,months); const span=end-start;
  const visible=campaigns.filter(c=>parse(c.end)>=start && parse(c.start)<end && !(hideExpired && (c.state_key==='expired'||c.state_key==='missed')));
  const counts={{planned:0,published:0,overdue:0,expired:0,due:0,missed:0}}; campaigns.forEach(c=>counts[c.state_key]=(counts[c.state_key]||0)+1);
  document.getElementById('cards').innerHTML=[['Planned / due',counts.planned+counts.due],['Published',counts.published],['Removal overdue',counts.overdue],['Expired',counts.expired+counts.missed],['Total campaigns',campaigns.length]].map(x=>`<div class=\"card\"><div class=\"num\">${{x[1]}}</div><div class=\"lbl\">${{x[0]}}</div></div>`).join('');
  const tl=document.getElementById('timeline'); tl.innerHTML=''; tl.style.setProperty('--month-w',(100/months)+'%');
  const mh=document.createElement('div'); mh.className='months'; mh.style.gridTemplateColumns=`repeat(${{months}},1fr)`; for(let i=0;i<months;i++){{let d=addMonths(start,i); let x=document.createElement('div');x.className='month';x.textContent=d.toLocaleDateString('en-GB',{{month:'short',year:'numeric'}});mh.appendChild(x)}} tl.appendChild(mh);
  if(!visible.length){{tl.insertAdjacentHTML('beforeend','<div class=\"empty\">No campaigns in this horizon.</div>');return}}
  visible.forEach(c=>{{const row=document.createElement('div');row.className='row'; row.innerHTML=`<div class=\"name\"><strong>${{esc(c.name)}}</strong><span>${{esc(c.promo)}} Â· ${{esc(c.state)}}</span></div><div class=\"track\"></div>`; const tr=row.querySelector('.track'); const cs=Math.max(parse(c.start),start), ce=Math.min(new Date(parse(c.end).getTime()+86400000),end); const left=Math.max(0,(cs-start)/span*100), width=Math.max(.35,(ce-cs)/span*100); const bar=document.createElement('div');bar.className='bar '+c.state_key;bar.style.left=left+'%';bar.style.width=width+'%';bar.title=`${{c.name}}\n${{c.start}} â†’ ${{c.end}}\n${{c.promo}}\n${{c.state}}`;bar.textContent=c.name;tr.appendChild(bar);tl.appendChild(row)}});
  if(today>=start&&today<end){{const line=document.createElement('div');line.className='today';line.style.left=`calc(230px + ${{(today-start)/span*100}}% * (100% - 230px) / 100)`; /* fallback corrected below */ tl.appendChild(line); requestAnimationFrame(()=>{{const w=tl.clientWidth-230;line.style.left=(230+w*((today-start)/span))+'px'}})}}
  renderKeys();
}}
function renderKeys(){{const horizon=addMonths(monthStart(today),months); const rows=keyDates.filter(k=>parse(k.date)<horizon).slice(0,40); document.getElementById('keyDates').innerHTML=rows.length?rows.map(k=>`<tr><td>${{fmt(parse(k.date))}}</td><td><span class=\"pill ${{k.kind.toLowerCase()}}\">${{esc(k.kind)}}</span></td><td>${{esc(k.campaign)}}</td><td>${{esc(k.label)}}</td></tr>`).join(''):'<tr><td colspan=\"4\" class=\"empty\">No upcoming key dates.</td></tr>'}}
document.querySelectorAll('[data-months]').forEach(b=>b.addEventListener('click',()=>{{months=Number(b.dataset.months);render()}}));
document.getElementById('expiredToggle').addEventListener('click',e=>{{hideExpired=!hideExpired;e.target.textContent=hideExpired?'Show expired':'Hide expired';render()}});
render();
</script>
</body></html>"""

    out_path.write_text(doc, encoding="utf-8")
    if open_after and os.name == "nt":
        os.startfile(out_path)  # type: ignore[attr-defined]
    return out_path

