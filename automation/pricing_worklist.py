"""Sales-prioritised product worklist for the existing Market Insights report."""
import json
from html import escape
from datetime import date
import re
import sys


def build_origin_sales(sales_note, *, sales_path=None):
    """Same 60-day eligible sales as the worklist, with origin-specific amounts.

    Identifiers are inspected only inside the shared local sales-reader boundary.
    Only aggregate country/product counts and currency amounts leave this function.
    """
    match = re.search(r'Completed sales, (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2});', sales_note)
    if not match:
        return dict(origins=[], by_origin={}, note='Buyer-origin sales could not be resolved for this report date.')
    m = sys.modules.get('automation.market_insights') or sys.modules.get('market_insights')
    if m is None:
        try:
            from automation import market_insights as m
        except ImportError:
            import market_insights as m
    as_of = date.fromisoformat(match[2])
    begin = date.fromisoformat(match[1])
    names, _ = m._campaign_inputs()
    aliases = {re.sub(r'\W+',' ', name.casefold()).strip():iso for iso,name in names.items()}
    resolve = lambda value: aliases.get(re.sub(r'\W+',' ', m._text(value).casefold()).strip()) or m._sales_iso2(value)
    path = sales_path if sales_path is not None else m._configured_sales_path()
    sales = m._campaign_sales(path, resolve, as_of, include_daily=True)
    if not sales['available']:
        return dict(origins=[], by_origin={}, note='Buyer-origin sales are unavailable.')
    by_origin = {}
    def origin(code):
        return by_origin.setdefault(code, dict(destinations={}, products={}, unmapped=[]))
    for (code, dest), values in sales['routes'].items():
        origin(code)['destinations'][dest] = values['units']
    def product_key(dest,plan,days):
        return json.dumps([dest,plan,int(days)], separators=(',',':'))
    for (code,dest,plan,days), units in sales['skus'].items():
        origin(code)['products'][product_key(dest,plan,days)] = dict(units=units, revenue={})
    for (_stamp,code,dest), units in sales['daily_unmapped'].items():
        if units>0 and dest not in origin(code)['unmapped']:
            origin(code)['unmapped'].append(dest)
    # Reuse precisely the shared reader's duplicate, reversal and status rules.
    raw = m._read_sales_csv(path)
    dates = raw['TransactionDate'].map(m._campaign_date)
    types = raw.TransactionType.fillna('').astype(str).str.strip().str.upper()
    statuses = raw.DocumentStatus.fillna('').astype(str).str.casefold()
    visible = dates.map(lambda d:d is not None and d<=as_of)
    cancelled = (types.isin(['CANCELLATION','REFUND','REVERSAL']) | statuses.str.contains('cancel|refund|revers',regex=True)) & visible
    accepted = types.eq('SALE') & statuses.str.strip().isin(['completeddeliveryandsettledinvoice','completed','settled','paid','delivered']) & ~cancelled & visible
    if 'LineID' in raw:
        ids = raw.LineID.fillna('').astype(str).str.strip()
        cancelled_ids = set(ids[cancelled & ids.ne('')])
        accepted &= ~ids.isin(cancelled_ids)
        fields = [c for c in ['LineID','BuyerCountry','Destination','Quantity','TransactionDate','TransactionType','DocumentStatus','PackageSize','ValidityDays','ProductName','TotalAmount','UnitPrice','ConnectXPriceCurrency','Currency','PaymentCurrency','TransactionCurrency'] if c in raw]
        variants = raw.loc[accepted & ids.ne(''),fields].drop_duplicates()
        conflicts = set(variants.loc[variants.LineID.duplicated(keep=False),'LineID'].astype(str))
        accepted &= ~ids.isin(conflicts)
        accepted &= ~(ids.duplicated(keep='first') & ids.ne(''))
    totals, missing, counted = {}, set(), {}
    for index,row in raw.loc[accepted].iterrows():
        day = dates.loc[index]
        if day is None or not begin<=day<=as_of:continue
        code,dest = resolve(row.BuyerCountry),resolve(row.Destination)
        plan = m._campaign_plan(row.get('PackageSize')) or m._campaign_plan(row.get('ProductName'))
        days = m._campaign_number(row.get('ValidityDays'))
        if days is None:
            duration = re.search(r'(\d+)\s*days?\b',m._text(row.get('ProductName')),re.I)
            days = float(duration[1]) if duration else None
        units = m._campaign_number(row.get('Quantity'))
        if days is None or not days.is_integer() or not units or units<=0:continue
        sk=(code,dest,plan,int(days))
        if sk not in sales['skus']:continue
        counted[sk]=counted.get(sk,0)+units
        currency = next((m._text(row.get(field)).upper() for field in ('ConnectXPriceCurrency','Currency','PaymentCurrency','TransactionCurrency') if m._text(row.get(field))), '')
        if currency not in ('EUR','USD'):continue
        amount = m._campaign_number(row.get('TotalAmount'))
        if amount is None:
            unit_price=m._campaign_number(row.get('UnitPrice'))
            if unit_price is not None:amount=unit_price*units
        key=(*sk,currency)
        if amount is None:missing.add(key)
        else:totals[key]=totals.get(key,0)+amount
    for (code,dest,plan,days,currency),amount in totals.items():
        sk=(code,dest,plan,days)
        value = None if (*sk,currency) in missing or abs(counted.get(sk,0)-sales['skus'][sk])>1e-8 else amount
        origin(code)['products'][product_key(dest,plan,days)]['revenue'][currency]=value
    return dict(origins=[dict(iso=code,name=names.get(code,code)) for code in sorted(by_origin)],
                by_origin=by_origin, note='Buyer-origin filter applies to product units, destination totals, recorded amounts, country cards and ranking. Competitive price advice remains shared across origins.')


