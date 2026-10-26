"""Campaign-start net prices and PA duration evidence from an isolated DB copy."""
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .engine import _state_net, _num, _text
from .store import PromoStore
from .date_price_changes import project_dataframe
try:
    from automation.pricing_timeline import PricingTimeline
except ImportError:
    from pricing_timeline import PricingTimeline


def _day(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _after_state(row):
    return dict(list_eur=_num(row.get('Price_EUR')), list_usd=_num(row.get('Price_USD')),
                promo_code=_text(row.get('PromoCode')), promo_type=_text(row.get('PromoType')),
                promo_value=_num(row.get('PromoValue')) or 0,
                net_eur=_num(row.get('PromoNet_EUR')), net_usd=_num(row.get('PromoNet_USD')))


def _planned_price_rows(preview, campaign):
    """Read final engine prices, including campaign-owned PA outside anchor days."""
    output=[];seen=set()
    unresolved_messages=set(preview.collisions)
    conflict_points={(issue.iso.upper(),issue.plan,issue.days) for issue in preview.collision_issues
        if f"{issue.campaign_name}: {issue.iso} / {issue.plan} / {issue.days}d already has {issue.current_promo_code}" in unresolved_messages}
    for row in preview.projected_rows:
        iso,plan=_text(row.get('ISO')).upper(),_text(row.get('Plan'))
        days=_num(row.get('Days'))
        if days is None:
            continue
        owner=_text(row.get('_PromoPlannerPromoOwner'))
        code=_text(row.get('PromoCode'))
        pa=(code=='PA' and owner==str(int(campaign.id or 0)))
        direct=(iso in campaign.destinations and plan in campaign.plans and int(days) in campaign.days)
        if not direct and not pa:
            continue
        state=_after_state(row)
        net_eur,net_usd=(_state_net(state,currency) for currency in ('EUR','USD'))
        approved=str(row.get('AllowBelowCost')).lower() in {'true','1','1.0'}
        blocked=not approved and any(_num(row.get(f'CostFloor_{currency}')) is not None and net is not None
            and net<_num(row.get(f'CostFloor_{currency}'))-1e-9 for currency,net in (('EUR',net_eur),('USD',net_usd)))
        gb=_num(row.get('GB'))
        identity=(iso,plan,int(days),gb,net_eur,net_usd,code,pa,blocked,approved)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(dict(iso=iso,destination=_text(row.get('Country')) or iso,plan=plan,
            days=int(days),gb=gb,net_eur=net_eur,net_usd=net_usd,
            list_eur=_num(row.get('Price_EUR')),list_usd=_num(row.get('Price_USD')),
            promo_code=code,is_adjustment=pa,blocked=blocked,approved_below_cost=approved,
            offer_owner=owner,conflict_unresolved=(iso,plan,int(days)) in conflict_points))
    return sorted(output,key=lambda row:(row['destination'],row['plan'],row['days'],row['gb'] or 0))


def collect_calendar_adjustments(store, campaign_ids, *, today):
    """Return campaign-start prices and actual PA durations in the saved plan.

    Future periods are projections, not evidence that a future promo is published.
    No execute/publish method runs; migrations/projections use a temporary backup.
    """
    result={int(cid):dict(rows=[],note='',available=True,prices=[],prices_available=False,prices_as_of='',prices_note='') for cid in campaign_ids}
    if not result:
        return result
    source=Path(store.path)
    with tempfile.TemporaryDirectory(prefix='tt-calendar-pa-') as folder:
        root=Path(folder);target=root/'outputs/promo_planner/promo_schedule.db'
        target.parent.mkdir(parents=True)
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as live:
            with closing(sqlite3.connect(target)) as snapshot:
                live.backup(snapshot)
        choices=source.parent/'issue_choices'
        if choices.is_dir():
            shutil.copytree(choices,target.parent/'issue_choices')
        timeline=PricingTimeline(target)
        baseline=timeline.baseline_info()
        if not baseline:
            for item in result.values():
                item.update(available=False,note='Adjusted durations unavailable: no database pricing baseline.',prices_note='Planned net prices unavailable: no database pricing baseline.')
            return result
        snapshot_store=PromoStore(target)
        campaigns=snapshot_store.list_campaigns()
        owners={int(c.id):c for c in campaigns if c.id in result}
        baseline_day=_day(baseline['effective_from'])
        first=max(today,baseline_day)
        price_dates=defaultdict(list)
        for cid,c in owners.items():
            target_day=_day(c.start_date) or today
            result[cid]['prices_as_of']=target_day.isoformat()
            if target_day<baseline_day:
                result[cid]['prices_note']=f'Campaign start {target_day.isoformat()} predates stored pricing history ({baseline_day.isoformat()}); start-date prices cannot be reconstructed.'
            else:
                price_dates[target_day].append(cid)
                if not c.start_date:
                    result[cid]['prices_note']='Start date was not recorded; these are planned net prices for today.'
        ends=[_day(c.end_date) for c in owners.values() if _day(c.end_date) and _day(c.end_date)>=first]
        last=max(ends)+timedelta(days=1) if ends else first+timedelta(days=1)
        boundaries={first,last}
        def add(value):
            if value and first<value<last:
                boundaries.add(value)
        for c in campaigns:
            add(_day(c.start_date))
            end=_day(c.end_date)
            add(end+timedelta(days=1) if end else None)
        # Include both price-event starts and expiries, not just campaign dates.
        with closing(sqlite3.connect(target)) as connection:
            for start,end in connection.execute('SELECT effective_from,expires_at FROM pricing_change_sets WHERE undone_at=""'):
                add(_day(start));add(_day(end))
        for path in (target.parent/'issue_choices').glob('*.json'):
            key=_day(path.stem)
            add(key+timedelta(days=1) if key else None)
        periods=sorted(boundaries)
        intervals=defaultdict(list);notes=defaultdict(set)
        stops=dict(zip(periods,periods[1:]))
        for start in sorted(set(stops)|set(price_dates)):
            stop=stops.get(start)
            active={cid:c for cid,c in owners.items() if stop and c.monotonic_destinations
                    and (not c.start_date or c.start_date<=start.isoformat()) and c.end_date>=start.isoformat()}
            priced=price_dates.get(start,[])
            if not active and not priced:
                continue
            key=(start-timedelta(days=1)).isoformat()
            try:
                raw=timeline.project(start,layers=('base','import','override'))
                _,preview=project_dataframe(raw,root/'unused.xlsx',snapshot_store,key,root,replay_executed=True)
            except Exception as exc:
                for cid in active:
                    result[cid]['available']=False
                    notes[cid].add(f'Adjusted durations unavailable for {start:%d %b %Y} to {stop-timedelta(days=1):%d %b %Y} ({type(exc).__name__}).')
                for cid in priced:
                    result[cid]['prices_note']=f'Start-date pricing could not be projected ({type(exc).__name__}); no prices were guessed.'
                continue
            for cid in priced:
                result[cid]['prices']=_planned_price_rows(preview,owners[cid])
                result[cid]['prices_available']=True
                if any(row['conflict_unresolved'] for row in result[cid]['prices']):
                    result[cid]['prices_note']+=' Unresolved promo overlaps exist on this date; the table shows the current saved projection pending resolution.'
            if not active:
                continue
            if preview.collisions:
                for cid in active:
                    notes[cid].add('Unresolved promo overlaps exist in this plan; adjusted durations can change after resolution.')
            period_identities=set()
            for row in preview.projected_rows:
                if _text(row.get('PromoCode'))!='PA':
                    continue
                try:
                    cid=int(row.get('_PromoPlannerPromoOwner') or 0)
                except (TypeError,ValueError):
                    continue
                if cid not in active:
                    continue
                c=active[cid];iso=_text(row.get('ISO')).upper();plan=_text(row.get('Plan'))
                if iso not in c.monotonic_destinations or plan not in c.plans:
                    continue
                try:
                    underlying=json.loads(_text(row.get('_PromoPlannerPAUnderlying')))
                    after=_after_state(row)
                    currencies=tuple(currency for currency in ('EUR','USD')
                        if _state_net(underlying,currency) is not None and _state_net(after,currency) is not None
                        and _state_net(after,currency)<_state_net(underlying,currency)-1e-9)
                    days=int(float(row['Days']))
                except (ValueError,TypeError,KeyError):
                    result[cid]['available']=False
                    notes[cid].add('Some PA duration evidence could not be read; no duration was guessed.')
                    continue
                if not currencies:
                    continue
                approved=str(row.get('AllowBelowCost')).lower() in {'true','1','1.0'}
                pending=(not approved and any(
                    _num(row.get(f'CostFloor_{currency}')) is not None and _state_net(after,currency) is not None
                    and _state_net(after,currency)<_num(row.get(f'CostFloor_{currency}'))-1e-9
                    for currency in ('EUR','USD')))
                identity=(cid,iso,_text(row.get('Country')) or iso,plan,days,currencies,pending)
                period_identities.add(identity)
            for identity in period_identities:
                bucket=intervals[identity]
                if bucket and bucket[-1][1]==start:
                    bucket[-1]=(bucket[-1][0],stop)
                else:
                    bucket.append((start,stop))
        groups=defaultdict(set)
        for (cid,iso,country,plan,days,currencies,pending),spans in intervals.items():
            for start,stop in spans:
                groups[(cid,iso,country,plan,currencies,pending,start,stop)].add(days)
        for (cid,iso,country,plan,currencies,pending,start,stop),days in sorted(groups.items()):
            result[cid]['rows'].append(dict(iso=iso,destination=country,plan=plan,days=sorted(days),
                currencies=list(currencies),start=start.isoformat(),end=(stop-timedelta(days=1)).isoformat(),
                pending_approval=pending))
        for cid,item in result.items():
            if first>today:
                notes[cid].add(f'Pricing evidence is available only from {first.isoformat()}.')
            item['note']=' '.join(sorted(notes[cid]))
    return result
