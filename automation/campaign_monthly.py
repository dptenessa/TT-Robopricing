"""Month-specific planning scenarios, not forecasts of sales or travellers."""
import calendar
import csv
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

VERSION = "1.0-monthly-planning"


def monthly_inputs(database, folder, today):
    season = {}
    path = Path(folder)/"destination_seasonality.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    season[(r["destination_iso2"], int(r["month"]))] = (
                        max(0,min(100,float(r["score"]))), r.get("source","configured model"))
                except (ValueError,KeyError,TypeError):
                    continue
    holidays = defaultdict(list)
    fresh_years = set()
    ads = {}
    if not Path(database).is_file():
        return season,holidays,fresh_years,ads
    with sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro",uri=True) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "holiday_periods" in tables:
            for origin,day,name in db.execute("SELECT origin_iso2,holiday_date,holiday_name FROM holiday_periods"):
                holidays[origin].append((day,name))
        if "collector_refresh" in tables:
            for origin,year in db.execute("""SELECT origin,period FROM collector_refresh
                WHERE source='holidays-v1' AND checked_at BETWEEN strftime('%s','now')-2592000 AND strftime('%s','now')"""):
                fresh_years.add((origin,year))
        if "google_ads_keywords" in tables:
            # One complete latest snapshot per origin, never sum multiple imports.
            rows=db.execute("""SELECT k.origin_iso2,k.destination_iso2,k.keyword,
                k.year,k.month,k.monthly_searches
                FROM google_ads_keywords k JOIN (
                  SELECT origin_iso2,MAX(observed_date) stamp FROM google_ads_keywords
                  WHERE observed_date BETWEEN ? AND ? AND source='Google Ads Keyword Planner'
                  GROUP BY origin_iso2
                ) latest ON k.origin_iso2=latest.origin_iso2 AND k.observed_date=latest.stamp
                WHERE k.source='Google Ads Keyword Planner'""",
                ((today-timedelta(days=59)).isoformat(),today.isoformat())).fetchall()
            keywords=defaultdict(dict)
            for origin,dest,keyword,year,month,value in rows:
                if value is not None and value>=0 and 1<=month<=12:
                    keywords[(origin,dest,keyword)][(year,month)]=float(value)
            totals=defaultdict(lambda:defaultdict(float))
            # Require the same complete 12-month history for every included keyword.
            for (origin,dest,keyword),months in keywords.items():
                dates=sorted(months)
                if len(dates)<12:
                    continue
                dates=dates[-12:]
                serial=[y*12+m for y,m in dates]
                age=today.year*12+today.month-serial[-1]
                if serial[-1]-serial[0]!=11 or not 1<=age<=6:
                    continue
                for ym in dates:
                    totals[(origin,dest)][ym]+=months[ym]
            for key,months in totals.items():
                # Avoid mixing keywords from incompatible historical periods.
                if len(months)!=12:
                    continue
                mean=sum(months.values())/12
                if mean>0:
                    ads[key]=(mean,{m:(y,v) for (y,m),v in months.items()})
    return season,holidays,fresh_years,ads


def plan_rows(frame, database, folder, today, score_fn):
    if frame.empty:
        return frame
    import pandas as pd
    season,holidays,fresh_years,ads=monthly_inputs(database,folder,today)
    current=today.replace(day=1)
    next_month=(current+timedelta(days=32)).replace(day=1)
    output=[]
    def parsed(value):
        try:return date.fromisoformat(str(value)[:10])
        except ValueError:return None
    for original in frame.to_dict("records"):
        for start in (current,next_month):
            end=start.replace(day=calendar.monthrange(start.year,start.month)[1])
            effective=max(start,today)
            row=dict(original)
            origin,dest=row["Origin"],row["DestinationISO"]
            signals=dict(row.get("PrioritySignals",{}))
            notes=[]
            seasonal,source=season.get((dest,start.month),(50,"neutral fallback"))
            notes.append(f"Destination seasonality for {start:%B}: {seasonal:g}/100 ({source}).")
            hs=[(d,n) for d,n in holidays[origin] if effective.isoformat()<=d<=end.isoformat()]
            holiday= min(70,35+15*len({d for d,n in hs})) if (origin,str(start.year)) in fresh_years else None
            notes.append("Origin public holidays: "+("; ".join(f"{n} ({d})" for d,n in hs) if holiday is not None and hs
                         else "none in the remaining planning window" if holiday is not None else "calendar unavailable")+" .")
            event_details=original.get("EventDetails",[])
            event_details=event_details if isinstance(event_details,list) else []
            events=[e for e in event_details if parsed(e.get("start")) and parsed(e.get("end") or e.get("start"))
                    and parsed(e["start"])<=end and parsed(e.get("end") or e["start"])>=effective]
            contributions=sorted([max(0,min(100,float(e.get("planning_score") or 0))) for e in events],reverse=True)
            event_score=min(90,(contributions[0] if contributions else 0)+sum(x*.08 for x in contributions[1:3]))
            signals["context"]=(seasonal+(holiday or 0)+event_score)/3
            notes.append(f"{len(events)} qualifying events overlap this month's remaining travel window; event signal {event_score:.1f}/100.")
            series=ads.get((origin,dest))
            if series and start.month in series[1]:
                mean,history=series
                year,volume=history[start.month]
                ratio=max(.5,min(2,volume/mean))
                # Historical shape adjusts current baseline, not today's measured search volume.
                raw=original.get("Baseline")
                try:base=float(raw)
                except (TypeError,ValueError):base=signals.get("ads",0)
                if not math.isfinite(base):base=signals.get("ads",0)
                signals["ads"]=round(max(0,min(100,base+20*math.log10(ratio))),1) if base>0 else 0
                notes.append(f"Ads historical {start:%B} {year}: {volume:,.0f} searches across matched keywords vs {mean:,.0f} monthly mean; seasonal ratio {volume/mean:.2f}, capped to 0.5–2 for scoring. Search month is not travel month.")
            else:
                notes.append("No complete usable monthly Ads history for this route; latest Ads baseline retained without a seasonal adjustment.")
            scores=score_fn(signals,row.get("SimFactor",1))
            row.update(PlanningMonth=start.strftime("%Y-%m"),PlanningLabel=start.strftime("%B %Y"),
                PlanningStart=effective.isoformat(),PlanningEnd=end.isoformat(),
                PlanningNotes=notes,ProfileScores=scores,PrioritySignals=signals,
                CampaignPriorityScore=round(scores["balanced"],1),EventDetails=events)
            output.append(row)
    return pd.DataFrame(output)
