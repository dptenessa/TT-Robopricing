"""Date-effective BI pricing, using the editor's projection without writing its DB."""
from datetime import date, timedelta
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

try:
    from pricing_timeline import PricingTimeline
    from promo_planner.store import PromoStore
    from promo_planner.date_price_changes import project_dataframe
    from pricing_book import normalize_pricing_dataframe
except ImportError:
    from automation.pricing_timeline import PricingTimeline
    from automation.promo_planner.store import PromoStore
    from automation.promo_planner.date_price_changes import project_dataframe
    from automation.pricing_book import normalize_pricing_dataframe


def pricing_date(value=None):
    return date.fromisoformat(str(value)) if value is not None else date.today()


class PricingHistoryUnavailable(ValueError):
    """The requested date predates the imported pricing baseline."""


def pricing_database(root):
    return Path(root) / 'outputs' / 'promo_planner' / 'promo_schedule.db'


def pricing_inputs(root):
    db = pricing_database(root)
    return [db, Path(str(db) + '-wal'), *sorted((db.parent / 'issue_choices').glob('*.json'))]


def pricing_signature(root, as_of=None):
    """Include the calendar day, WAL and decisions, even if Excel never changes."""
    stamps = []
    for path in pricing_inputs(root):
        stat = path.stat() if path.exists() else None
        stamps.append((str(path.resolve()), stat.st_size if stat else None,
                       stat.st_mtime_ns if stat else None))
    return hashlib.sha256(json.dumps([pricing_date(as_of).isoformat(), stamps]).encode()).hexdigest()


def pricing_metadata_path(output):
    return Path(output).with_suffix('.pricing.json')


def load_pricing_snapshot(root, *, as_of=None):
    """Published prices effective at midnight on as_of; never use the UI cursor.

    SQLite backup includes committed WAL data. All projection/migration helpers
    operate on that temporary copy, leaving the live database and Excel untouched.
    """
    root = Path(root)
    day = pricing_date(as_of)
    source = pricing_database(root)
    if not source.is_file():
        raise FileNotFoundError(f'Pricing database not found: {source}. BI requires the pricing database, not an Excel export.')
    with tempfile.TemporaryDirectory(prefix='tt-pricing-snapshot-') as folder:
        scratch = Path(folder)
        target = pricing_database(scratch)
        target.parent.mkdir(parents=True)
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(target)) as snapshot:
                live.backup(snapshot)
        timeline = PricingTimeline(target)
        info = timeline.baseline_info()
        if not info:
            raise ValueError('Pricing database has no baseline. Initialize it in the pricing editor before running BI.')
        if day.isoformat() < info['effective_from']:
            raise PricingHistoryUnavailable(f'No database pricing baseline exists for {day}; earliest is {info["effective_from"]}.')
        choices = source.parent / 'issue_choices'
        if choices.is_dir():
            shutil.copytree(choices, target.parent / 'issue_choices')
        store = PromoStore(target)
        key_date = (day - timedelta(days=1)).isoformat()
        base = timeline.project(day, layers=('base', 'import', 'override'), include_drafts=False)
        projected, preview = project_dataframe(base, scratch / 'unused.xlsx', store, key_date,
                                              scratch, replay_executed=True, include_drafts=False)
        if preview.collisions:
            raise ValueError(f'Pricing for {day} has unresolved promotion conflicts. Resolve them in Promo Planner before running BI.')
        result = normalize_pricing_dataframe(projected)
        result.attrs['pricing_as_of'] = day.isoformat()
        return result
