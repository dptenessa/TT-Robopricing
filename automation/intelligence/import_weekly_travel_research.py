#!/usr/bin/env python3
"""Import a ChatGPT weekly travel-research JSON report into the existing events.db.

Stories are written to destination_articles/destination_article_topics; only
source-dated, destination-specific occurrences are written to events. The input
is idempotent, uses the existing 60-day story retention rules, and never creates
or replaces the database file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from destination_coverage import (
    TOPICS,
    canonical_url,
    country_codes,
    ensure_schema as ensure_coverage_schema,
    prune_coverage,
    store_coverage,
)
from event_matching import scoreable

VERSION = "1.0.0-weekly-agent-import"
ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "events.db"
DEFAULT_REPORT = ROOT / "inbox" / "weekly_travel_research.json"
SCHEMA_VERSION = "weekly-travel-research-v1"


def parse_iso_date(value: object, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def load_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Expected JSON schema_version {SCHEMA_VERSION!r}")
    if not isinstance(value.get("items"), list):
        raise ValueError("Report must contain an items array")
    value["report_date"] = parse_iso_date(value.get("report_date"), "report_date")
    return value


def load_country_names() -> dict[str, str]:
    names: dict[str, str] = {}
    path = ROOT / "inputs" / "travel_demand" / "destination_aliases.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = str(row.get("destination_iso2") or "").strip().upper()
                name = str(row.get("country_name") or "").strip()
                if code and name:
                    names[code] = name
    return names


def existing_event_table(db: sqlite3.Connection) -> set[str]:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
    if not row:
        raise RuntimeError("The selected existing database has no events table; import stopped without creating a database.")
    return {r[1] for r in db.execute("PRAGMA table_info(events)")}


def ensure_import_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_research_imports (
            report_sha256 TEXT PRIMARY KEY,
            report_date TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            destination_links_written INTEGER NOT NULL,
            dated_events_written INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL
        )
    """)


def validate_url(value: object) -> str:
    url = canonical_url(value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Each story needs a canonical HTTPS source URL")
    return url


def article_payload(item: dict, report_date: str) -> tuple[dict, list[dict]]:
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    if not title or not summary:
        raise ValueError("item title and summary are required")
    published = parse_iso_date(item.get("published_date"), "published_date")
    url = validate_url(item.get("url"))
    publisher = str(item.get("publisher") or urlsplit(url).hostname or "").strip()
    if not publisher:
        raise ValueError("publisher is missing")
    destinations = item.get("destinations", [])
    if not isinstance(destinations, list):
        raise ValueError("destinations must be an array")
    valid_codes = country_codes()
    clean_destinations = []
    evidence_parts = []
    for entry in destinations:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("country_iso2") or "").strip().upper()
        evidence = str(entry.get("evidence") or "").strip()
        topics = entry.get("topics", [])
        if code not in valid_codes or not isinstance(topics, list) or not evidence:
            continue
        selected_topics = sorted({str(x).strip().lower() for x in topics if str(x).strip().lower() in TOPICS})
        if not selected_topics:
            continue
        clean_destinations.append({"country_iso2": code, "topics": selected_topics,
                                   "evidence": evidence, "confidence": "medium"})
        evidence_parts.append(evidence)
    item_record = {
        "event_name": title,
        "description": summary + (("\nSource evidence: " + "\n".join(dict.fromkeys(evidence_parts))) if evidence_parts else ""),
        "source_url": url,
        "published_date": published,
    }
    return item_record, clean_destinations


