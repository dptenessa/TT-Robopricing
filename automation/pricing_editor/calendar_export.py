from __future__ import annotations

import html
import json
import os
from datetime import date, datetime
from pathlib import Path
import pandas as pd

from .engine import _num, _text, round_promo_price
from .store import Campaign, PromoStore
try:
    from automation.pricing_timeline import PricingTimeline
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from pricing_timeline import PricingTimeline


def _display_product_name(name: str) -> str:
    """Use requested colleague-facing labels while retaining source matching."""
    key = " ".join(str(name or "").strip().casefold().split())
    return {
        "basic": "Lite",
        "medium": "Standard",
        "moderate": "Standard",
        "large": "Max",
        "unlimited": "Unlimited",
    }.get(key, str(name or ""))


def _destination_names(pricebook: pd.DataFrame) -> dict[str, str]:
    """Read destination display names from the shared pricing projection."""
    names: dict[str, str] = {}
    for row in pricebook.to_dict(orient="records"):
        iso = _text(row.get("ISO")).upper()
        country = _text(row.get("Country"))
        if iso and country:
            names.setdefault(iso, country)
    return names


def _campaign_state(store: PromoStore, campaign: Campaign) -> tuple[str, str]:
    """Return the planner's explicit publication state; dates alone never imply applied."""
    if campaign.official_partner_pack or campaign.official_partner_pack_at or store.has_execution(campaign.id, "START"):
        return "Published", "applied"
    if campaign.status == "ACTIVE_IMPORTED":
        return "Published (imported)", "applied"
    return "Scheduled", "scheduled"


def _campaign_prices(pricebook: pd.DataFrame, campaign: Campaign) -> list[dict]:
    """Snapshot list and promo prices for each affected product, duration and currency."""
    required = {"ISO", "Plan", "Days"}
    if not required.issubset(set(pricebook.columns)):
        return []
    grouped: dict[tuple[str, str, str, int, str], set[tuple[float, float]]] = {}
    destinations = {str(code).upper() for code in campaign.destinations}
    plans = set(campaign.plans)
    days = {int(day) for day in campaign.days}
    for row in pricebook.to_dict(orient="records"):
        iso = _text(row.get("ISO")).upper()
        plan = _text(row.get("Plan"))
        country = _text(row.get("Country"))
        day_value = _num(row.get("Days"))
        if iso not in destinations or plan not in plans or day_value is None or int(day_value) not in days:
            continue
        day = int(day_value)
        for currency in ("EUR", "USD"):
            list_price = _num(row.get(f"Price_{currency}"))
            if list_price is None:
                continue
            promo_price = round_promo_price(list_price, campaign.promo_type, campaign.promo_value)
            grouped.setdefault((iso, country, plan, day, currency), set()).add((round(list_price, 2), promo_price))

    result = []
    for (iso, country, plan, day, currency), prices in sorted(grouped.items()):
        for list_price, promo_price in sorted(prices):
            result.append({
                "destination": country or iso,
                "iso": iso,
                "product": _display_product_name(plan),
                "days": day,
                "currency": currency,
                "list_price": list_price,
                "promo_price": promo_price,
            })
    return result


