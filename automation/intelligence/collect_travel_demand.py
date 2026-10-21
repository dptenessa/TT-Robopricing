#!/usr/bin/env python3
"""T-Travel demand-signal collector.

V1.2:
- public holidays via Nager.Date
- transparent seasonality proxy
- official Google Trends international BigQuery dataset
- Google Ads Keyword Planner historical metrics (optional)

Important design:
Google Trends raw observations are stored before interpretation. The collector
records destination-related terms found in the daily Top/Rising tables over the
last 30 days. Search momentum is then derived from that stored history, rather
than treating a single rank as a fake 0-100 momentum score.
"""
from __future__ import annotations
from functools import lru_cache
from collector_cache import ensure_cache_schema, fresh, mark, cached_trends

from route_policy import route_allowed, origin_allowed, ROUTE_POLICY_VERSION, configure_input_dir


from pathlib import Path
import argparse
import csv
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import urllib.request
import urllib.error
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

VERSION = "1.7.2-trends-adc-recovery"

TEST_ORIGINS = ["DE", "FR"]
TEST_DESTINATIONS = ["JP", "TR", "ES", "MA", "US"]
PORTFOLIO_FILE = r"C:\Users\ddeblock\OneDrive - Hrvatski Telekom\Documents\Python\TT-Robopricing\inputs\WS_PPG.csv"
INPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "inputs",
    "travel_demand",
)

GOOGLE_ADS_ORIGINS: dict[str, dict[str, str]] = {}
DESTINATION_NAMES: dict[str, dict[str, str]] = {"EN": {}}


def load_portfolio_destinations(path: str = PORTFOLIO_FILE) -> list[str]:
    """Load the complete destination portfolio from the pricing CSV."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read destination portfolio CSV: {path}\n{exc}"
        ) from exc

    destinations = sorted({
        str(row.get("ISO_Code_A2") or "").strip().upper()
        for row in rows
        if str(row.get("ISO_Code_A2") or "").strip()
    })
    if not destinations:
        raise RuntimeError(
            f"No ISO_Code_A2 destinations found in portfolio CSV: {path}"
        )

    # Add all portfolio country names as English fallbacks for Ads/Trends.
    for row in rows:
        code = str(row.get("ISO_Code_A2") or "").strip().upper()
        country = str(row.get("country") or "").strip()
        if code and country:
            DESTINATION_NAMES.setdefault("EN", {}).setdefault(code, country)

    return destinations


def load_input_files(
    input_dir: str = INPUT_DIR,
    portfolio_file: str = PORTFOLIO_FILE,
    all_origins: bool = False,
) -> tuple[list[str], list[str]]:
    """Load configurable origins, aliases, templates and seasonality inputs.

    The CSV can contain the complete global origin universe. By default only
    rows marked enabled are used; ``all_origins`` activates every valid ISO-2
    row for a full-world run.
    """
    global GOOGLE_ADS_ORIGINS, DESTINATION_NAMES, TREND_ALIASES
    global TRAVEL_TEMPLATES, SEASONALITY, SEASONALITY_META

    def read_rows(filename: str) -> list[dict[str, str]]:
        path = os.path.join(input_dir, filename)
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except OSError as exc:
            raise RuntimeError(f"Cannot read input file: {path}\n{exc}") from exc

    trend_matches_destination.cache_clear()
    origin_rows = read_rows("origins.csv")
    origins = [
        str(row.get("origin_iso2") or "").strip().upper()
        for row in origin_rows
        if str(row.get("origin_iso2") or "").strip()
        and (
            all_origins
            or str(row.get("enabled") or "1").strip()
            not in {"0", "false", "no"}
        )
    ]

    for row in origin_rows:
        code = str(row.get("origin_iso2") or "").strip().upper()
        geo_id = str(row.get("google_geo_id") or "").strip()
        language_id = str(row.get("google_language_id") or "").strip()
        if code and geo_id and language_id:
            GOOGLE_ADS_ORIGINS[code] = {
                "geo_id": geo_id,
                "language_id": language_id,
            }

    aliases = read_rows("destination_aliases.csv")
    for row in aliases:
        code = str(row.get("destination_iso2") or "").strip().upper()
        name = str(row.get("country_name") or "").strip()
        values = [x.strip().lower() for x in str(row.get("aliases") or "").split("|") if x.strip()]
        if code:
            DESTINATION_NAMES.setdefault("EN", {})[code] = name or code
            TREND_ALIASES[code] = values or [name.lower() or code.lower()]

    templates = read_rows("origin_keyword_templates.csv")
    loaded_templates: dict[str, list[str]] = defaultdict(list)
    for row in templates:
        code = str(row.get("origin_iso2") or "").strip().upper()
        template = str(row.get("template") or "").strip()
        if code and template:
            loaded_templates[code].append(template)
    for code, values in loaded_templates.items():
        if code == "DEFAULT":
            continue
        TRAVEL_TEMPLATES[code] = values
    if loaded_templates.get("DEFAULT"):
        TRAVEL_TEMPLATES["DEFAULT"] = loaded_templates["DEFAULT"]

    seasonality = read_rows("destination_seasonality.csv")
    for row in seasonality:
        code = str(row.get("destination_iso2") or "").strip().upper()
        try:
            month = int(str(row.get("month") or ""))
            score = float(str(row.get("score") or ""))
        except ValueError:
            continue
        if code and 1 <= month <= 12:
            SEASONALITY.setdefault(code, {})[month] = score
            source = str(row.get("source") or "input").strip()
            confidence = str(row.get("confidence") or "unknown").strip()
            SEASONALITY_META.setdefault(code, {})[month] = f"{source} / {confidence}"

    if not origins:
        raise RuntimeError(f"No enabled origins found in {os.path.join(input_dir, 'origins.csv')}")
    return origins, load_portfolio_destinations(portfolio_file)

TREND_ALIASES: dict[str, list[str]] = {}
TRAVEL_TEMPLATES: dict[str, list[str]] = {}
SEASONALITY: dict[str, dict[int, float]] = {}
SEASONALITY_META: dict[str, dict[int, str]] = {}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS demand_observations (
        observed_date TEXT NOT NULL,
        origin_iso2 TEXT NOT NULL,
        destination_iso2 TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        signal_value REAL,
        baseline_value REAL,
        normalized_score REAL,
        source TEXT NOT NULL,
        confidence REAL,
        metadata_json TEXT,
        PRIMARY KEY (observed_date, origin_iso2, destination_iso2, signal_type, source)
    );

    CREATE TABLE IF NOT EXISTS holiday_periods (
        origin_iso2 TEXT NOT NULL,
        holiday_date TEXT NOT NULL,
        holiday_name TEXT NOT NULL,
        local_name TEXT,
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (origin_iso2, holiday_date, holiday_name)
    );

    CREATE TABLE IF NOT EXISTS google_trends_terms (
        refresh_date TEXT NOT NULL,
        origin_iso2 TEXT NOT NULL,
        term TEXT NOT NULL,
        kind TEXT NOT NULL,
        rank INTEGER,
        dataset_score REAL,
        week TEXT,
        captured_at TEXT NOT NULL,
        PRIMARY KEY (refresh_date, origin_iso2, term, kind)
    );

    CREATE INDEX IF NOT EXISTS idx_demand_corridor_date
      ON demand_observations(origin_iso2, destination_iso2, observed_date);

    CREATE INDEX IF NOT EXISTS idx_holidays_origin_date
      ON holiday_periods(origin_iso2, holiday_date);

    CREATE INDEX IF NOT EXISTS idx_trends_origin_date
      ON google_trends_terms(origin_iso2, refresh_date);

    CREATE TABLE IF NOT EXISTS google_ads_keywords (
        observed_date TEXT NOT NULL,
        origin_iso2 TEXT NOT NULL,
        destination_iso2 TEXT NOT NULL,
        keyword TEXT NOT NULL,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        monthly_searches REAL,
        avg_monthly_searches REAL,
        source TEXT NOT NULL,
        PRIMARY KEY (
            observed_date, origin_iso2, destination_iso2,
            keyword, year, month, source
        )
    );

    CREATE INDEX IF NOT EXISTS idx_ads_corridor_date
      ON google_ads_keywords(origin_iso2, destination_iso2, observed_date);
    """)