def insert_event(db: sqlite3.Connection, columns: set[str], item: dict,
                 occurrence: dict, report_date: str, run_date: date,
                 country_names: dict[str, str]) -> bool:
    code = str(occurrence.get("country_iso2") or "").strip().upper()
    if code not in country_codes():
        return False
    start = parse_iso_date(occurrence.get("start_date"), "event start_date")
    end = parse_iso_date(occurrence.get("end_date"), "event end_date", allow_empty=True) or start
    if end < start:
        raise ValueError("event end_date precedes start_date")
    event_start = date.fromisoformat(start)
    if event_start < run_date or event_start > run_date + timedelta(days=90):
        return False
    country = country_names.get(code, "")
    if not country:
        # Resolve common country names without making up a label.
        try:
            import pycountry
            country = pycountry.countries.get(alpha_2=code).name
        except (ImportError, AttributeError):
            return False
    destination = str(occurrence.get("destination") or "").strip() or country
    event_name = str(occurrence.get("event_name") or item.get("title") or "").strip()
    if not event_name:
        return False
    row = {
        "Occasion_ID": "AGT" + hashlib.sha256(
            f"{item['source_url']}|{code}|{destination}|{start}|{event_name}".encode("utf-8")
        ).hexdigest()[:12].upper(),
        "Occasion_Event": event_name,
        "Occasion_Type": "Travel event",
        "Country": country,
        "Destination": destination,
        "Start_Date": start,
        "End_Date": end,
        "Main_Traveller_Segment": str(item.get("audience_segment") or "").strip(),
        "Interests_Motivation": str(item.get("interests") or "").strip(),
        "Opportunity_Score": "",
        "Priority": "Prepare" if item.get("importance") == "high" else "Monitor",
        "Confidence": str(item.get("confidence") or "medium").strip().lower(),
        "Source_URL": item["source_url"],
        "Source_Type": "Weekly agent research",
        "Last_Researched": report_date,
        "Notes": " ".join(x for x in [str(item.get("summary") or "").strip(),
                                       str(item.get("campaign_relevance") or "").strip()] if x),
        "Content_Classification": "specific dated event",
        "first_seen": report_date,
        "last_seen": report_date,
        "times_seen": "1",
    }
    row = {key: value for key, value in row.items() if key in columns}
    required = {"Occasion_ID", "Occasion_Event", "Country", "Destination", "Start_Date", "Source_URL"}
    if not required.issubset(row):
        raise RuntimeError("events table is missing required columns: " + ", ".join(sorted(required - row.keys())))
    # Run the same corridor-specific test as the opportunity builder before writing.
    event_for_match = {
        "Country": country, "Destination": destination, "Start_Date": start,
        "End_Date": end, "Content_Classification": row.get("Content_Classification", ""),
        "Notes": row.get("Notes", ""),
    }
    if not scoreable(event_for_match):
        return False
    duplicate = db.execute(
        'SELECT 1 FROM events WHERE "Source_URL"=? AND "Occasion_Event"=? AND "Start_Date"=? LIMIT 1',
        (item["source_url"], event_name, start),
    ).fetchone()
    if duplicate:
        return False
    names = list(row)
    quoted = ",".join('"' + name.replace('"', '""') + '"' for name in names)
    placeholders = ",".join("?" for _ in names)
    db.execute(f"INSERT OR IGNORE INTO events ({quoted}) VALUES ({placeholders})", [row[x] for x in names])
    return db.execute('SELECT changes()').fetchone()[0] > 0


def import_report(database: Path, report_path: Path, as_of: date | None = None) -> dict:
    if not database.is_file():
        raise FileNotFoundError(f"Existing Intelligence database not found: {database}")
    if not report_path.is_file():
        with sqlite3.connect(database, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            existing_event_table(db)
            ensure_import_schema(db)
        return {"status": "no_report", "message": f"No weekly report found at {report_path}; nothing imported."}
    raw_report = report_path.read_bytes()
    report_hash = hashlib.sha256(raw_report).hexdigest()
    report = load_report(report_path)
    run_day = as_of or date.today()
    run_date = run_day.isoformat()
    cutoff = (run_day - timedelta(days=59)).isoformat()
    stories_written = events_written = skipped_old = invalid_items = 0
    with sqlite3.connect(database, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        event_columns = existing_event_table(db)
        ensure_coverage_schema(db)
        ensure_import_schema(db)
        if db.execute("SELECT 1 FROM agent_research_imports WHERE report_sha256=?", (report_hash,)).fetchone():
            return {"status": "already_imported", "report_date": report["report_date"],
                    "message": "This exact weekly report was already imported."}
        countries = load_country_names()
        with db:
            for raw in report["items"]:
                try:
                    if not isinstance(raw, dict):
                        raise ValueError("item must be an object")
                    story, destinations = article_payload(raw, report["report_date"])
                    story_is_old = story["published_date"] < cutoff
                    if story_is_old:
                        skipped_old += 1
                    # Upsert only destination-grounded stories. The shared validator
                    # checks that the evidence excerpt names a mapped place in the item.
                    if destinations and not story_is_old:
                        stories_written += store_coverage(db, story, destinations, "ChatGPT weekly travel research", run_date)
                    occurrences = raw.get("events", [])
                    if not isinstance(occurrences, list):
                        raise ValueError("events must be an array")
                    for occurrence in occurrences:
                        if isinstance(occurrence, dict) and insert_event(
                            db, event_columns, story, occurrence, report["report_date"], run_day, countries
                        ):
                            events_written += 1
                except (ValueError, TypeError, KeyError) as exc:
                    invalid_items += 1
                    print(f"AGENT RESEARCH WARNING: {str(raw.get('title','[untitled]'))[:100]}: {exc}")
            pruned = prune_coverage(db, run_day)
            db.execute("""
                INSERT INTO agent_research_imports VALUES(?,?,?,?,?,?,?)
            """, (report_hash, report["report_date"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  len(report["items"]), stories_written, events_written, skipped_old + invalid_items))
    return {"status": "imported", "items": len(report["items"]), "destination_links_written": stories_written,
            "dated_events_written": events_written, "older_than_60d_skipped": skipped_old,
            "invalid_items_skipped": invalid_items, "expired_story_rows_pruned": pruned,
            "report_date": report["report_date"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB,
                        help="Existing Intelligence events.db; must already exist")
    parser.add_argument("--input", type=Path, default=DEFAULT_REPORT,
                        help="Saved weekly_travel_research-v1 JSON report")
    args = parser.parse_args()
    try:
        result = import_report(args.database, args.input)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"AGENT RESEARCH IMPORT ERROR: {exc}", file=sys.stderr)
        return 1
    print("AGENT RESEARCH: " + json.dumps(result, ensure_ascii=False))
    print("Agent research is supplemental evidence; no route-demand or audience-volume values were inferred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
