#!/usr/bin/env python3
"""Preview/apply shared route exclusions to derived SQLite tables; back up before deletion."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from route_policy import route_allowed, origin_weights, ROUTE_POLICY_VERSION

TABLES = ('demand_observations', 'opportunity_scores', 'google_ads_keywords')

def clean_database(path, apply=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts={}
        for table in TABLES:
            if table not in tables:
                continue
            cols={r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if not {'origin_iso2','destination_iso2'}<=cols:
                raise ValueError(f'Unexpected schema: {table}')
            counts[table]=sum(n for o,d,n in db.execute(f'SELECT origin_iso2,destination_iso2,COUNT(*) FROM "{table}" GROUP BY origin_iso2,destination_iso2') if not route_allowed(o,d))
    print('Route policy:',ROUTE_POLICY_VERSION)
    print('Origin weights:', 'loaded' if origin_weights() else 'missing; only domestic/intra-EU exclusions applied')
    for table,count in counts.items():
        print(f'{table}: {count} excluded rows')
    if not apply or not sum(counts.values()):
        print('Preview only; use --apply to back up and delete.' if not apply else 'Nothing to remove.')
        return counts, None
    backup_dir=path.parent/'backups';backup_dir.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=backup_dir/f'{path.stem}_before_route_cleanup_{stamp}.db'
    with sqlite3.connect(str(path)) as db:
        db.execute('PRAGMA busy_timeout=30000')
        with sqlite3.connect(str(backup)) as dest:
            db.backup(dest)
            if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise RuntimeError('Backup integrity check failed; no deletion performed')
        db.create_function('route_allowed',2,lambda o,d:int(route_allowed(o,d)))
        with db:
            for table in counts:
                cursor=db.execute(f'DELETE FROM "{table}" WHERE route_allowed(origin_iso2,destination_iso2)=0')
                print(f'{table}: deleted {cursor.rowcount}')
    print('Backup:',backup)
    print('Events, holidays, raw Trends and source sales were retained. Rebuild opportunity scores and reports.')
    return counts,backup

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',default=str(Path(__file__).resolve().parent/'events.db'))
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    clean_database(args.database,args.apply)

if __name__=='__main__':
    main()
