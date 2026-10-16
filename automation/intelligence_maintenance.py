"""Bounded retention for Intelligence only. Never touches pricing history."""
from datetime import date, timedelta
from contextlib import closing
import errno
import time
from pathlib import Path
import os
import sqlite3
import tempfile

VERSION = "1.0.1-close-backup-before-replace"


def clean_database(database, backup_path, as_of=None):
    database = Path(database).resolve()
    if not database.is_file():
        return {"status": "skipped", "reason": "Intelligence database does not exist"}
    cutoff = ((as_of or date.today()) - timedelta(days=59)).isoformat()
    db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        plans = []
        def add(table, columns, expression):
            if table not in tables:
                return
            actual = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(columns) <= actual:
                raise RuntimeError(f"Unexpected schema for {table}; cleanup cancelled")
            # Invalid/unknown dates are retained for review, never guessed.
            predicate = f"julianday({expression}) < julianday(?)"
            plans.append((table, predicate, (cutoff,)))
        for table, column in [
            ("google_trends_terms", "refresh_date"),
            ("demand_observations", "observed_date"),
            ("opportunity_scores", "score_date"),
            ("google_ads_keywords", "observed_date"),
            ("holiday_periods", "holiday_date"),
            ("radar_processed", "processed_date"),
        ]:
            add(table, [column], f'"{column}"')
        add("events", ["End_Date", "Start_Date"],
            'COALESCE(NULLIF("End_Date",\'\'),NULLIF("Start_Date",\'\'))')
        if "destination_articles" in tables:
            cols = {r[1] for r in db.execute('PRAGMA table_info(destination_articles)')}
            if not {"published_date","first_seen","url"} <= cols:
                raise RuntimeError("Unexpected article schema; cleanup cancelled")
            if "destination_article_topics" in tables:
                plans.append(("destination_article_topics",
                    "url IN (SELECT url FROM destination_articles WHERE "
                    "julianday(COALESCE(NULLIF(published_date,''),first_seen)) < julianday(?))",
                    (cutoff,)))
            add("destination_articles", ["published_date","first_seen"],
                "COALESCE(NULLIF(published_date,''),first_seen)")
        if "collector_refresh" in tables:
            plans.append(("collector_refresh",
                "source='trends-v1' AND julianday(period)<julianday(?)",(cutoff,)))
        # Keep coverage_first_seen hashes: these tiny tombstones prevent an old,
        # undated article from returning as a newly discovered story.
        db.execute("BEGIN IMMEDIATE")
        counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {p}',args).fetchone()[0]
                  for t,p,args in plans}
        if not any(counts.values()):
            db.rollback()
            return {"status":"current","cutoff":cutoff,"deleted":counts}
        # One rotating consistent backup, made while other writers are excluded.
        backup_path = Path(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="retention-",suffix=".db",dir=backup_path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=5)) as reader:
                with closing(sqlite3.connect(temp)) as backup:
                    reader.backup(backup)
            # SQLite context managers commit/rollback; closing() also releases
            # Windows file handles before replace. Retry brief sync/AV locks.
            for attempt, delay in enumerate((0, 0.2, 0.5, 1, 2)):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(temp, backup_path)
                    break
                except OSError as exc:
                    locked = (getattr(exc, "winerror", None) in (5, 32, 33)
                              or exc.errno in (errno.EACCES, errno.EBUSY, errno.EPERM))
                    if not locked or attempt == 4:
                        raise
        finally:
            try:
                if os.path.exists(temp):
                    os.unlink(temp)
            except OSError:
                # Preserve the actual backup error; no database deletes have run.
                pass
        for table,predicate,args in plans:
            db.execute(f'DELETE FROM "{table}" WHERE {predicate}',args)
        db.commit()
        return {"status":"cleaned","cutoff":cutoff,"deleted":counts,
                "backup":str(backup_path),"note":"Freed pages are reused; no automatic VACUUM"}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
