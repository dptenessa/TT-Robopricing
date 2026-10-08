#!/usr/bin/env python3
"""Build transparent origin->destination opportunity scores from events.db."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

from event_matching import classify, destination_matches, origin_matches, overlaps, parse_date, scoreable

VERSION = "1.7-distance-factor"
SALES_LOOKBACK_DAYS = 60
MIN_ORIGIN_MARKET_WEIGHT = 50.0
SALES_COUNTRY_ALIASES = {}

try:
    from config import SALES_FILE
except (ImportError, AttributeError):
    SALES_FILE = None

WEIGHTS = {
    "baseline_search": 0.18,
    "search_momentum": 0.18,
    "observed_sales": 0.25,
    "seasonality": 0.10,
    "events": 0.15,
    "holiday": 0.09,
    "distance": 0.05,
}

# Approximate country-centre coordinates.  Distance is deliberately a small
# model factor, not a forecast of demand. Countries without a coordinate have
# a neutral distance score and are reported as such in components_json.
COUNTRY_COORDS = {
    "DE": (51.2, 10.5), "FR": (46.5, 2.5), "NL": (52.2, 5.3),
    "GB": (54.5, -2.5), "IE": (53.4, -8.0), "IT": (42.8, 12.5),
    "ES": (40.2, -3.7), "PT": (39.5, -8.0), "BE": (50.8, 4.5),
    "LU": (49.8, 6.1), "CH": (46.8, 8.2), "AT": (47.6, 14.1),
    "HR": (45.1, 15.2), "SI": (46.1, 14.9), "GR": (39.1, 22.9),
    "AL": (41.2, 20.0), "ME": (42.7, 19.3), "BA": (44.2, 17.7),
    "RS": (44.0, 20.8), "MK": (41.6, 21.7), "BG": (42.7, 25.5),
    "RO": (45.9, 24.9), "HU": (47.2, 19.5), "CZ": (49.8, 15.5),
    "SK": (48.7, 19.7), "PL": (52.1, 19.1), "UA": (49.0, 32.0),
    "IS": (64.9, -19.0), "TR": (39.0, 35.0), "CY": (35.1, 33.4),
    "MT": (35.9, 14.4), "US": (39.8, -98.6), "CA": (56.1, -106.3),
    "MX": (23.6, -102.6), "BR": (-10.8, -51.9), "AR": (-38.4, -63.6),
    "CL": (-35.7, -71.5), "PE": (-9.2, -75.0), "CO": (4.6, -74.1),
    "EC": (-1.4, -78.4), "BO": (-16.3, -63.6), "UY": (-32.5, -55.8),
    "CR": (9.9, -84.2), "PA": (8.5, -80.8), "JP": (36.2, 138.3),
    "KR": (36.5, 127.9), "CN": (35.9, 104.2), "TW": (23.7, 121.0),
    "HK": (22.3, 114.2), "MO": (22.2, 113.5), "TH": (15.9, 100.9),
    "VN": (14.1, 108.3), "KH": (12.6, 104.9), "LA": (19.9, 102.5),
    "MY": (4.2, 101.9), "SG": (1.35, 103.8), "ID": (-2.5, 118.0),
    "PH": (12.9, 122.0), "IN": (22.0, 79.0), "LK": (7.9, 80.8),
    "NP": (28.4, 84.1), "AU": (-25.3, 133.8), "NZ": (-41.8, 171.2),
    "FJ": (-17.7, 178.1), "AE": (24.3, 54.4), "SA": (23.9, 45.1),
    "QA": (25.3, 51.2), "OM": (21.5, 55.9), "IL": (31.0, 34.9),
    "JO": (31.2, 36.5), "EG": (26.8, 30.8), "MA": (31.8, -7.1),
    "TN": (34.0, 9.0), "DZ": (28.0, 1.7), "ZA": (-30.6, 22.9),
    "NG": (9.1, 8.7), "GH": (7.9, -1.0), "KE": (0.2, 37.9),
    "TZ": (-6.3, 34.9), "ET": (9.1, 40.5), "MU": (-20.3, 57.6),
}

def corridor_distance(origin, destination):
    """Return approximate country-centre distance in km, or None."""
    a = COUNTRY_COORDS.get(str(origin).upper())
    b = COUNTRY_COORDS.get(str(destination).upper())
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return round(6371.0 * 2 * math.asin(math.sqrt(h)))

def distance_score(distance_km):
    """Convert distance to a modest travel-friction score (shorter = higher)."""
    if distance_km is None:
        return None
    return round(max(10.0, 100.0 - (float(distance_km) / 15000.0) * 90.0), 1)

COUNTRY_ALIASES = {
    "JP": ["japan", "tokyo", "osaka", "kyoto"],
    "TR": ["turkey", "türkiye", "turkiye", "istanbul", "antalya"],
    "ES": ["spain", "madrid", "barcelona", "mallorca", "ibiza"],
    "MA": ["morocco", "marrakech", "casablanca"],
    "US": ["united states", "usa", "u.s.", "new york", "miami", "los angeles", "las vegas"],
    "MX": ["mexico", "cancun", "mexico city"],
    "CA": ["canada", "toronto", "vancouver", "montreal"],
    "TH": ["thailand", "bangkok", "phuket"],
    "AE": ["united arab emirates", "uae", "dubai", "abu dhabi"],
    "EG": ["egypt", "cairo", "hurghada", "sharm"],
    "CH": ["switzerland", "swiss", "geneva", "zurich", "basel", "bern", "lucerne"],
    "AR": ["argentina", "buenos aires", "patagonia"],
    "CL": ["chile", "santiago", "atacama"],
    "PE": ["peru", "lima", "cusco", "machu picchu"],
    "IS": ["iceland", "reykjavik"],
    "AL": ["albania", "tirana", "albanian"],
    "PT": ["portugal", "lisbon", "porto", "madeira"],
    "IT": ["italy", "rome", "milan", "venice", "sicily"],
    "FR": ["france", "paris", "nice", "cannes"],
    "GB": ["united kingdom", "england", "london", "scotland", "wales"],
    "DE": ["germany", "berlin", "munich", "frankfurt"],
    "AT": ["austria", "vienna", "salzburg", "innsbruck"],
    "NL": ["netherlands", "amsterdam", "rotterdam"],
    "BE": ["belgium", "brussels", "antwerp"],
    "HR": ["croatia", "split", "dubrovnik", "zagreb", "hvar"],
    "GR": ["greece", "athens", "crete", "rhodes", "santorini"],
    "JP": ["japan", "tokyo", "osaka", "kyoto"],
}
ORIGIN_ALIASES = {
    "DE": ["germany", "german", "de"],
    "FR": ["france", "french", "fr"],
    "NL": ["netherlands", "dutch", "nl"],
    "GB": ["united kingdom", "uk", "british", "gb"],
    "IT": ["italy", "italian", "it"],
    "ES": ["spain", "spanish", "es"],
    "HR": ["croatia", "croatian", "hr"],
    "AT": ["austria", "austrian", "at"],
    "CH": ["switzerland", "swiss", "ch"],
}

def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def columns(db, table):
    return {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}

def ensure_schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS opportunity_scores (
      score_date TEXT NOT NULL,
      origin_iso2 TEXT NOT NULL,
      destination_iso2 TEXT NOT NULL,
      baseline_search_score REAL,
      search_momentum_score REAL,
      observed_sales_score REAL,
      observed_sales_units REAL,
      observed_sales_revenue REAL,
      seasonality_score REAL,
      events_score REAL,
      holiday_score REAL,
      distance_km REAL,
      distance_score REAL,
      opportunity_score REAL,
      data_coverage REAL,
      change_7d REAL,
      change_30d REAL,
      status TEXT,
      components_json TEXT,
      PRIMARY KEY(score_date, origin_iso2, destination_iso2)
    );
    CREATE INDEX IF NOT EXISTS idx_opp_corridor_date
      ON opportunity_scores(origin_iso2, destination_iso2, score_date);
    """)
    cols = columns(db, "opportunity_scores")
    if "data_coverage" not in cols:
        db.execute('ALTER TABLE opportunity_scores ADD COLUMN data_coverage REAL')
    for column, sql_type in (
        ("observed_sales_score", "REAL"),
        ("observed_sales_units", "REAL"),
        ("observed_sales_revenue", "REAL"),
        ("distance_km", "REAL"),
        ("distance_score", "REAL"),
    ):
        if column not in cols:
            db.execute(f'ALTER TABLE opportunity_scores ADD COLUMN {column} {sql_type}')


