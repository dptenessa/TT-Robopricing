"""Read date-effective public offer prices for sales diagnostics; never write live DBs."""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import math
import re
import sqlite3


def build_price_evidence(payload, root=None, *, loader=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    database = root / 'outputs/promo_planner/promo_schedule.db'
    empty = dict(snapshots=[], note='Pricing database unavailable; sales comparisons still work.', changes=[])
    if not database.exists():
        return empty
    if loader is None:
        try:
            from pricing_snapshot import load_pricing_snapshot
        except ImportError:
            from automation.pricing_snapshot import load_pricing_snapshot
        loader = load_pricing_snapshot
    as_of = date.fromisoformat(payload['as_of'])
    first = date.fromisoformat(payload['start']) if payload.get('start') else as_of - timedelta(days=179)
    boundaries = {first.isoformat()}
    try:
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'pricing_baseline_meta' in tables:
                row = con.execute('SELECT effective_from FROM pricing_baseline_meta WHERE id=1').fetchone()
                if row:
                    boundaries.add(row[0])
            if 'pricing_change_sets' in tables:
                for start, end in con.execute("SELECT effective_from,expires_at FROM pricing_change_sets WHERE undone_at='' AND effective_from<=?", (as_of.isoformat(),)):
                    boundaries.update(d for d in (start, end) if d)
            if 'campaigns' in tables:
                for start, end in con.execute('SELECT start_date,end_date FROM campaigns'):
                    if start:
                        boundaries.add(start)
                    if end:
                        boundaries.add((date.fromisoformat(end)+timedelta(days=1)).isoformat())
    except (sqlite3.Error, ValueError):
        return dict(empty, note='Pricing dates could not be read; no price-change explanation is inferred.')
    boundaries = sorted(d for d in boundaries if first.isoformat() <= d <= as_of.isoformat())
    products = {(r[2], str(r[3]).casefold(), int(r[4])) for r in payload.get('rows', [])}
    known = {c['iso'] for c in payload.get('countries', [])}
    aliases = {c['name'].casefold(): c['iso'] for c in payload.get('countries', [])}
    def codes(row):
        # Exact destination identifiers only: never expand regional offers into country products.
        found = set()
        for field in ('ISO','ISO2','ISO3'):
            for value in re.split(r'[,;|/\s]+', str(row.get(field, '') or '').upper()):
                if value in known:
                    found.add(value)
                elif len(value) == 3 and value.isalpha():
                    try:
                        import pycountry
                        country = pycountry.countries.get(alpha_3=value)
                        if country and country.alpha_2 in known:
                            found.add(country.alpha_2)
                    except ImportError:
                        pass
        name = str(row.get('Country','')).casefold()
        if name in aliases:
            found.add(aliases[name])
        return found
    snapshots, errors, changes = [], 0, []
    previous = None
    print(f'Sales diagnosis: loading published pricing at {len(boundaries)} change boundaries.', flush=True)
    for stamp in boundaries:
        try:
            frame = loader(root, as_of=stamp)
            prices = {}
            for row in frame.to_dict('records'):
                plan = str(row.get('Plan','')).strip()
                try:
                    days = int(float(row.get('Days',0)))
                except (TypeError,ValueError,OverflowError):
                    continue
                for iso in codes(row):
                    if (iso, plan.casefold(), days) not in products:
                        continue
                    for currency in ('EUR','USD'):
                        try:
                            net = float(row.get(f'FinalPriceAfterPromo_{currency}'))
                        except (ValueError,TypeError):
                            continue
                        if not math.isfinite(net) or net < 0:
                            continue
                        key = (iso,plan,days,currency)
                        value = (round(net,4), str(row.get('PromoCode','') or '').strip())
                        # Ambiguous duplicate prices are unavailable, never picked arbitrarily.
                        prices[key] = value if key not in prices or prices[key] == value else None
            values = [[*key,*value] for key,value in sorted(prices.items()) if value is not None]
            if previous is not None and values != previous:
                changes.append(stamp)
            snapshots.append(dict(date=stamp, rows=values))
            previous = values
        except Exception as exc:
            # A failed boundary is a gap, not permission to carry older prices forward.
            snapshots.append(dict(date=stamp, rows=None))
            previous = None
            errors += 1
            print(f'Sales diagnosis: pricing unavailable at {stamp} ({type(exc).__name__}).', flush=True)
    note = 'Published database offer prices at comparison endpoints; not realised transaction prices. All interim price changes are flagged.'
    if errors:
        note += f' {errors} historical boundary/boundaries unavailable; affected comparisons have no price explanation.'
    return dict(snapshots=snapshots, changes=changes, note=note)