def build_calendar_data(
    store: PromoStore,
    pricebook: pd.DataFrame | None = None,
    *,
    today: date | None = None,
) -> list[dict]:
    """Snapshot active-today and future, non-deleted campaigns with configured prices."""
    today = today or date.today()
    pricebook = pricebook if pricebook is not None else PricingTimeline(store.path).project()
    destination_names = _destination_names(pricebook)
    result: list[dict] = []
    for campaign in store.list_campaigns():
        status_token = str(campaign.status or "").strip().upper()
        if status_token not in {"PLANNED", "ACTIVE_IMPORTED"} or "CANCEL" in status_token:
            continue
        if store.has_execution(campaign.id, "END"):
            continue
        try:
            start = date.fromisoformat(campaign.start_date)
            end = date.fromisoformat(campaign.end_date)
        except (TypeError, ValueError):
            # Existing imported campaigns may not have a known start; their end still
            # determines whether they are current/upcoming when present.
            start = None
            try:
                end = date.fromisoformat(campaign.end_date)
            except (TypeError, ValueError):
                end = None
        if end is not None and end < today:
            continue
        if start is not None and end is not None and end < start:
            continue
        if start is None and end is None:
            continue

        status, status_key = _campaign_state(store, campaign)
        dest_codes = list(dict.fromkeys(campaign.destinations))
        # Only an imported campaign or one with an actual, non-undone START
        # execution is considered active today. Date overlap alone is not proof
        # that a planned campaign was applied.
        is_applied = status_key == "applied"
        result.append({
            "name": campaign.name,
            "promo_code": campaign.promo_code,
            "discount": (f"{campaign.promo_value:g}% off" if campaign.promo_type == "percent" else
                         f"{campaign.promo_value:g} off in EUR / USD" if campaign.promo_type == "absolute" else
                         f"Fixed net target {campaign.promo_value:g} EUR / USD"),
            "monotonic_destinations": list(campaign.monotonic_destinations),
            "monotonic_all_promos": bool(campaign.monotonic_all_promos),
            "blanket_below_cost": bool(campaign.allow_below_cost),
            "promo": campaign.promo_name or campaign.promo_code,
            # Keep ISO2 strings here: destination filtering uses exact code matches.
            "destinations": dest_codes,
            "destination_names": {
                code: destination_names.get(code, "") for code in dest_codes
            },
            "destination_labels": [
                f"{destination_names.get(code, code)} ({code})" if destination_names.get(code) else code
                for code in dest_codes
            ],
            "products": [_display_product_name(plan) for plan in campaign.plans],
            "durations": list(campaign.days),
            "start": campaign.start_date or "",
            "end": campaign.end_date or "",
            "status": status,
            "status_key": status_key,
            "notes": campaign.notes or "",
            "active_today": is_applied and (start is None or start <= today) and (end is None or end >= today),
            "prices": _campaign_prices(pricebook, campaign),
        })
    result.sort(key=lambda c: (c["start"] or "0000-00-00", c["end"] or "9999-12-31", c["name"].casefold()))
    return result


