#!/usr/bin/env python3
"""Import a Google Ads Keyword Planner CSV into events.db.

This is a temporary, manual alternative to the Google Ads API. Export one
Keyword Planner historical-metrics file per origin, then run for that origin.
The importer stores a baseline-search observation using the same score scale as
the API collector, so the opportunity builder can use it immediately.
"""
from __future__ import annotations
from route_policy import route_allowed, origin_allowed, configure_input_dir, ROUTE_POLICY_VERSION


import argparse
import csv
import io
import json
import math
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path


VERSION = "2.1-localized-manual-keywords"


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def number(value: object) -> float:
    text = str(value or "").strip().replace("%", "")
    if not text:
        return 0.0
    # Google exports may use either 1,234 or 1.234, depending on locale.
    if "," in text and "." in text:
        text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def aliases(input_dir: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    path = input_dir / "destination_aliases.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                code = str(row.get("destination_iso2") or "").strip().upper()
                values = [row.get("country_name", ""), row.get("aliases", "")]
                result[code] = [norm(x) for value in values for x in str(value or "").split("|") if norm(x)]
    # Useful fallbacks for older input folders.
    fallbacks = {
        "DE": ["germany", "deutschland", "berlin", "munich"],
        "FR": ["france", "paris", "nice"],
        "ES": ["spain", "madrid", "barcelona"],
        "MA": ["morocco", "marrakech", "casablanca"],
        "TR": ["turkey", "turkiye", "istanbul", "antalya"],
        "US": ["united states", "usa", "new york", "miami"],
        "JP": ["japan", "tokyo", "osaka", "kyoto"],
        "IT": ["italy", "rome", "milan", "venice"],
        "GB": ["united kingdom", "uk", "london"],
    }
    for code, values in fallbacks.items():
        result[code] = list(dict.fromkeys([*result.get(code, []), *values]))
    return result


def keyword_destinations(path: Path | None, origin: str) -> dict[str, str]:
    """Resolve supplied localized keywords explicitly before guessing aliases."""
    if path is None:
        return {}
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not {'origin_iso2', 'keyword', 'destination_iso2'}.issubset(reader.fieldnames or []):
            raise ValueError('Keyword map needs origin_iso2, keyword, destination_iso2 columns.')
        for row in reader:
            if str(row['origin_iso2']).strip().upper() != origin:
                continue
            key = norm(row['keyword'])
            code = str(row['destination_iso2']).strip().upper()
            if key and len(code) == 2 and code.isalpha():
                if key in result and result[key] != code:
                    raise ValueError(f'Ambiguous destination for keyword: {row["keyword"]}')
                result[key] = code
    return result


def find_column(headers: list[str], *needles: str) -> str | None:
    for header in headers:
        h = norm(header)
        if all(needle in h for needle in needles):
            return header
    return None


def destination_for(keyword: str, direct: object, known: dict[str, list[str]]) -> str | None:
    code = str(direct or "").strip().upper()
    if code in known:
        return code
    text = norm(keyword)
    matches = [(code, alias) for code, values in known.items() for alias in values if alias and alias in text]
    return sorted(matches, key=lambda x: len(x[1]), reverse=True)[0][0] if matches else None


def baseline(avg_monthly_searches: float) -> float:
    if avg_monthly_searches <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 20.0 * math.log10(avg_monthly_searches / 10.0))), 1)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
    CREATE TABLE IF NOT EXISTS demand_observations (
      observed_date TEXT NOT NULL, origin_iso2 TEXT NOT NULL,
      destination_iso2 TEXT NOT NULL, signal_type TEXT NOT NULL,
      signal_value REAL, baseline_value REAL, normalized_score REAL,
      source TEXT NOT NULL, confidence REAL, metadata_json TEXT,
      PRIMARY KEY (observed_date, origin_iso2, destination_iso2, signal_type, source)
    )
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS google_ads_keywords (
      observed_date TEXT NOT NULL, origin_iso2 TEXT NOT NULL,
      destination_iso2 TEXT NOT NULL, keyword TEXT NOT NULL,
      year INTEGER NOT NULL, month INTEGER NOT NULL,
      monthly_searches REAL, avg_monthly_searches REAL, source TEXT NOT NULL,
      PRIMARY KEY (observed_date, origin_iso2, destination_iso2, keyword, year, month, source)
    )
    """)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a manual Google Ads Keyword Planner CSV")
    parser.add_argument("--export", required=True, type=Path, help="Downloaded Keyword Planner CSV")
    parser.add_argument("--origin", required=True, help="Origin ISO2 used for the Keyword Planner location")
    parser.add_argument("--database", default=str(Path(__file__).resolve().parent / "events.db"))
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parent / "inputs" / "travel_demand")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--keyword-map", type=Path, help="Optional exact localized keyword/destination mapping CSV")
    args = parser.parse_args()

    origin = args.origin.strip().upper()
    configure_input_dir(args.input_dir)
    if not origin_allowed(origin):
        raise SystemExit(f'Origin {origin} is excluded by the current market-weight policy. Check origin_market_weights.csv.')
    exact_destinations = keyword_destinations(args.keyword_map, origin)
    if not Path(args.database).is_file():
        raise SystemExit(f'Existing BI database not found: {args.database}. Check the project folder and --database path.')
    raw = args.export.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        export_text = raw.decode("utf-16")
    else:
        export_text = raw.decode("utf-8-sig")
    # Google Ads prepends a report title and date-range line, and this export
    # uses tabs even though the file extension is CSV. Start at the real table
    # header rather than treating the report title as the header row.
    lines = export_text.splitlines()
    header_index = next(
        (index for index, line in enumerate(lines)
         if "Keyword" in line and "Avg. monthly searches" in line),
        None,
    )
    if header_index is None:
        raise SystemExit("Could not locate the Google Ads keyword table header.")
    table_text = "\n".join(lines[header_index:])
    try:
        dialect = csv.Sniffer().sniff(table_text[:2000], delimiters="\t,")
    except csv.Error:
        dialect = csv.excel_tab
    rows = list(csv.DictReader(io.StringIO(table_text), dialect=dialect))
    if not rows:
        raise SystemExit("The Google Ads export contains no rows.")

    headers = list(rows[0])
    keyword_col = find_column(headers, "keyword") or find_column(headers, "search", "term")
    avg_col = find_column(headers, "avg", "monthly", "search") or find_column(headers, "average", "monthly", "search")
    direct_dest_col = next((h for h in headers if norm(h) in {"destination", "destination iso2", "destination country"}), None)
    if not keyword_col:
        raise SystemExit("Could not find a Keyword column in the export.")
    if not avg_col:
        raise SystemExit("Could not find an Avg. monthly searches column in the export.")
    for row in rows:
        value = str(row.get(avg_col) or '').strip()
        if re.search(r'\d\s*[–—-]\s*\d|\d\s*[kKmM]\b', value):
            raise SystemExit('The export contains search-volume ranges or abbreviated values. Download numeric historical metrics before importing.')

    month_names = {name.lower(): index for index, name in enumerate(
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
    month_columns = {}
    for header in headers:
        match = re.fullmatch(r"Searches:\s*([A-Za-z]{3})\s+(\d{4})", header.strip())
        if match and match[1].lower() in month_names:
            month_columns[header] = (int(match[2]), month_names[match[1].lower()])
    history_rows = {}
    known = aliases(args.input_dir)
    for code in exact_destinations.values():
        known.setdefault(code, [])
    def resolve(row):
        keyword = row.get(keyword_col, '')
        return exact_destinations.get(norm(keyword)) or destination_for(
            keyword, row.get(direct_dest_col, '') if direct_dest_col else '', known)
    totals: dict[str, float] = {}
    matched_rows = 0
    for row in rows:
        dest = resolve(row)
        if not dest or not route_allowed(origin, dest):
            continue
        totals[dest] = totals.get(dest, 0.0) + number(row.get(avg_col))
        matched_rows += 1
        keyword = str(row.get(keyword_col, "")).strip()
        for column, (year, month) in month_columns.items():
            raw_value = str(row.get(column) or "").strip()
            # Missing values are not zero searches.
            if not re.fullmatch(r"[0-9][0-9,. ]*", raw_value):
                continue
            value = number(raw_value)
            history_rows[(dest, keyword, year, month)] = (
                args.date, origin, dest, keyword, year, month, value,
                number(row.get(avg_col)), "Google Ads Keyword Planner")


    if not totals:
        raise SystemExit("No exported keywords could be mapped to destinations. Check the keyword language/aliases.")

    db = sqlite3.connect(args.database)
    ensure_schema(db)
    source = "Google Ads Keyword Planner"
    for dest, average in totals.items():
        score = baseline(average)
        metadata = {
            "avg_monthly_searches": round(average, 2),
            "manual_export": str(args.export),
            "matched_keyword_rows": sum(1 for row in rows if resolve(row) == dest),
            "interpretation": "Manual Keyword Planner snapshot; not campaign performance",
        }
        db.execute("""
        INSERT INTO demand_observations(
          observed_date, origin_iso2, destination_iso2, signal_type,
          signal_value, baseline_value, normalized_score, source, confidence, metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(observed_date, origin_iso2, destination_iso2, signal_type, source)
        DO UPDATE SET signal_value=excluded.signal_value,
          baseline_value=excluded.baseline_value,
          normalized_score=excluded.normalized_score,
          confidence=excluded.confidence,
          metadata_json=excluded.metadata_json
        """, (args.date, origin, dest, "baseline_search", score, average, score, source, 0.80, json.dumps(metadata, ensure_ascii=False)))
    # Replace this origin's manual monthly snapshot atomically on re-import.
    # Older snapshots remain history; report reads one latest collection only.
    if month_columns:
        db.execute("DELETE FROM google_ads_keywords WHERE observed_date=? AND origin_iso2=? AND source=?",
                   (args.date, origin, source))
        db.executemany("INSERT INTO google_ads_keywords VALUES(?,?,?,?,?,?,?,?,?)",
                       history_rows.values())
    db.commit()
    db.close()
    print(f"Monthly history: {len(history_rows)} keyword/month rows from {len(month_columns)} monthly columns")
    print(f"Imported {matched_rows} keyword rows for {origin}; updated {len(totals)} destination baselines in {args.database}")
    for dest, average in sorted(totals.items(), key=lambda x: -x[1])[:20]:
        print(f"  {origin}->{dest}: baseline {baseline(average):.1f} ({average:,.0f} average monthly searches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