def upsert_observation(db, observed, origin, dest, signal_type, score,
                       source, confidence, metadata):
    """Store one dated demand signal without creating duplicate rows."""
    db.execute("""
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
        )
    """)
    db.execute("""
        INSERT INTO demand_observations(
            observed_date, origin_iso2, destination_iso2, signal_type,
            signal_value, baseline_value, normalized_score, source,
            confidence, metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(observed_date, origin_iso2, destination_iso2, signal_type, source)
        DO UPDATE SET
            signal_value=excluded.signal_value,
            baseline_value=excluded.baseline_value,
            normalized_score=excluded.normalized_score,
            confidence=excluded.confidence,
            metadata_json=excluded.metadata_json
    """, (
        observed, origin, dest, signal_type, score, None, score, source,
        confidence, json.dumps(metadata, ensure_ascii=False),
    ))


def parse_sales_date(value):
    """Parse the common date formats used in the sales export."""
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def sales_country_code(value):
    """Resolve a country name or ISO code to ISO-2 without hard-coding the portfolio."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    normalized = unicodedata.normalize("NFKD", raw)
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    manual = {
        "united states": "US", "usa": "US", "united kingdom": "GB",
        "uk": "GB", "great britain": "GB", "south korea": "KR",
        "czech republic": "CZ", "czechia": "CZ", "uae": "AE",
    }
    if normalized in manual:
        return manual[normalized]
    if normalized in SALES_COUNTRY_ALIASES:
        return SALES_COUNTRY_ALIASES[normalized]
    try:
        import pycountry
        country = pycountry.countries.lookup(raw)
        return country.alpha_2.upper()
    except (ImportError, LookupError):
        return None


def load_sales_country_aliases():
    """Load project-owned country names/aliases for sales CSV normalization."""
    global SALES_COUNTRY_ALIASES
    folders = [Path(__file__).resolve().parent / "inputs" / "travel_demand"]
    try:
        from config import BASE_DIR
        folders.insert(0, Path(BASE_DIR) / "inputs" / "travel_demand")
    except (ImportError, AttributeError):
        pass

    aliases = {}
    for folder in folders:
        for filename, code_key, name_key, alias_key in (
            ("origins.csv", "origin_iso2", "country_name", "aliases"),
            ("destination_aliases.csv", "destination_iso2", "country_name", "aliases"),
        ):
            path = folder / filename
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        code = str(row.get(code_key) or "").strip().upper()
                        values = [row.get(name_key, ""), row.get(alias_key, "")]
                        if not code:
                            continue
                        for value in values:
                            for item in str(value or "").split("|"):
                                normalized = unicodedata.normalize("NFKD", item)
                                normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
                                normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
                                if normalized:
                                    aliases[normalized] = code
            except OSError:
                continue
    SALES_COUNTRY_ALIASES = aliases


def load_sales_scores(path, as_of, lookback_days=SALES_LOOKBACK_DAYS):
    """Return recent observed-sales scores by origin and destination.

    Sales are an additional first-party signal alongside Google Ads. They
    describe proven T-Travel demand, not total market size.
    """
    load_sales_country_aliases()
    if not path:
        print("SALES SKIP: SALES_FILE is not configured")
        return {}
    path = Path(path)
    if not path.exists():
        print(f"SALES SKIP: file not found: {path}")
        return {}

    start = as_of - timedelta(days=lookback_days - 1)
    totals = {}
    rows_seen = 0
    rows_used = 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows_seen += 1
                transaction_type = str(row.get("TransactionType") or "").strip().upper()
                status = str(row.get("DocumentStatus") or "").strip().casefold()
                if transaction_type != "SALE" or any(x in status for x in ("cancel", "refund", "void")):
                    continue
                transaction_date = parse_sales_date(
                    row.get("TransactionDate") or row.get("TransactionTimestamp")
                )
                if not transaction_date or not (start <= transaction_date <= as_of):
                    continue
                origin = sales_country_code(row.get("BuyerCountry"))
                destination = sales_country_code(row.get("Destination"))
                if not origin or not destination:
                    continue
                try:
                    quantity = float(str(row.get("Quantity") or "1").replace(",", "."))
                except ValueError:
                    quantity = 1.0
                try:
                    revenue = float(str(row.get("NetAmount") or "0").replace(",", "."))
                except ValueError:
                    revenue = 0.0
                key = (origin, destination)
                item = totals.setdefault(key, {"units": 0.0, "revenue": 0.0})
                item["units"] += max(0.0, quantity)
                item["revenue"] += max(0.0, revenue)
                rows_used += 1
    except OSError as exc:
        print(f"SALES SKIP: cannot read {path}: {exc}")
        return {}

    if not totals:
        print(f"SALES: 0 usable sales in the last {lookback_days} days ({rows_seen} rows read)")
        return {}

    max_units = max(item["units"] for item in totals.values())
    denominator = math.log1p(max(10.0, max_units))
    output = {}
    for key, item in totals.items():
        score = min(100.0, 20.0 + 80.0 * math.log1p(item["units"]) / denominator)
        output[key] = {
            "score": round(score, 1),
            "units": round(item["units"], 2),
            "revenue": round(item["revenue"], 2),
            "lookback_days": lookback_days,
        }
    print(
        f"SALES: {rows_used} completed sale rows used from {rows_seen} rows; "
        f"{len(output)} origin/destination corridors in the last {lookback_days} days"
    )
    return output


def load_origin_market_weights():
    """Load origin weights used to restrict campaign opportunity scoring."""
    candidates = [Path(__file__).resolve().parent / "inputs" / "travel_demand" / "origin_market_weights.csv"]
    try:
        from config import BASE_DIR
        candidates.insert(0, Path(BASE_DIR) / "inputs" / "travel_demand" / "origin_market_weights.csv")
    except (ImportError, AttributeError):
        pass

    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                weights = {}
                for row in csv.DictReader(handle):
                    code = str(row.get("origin_iso2") or row.get("iso2") or "").strip().upper()
                    raw = row.get("weight") or row.get("market_weight") or row.get("origin_weight")
                    value = num(raw)
                    if code and value is not None:
                        weights[code] = value
                if weights:
                    return weights
        except OSError:
            continue
    return {}

def latest_signal(db, as_of, origin, dest, signal_type):
    if not table_exists(db, "demand_observations"):
        return None
    row = db.execute("""
      SELECT normalized_score FROM demand_observations
      WHERE observed_date<=? AND origin_iso2=? AND destination_iso2=? AND signal_type=?
      ORDER BY observed_date DESC LIMIT 1
    """, (as_of, origin, dest, signal_type)).fetchone()
    return None if not row or row[0] is None else float(row[0])

def num(v):
    if v is None: return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None

def text_match(text, aliases):
    """Match complete words/phrases, never arbitrary substrings.

    This is important for ISO fallbacks: the old ``CH`` fallback matched
    the letters ``ch`` inside ``Chile`` and ``Michoacán``, falsely assigning
    Latin-American events to Switzerland.
    """
    def normalise(value):
        value = unicodedata.normalize("NFKD", str(value or ""))
        value = value.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    raw = str(text or "")
    t = normalise(raw)
    if not t:
        return False

    for alias in aliases:
        a = normalise(alias)
        if not a:
            continue
        # Accept ISO codes only when explicitly written as uppercase tokens.
        # This prevents common words such as "in" from matching a country.
        if len(a) == 2 and a.isalpha():
            code_pattern = r"(?<![A-Za-z0-9])" + re.escape(a.upper()) + r"(?![A-Za-z0-9])"
            if re.search(code_pattern, raw):
                return True
            continue
        # All aliases, including two-letter ISO codes, must be complete
        # tokens. Therefore CH does not match Chile or Michoacán.
        pattern = r"(?<![a-z0-9])" + re.escape(a).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, t):
            return True
    return False


def country_aliases(code, static_aliases):
    """Build safe aliases for any ISO-2 country in the portfolio."""
    code = str(code or "").strip().upper()
    aliases = list(static_aliases.get(code, []))
    aliases.append(code)
    try:
        import pycountry
        country = pycountry.countries.get(alpha_2=code)
        if country:
            for field in ("name", "official_name", "common_name"):
                value = getattr(country, field, None)
                if value:
                    aliases.append(value)
    except ImportError:
        pass
    return list(dict.fromkeys(aliases))


def parse_date(value):
    """Parse the ISO date prefix used by the event catalogue."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None