def generate_promotion_calendar(
    store: PromoStore,
    out_path: Path,
    pricebook: pd.DataFrame | None = None,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
    open_after: bool = False,
) -> Path:
    today = today or date.today()
    generated_at = generated_at or datetime.now().astimezone()
    campaigns = build_calendar_data(store, pricebook, today=today)
    # JSON is embedded inside a script element, so encode markup characters as
    # JSON Unicode escapes as well as applying DOM escaping when rendered.
    data_json = (
        json.dumps(campaigns, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("</", "<\\/")
    )
    generated = generated_at.strftime("%d.%m.%Y %H:%M %Z").strip()
    today_display = today.strftime("%d.%m.%Y")

    doc = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>T-Travel Promotion Calendar</title>
<style>
:root{{--pink:#e20074;--pink-dark:#b0005a;--pink-soft:#fce7f2;--ink:#202124;--muted:#68707b;--line:#e6e8ed;--bg:#f5f6f8;--green:#147a45;--green-bg:#e9f7ef;--blue:#315ea8;--blue-bg:#eaf2ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI",Arial,sans-serif}}.wrap{{max-width:1240px;margin:auto;padding:28px 22px 44px}}
.print-toolbar{{display:flex;justify-content:flex-end;margin-bottom:12px}}button{{cursor:pointer;font:inherit;padding:8px 13px;border:1px solid #cbd0d8;border-radius:7px;background:white}}.price-details{{margin:14px 0}}.price-details>summary{{display:block;font-size:13px;color:var(--muted);padding:8px 0}}header{{background:#111;color:white;padding:24px;border-radius:14px}}h1{{font-size:clamp(25px,4vw,36px);line-height:1.15;margin:0}}.meta{{color:#ddd;font-size:13px;margin-top:8px}}.snapshot{{margin:14px 0 0;padding:10px 12px;background:#27232a;border-left:4px solid var(--pink);border-radius:5px;color:#fff;font-size:13px}}
.summary{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}}.card,.filters,.campaign{{background:white;border:1px solid var(--line);border-radius:12px}}.card{{padding:16px}}.number{{font-size:28px;font-weight:750;color:var(--pink-dark)}}.label{{color:var(--muted);font-size:13px}}
.filters{{padding:14px;margin-bottom:18px}}.filter-grid{{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:10px}}label{{font-size:12px;font-weight:700;color:#4c535d;display:block;margin-bottom:4px}}select{{font:inherit;width:100%;padding:9px 10px;border:1px solid #cbd0d8;border-radius:7px;background:white}}.filter-actions{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:10px;color:var(--muted);font-size:13px}}
.calendar{{background:white;border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:16px 0 24px}}.cal-title{{padding:13px 16px;font-weight:750;border-bottom:1px solid var(--line)}}.timeline-scroll{{overflow-x:auto}}.timeline{{min-width:900px}}.month-row,.timeline-row{{display:grid;grid-template-columns:220px minmax(680px,1fr)}}.month-row{{height:38px;border-bottom:1px solid var(--line)}}.month-labels{{position:relative;border-left:1px solid var(--line)}}.month-label{{position:absolute;top:9px;transform:translateX(5px);font-size:11px;color:var(--muted);font-weight:700;white-space:nowrap}}.timeline-row{{min-height:42px;border-bottom:1px solid #f0f1f4}}.timeline-row:last-child{{border-bottom:0}}.timeline-name{{padding:10px 12px;font-size:12px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-right:1px solid var(--line)}}.timeline-track{{position:relative;min-height:42px;background:repeating-linear-gradient(to right,transparent 0,transparent calc(var(--month-step) - 1px),#eef0f3 calc(var(--month-step) - 1px),#eef0f3 var(--month-step))}}.timeline-bar{{position:absolute;top:9px;height:24px;border-radius:6px;background:#6b8fdc;color:white;padding:3px 8px;font-size:11px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:7px}}.timeline-bar.applied{{background:#27885a}}.today-line{{position:absolute;top:0;bottom:0;width:2px;background:var(--pink);z-index:2;pointer-events:none}}.today-label{{position:absolute;top:1px;left:4px;background:white;color:var(--pink);font-size:9px;font-weight:800;padding:1px 3px;white-space:nowrap}}.timeline-empty{{padding:24px;color:var(--muted);text-align:center}}
.campaign{{margin:10px 0;overflow:hidden}}.campaign[hidden]{{display:none}}.campaign summary{{cursor:pointer;list-style:none;padding:16px 18px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center}}.campaign summary::-webkit-details-marker{{display:none}}.campaign summary:after{{content:"＋";color:var(--pink);font-weight:800;font-size:20px}}.campaign[open] summary:after{{content:"−"}}.campaign summary:hover{{background:#fff9fc}}.name{{font-weight:750;font-size:17px}}.subline{{color:var(--muted);font-size:13px;margin-top:3px}}.badges{{display:flex;gap:7px;align-items:center;flex-wrap:wrap;justify-content:flex-end}}.badge{{border-radius:999px;padding:4px 9px;font-size:11px;font-weight:750;white-space:nowrap}}.scheduled{{background:var(--blue-bg);color:var(--blue)}}.applied{{background:var(--green-bg);color:var(--green)}}.body{{padding:0 18px 18px;border-top:1px solid var(--line)}}.facts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:15px 0}}.fact-title{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:750}}.fact-value{{margin-top:3px;overflow-wrap:anywhere}}.durations{{display:flex;flex-wrap:wrap;gap:6px}}.duration{{background:#f0f1f4;border-radius:6px;padding:3px 8px;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:9px}}th,td{{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}td.numcell{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}.price-heading{{font-size:14px;margin:8px 0 0}}.prices-empty{{color:var(--muted);font-size:13px}}.notes{{white-space:pre-wrap;background:#fff7fb;border-left:3px solid var(--pink);padding:10px 12px;margin-top:14px;overflow-wrap:anywhere}}.empty{{background:white;border:1px dashed #c8cbd2;padding:30px;text-align:center;color:var(--muted);border-radius:12px}}.empty[hidden]{{display:none}}.section{{font-size:20px;margin:22px 0 8px}}footer{{color:var(--muted);font-size:12px;margin-top:22px}}
@media(max-width:720px){{.wrap{{padding:14px 11px 28px}}.summary{{grid-template-columns:1fr 1fr}}.filter-grid{{grid-template-columns:1fr 1fr}}.campaign summary{{grid-template-columns:1fr;gap:7px}}.campaign summary:after{{position:absolute;right:28px}}.campaign summary{{position:relative;padding-right:48px}}.badges{{justify-content:flex-start}}.facts{{grid-template-columns:1fr}}.table-wrap{{overflow-x:auto}}table{{min-width:650px}}}}
@media(max-width:420px){{.filter-grid{{grid-template-columns:1fr}}}}
@page{{size:A4 landscape;margin:12mm}}
@media print{{body{{background:white;font-size:10pt;print-color-adjust:exact;-webkit-print-color-adjust:exact}}.wrap{{max-width:none;padding:0}}.print-toolbar,.filters,.campaign>summary:after,.price-details{{display:none!important}}header{{background:white;color:var(--ink);border-bottom:3px solid var(--pink);padding:10px 0;border-radius:0}}header .meta{{color:#555}}.snapshot{{background:white;color:#333;border:0;padding:0}}.summary{{margin:10px 0}}.card{{padding:8px}}.number{{font-size:20px}}.campaign{{break-inside:auto;border-radius:0}}.campaign>summary{{break-after:avoid}}.facts,.notes{{break-inside:avoid}}.timeline-scroll{{overflow:visible}}.timeline{{min-width:0}}.month-row,.timeline-row{{grid-template-columns:180px minmax(0,1fr)}}.timeline-name{{white-space:normal;overflow-wrap:anywhere}}.timeline-row{{break-inside:avoid}}.campaign[hidden]{{display:none!important}}}}
</style></head><body><main class="wrap"><div class="print-toolbar"><button type="button" id="printCalendar" aria-label="Print campaign calendar">🖨 Print / Save PDF</button></div>
<header><h1>T-Travel Promotion Calendar · today onwards</h1><div class="meta">Snapshot generated {html.escape(generated)} · Calendar date {html.escape(today_display)}</div><div class="snapshot">This file is a snapshot of the planner at export time. It is not a live calendar and does not refresh automatically.</div></header>
<section class="summary" aria-label="Promotion summary"><div class="card"><div class="number" id="activeCount">0</div><div class="label">Active today</div></div><div class="card"><div class="number" id="upcomingCount">0</div><div class="label">Planned / upcoming promotions</div></div><div class="card"><div class="number" id="visibleCount">0</div><div class="label">Promotions shown</div></div></section>
<section class="filters"><div class="filter-grid"><div><label for="destinationFilter">Destination</label><select id="destinationFilter"><option value="">All destinations</option></select></div><div><label for="productFilter">Product / plan</label><select id="productFilter"><option value="">All products</option></select></div><div><label for="statusFilter">Status</label><select id="statusFilter"><option value="">All statuses</option><option value="scheduled">Scheduled</option><option value="applied">Published</option></select></div></div><div class="filter-actions"><span id="resultText"></span><button type="button" id="clearFilters">Clear filters</button></div></section>
<h2 class="section">Promotions</h2><div class="calendar"><div class="cal-title">Promotion schedule · today onwards</div><div class="timeline-scroll"><div id="timeline" class="timeline"></div></div></div><div id="campaigns"></div><div class="empty" id="empty" hidden>No active or upcoming promotions match this calendar and filter selection.</div>
<footer>Package duration is data validity in days; campaign dates show when the promotion is scheduled.</footer>
</main><script>
const campaigns={data_json};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const euro=n=>n==null?"—":new Intl.NumberFormat("de-DE",{{minimumFractionDigits:2,maximumFractionDigits:2}}).format(n);
const usd=n=>n==null?"—":new Intl.NumberFormat("de-DE",{{minimumFractionDigits:2,maximumFractionDigits:2}}).format(n);
const money=(n,c)=>n==null?"—":`${{new Intl.NumberFormat("de-DE",{{minimumFractionDigits:2,maximumFractionDigits:2}}).format(n)}} ${{c}}`;
const humanDate=s=>s?new Intl.DateTimeFormat("en-GB",{{day:"2-digit",month:"short",year:"numeric",timeZone:"UTC"}}).format(new Date(s+"T00:00:00Z")):"Not specified";
const allDest=[...new Set(campaigns.flatMap(c=>c.destinations))].sort((a,b)=>a.localeCompare(b));const allProducts=[...new Set(campaigns.flatMap(c=>c.products))].sort((a,b)=>a.localeCompare(b));
const addOptions=(id,values)=>{{const el=document.getElementById(id);values.forEach(v=>{{const o=document.createElement("option");o.value=v;o.textContent=v;el.appendChild(o)}})}};addOptions("destinationFilter",allDest);addOptions("productFilter",allProducts);
const host=document.getElementById("campaigns");campaigns.forEach((c,i)=>{{const el=document.createElement("details");el.className="campaign";el.dataset.index=i;const statusClass=c.status_key;const destText=c.destinations.map(iso=>c.destination_names?.[iso]?`${{c.destination_names[iso]}} (${{iso}})`:iso).join(", ");const priceRows=(c.prices||[]).map(p=>`<tr><td>${{esc(p.destination)}} (${{esc(p.iso)}})</td><td>${{esc(p.product)}}</td><td>${{p.days}} days</td><td class="numcell">${{money(p.list_price,p.currency)}}</td><td class="numcell">${{money(p.promo_price,p.currency)}}</td></tr>`).join("");el.innerHTML=`<summary><div><div class="name">${{esc(c.name)}}</div><div class="subline">${{esc(c.promo)}} · ${{esc(destText)}} · ${{humanDate(c.start)}} – ${{humanDate(c.end)}}</div></div><div class="badges"><span class="badge ${{statusClass}}">${{esc(c.status)}}</span><span class="badge">${{c.active_today?"Active today":c.start&&c.start<="{today.isoformat()}"?"Planned · not published":"Upcoming"}}</span></div></summary><div class="body"><div class="facts"><div><div class="fact-title">Campaign dates · end date inclusive</div><div class="fact-value">${{humanDate(c.start)}} – ${{humanDate(c.end)}}</div></div><div><div class="fact-title">Destinations</div><div class="fact-value">${{esc(destText||"—")}}</div></div><div><div class="fact-title">Affected products / plans</div><div class="fact-value">${{esc(c.products.join(", ")||"—")}}</div></div><div><div class="fact-title">Package durations</div><div class="fact-value durations">${{c.durations.length?c.durations.map(d=>`<span class="duration">${{d}} days</span>`).join(""):"—"}}</div></div></div><div class="facts"><div><div class="fact-title">Offer</div><div class="fact-value">${{esc(c.discount)}} · code ${{esc(c.promo_code)}}</div></div><div><div class="fact-title">Shorter-duration alignment</div><div class="fact-value">${{c.monotonic_destinations.length?esc(c.monotonic_destinations.join(", "))+(c.monotonic_all_promos?" · includes other promos":" · regular prices only"):"Not enabled"}}</div></div><div><div class="fact-title">Campaign-wide cost-floor override</div><div class="fact-value">${{c.blanket_below_cost?"Enabled":"No blanket override; individual approvals may exist"}}</div></div></div><details class="price-details"><summary>Illustrative pack prices · current list-price baseline</summary><p class="prices-empty">These apply the campaign discount to the current list-price snapshot. Dated price changes, overlapping campaigns, cost-floor decisions and PA adjustments can change the final prices; check the selected date in the Pricing Editor. This price appendix is omitted when printing.</p>${{priceRows?`<table><thead><tr><th>Destination</th><th>Product</th><th>Duration</th><th>List price</th><th>Promo price</th></tr></thead><tbody>${{priceRows}}</tbody></table>`:`<p class="prices-empty">Price details are unavailable for this campaign.</p>`}}</details>${{c.notes?`<div class="notes"><strong>Campaign notes</strong><br>${{esc(c.notes)}}</div>`:""}}</div>`;host.appendChild(el)}});
function renderTimeline(){{const root=document.getElementById("timeline");root.innerHTML="";const visible=campaigns.filter((c,i)=>!document.querySelector(`.campaign[data-index="${{i}}"]`).hidden);if(!visible.length){{root.innerHTML='<div class="timeline-empty">No promotions match these filters.</div>';return}}const start=new Date("{today.isoformat()}T00:00:00Z");let end=new Date(start);end.setUTCMonth(end.getUTCMonth()+6);for(const c of visible)if(c.end){{const d=new Date(c.end+"T00:00:00Z");if(d>end)end=d}}end.setUTCDate(end.getUTCDate()+1);const span=end-start;const monthCount=(end.getUTCFullYear()-start.getUTCFullYear())*12+end.getUTCMonth()-start.getUTCMonth()+1;const monthStep=100/monthCount;const monthRow=document.createElement("div");monthRow.className="month-row";monthRow.innerHTML='<div></div><div class="month-labels"></div>';const labels=monthRow.lastElementChild;labels.style.setProperty("--month-step",monthStep+"%");for(let d=new Date(Date.UTC(start.getUTCFullYear(),start.getUTCMonth(),1));d<end;d.setUTCMonth(d.getUTCMonth()+1)){{const label=document.createElement("span");label.className="month-label";label.style.left=Math.max(0,(d-start)/span*100)+"%";label.textContent=new Intl.DateTimeFormat("en-GB",{{month:"short",year:"numeric",timeZone:"UTC"}}).format(d);labels.appendChild(label)}}root.appendChild(monthRow);visible.forEach(c=>{{const row=document.createElement("div");row.className="timeline-row";const label=document.createElement("div");label.className="timeline-name";label.textContent=c.name;const track=document.createElement("div");track.className="timeline-track";track.style.setProperty("--month-step",monthStep+"%");const today=document.createElement("div");today.className="today-line";today.style.left="0";today.innerHTML='<span class="today-label">TODAY</span>';track.appendChild(today);const s=c.start?new Date(c.start+"T00:00:00Z"):start;const e=c.end?new Date(c.end+"T00:00:00Z"):new Date(end);if(c.end)e.setUTCDate(e.getUTCDate()+1);const left=Math.max(0,(s-start)/span*100);const right=Math.min(100,((e-start)/span*100));const bar=document.createElement("div");bar.className="timeline-bar "+(c.status_key==="applied"?"applied":"");bar.style.left=left+"%";bar.style.width=Math.max(.7,right-left)+"%";bar.title=`${{c.name}} · ${{humanDate(c.start)}} – ${{humanDate(c.end)}} · ${{c.status}}`;bar.textContent=c.promo;track.appendChild(bar);row.append(label,track);root.appendChild(row)}})}}
function update(){{const dest=document.getElementById("destinationFilter").value,prod=document.getElementById("productFilter").value,status=document.getElementById("statusFilter").value;let visible=0;document.querySelectorAll(".campaign").forEach(el=>{{const c=campaigns[Number(el.dataset.index)];const ok=(!dest||c.destinations.includes(dest))&&(!prod||c.products.includes(prod))&&(!status||c.status_key===status);el.hidden=!ok;if(ok)visible++}});const active=campaigns.filter(c=>c.active_today).length;document.getElementById("activeCount").textContent=active;document.getElementById("upcomingCount").textContent=campaigns.length-active;document.getElementById("visibleCount").textContent=visible;document.getElementById("resultText").textContent=`Showing ${{visible}} of ${{campaigns.length}} promotions`;document.getElementById("empty").hidden=visible!==0;renderTimeline()}}
["destinationFilter","productFilter","statusFilter"].forEach(id=>document.getElementById(id).addEventListener("input",update));document.getElementById("clearFilters").addEventListener("click",()=>{{["destinationFilter","productFilter","statusFilter"].forEach(id=>document.getElementById(id).value="");update()}});update();
let printState=[];
window.addEventListener("beforeprint",()=>{{printState=[...document.querySelectorAll(".campaign")].map(el=>[el,el.open]);printState.forEach(([el])=>{{if(!el.hidden)el.open=true}})}});
window.addEventListener("afterprint",()=>{{printState.forEach(([el,wasOpen])=>el.open=wasOpen);printState=[]}});
document.getElementById("printCalendar").addEventListener("click",()=>window.print());
</script></body></html>'''

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    if open_after:
        try:
            os.startfile(out_path)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    return out_path
