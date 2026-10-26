"""Persist explicit point-level approval using the planner's existing decisions."""
from datetime import date, timedelta, datetime
import json
from pathlib import Path


def approve_below_cost_point(root, key_date, point, campaigns):
    effective=(date.fromisoformat(key_date)+timedelta(days=1)).isoformat()
    iso,plan,days=str(point['iso']).upper(),str(point['plan']),int(point['days'])
    currencies=[c for c,below in point.get('below_cost_floor_by_currency',{}).items() if below]
    if not currencies:return 0
    owner=str(point.get('promo_planner_owner') or '')
    active=[c for c in campaigns if (not c.start_date or c.start_date<=effective) and c.end_date>=effective]
    campaign=next((c for c in active if str(c.id)==owner),None)
    # The projection identifies campaign-owned offers and PAs explicitly.
    # Manual offers remain campaign 0, keeping approval away from other campaigns.
    campaign_id=int(campaign.id or 0) if campaign else 0
    if owner and campaign is None:raise ValueError('The owning campaign is not active on this date; reload the price view.')
    directory=Path(root)/'outputs/promo_planner/issue_choices'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(key_date+'.json')
    data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(data,dict):raise ValueError('Invalid saved cost-floor decisions; nothing was replaced.')
    records=data.get('cost_floor',[])
    if not isinstance(records,list) or any(not isinstance(r,list) or len(r)!=6 for r in records):raise ValueError('Invalid saved cost-floor records; nothing was replaced.')
    choices={tuple(r[:5]):r[5] for r in records}
    for currency in currencies:choices[(campaign_id,iso,plan,days,str(currency).upper())]='override'
    data['cost_floor']=[list(k)+[value] for k,value in choices.items()]
    data['saved_at']=datetime.now().isoformat(timespec='seconds')
    temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,indent=2),encoding='utf-8');temporary.replace(path)
    return len(currencies)