def event_score(db, origin, dest, as_of: date, horizon=90):
    if not table_exists(db, "events"):
        return None, []
    cols = columns(db, "events")
    needed = {"Start_Date"}
    if not needed.issubset(cols):
        return None, []

    wanted = [c for c in ["Occasion_Event","Country","Destination","Region","Start_Date","End_Date",
                           "Origin_Markets","Opportunity_Score","Priority","Confidence",
                           "Estimated_Addressable_Travellers","Estimated_Foreign_Visitors",
                           "Source_URL"] if c in cols]
    qcols = ",".join(f'"{c}"' for c in wanted)
    end = (as_of + timedelta(days=horizon)).isoformat()
    # Include events that started before the window but are still running.
    # An event overlaps the horizon when start <= horizon_end and end >= as_of.
    rows = db.execute(
        f'''SELECT {qcols} FROM events
            WHERE "Start_Date" IS NOT NULL
              AND TRIM("Start_Date") <> '' '''
    ).fetchall()
    idx = {c:i for i,c in enumerate(wanted)}
    aliases = country_aliases(dest, COUNTRY_ALIASES)
    oaliases = country_aliases(origin, ORIGIN_ALIASES)
    contributions = []

    for row in rows:
        event = {key: row[position] for key, position in idx.items()}
        if not destination_matches(event, dest):
            continue
        if not overlaps(event, as_of, horizon):
            continue
        # Generic travel patterns and multi-destination occasions remain
        # available to the dashboard, but do not inflate a specific corridor's
        # event score. They need country-level dates before being scoreable.
        if not scoreable(event):
            continue
        start = parse_date(event.get("Start_Date"))
        event_end = parse_date(event.get("End_Date")) or start
        days = max(0, (start-as_of).days)
        timing = max(0.20, 1.0 - days / float(horizon + 15))

        base = num(row[idx["Opportunity_Score"]]) if "Opportunity_Score" in idx else None
        if base is None:
            priority = str(row[idx["Priority"]] or "") if "Priority" in idx else ""
            base = {"Activate":80, "Prepare":60, "Monitor":35, "Exclude":0}.get(priority, 45)

        origin_factor = 0.80
        if "Origin_Markets" in idx and row[idx["Origin_Markets"]]:
            origin_factor = 1.0 if origin_matches(event, origin) else 0.65

        # Optional scale boost, capped and modest because RSS estimates may be noisy.
        travellers = None
        for c in ["Estimated_Addressable_Travellers","Estimated_Foreign_Visitors"]:
            if c in idx:
                travellers = num(row[idx[c]])
                if travellers is not None: break
        scale = 1.0
        if travellers and travellers > 0:
            scale = min(1.20, 0.85 + math.log10(max(10, travellers))/20.0)

        contribution = max(0, min(100, base * timing * origin_factor * scale))
        contributions.append({
            "event": str(row[idx["Occasion_Event"]] or "") if "Occasion_Event" in idx else "",
            "country": str(row[idx["Country"]] or "") if "Country" in idx else "",
            "destination": str(row[idx["Destination"]] or "") if "Destination" in idx else "",
            "start_date": start.isoformat(),
            "end_date": event_end.isoformat(),
            "source_url": str(row[idx["Source_URL"]] or "") if "Source_URL" in idx else "",
            "contribution": round(contribution,1),
        })

    contributions.sort(key=lambda x:x["contribution"], reverse=True)
    if not contributions:
        return 0.0, []

    # Conservative aggregation:
    # strongest event drives the score; additional events add only diminishing uplift.
    strongest = contributions[0]["contribution"]
    extras = contributions[1:]
    uplift = 0.0
    if len(extras) >= 1:
        uplift += min(8.0, extras[0]["contribution"] * 0.12)
    if len(extras) >= 2:
        uplift += min(5.0, extras[1]["contribution"] * 0.08)
    if len(extras) >= 3:
        uplift += min(3.0, max(e["contribution"] for e in extras[2:]) * 0.05)

    score = min(90.0, strongest + uplift)
    return round(score,1), contributions[:10]