class HolidayDataUnavailable(RuntimeError):
    """The source explicitly returned no calendar; not an empty holiday year."""


def _holiday_json(endpoint: str, timeout: int):
    req = urllib.request.Request(
        "https://date.nager.at/api/v3/" + endpoint,
        headers={"User-Agent": "T-Travel Demand Collector/" + VERSION,
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            body = response.read()
            content_type = response.headers.get("Content-Type", "unknown")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Nager.Date HTTP {exc.code} for {endpoint}") from exc
    if status == 204:
        raise HolidayDataUnavailable("Nager.Date returned HTTP 204 (no calendar supplied)")
    if not body.strip():
        raise RuntimeError(f"Nager.Date HTTP {status}: empty response for {endpoint}")
    try:
        result = json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(
            f"Nager.Date HTTP {status}: invalid JSON ({content_type}) for {endpoint}"
        ) from exc
    if not isinstance(result, list):
        raise RuntimeError(f"Nager.Date expected an array for {endpoint}")
    return result


def holiday_support(db, timeout, force=False, offline=False):
    """Cache the provider's country list for seven days; never guess support."""
    db.execute("""CREATE TABLE IF NOT EXISTS holiday_source_cache (
        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, checked_at REAL NOT NULL)""")
    cached = db.execute(
        "SELECT payload,checked_at FROM holiday_source_cache WHERE cache_key='countries'"
    ).fetchone()
    if cached and not force and 0 <= time.time()-cached[1] < 7*86400:
        return set(json.loads(cached[0]))
    if offline:
        return None
    try:
        rows = _holiday_json("AvailableCountries", timeout)
        codes = {r["countryCode"].upper() for r in rows
                 if isinstance(r, dict) and isinstance(r.get("countryCode"), str)
                 and len(r["countryCode"]) == 2 and r["countryCode"].isalpha()}
        if not codes or len(codes) != len(rows):
            raise RuntimeError("Nager.Date country list is empty or malformed")
        with db:
            db.execute("INSERT OR REPLACE INTO holiday_source_cache VALUES(?,?,?)",
                       ("countries", json.dumps(sorted(codes)), time.time()))
        return codes
    except Exception as exc:
        print(f"HOLIDAY ERR country-list: {exc}; trying individual calendars")
        return None


def fetch_public_holidays(country: str, year: int, timeout: int) -> list[dict]:
    rows = _holiday_json(f"PublicHolidays/{year}/{country}", timeout)
    # Validate the whole response before replacing a previously valid snapshot.
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
            raise RuntimeError("Nager.Date holiday record has no valid name")
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Nager.Date holiday record has no valid date") from exc
        if day.year != year or row.get("countryCode") != country:
            raise RuntimeError("Nager.Date holiday record is outside requested country/year")
    return rows


def refresh_holiday_calendars(db, origins, as_of, timeout, force=False, offline=False):
    supported = holiday_support(db, timeout, force, offline)
    years = sorted({as_of.year, (as_of + timedelta(days=90)).year})
    if offline:
        print("HOLIDAYS: offline; using only valid cached calendars")
        return
    for origin in origins:
        if supported is not None and origin not in supported:
            # Invalidate old success markers, not historical holiday records.
            with db:
                db.execute("DELETE FROM collector_refresh WHERE source='holidays-v1' AND origin=?", (origin,))
            print(f"HOLIDAYS {origin}: unsupported by Nager.Date; holiday signal N/A")
            continue
        for year in years:
            if not force and holidays_cached(db, origin, year):
                print(f"HOLIDAYS {origin}/{year}: cached calendar")
                continue
            if not force and fresh(db, "holidays-unavailable-v1", origin, str(year), 86400):
                print(f"HOLIDAYS {origin}/{year}: source supplied no calendar; retry after 24h")
                continue
            try:
                rows = fetch_public_holidays(origin, year, timeout)
                with db:
                    db.execute("""DELETE FROM holiday_periods WHERE origin_iso2=?
                        AND source='Nager.Date' AND holiday_date BETWEEN ? AND ?""",
                        (origin, f"{year}-01-01", f"{year}-12-31"))
                    count = store_holidays(db, origin, rows)
                    mark(db, "holidays-v1", origin, str(year))
                    db.execute("""DELETE FROM collector_refresh
                        WHERE source='holidays-unavailable-v1' AND origin=? AND period=?""",
                        (origin, str(year)))
                print(f"HOLIDAYS {origin}/{year}: {count} loaded/refreshed")
            except HolidayDataUnavailable as exc:
                with db:
                    mark(db, "holidays-unavailable-v1", origin, str(year))
                    db.execute("""DELETE FROM collector_refresh
                        WHERE source='holidays-v1' AND origin=? AND period=?""",
                        (origin, str(year)))
                print(f"HOLIDAYS {origin}/{year}: {exc}; signal unavailable, retry after 24h")
            except Exception as exc:
                print(f"HOLIDAY ERR {origin}/{year}: {exc}; refresh not marked successful")


def store_holidays(db: sqlite3.Connection, country: str, rows: list[dict]) -> int:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    n = 0

    for row in rows:
        if not row.get("date") or not row.get("name"):
            continue

        db.execute("""
        INSERT INTO holiday_periods(
            origin_iso2, holiday_date, holiday_name,
            local_name, source, fetched_at
        )
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(origin_iso2, holiday_date, holiday_name)
        DO UPDATE SET
            local_name=excluded.local_name,
            source=excluded.source,
            fetched_at=excluded.fetched_at
        """, (
            country,
            row["date"],
            row["name"],
            row.get("localName", ""),
            "Nager.Date",
            now,
        ))
        n += 1

    return n


def holidays_cached(db: sqlite3.Connection, country: str, year: int) -> bool:
    return fresh(db, "holidays-v1", country, str(year), 30*86400)


def holiday_score(
    db: sqlite3.Connection,
    origin: str,
    as_of: date,
    horizon_days: int = 45,
) -> tuple[float | None, dict]:
    end = as_of + timedelta(days=horizon_days)
    if not all(holidays_cached(db, origin, year) for year in range(as_of.year, end.year + 1)):
        return None, {"upcoming": [], "available": False,
                      "note": "Holiday calendar unavailable, incomplete or stale; not evidence of no holidays"}

    rows = db.execute("""
        SELECT holiday_date, holiday_name
        FROM holiday_periods
        WHERE origin_iso2=?
          AND holiday_date BETWEEN ? AND ?
        ORDER BY holiday_date
    """, (
        origin,
        as_of.isoformat(),
        end.isoformat(),
    )).fetchall()

    if not rows:
        return 35.0, {
            "upcoming": [],
            "note": "No public holiday in horizon; school holidays not yet included",
        }

    best = 35.0
    upcoming = []

    for ds, name in rows:
        d = date.fromisoformat(ds)
        days = (d - as_of).days
        score = max(40.0, 70.0 - 0.7 * days)
        best = max(best, score)
        upcoming.append({
            "date": ds,
            "name": name,
            "days_away": days,
        })

    return round(min(70.0, best), 1), {
        "upcoming": upcoming,
        "note": "Public-holiday proxy only; school holidays not yet modeled",
    }


def seasonality_score(destination: str, month: int) -> tuple[float, str]:
    if destination in SEASONALITY:
        return (
            float(SEASONALITY[destination].get(month, 50)),
            SEASONALITY_META.get(destination, {}).get(month, "configured input"),
        )
    return 50.0, "neutral fallback"


OBSERVATION_SQL = '\n    INSERT INTO demand_observations(\n        observed_date, origin_iso2, destination_iso2,\n        signal_type, signal_value, baseline_value,\n        normalized_score, source, confidence, metadata_json\n    )\n    VALUES(?,?,?,?,?,?,?,?,?,?)\n    ON CONFLICT(\n        observed_date, origin_iso2, destination_iso2,\n        signal_type, source\n    )\n    DO UPDATE SET\n        signal_value=excluded.signal_value,\n        baseline_value=excluded.baseline_value,\n        normalized_score=excluded.normalized_score,\n        confidence=excluded.confidence,\n        metadata_json=excluded.metadata_json\n    '

def upsert_observation(
    db: sqlite3.Connection,
    observed: str,
    origin: str,
    dest: str,
    kind: str,
    score: float,
    source: str,
    confidence: float,
    metadata: dict,
) -> None:
    if not route_allowed(origin, dest):
        return
    db.execute(OBSERVATION_SQL, (
        observed,
        origin,
        dest,
        kind,
        score,
        None,
        score,
        source,
        confidence,
        json.dumps(metadata, ensure_ascii=False),
    ))


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def baseline_score_from_volume(avg_monthly_searches: float) -> float:
    if avg_monthly_searches <= 0:
        return 0.0
    return round(
        clamp(20.0 * math.log10(avg_monthly_searches / 10.0)),
        1,
    )


def momentum_score_from_monthly(values: list[float]) -> tuple[float | None, dict]:
    vals = [
        float(v)
        for v in values
        if v is not None and float(v) >= 0
    ]

    if len(vals) < 4:
        return None, {"note": "Not enough monthly Google Ads history"}

    latest = vals[-1]
    historical = vals[:-1]
    med = statistics.median(historical)

    if med <= 0:
        return None, {"note": "Historical median is zero"}

    ratio = latest / med
    score = clamp(50.0 + 40.0 * math.log(ratio, 2))

    return round(score, 1), {
        "latest_month_searches": latest,
        "historical_median": med,
        "ratio": round(ratio, 3),
    }


def keyword_set(origin: str, destination: str) -> list[str]:
    names = DESTINATION_NAMES.get(origin, DESTINATION_NAMES["EN"])
    dname = names.get(destination, destination)

    templates = TRAVEL_TEMPLATES.get(
        origin,
        ["travel {d}", "flights {d}", "holiday {d}"],
    )

    return [template.format(d=dname) for template in templates]


def store_google_ads_monthly(
    db: sqlite3.Connection,
    observed: str,
    origin: str,
    destination: str,
    keyword: str,
    year: int,
    month: int,
    monthly_searches: float,
    avg_monthly_searches: float,
) -> None:
    if not route_allowed(origin, destination):
        return
    db.execute("""
    INSERT INTO google_ads_keywords(
        observed_date, origin_iso2, destination_iso2, keyword,
        year, month, monthly_searches, avg_monthly_searches, source
    )
    VALUES(?,?,?,?,?,?,?,?,?)
    ON CONFLICT(
        observed_date, origin_iso2, destination_iso2,
        keyword, year, month, source
    )
    DO UPDATE SET
        monthly_searches=excluded.monthly_searches,
        avg_monthly_searches=excluded.avg_monthly_searches
    """, (
        observed, origin, destination, keyword,
        year, month, monthly_searches, avg_monthly_searches,
        "Google Ads Keyword Planner",
    ))


def collect_google_ads(
    db: sqlite3.Connection,
    observed: str,
    origins: list[str],
    destinations: list[str],
    customer_id: str,
    config_path: str | None,
) -> dict[tuple[str, str], float]:
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        print("GOOGLE ADS SKIP: install with: pip install google-ads")
        return {}

    try:
        if config_path:
            client = GoogleAdsClient.load_from_storage(config_path)
        else:
            client = GoogleAdsClient.load_from_storage()
    except Exception as exc:
        print(f"GOOGLE ADS SKIP: could not load google-ads.yaml ({exc})")
        return {}

    service = client.get_service("KeywordPlanIdeaService")
    googleads_service = client.get_service("GoogleAdsService")
    output: dict[tuple[str, str], float] = {}

    for origin in origins:
        cfg = GOOGLE_ADS_ORIGINS.get(origin)
        if not cfg:
            print(
                f"GOOGLE ADS {origin}: unsupported origin in V1.2; skipping"
            )
            continue

        keywords: list[str] = []
        keyword_to_dest: dict[str, str] = {}

        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            for keyword in keyword_set(origin, dest):
                keywords.append(keyword)
                keyword_to_dest[keyword.lower()] = dest

        try:
            request = client.get_type(
                "GenerateKeywordHistoricalMetricsRequest"
            )
            request.customer_id = customer_id.replace("-", "")
            request.keywords.extend(keywords)
            request.geo_target_constants.append(
                googleads_service.geo_target_constant_path(
                    cfg["geo_id"]
                )
            )
            request.keyword_plan_network = (
                client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
            )
            request.language = (
                googleads_service.language_constant_path(
                    cfg["language_id"]
                )
            )

            response = service.generate_keyword_historical_metrics(
                request=request
            )
        except Exception as exc:
            print(f"GOOGLE ADS ERR {origin}: {exc}")
            continue

        average_by_dest = {dest: 0.0 for dest in destinations}
        months_by_dest: dict[str, dict[tuple[int, int], float]] = {
            dest: {} for dest in destinations
        }

        matched = 0

        for result in response.results:
            text_key = str(result.text or "").lower()
            dest = keyword_to_dest.get(text_key)

            if not dest:
                for candidate in destinations:
                    name = DESTINATION_NAMES.get(
                        origin,
                        DESTINATION_NAMES["EN"],
                    ).get(candidate, "").lower()
                    if name and name in text_key:
                        dest = candidate
                        break

            if not dest:
                continue

            metrics = result.keyword_metrics
            average_by_dest[dest] += float(
                metrics.avg_monthly_searches or 0
            )

            for item in metrics.monthly_search_volumes:
                month_number = int(item.month)
                year_number = int(item.year)
                monthly_value = float(item.monthly_searches or 0)
                key = (year_number, month_number)
                months_by_dest[dest][key] = (
                    months_by_dest[dest].get(key, 0.0)
                    + monthly_value
                )

                store_google_ads_monthly(
                    db,
                    observed,
                    origin,
                    dest,
                    str(result.text or ""),
                    year_number,
                    month_number,
                    monthly_value,
                    float(metrics.avg_monthly_searches or 0),
                )

            matched += 1

        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            average = average_by_dest[dest]
            baseline = baseline_score_from_volume(average)

            upsert_observation(
                db,
                observed,
                origin,
                dest,
                "baseline_search",
                baseline,
                "Google Ads Keyword Planner",
                0.90,
                {
                    "avg_monthly_searches": average,
                    "keywords": keyword_set(origin, dest),
                    "interpretation": (
                        "Structural travel-search demand; normalized on a "
                        "logarithmic scale and not a market-share estimate"
                    ),
                },
            )

            ordered_months = [
                value
                for _, value in sorted(
                    months_by_dest[dest].items()
                )
            ]

            ads_momentum, meta = momentum_score_from_monthly(
                ordered_months
            )

            if ads_momentum is not None:
                upsert_observation(
                    db,
                    observed,
                    origin,
                    dest,
                    "google_ads_momentum",
                    ads_momentum,
                    "Google Ads Keyword Planner",
                    0.80,
                    meta,
                )
                output[(origin, dest)] = ads_momentum

        print(
            f"GOOGLE ADS {origin}: "
            f"{matched} keyword rows matched"
        )
        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            avg = average_by_dest[dest]
            if avg > 0:
                print(
                    f"  {origin}->{dest}: baseline "
                    f"{baseline_score_from_volume(avg):.1f} "
                    f"({avg:,.0f} avg monthly searches)"
                )

    return output


@lru_cache(maxsize=200000)
def trend_matches_destination(term: str, destination: str) -> bool:
    normalized = " ".join(
        str(term or "").lower().replace("–", " ").replace("-", " ").split()
    )

    aliases = list(TREND_ALIASES.get(destination, []))
    aliases.append(DESTINATION_NAMES.get("EN", {}).get(destination, destination).lower())
    aliases.append(destination.lower())
    return any(alias.lower() in normalized for alias in aliases)


def store_trend_row(
    db: sqlite3.Connection,
    row,
    captured_at: str,
) -> None:
    db.execute("""
    INSERT INTO google_trends_terms(
        refresh_date, origin_iso2, term, kind, rank,
        dataset_score, week, captured_at
    )
    VALUES(?,?,?,?,?,?,?,?)
    ON CONFLICT(refresh_date, origin_iso2, term, kind)
    DO UPDATE SET
        rank=excluded.rank,
        dataset_score=excluded.dataset_score,
        week=excluded.week,
        captured_at=excluded.captured_at
    """, (
        str(row.refresh_date),
        str(row.country_code),
        str(row.term),
        str(row.kind),
        int(row.rank) if row.rank is not None else None,
        float(row.score) if row.score is not None else None,
        str(row.week) if getattr(row, "week", None) is not None else "",
        captured_at,
    ))


def collect_google_trends_history(
    db: sqlite3.Connection,
    observed: str,
    origins: list[str],
    destinations: list[str],
    project_id: str,
    history_days: int = 30,
    refresh: bool = False,
    offline: bool = False,
) -> dict[tuple[str, str], float]:
    """Fetch and store daily Top/Rising terms for the recent history window."""
    min_date = date.fromisoformat(observed) - timedelta(days=history_days - 1)
    relogin_attempted = False

    def needs_adc_login(exc: Exception) -> bool:
        message = str(exc).lower()
        auth_markers = (
            "invalid_grant", "reauthentication", "invalid credentials",
            "application default credentials", "refresherror",
            "insufficient authentication scopes", "access_token_scope_insufficient",
        )
        if any(marker in message for marker in auth_markers):
            return True
        try:
            from google.api_core.exceptions import Unauthenticated
            if isinstance(exc, Unauthenticated):
                return True
        except ImportError:
            pass
        try:
            from google.auth.exceptions import RefreshError
            if isinstance(exc, RefreshError):
                return True
        except ImportError:
            pass
        return False

    def refresh_adc_interactively() -> bool:
        """Launch Google's supported ADC login only after an auth failure."""
        gcloud = shutil.which("gcloud")
        if not gcloud:
            print(
                "GOOGLE TRENDS AUTH: sign-in is required, but gcloud was not found. "
                "Install Google Cloud CLI, then run: gcloud auth application-default "
                "login --scopes=https://www.googleapis.com/auth/cloud-platform",
                flush=True,
            )
            return False
        print(
            "GOOGLE TRENDS AUTH: Google requires sign-in. Opening the standard "
            "Google Cloud CLI sign-in; complete it in the browser window.",
            flush=True,
        )
        command = [
            gcloud, "auth", "application-default", "login",
            "--scopes=https://www.googleapis.com/auth/cloud-platform",
        ]
        try:
            options = {"check": False}
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            else:
                options["stdin"] = None
            result = subprocess.run(command, **options)
        except OSError as exc:
            print(f"GOOGLE TRENDS AUTH ERROR: could not start gcloud ({exc}).", flush=True)
            return False
        if result.returncode:
            print(
                "GOOGLE TRENDS AUTH ERROR: sign-in did not complete. "
                "No Trends refresh was recorded as successful.",
                flush=True,
            )
            return False
        quota = subprocess.run(
            [gcloud, "auth", "application-default", "set-quota-project", project_id],
            check=False,
            **({"creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0)} if os.name == "nt" else {}),
        )
        if quota.returncode:
            print(
                "GOOGLE TRENDS AUTH: sign-in completed, but the quota project "
                f"could not be set to {project_id}. Grant serviceusage.services.use "
                "on that project, then run gcloud auth application-default "
                f"set-quota-project {project_id} and retry.",
                flush=True,
            )
            return False
        return True

    def query(lo, hi, members):
        from google.cloud import bigquery
        nonlocal relogin_attempted
        client = bigquery.Client(project=project_id)
        origin_sql = ",".join("'"+o+"'" for o in members if len(o)==2 and o.isalpha())
        sql = f"""
        WITH top_terms AS (
          SELECT DISTINCT
            refresh_date,
            country_code,
            term,
            score,
            rank,
            CAST(NULL AS STRING) AS week,
            'top' AS kind
          FROM `bigquery-public-data.google_trends.international_top_terms`
          WHERE refresh_date BETWEEN DATE('{lo}')
                                  AND DATE('{hi}')
            AND country_code IN ({origin_sql})
        ),
        rising_terms AS (
          SELECT DISTINCT
            refresh_date,
            country_code,
            term,
            score,
            rank,
            CAST(NULL AS STRING) AS week,
            'rising' AS kind
          FROM `bigquery-public-data.google_trends.international_top_rising_terms`
          WHERE refresh_date BETWEEN DATE('{lo}')
                                  AND DATE('{hi}')
            AND country_code IN ({origin_sql})
        )
        SELECT * FROM top_terms
        UNION ALL
        SELECT * FROM rising_terms
        ORDER BY refresh_date DESC, country_code, kind, rank
        """
    
        try:
            return list(client.query(sql).result())
        except Exception as exc:
            if not needs_adc_login(exc):
                raise
            if relogin_attempted:
                print(
                    "GOOGLE TRENDS AUTH ERROR: authentication still fails after "
                    "sign-in. Check the selected Google account, BigQuery access, "
                    "API enablement and quota-project permission.",
                    flush=True,
                )
                raise
            relogin_attempted = True
            if not refresh_adc_interactively():
                raise
            # The client created before the login still holds the old credentials.
            client = bigquery.Client(project=project_id)
            return list(client.query(sql).result())
    rows = cached_trends(db, observed, origins, history_days, query,
                         force=refresh, offline=offline)
    rows_by_origin = defaultdict(list)
    for row in rows:
        rows_by_origin[str(row.country_code)].append(row)

    output: dict[tuple[str, str], float] = {}

    for origin in origins:
        by_term = defaultdict(list)
        for row in rows_by_origin.get(origin, []):
            by_term[str(row.term)].append(row)
        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            rows_match = [row for term, term_rows in by_term.items()
                          if trend_matches_destination(term, dest) for row in term_rows]

            day_scores: dict[str, float] = {}
            terms_by_day: defaultdict[str, set[str]] = defaultdict(set)

            aliases = TREND_ALIASES.get(dest, [dest.lower()])

            for trend_row in rows_match:
                refresh_date = str(trend_row.refresh_date)
                kind = str(trend_row.kind)
                term = str(trend_row.term)
                rank = trend_row.rank

                if not trend_matches_destination(term, dest):
                    continue

                day = str(refresh_date)
                days_old = (
                    date.fromisoformat(observed) - date.fromisoformat(day)
                ).days

                recency = math.exp(-days_old / 10.0)

                rank_value = max(
                    0.0,
                    1.0 - ((int(rank or 25) - 1) / 25.0),
                )

                type_multiplier = 1.20 if str(kind) == "rising" else 0.85
                contribution = recency * rank_value * type_multiplier

                day_scores[day] = day_scores.get(day, 0.0) + contribution
                terms_by_day[day].add(str(term))

            if not day_scores:
                continue

            # ---- Transparent components ---------------------------------
            # Presence: how many of the last 30 days had at least one
            # destination-related Top/Rising term.
            active_days = len(day_scores)
            presence = min(1.0, active_days / 12.0)

            # Activity: average daily strength, where rank 1 is strongest.
            # Rising terms get a modest uplift because "rising" is more
            # indicative of momentum than merely being a popular term.
            daily_values = list(day_scores.values())
            activity = min(
                1.0,
                (sum(min(1.5, value) for value in daily_values) / max(1, active_days)) / 1.15,
            )

            # Recent vs prior activity. We need at least one observation on
            # both sides. No history on the prior side means we do not call
            # it an acceleration; we only score current activity.
            recent = []
            prior = []

            for day, value in day_scores.items():
                age = (
                    date.fromisoformat(observed)
                    - date.fromisoformat(day)
                ).days
                if age <= 6:
                    recent.append(value)
                elif 7 <= age <= 29:
                    prior.append(value)

            recent_avg = sum(recent) / len(recent) if recent else 0.0
            prior_avg = sum(prior) / len(prior) if prior else 0.0

            if prior_avg > 0:
                acceleration_ratio = recent_avg / prior_avg
                # Cap the influence of one sudden spike. We care about a
                # sustained change, not a single viral day.
                accel_component = clamp(
                    50.0 + 25.0 * math.log(
                        max(0.25, min(4.0, acceleration_ratio)),
                        2,
                    )
                )
            elif recent_avg > 0:
                acceleration_ratio = None
                accel_component = 65.0
            else:
                acceleration_ratio = None
                accel_component = 50.0

            # Convert the three transparent ingredients into a conservative
            # 0-100 momentum score.
            score = round(
                clamp(
                    100.0 * (
                        0.45 * presence
                        + 0.35 * activity
                        + 0.20 * (accel_component / 100.0)
                    )
                ),
                1,
            )

            matched_terms = sorted({
                term
                for terms in terms_by_day.values()
                for term in terms
            })

            best_rank = None
            best_kind = None
            best_term = None
            best_value = -1.0

            for trend_row in rows_match:
                refresh_date = str(trend_row.refresh_date)
                if refresh_date not in day_scores:
                    continue
                kind = str(trend_row.kind)
                rank = trend_row.rank
                term = str(trend_row.term)
                if not trend_matches_destination(term, dest):
                    continue
                rank_num = int(rank or 25)
                type_multiplier = 1.20 if kind == "rising" else 0.85
                value = (1.0 - ((rank_num - 1) / 25.0)) * type_multiplier
                if value > best_value:
                    best_value = value
                    best_rank = rank_num
                    best_kind = kind
                    best_term = term

            meta = {
                "history_days": history_days,
                "active_days": active_days,
                "active_day_share": round(active_days / max(1, history_days), 3),
                "presence_score": round(presence * 100.0, 1),
                "activity_score": round(activity * 100.0, 1),
                "acceleration_score": round(accel_component, 1),
                "recent_avg": round(recent_avg, 3),
                "prior_avg": round(prior_avg, 3),
                "acceleration_ratio": (
                    round(acceleration_ratio, 3)
                    if acceleration_ratio is not None
                    else None
                ),
                "best_rank": best_rank,
                "best_kind": best_kind,
                "best_term": best_term,
                "matched_terms": matched_terms[:25],
                "days_with_matches": sorted(day_scores),
            }

            upsert_observation(
                db,
                observed,
                origin,
                dest,
                "search_momentum",
                score,
                "Google Trends BigQuery",
                0.85,
                meta,
            )

            output[(origin, dest)] = score

    print(
        f"GOOGLE TRENDS: calculated momentum for "
        f"{len(output)} origin/destination corridors"
    )
    for (origin, dest), score in sorted(output.items()):
        row = db.execute("""
            SELECT metadata_json
            FROM demand_observations
            WHERE observed_date=? AND origin_iso2=? AND destination_iso2=?
              AND signal_type='search_momentum'
            ORDER BY source DESC
            LIMIT 1
        """, (observed, origin, dest)).fetchone()
        meta = json.loads(row[0]) if row and row[0] else {}
        print(
            f"  {origin}->{dest}: {score:.1f} | "
            f"active_days {meta.get('active_days', 0)}/{meta.get('history_days', history_days)} | "
            f"presence {meta.get('presence_score', 0):.1f} | "
            f"activity {meta.get('activity_score', 0):.1f} | "
            f"accel {meta.get('acceleration_score', 0):.1f} | "
            f"best {meta.get('best_kind', 'N/A')}#{meta.get('best_rank', 'N/A')}"
        )

    return output


def store_combined_momentum(
    db: sqlite3.Connection,
    observed: str,
    origins: list[str],
    destinations: list[str],
    trends: dict[tuple[str, str], float],
    ads: dict[tuple[str, str], float],
) -> None:
    for origin in origins:
        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            trend_score = trends.get((origin, dest))
            ads_score = ads.get((origin, dest))

            if trend_score is None and ads_score is None:
                continue

            if trend_score is not None and ads_score is not None:
                score = round(
                    0.70 * trend_score + 0.30 * ads_score,
                    1,
                )
                source = "Google Trends + Google Ads"
                confidence = 0.90
            elif trend_score is not None:
                continue
            else:
                score = ads_score
                source = "Google Ads Keyword Planner"
                confidence = 0.75

            upsert_observation(
                db,
                observed,
                origin,
                dest,
                "search_momentum",
                score,
                source,
                confidence,
                {
                    "google_trends": trend_score,
                    "google_ads": ads_score,
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect T-Travel demand signals"
    )

    parser.add_argument("--database", default=str(Path(__file__).resolve().parent / "events.db"))
    parser.add_argument(
        "--input-dir",
        default=INPUT_DIR,
        help="Folder containing origins.csv, aliases, templates and seasonality inputs",
    )
    parser.add_argument(
        "--portfolio-file",
        default=PORTFOLIO_FILE,
        help="Portfolio CSV containing ISO_Code_A2 and country columns",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Small DE/FR x JP/TR/ES/MA/US sample",
    )
    parser.add_argument(
        "--origins",
        default=None,
        help="Comma-separated ISO-2 origins. Default: origins.csv",
    )
    parser.add_argument(
        "--all-origins",
        action="store_true",
        help=(
            "Use every valid origin in origins.csv, not only enabled rows. "
            "Useful when some rows are disabled."
        ),
    )
    parser.add_argument(
        "--destinations",
        default=None,
        help=(
            "Comma-separated ISO-2 destinations. "
            "Default: all destinations from the portfolio CSV."
        ),
    )
    parser.add_argument(
        "--date",
        help="Observation date YYYY-MM-DD (default today)",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Disable external network calls; reuse complete cached Trends history",
    )
    parser.add_argument(
        "--refresh-holidays",
        action="store_true",
        help="Refetch cached Nager.Date holiday snapshots",
    )
    parser.add_argument("--refresh-trends", action="store_true", help="Requery the full requested Trends window")
    parser.add_argument("--timeout", type=int, default=20)

    parser.add_argument(
        "--google-trends-project", default="t-travel-intelligence",
        help="Google Cloud project ID for Trends BigQuery dataset",
    )
    parser.add_argument("--google-trends-history-days",
        type=int,
        default=30,
        help="Number of recent days of Trends history to collect",
    )
    parser.add_argument(
        "--google-ads-customer-id",
        help="Google Ads customer ID for Keyword Planner metrics",
    )
    parser.add_argument(
        "--google-ads-config",
        help="Path to local google-ads.yaml",
    )
    parser.add_argument(
        "--skip-google-trends",
        action="store_true",
    )
    parser.add_argument(
        "--skip-google-ads",
        action="store_true",
    )

    args = parser.parse_args()
    configure_input_dir(args.input_dir)

    as_of = (
        date.fromisoformat(args.date)
        if args.date
        else date.today()
    )

    config_origins, config_destinations = load_input_files(
        args.input_dir,
        args.portfolio_file,
        args.all_origins,
    )

    origins = (
        TEST_ORIGINS
        if args.test
        else [
            item.strip().upper()
            for item in args.origins.split(",")
            if item.strip()
        ] if args.origins else config_origins
    )

    if args.test:
        destinations = TEST_DESTINATIONS
    elif args.destinations:
        destinations = [
            item.strip().upper()
            for item in args.destinations.split(",")
            if item.strip()
        ]
    else:
        destinations = config_destinations
        print(
            f"PORTFOLIO: loaded {len(destinations)} destinations from "
            f"{args.portfolio_file}"
        )

    origins = [o for o in origins if origin_allowed(o, args.input_dir)]
    print("Route policy:", ROUTE_POLICY_VERSION)
    db = sqlite3.connect(args.database)
    ensure_schema(db)
    ensure_cache_schema(db)
    db.execute("PRAGMA busy_timeout=30000")

    print(f"T-Travel Demand Collector {VERSION}")
    print(
        "Date:",
        as_of,
        "| origins:",
        origins,
        "| destinations:",
        destinations,
    )

    refresh_holiday_calendars(
        db, origins, as_of, args.timeout,
        force=args.refresh_holidays, offline=args.no_network,
    )

    base_rows = []
    observed = as_of.isoformat()
    for origin in origins:
        hscore, hmeta = holiday_score(db, origin, as_of)
        for dest in destinations:
            if not route_allowed(origin, dest):
                continue
            sscore, note = seasonality_score(dest, as_of.month)
            base_rows.append((observed, origin, dest, "holiday", hscore, None, hscore,
                "Nager.Date public holidays", 0.60 if hscore is not None else 0.0, json.dumps(hmeta, ensure_ascii=False)))
            base_rows.append((observed, origin, dest, "seasonality", sscore, None, sscore,
                "V1 heuristic", 0.35, json.dumps({"note":note,"month":as_of.month},ensure_ascii=False)))
    with db:
        db.executemany(OBSERVATION_SQL, base_rows)
    written = len(base_rows)

    trends_scores: dict[tuple[str, str], float] = {}
    ads_scores: dict[tuple[str, str], float] = {}

    if not args.skip_google_trends:
        if args.google_trends_project:
            trends_scores = collect_google_trends_history(
                db,
                as_of.isoformat(),
                origins,
                destinations,
                args.google_trends_project,
                max(1, args.google_trends_history_days),
                refresh=args.refresh_trends, offline=args.no_network,
            )
        else:
            print(
                "GOOGLE TRENDS SKIP: pass "
                "--google-trends-project PROJECT_ID to enable"
            )

    if not args.skip_google_ads and not args.no_network:
        if args.google_ads_customer_id:
            ads_scores = collect_google_ads(
                db,
                as_of.isoformat(),
                origins,
                destinations,
                args.google_ads_customer_id,
                args.google_ads_config,
            )
        else:
            print(
                "GOOGLE ADS SKIP: pass "
                "--google-ads-customer-id CUSTOMER_ID to enable"
            )

    store_combined_momentum(
        db,
        as_of.isoformat(),
        origins,
        destinations,
        trends_scores,
        ads_scores,
    )

    db.commit()

    count = db.execute(
        "SELECT COUNT(*) FROM demand_observations"
    ).fetchone()[0]

    trend_count = db.execute(
        "SELECT COUNT(*) FROM google_trends_terms"
    ).fetchone()[0]

    db.close()

    print(
        f"Wrote/refreshed {written} base observations; "
        f"demand_observations now has {count} rows."
    )
    print(
        f"google_trends_terms now has {trend_count} raw observations."
    )
    print(
        "Google signals are stored only when official Google "
        "sources are configured; otherwise they remain N/A."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