def render_pricing_worklist(rows, sales_note, sales_available, *, origin_sales=None):
    payload=json.dumps(rows,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
    if origin_sales is None:
        try:
            origin_sales=build_origin_sales(sales_note) if sales_available else dict(origins=[],by_origin={},note='Sales unavailable or stale; buyer-origin filtering is disabled.')
        except (OSError,ValueError,KeyError):
            origin_sales=dict(origins=[],by_origin={},note='Buyer-origin sales could not be loaded; showing the existing all-origins figures.')
    origin_payload=json.dumps(origin_sales,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
    return '''<section class="card pricing-actions" id="pricing-actions">
<style>
.pricing-actions{border-top:4px solid #e20074}.pw-filters{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:12px;margin:18px 0}.pw-filters label{display:grid;gap:5px;color:#66727f;font-size:12px}.pw-filters select,.pw-filters input{width:100%;padding:10px;border:1px solid #d8dee4;border-radius:7px;background:white}.pw-country-strip{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.pw-country-strip button{border:1px solid #d8dee4;border-radius:8px;background:#fafbfc;padding:8px 12px;text-align:left;cursor:pointer}.pw-country-strip small{display:block;color:#66727f}.pw-table td{white-space:normal;vertical-align:top;min-width:100px;padding:12px}.pw-table td:first-child{min-width:180px}.pw-table td:last-child{min-width:220px}.pw-table small{display:block;color:#66727f;margin-top:4px}.pw-table button,.pw-more{background:#fff;border:1px solid #d8dee4;border-radius:6px;padding:5px 9px;cursor:pointer;margin-top:8px}.pw-detail{background:#fafbfc}.pw-detail table{font-size:12px}.pw-detail td{padding:6px;min-width:0}.pw-detail th{position:static}.pw-count{font-variant-numeric:tabular-nums;font-weight:650}.pw-table .up{color:#177a3f}.pw-table .down{color:#b42318}@media(max-width:800px){.pw-filters{grid-template-columns:1fr 1fr}}@media(max-width:500px){.pw-filters{grid-template-columns:1fr}}
</style>
<h2>Pricing actions where we sell</h2>
<p class="chart-sub">Specific destination, plan and duration · latest trusted competitor prices · the same price advice as Campaign opportunities.</p>
<div class="pw-filters">
<label>Buyer origin<select id="pw-origin"><option value="all">All buyer origins</option></select></label>
<label>Sales relevance<select id="pw-scope"><option value="sold">Destinations with sales</option><option value="products">Products with matched sales</option><option value="all">All destinations</option></select></label>
<label>Destination<select id="pw-country"><option value="all">All destinations</option></select></label>
<label>Action<select id="pw-action"><option value="all">All actions</option><option>Decrease</option><option>Increase</option><option>Keep</option><option>Review cost floor</option><option>Insufficient evidence</option></select></label>
<label>Currency<select id="pw-currency"><option>EUR</option><option>USD</option><option value="all">EUR and USD</option></select></label>
<label>Plan<select id="pw-plan"><option value="all">All plans</option></select></label>
<label>Duration<select id="pw-days"><option value="all">All durations</option></select></label>
<label>Price position<select id="pw-position"><option value="all">All positions</option><option>Competition cheaper</option><option>T-Travel cheaper</option><option>Similar prices</option><option>Unknown</option></select></label>
<label>Search<input id="pw-search" type="search" placeholder="Country, plan, provider…"></label>
</div>
<div id="pw-summary" role="status"></div><div id="pw-countries" class="pw-country-strip"></div>
<div class="table-wrap"><table class="pw-table"><thead><tr><th>Destination / product</th><th>Product sales · 60d</th><th>Destination sales · 60d</th><th>Our net</th><th>Credible benchmark</th><th>Price position</th><th>Suggested net</th><th>Action &amp; evidence</th></tr></thead><tbody id="pw-body"></tbody></table></div>
<button id="pw-more" class="pw-more" hidden>Show 30 more</button>
<p class="note">'''+escape(sales_note)+'''</p>
<p class="note">Product units match destination, plan and duration for the selected buyer origin, combining currencies. Prices and competitive recommendations apply to the destination product across origins. Recorded sales amounts are shown only in their original reporting currency, without FX conversion. Missing or unmapped sales stay unknown. Destination totals are context and are never allocated to individual products. Price advice is advisory; a cheaper price alone never triggers an increase.</p>
<p class="note">'''+escape(origin_sales['note'])+'''</p>
</section>
<script type="application/json" id="pw-origin-data">'''+origin_payload+'''</script>
<script type="application/json" id="pw-data">'''+payload+'''</script>
<script>
(()=>{
const data=JSON.parse(document.getElementById('pw-data').textContent),available='''+str(bool(sales_available)).lower()+''';
const originData=JSON.parse(document.getElementById('pw-origin-data').textContent);
const ids=['origin','scope','country','action','currency','plan','days','position','search'],controls=Object.fromEntries(ids.map(k=>[k,document.getElementById('pw-'+k)]));
controls.origin.value='all';
originData.origins.forEach(o=>controls.origin.add(new Option(o.name+' ('+o.iso+')',o.iso)));
controls.origin.disabled=!available||!originData.origins.length;
const countries=new Map(data.map(r=>[r.iso,r.country]));[...countries].sort((a,b)=>a[1].localeCompare(b[1])).forEach(([iso,name])=>controls.country.add(new Option(name+' ('+iso+')',iso)));
[...new Set(data.map(r=>r.plan))].sort().forEach(v=>controls.plan.add(new Option(v,v)));
[...new Set(data.map(r=>r.days))].sort((a,b)=>a-b).forEach(v=>controls.days.add(new Option(v+' days',v)));
if(!available){controls.scope.value='all';controls.scope.disabled=true;}
let limit=30,filtered=[];
const fmt=(v,d=2)=>v===null||v===undefined?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:d}),money=(v,c)=>v===null||v===undefined?'—':fmt(v)+' '+c,pct=v=>v===null||v===undefined?'—':(v>=0?'+':'')+(v*100).toFixed(1)+'%';
function add(parent,tag,value,cls){const e=document.createElement(tag);e.textContent=value;if(cls)e.className=cls;parent.append(e);return e;}
function render(){
const q=controls.search.value.toLowerCase();
const scoped=controls.origin.value==='all'?data:data.map(r=>{const origin=originData.by_origin[controls.origin.value],stats=origin?.products[JSON.stringify([r.iso,r.plan,r.days])];return {...r,units:stats?stats.units:origin?.unmapped.includes(r.iso)?null:0,country_units:origin?.destinations[r.iso]??0,revenue:stats?.revenue[r.currency]??null};});
filtered=scoped.filter(r=>(controls.scope.value==='all'||(controls.scope.value==='products'?r.units>0:r.country_units>0))&&(controls.country.value==='all'||r.iso===controls.country.value)&&(controls.currency.value==='all'||r.currency===controls.currency.value)&&(controls.action.value==='all'||r.action===controls.action.value)&&(controls.plan.value==='all'||r.plan===controls.plan.value)&&(controls.days.value==='all'||r.days===Number(controls.days.value))&&(controls.position.value==='all'||r.position===controls.position.value)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));
filtered.sort((a,b)=>(b.units??-1)-(a.units??-1)||(b.country_units??-1)-(a.country_units??-1)||a.country.localeCompare(b.country)||a.days-b.days||a.currency.localeCompare(b.currency));
const summary=document.getElementById('pw-summary');summary.replaceChildren();
add(summary,'b',filtered.length.toLocaleString()+' price views · '+new Set(filtered.map(r=>r.iso)).size+' destinations');
add(summary,'span',' · '+(controls.origin.value==='all'?'All buyer origins':originData.origins.find(o=>o.iso===controls.origin.value)?.name||controls.origin.value));
add(summary,'span',' · '+filtered.filter(r=>r.action==='Decrease').length+' decrease · '+filtered.filter(r=>r.action==='Increase').length+' increase · '+filtered.filter(r=>r.action==='Keep').length+' keep');
const strip=document.getElementById('pw-countries');strip.replaceChildren();const grouped=new Map();
filtered.forEach(r=>{if(!grouped.has(r.iso))grouped.set(r.iso,{country:r.country,units:r.country_units,up:0,down:0});const g=grouped.get(r.iso);g.up+=r.action==='Increase'?1:0;g.down+=r.action==='Decrease'?1:0;});
[...grouped].sort((a,b)=>(b[1].units??-1)-(a[1].units??-1)).slice(0,6).forEach(([iso,g])=>{const button=add(strip,'button',g.country);add(button,'small',fmt(g.units,0)+' destination units · ↓ '+g.down+' / ↑ '+g.up+' price decisions');button.addEventListener('click',()=>{controls.country.value=iso;limit=30;render();});});
const body=document.getElementById('pw-body');body.replaceChildren();
filtered.slice(0,limit).forEach(r=>{
const tr=document.createElement('tr');body.append(tr);let td=add(tr,'td','');add(td,'b',r.country+' ('+r.iso+')');add(td,'small',r.plan+' · '+r.days+' days · '+(r.plan==='Unlimited'?'Unlimited':fmt(r.gb,1)+' GB'));
td=add(tr,'td','');add(td,'span',fmt(r.units,0)+' matched units','pw-count');add(td,'small',r.revenue===null?'Sales amount unavailable':money(r.revenue,r.currency)+' recorded amount');
add(tr,'td',fmt(r.country_units,0)+' units');add(tr,'td',money(r.current,r.currency));
td=add(tr,'td',money(r.benchmark,r.currency));add(td,'small',r.provider||'No trusted benchmark');if(r.method)add(td,'small',r.method==='price/GB equivalent'?'Calculated price/GB equivalent':r.method);
td=add(tr,'td',r.position);add(td,'small',pct(r.gap)+' versus benchmark');if(r.competitor_movement!==null&&r.competitor_movement!==undefined)add(td,'small','Competitors 28d: '+pct(r.competitor_movement));if(r.gap_change!==null&&r.gap_change!==undefined){add(td,'small',(r.gap_change>=0?'+':'')+(r.gap_change*100).toFixed(1)+' pp position change');add(td,'small',r.gap_change_basis);}
td=add(tr,'td',money(r.proposed,r.currency));if(r.change!==null)add(td,'small',pct(r.change));
td=add(tr,'td','');add(td,'b',r.action,r.action==='Decrease'?'down':r.action==='Increase'?'up':'');add(td,'small',r.reason);
const button=add(td,'button','Show evidence');button.setAttribute('aria-expanded','false');let detail=null;
button.addEventListener('click',()=>{if(detail){detail.remove();detail=null;button.textContent='Show evidence';button.setAttribute('aria-expanded','false');return;}
detail=document.createElement('tr');detail.className='pw-detail';const cell=add(detail,'td','');cell.colSpan=8;
add(cell,'p','Cost floor: '+money(r.floor,r.currency)+' · Latest retained competitor observation: '+(r.observed||'Unavailable')+(r.model_reason?' · Engine reason: '+r.model_reason:''));
add(cell,'p','Same duration; capped offers must be within 0.5–2× our allowance. At least two fresh providers are required. A price/GB equivalent is calculated and is not a purchasable pack. Unlimited fair-use limits may differ.');
if(r.offers.length){const t=add(cell,'table',''),head=add(t,'thead',''),hr=add(head,'tr','');['Provider','Product','Duration','Allowance','Stored price','Equivalent for our GB','Observed'].forEach(x=>add(hr,'th',x));const tb=add(t,'tbody','');r.offers.forEach(o=>{const row=add(tb,'tr','');[o.provider,o.plan||'—',o.days+' days',o.unlimited?'Unlimited':fmt(o.gb,1)+' GB',money(o.price,r.currency),money(o.equivalent,r.currency),o.observed||'—'].forEach(x=>add(row,'td',x));});}else add(cell,'p','No fresh comparable offers. Missing evidence does not mean we are competitive.');
tr.after(detail);button.textContent='Hide evidence';button.setAttribute('aria-expanded','true');});
});
if(!filtered.length){const td=add(add(body,'tr',''),'td','No products match these filters. Try All destinations or another action.');td.colSpan=8;}
document.getElementById('pw-more').hidden=limit>=filtered.length;
}
ids.forEach(k=>controls[k].addEventListener(k==='search'?'input':'change',()=>{limit=30;render();}));document.getElementById('pw-more').addEventListener('click',()=>{limit+=30;render();});render();
})();
</script>'''