def historical_change(db, origin, dest, as_of, days):
    target = (as_of - timedelta(days=days)).isoformat()
    row = db.execute("""
      SELECT opportunity_score FROM opportunity_scores
      WHERE origin_iso2=? AND destination_iso2=? AND score_date<=?
      ORDER BY score_date DESC LIMIT 1
    """, (origin,dest,target)).fetchone()
    return None if not row else float(row[0])

def weighted_score(parts):
    """Conservatively score missing signals as zero evidence."""
    if not any(value is not None for value in parts.values()):
        return None
    total_weight = sum(WEIGHTS.values())
    score = sum(
        WEIGHTS[key] * (float(value) if value is not None else 0.0)
        for key, value in parts.items()
    )
    return round(score / total_weight, 1)

def data_coverage(parts):
    """Weighted percentage of intended model currently backed by data."""
    present_weight = sum(WEIGHTS[k] for k,v in parts.items() if v is not None)
    return round(100.0 * present_weight / sum(WEIGHTS.values()), 1)

def status(score, c7):
    if score is None: return "NO DATA"
    trend = "ACCELERATING" if c7 is not None and c7 >= 5 else "COOLING" if c7 is not None and c7 <= -5 else "STABLE"
    level = "STRONG" if score >= 75 else "PROMISING" if score >= 60 else "NORMAL" if score >= 40 else "WEAK"
    return f"{level} / {trend}"

def main():
    p=argparse.ArgumentParser(description="Build T-Travel opportunity scores")
    p.add_argument("--database", default="events.db")
    p.add_argument("--test", action="store_true", help="Score only sample corridors")
    p.add_argument("--origin", help="Single origin ISO2")
    p.add_argument(
        "--origins",
        default="",
        help="Comma-separated origin ISO2 codes; explicit origins bypass the market-weight filter",
    )
    p.add_argument("--destinations", default="")
    p.add_argument("--date", help="Score date YYYY-MM-DD")
    args=p.parse_args()
    as_of=date.fromisoformat(args.date) if args.date else date.today()

    db=sqlite3.connect(args.database)
    ensure_schema(db)

    if args.origin:
        origins=[args.origin.upper()]
    elif args.origins:
        origins=[x.strip().upper() for x in args.origins.split(",") if x.strip()]
    elif args.test:
        origins=["DE","FR"]
    else:
        rows=db.execute("SELECT DISTINCT origin_iso2 FROM demand_observations").fetchall() if table_exists(db,"demand_observations") else []
        origins=[r[0] for r in rows]

    if args.destinations:
        dests=[x.strip().upper() for x in args.destinations.split(",") if x.strip()]
    elif args.test:
        dests=["JP","TR","ES","MA","US"]
    else:
        rows=db.execute("SELECT DISTINCT destination_iso2 FROM demand_observations").fetchall() if table_exists(db,"demand_observations") else []
        dests=[r[0] for r in rows]

    if not args.origin and not args.origins and not args.test:
        origin_weights = load_origin_market_weights()
        if origin_weights:
            before = len(origins)
            origins = [
                origin for origin in origins
                if origin_weights.get(origin, 0.0) >= MIN_ORIGIN_MARKET_WEIGHT
            ]
            print(
                f"ORIGIN FILTER: {len(origins)} of {before} origins retained "
                f"(market weight >= {MIN_ORIGIN_MARKET_WEIGHT:g})"
            )
        else:
            print("ORIGIN FILTER SKIP: origin_market_weights.csv not found")

    sales_scores = load_sales_scores(
        SALES_FILE,
        as_of,
        SALES_LOOKBACK_DAYS,
    )
    sales_scores = {
        key: item for key, item in sales_scores.items()
        if key[0] in origins
    }
    for (origin, dest), item in sales_scores.items():
        upsert_observation(
            db,
            as_of.isoformat(),
            origin,
            dest,
            "observed_sales_60d",
            item["score"],
            "T-Travel sales export",
            0.95,
            {
                "units": item["units"],
                "revenue": item["revenue"],
                "lookback_days": item["lookback_days"],
                "interpretation": "Observed completed sales; not total market demand",
            },
        )

    print(f"T-Travel Opportunity Builder {VERSION} | {as_of}")
    for origin in origins:
        for dest in dests:
            baseline=latest_signal(db,as_of.isoformat(),origin,dest,"baseline_search")
            momentum=latest_signal(db,as_of.isoformat(),origin,dest,"search_momentum")
            sales_item=sales_scores.get((origin,dest))
            sales_score=sales_item["score"] if sales_item else None
            season=latest_signal(db,as_of.isoformat(),origin,dest,"seasonality")
            holiday=latest_signal(db,as_of.isoformat(),origin,dest,"holiday")
            distance_km=corridor_distance(origin, dest)
            distance=distance_score(distance_km)
            events, event_details=event_score(db,origin,dest,as_of)
            parts={"baseline_search":baseline,"search_momentum":momentum,"seasonality":season,
                   "events":events,"holiday":holiday,"observed_sales":sales_score,
                   "distance":distance}
            total=weighted_score(parts)
            old7=historical_change(db,origin,dest,as_of,7)
            old30=historical_change(db,origin,dest,as_of,30)
            c7=None if total is None or old7 is None else round(total-old7,1)
            c30=None if total is None or old30 is None else round(total-old30,1)
            coverage=data_coverage(parts)
            st=status(total,c7)
            meta={"weights":WEIGHTS,"events":event_details,
                  "missing":[k for k,v in parts.items() if v is None],
                  "data_coverage":coverage,
                  "sales_lookback_days":SALES_LOOKBACK_DAYS,
                  "baseline_source":"Google Ads Keyword Planner" if baseline is not None else None,
                  "distance_km":distance_km,
                  "distance_score":distance,
                  "distance_method":"approximate country-centre great-circle distance" if distance_km is not None else "not available; neutral evidence"}
            db.execute("""
              INSERT INTO opportunity_scores
                (score_date,origin_iso2,destination_iso2,baseline_search_score,search_momentum_score,
                 observed_sales_score,observed_sales_units,observed_sales_revenue,
                 seasonality_score,events_score,holiday_score,distance_km,distance_score,
                 opportunity_score,data_coverage,change_7d,change_30d,status,components_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(score_date,origin_iso2,destination_iso2) DO UPDATE SET
                baseline_search_score=excluded.baseline_search_score,
                search_momentum_score=excluded.search_momentum_score,
                observed_sales_score=excluded.observed_sales_score,
                observed_sales_units=excluded.observed_sales_units,
                observed_sales_revenue=excluded.observed_sales_revenue,
                seasonality_score=excluded.seasonality_score,
                events_score=excluded.events_score,
                holiday_score=excluded.holiday_score,
                distance_km=excluded.distance_km,
                distance_score=excluded.distance_score,
                opportunity_score=excluded.opportunity_score,
                data_coverage=excluded.data_coverage,
                change_7d=excluded.change_7d, change_30d=excluded.change_30d,
                status=excluded.status, components_json=excluded.components_json
            """,(as_of.isoformat(),origin,dest,baseline,momentum,
                 sales_score,
                 sales_item["units"] if sales_item else None,
                 sales_item["revenue"] if sales_item else None,
                 season,events,holiday,distance_km,distance,total,coverage,c7,c30,st,
                 json.dumps(meta,ensure_ascii=False)))
            def f(x): return "N/A" if x is None else f"{x:.1f}"
            label = "Provisional" if coverage < 99.9 else "Opportunity"
            print(f"{origin}->{dest}  {label} {f(total):>5} | coverage {coverage:>5.1f}% | baseline {f(baseline):>5} | momentum {f(momentum):>5} | sales {f(sales_score):>5} | season {f(season):>5} | events {f(events):>5} | holidays {f(holiday):>5} | distance {f(distance):>5} | 7d {f(c7):>5} | {st}")

    db.commit()
    db.close()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
