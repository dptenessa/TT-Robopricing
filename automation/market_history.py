from __future__ import annotations

import argparse
import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

try:
    from pipeline_files import FILES, PipelineFiles
except ImportError:
    from automation.pipeline_files import FILES, PipelineFiles


SCHEMA_VERSION = 2
MATERIAL_CHANGE_PCT = 0.01


@dataclass
class HistoryUpdateStats:
    db_path: Path
    scanned_files: int = 0
    imported_files: int = 0
    skipped_files: int = 0
    source_rows: int = 0
    observation_rows: int = 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text


def _num(value: Any) -> float | None:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    return float(number)


def _norm_text(value: Any) -> str:
    return " ".join(_text(value).lower().split())


def _norm_plan(value: Any) -> str:
    return _norm_text(value).replace(" ", "_") or "unknown"


def _plan_type(plan: Any, gb: Any) -> str:
    plan_text = _norm_text(plan)
    gb_text = _norm_text(gb)
    if "unlimited" in plan_text or "unlimited" in gb_text:
        return "unlimited"
    return "capped"


def _round_key(value: Any, digits: int = 6) -> str:
    num = _num(value)
    if num is None or not math.isfinite(num):
        return ""
    rounded = round(num, digits)
    if float(rounded).is_integer():
        return str(int(rounded))
    return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _stable_product_key(
    provider: Any,
    iso: Any,
    plan: Any,
    plan_type: Any,
    days: Any,
    gb: Any,
    currency: Any,
) -> str:
    # Deliberately excludes variant_id and product name. Some providers embed the
    # current price in those fields (notably Saily), which made legitimate price
    # changes look like brand-new products in the old current-vs-previous logic.
    parts = [
        _norm_text(provider),
        _text(iso).upper(),
        _norm_plan(plan),
        _norm_text(plan_type),
        _round_key(days),
        "unlimited" if _norm_text(plan_type) == "unlimited" else _round_key(gb),
        _text(currency).upper(),
    ]
    return "|".join(parts)


def stable_product_key(
    provider: Any,
    iso: Any,
    plan: Any,
    days: Any,
    gb: Any,
    currency: Any,
    *,
    plan_type: Any | None = None,
) -> str:
    """Public stable commercial-product identity used across history/reporting.

    Variant IDs and product names are deliberately excluded because several
    providers change those values when a price or promotion changes.
    """
    resolved_plan_type = plan_type if _text(plan_type) else _plan_type(plan, gb)
    return _stable_product_key(provider, iso, plan, resolved_plan_type, days, gb, currency)


def _source_snapshot_date(path: Path) -> str:
    stem = path.stem
    for prefix in ("combined_scrape_",):
        if stem.startswith(prefix):
            candidate = stem[len(prefix):]
            try:
                return pd.Timestamp(candidate).date().isoformat()
            except Exception:
                pass
    return ""


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ingested_sources (
            source_path TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            source_rows INTEGER NOT NULL DEFAULT 0,
            observation_rows INTEGER NOT NULL DEFAULT 0,
            ingested_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_date TEXT NOT NULL,
            snapshot_date TEXT,
            provider TEXT NOT NULL,
            iso TEXT NOT NULL,
            iso3 TEXT,
            country TEXT,
            plan TEXT,
            plan_key TEXT NOT NULL,
            plan_type TEXT NOT NULL,
            days REAL,
            gb REAL,
            currency TEXT NOT NULL,
            price REAL NOT NULL,
            eur_price REAL,
            usd_price REAL,
            special_offer TEXT,
            variant_id TEXT,
            name TEXT,
            product_key TEXT NOT NULL,
            source_file TEXT,
            inserted_at TEXT NOT NULL,
            UNIQUE(product_key, observed_date)
        );

        CREATE INDEX IF NOT EXISTS idx_market_obs_product_date
            ON market_observations(product_key, observed_date);
        CREATE INDEX IF NOT EXISTS idx_market_obs_iso_date
            ON market_observations(iso, observed_date);
        CREATE INDEX IF NOT EXISTS idx_market_obs_provider_date
            ON market_observations(provider, observed_date);
        CREATE INDEX IF NOT EXISTS idx_market_obs_date
            ON market_observations(observed_date);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _prepare_observations(df: pd.DataFrame, source_path: Path) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    defaults = {
        "Provider": "",
        "ISO": "",
        "ISO3": "",
        "Country": "",
        "Plan": "",
        "GB": pd.NA,
        "Days": pd.NA,
        "Price": pd.NA,
        "Currency": "",
        "PriceDate": "",
        "SpecialOffer": "",
        "variant_id": "",
        "name": "",
        "eur_price": pd.NA,
        "usd_price": pd.NA,
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default

    out["provider"] = out["Provider"].map(_norm_text)
    out["iso"] = out["ISO"].fillna("").astype(str).str.strip().str.upper()
    out["iso3"] = out["ISO3"].fillna("").astype(str).str.strip().str.upper()
    out["country"] = out["Country"].fillna("").astype(str).str.strip()
    out["plan"] = out["Plan"].fillna("").astype(str).str.strip()
    out["plan_key"] = out["plan"].map(_norm_plan)
    out["plan_type"] = [
        _plan_type(plan, gb) for plan, gb in zip(out["Plan"], out["GB"])
    ]

    out["days"] = pd.to_numeric(out["Days"], errors="coerce")
    out["gb"] = pd.to_numeric(out["GB"], errors="coerce")
    out.loc[out["plan_type"] == "unlimited", "gb"] = pd.NA
    out["currency"] = out["Currency"].fillna("").astype(str).str.strip().str.upper()
    out["price"] = pd.to_numeric(out["Price"], errors="coerce")
    out["eur_price"] = pd.to_numeric(out["eur_price"], errors="coerce")
    out["usd_price"] = pd.to_numeric(out["usd_price"], errors="coerce")

    snapshot_date = _source_snapshot_date(source_path)
    source_price_dates = pd.to_datetime(out["PriceDate"], errors="coerce")

    # Historical movement must be keyed to when *we observed the market*, not
    # to a provider's own publication/effective date. Vodafone's PDF is a good
    # example: PriceDate can remain 2026-08-12 across several weekly scrapes.
    # Using that field as the observation date collapses distinct weekly
    # snapshots and makes current data look stale. Dated combined-history files
    # are therefore authoritative for observed_date.
    if snapshot_date:
        observed_dates = pd.Series(
            pd.Timestamp(snapshot_date), index=out.index, dtype="datetime64[ns]"
        )
    else:
        # Fallback for a project that has only combined_scrape_latest.csv and no
        # dated history yet. Prefer source PriceDate where available, otherwise
        # use the file modification date as the best available observation date.
        fallback_date = pd.Timestamp.fromtimestamp(source_path.stat().st_mtime).normalize()
        observed_dates = source_price_dates.fillna(fallback_date)

    out["observed_date"] = observed_dates.dt.date.astype("string")
    out["snapshot_date"] = snapshot_date

    out["special_offer"] = out["SpecialOffer"].fillna("").astype(str).str.strip()
    out["variant_id"] = out["variant_id"].fillna("").astype(str).str.strip()
    out["name"] = out["name"].fillna("").astype(str).str.strip()

    out = out[
        out["provider"].ne("")
        & out["iso"].ne("")
        & out["currency"].ne("")
        & out["price"].notna()
        & (out["price"] > 0)
        & out["observed_date"].notna()
    ].copy()
    if out.empty:
        return out

    out["product_key"] = [
        _stable_product_key(provider, iso, plan, plan_type, days, gb, currency)
        for provider, iso, plan, plan_type, days, gb, currency in zip(
            out["provider"],
            out["iso"],
            out["plan"],
            out["plan_type"],
            out["days"],
            out["gb"],
            out["currency"],
        )
    ]

    # Several scrapers occasionally emit duplicate rows for the same commercial
    # offer. Collapse them to one observation so trend counts are not inflated.
    group_cols = ["product_key", "observed_date"]
    agg = out.groupby(group_cols, dropna=False, as_index=False).agg(
        snapshot_date=("snapshot_date", "max"),
        provider=("provider", "first"),
        iso=("iso", "first"),
        iso3=("iso3", "first"),
        country=("country", "first"),
        plan=("plan", "first"),
        plan_key=("plan_key", "first"),
        plan_type=("plan_type", "first"),
        days=("days", "median"),
        gb=("gb", "median"),
        currency=("currency", "first"),
        price=("price", "median"),
        eur_price=("eur_price", "median"),
        usd_price=("usd_price", "median"),
        special_offer=("special_offer", "first"),
        variant_id=("variant_id", "first"),
        name=("name", "first"),
    )
    agg["source_file"] = source_path.name
    return agg


def ingest_combined_csv(
    path: str | Path,
    *,
    db_path: str | Path | None = None,
    force: bool = False,
) -> tuple[int, int, bool]:
    path = Path(path).resolve()
    if not path.exists():
        return 0, 0, False

    db_path = Path(db_path or FILES.market_history_db)
    source_key = str(path)
    fingerprint = _fingerprint(path)

    with _connect(db_path) as conn:
        _init_schema(conn)
        previous = conn.execute(
            "SELECT fingerprint FROM ingested_sources WHERE source_path = ?",
            (source_key,),
        ).fetchone()
        if previous and previous[0] == fingerprint and not force:
            return 0, 0, False

        df = pd.read_csv(path, low_memory=False)
        source_rows = int(len(df))
        observations = _prepare_observations(df, path)

        # A changed/re-imported snapshot replaces its previous DB contribution
        # completely.  Upserts alone are not enough: if a corrected CSV removes
        # a bad row, that old observation would otherwise survive forever.
        # Raw CSV history remains the source of truth; SQLite is a rebuildable
        # analytical index of those snapshots.
        conn.execute(
            "DELETE FROM market_observations WHERE source_file = ?",
            (path.name,),
        )

        if observations.empty:
            conn.execute(
                "INSERT INTO ingested_sources(source_path, fingerprint, source_rows, observation_rows, ingested_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, source_rows=excluded.source_rows, "
                "observation_rows=excluded.observation_rows, ingested_at=excluded.ingested_at",
                (source_key, fingerprint, source_rows, 0, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            return source_rows, 0, True

        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for rec in observations.to_dict(orient="records"):
            rows.append(
                (
                    _text(rec.get("observed_date")),
                    _text(rec.get("snapshot_date")),
                    _text(rec.get("provider")),
                    _text(rec.get("iso")),
                    _text(rec.get("iso3")),
                    _text(rec.get("country")),
                    _text(rec.get("plan")),
                    _text(rec.get("plan_key")),
                    _text(rec.get("plan_type")),
                    _num(rec.get("days")),
                    _num(rec.get("gb")),
                    _text(rec.get("currency")),
                    _num(rec.get("price")),
                    _num(rec.get("eur_price")),
                    _num(rec.get("usd_price")),
                    _text(rec.get("special_offer")),
                    _text(rec.get("variant_id")),
                    _text(rec.get("name")),
                    _text(rec.get("product_key")),
                    path.name,
                    now,
                )
            )

        conn.executemany(
            """
            INSERT INTO market_observations(
                observed_date, snapshot_date, provider, iso, iso3, country,
                plan, plan_key, plan_type, days, gb, currency, price,
                eur_price, usd_price, special_offer, variant_id, name,
                product_key, source_file, inserted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(product_key, observed_date) DO UPDATE SET
                snapshot_date=excluded.snapshot_date,
                country=excluded.country,
                iso3=excluded.iso3,
                plan=excluded.plan,
                price=excluded.price,
                eur_price=excluded.eur_price,
                usd_price=excluded.usd_price,
                special_offer=excluded.special_offer,
                variant_id=excluded.variant_id,
                name=excluded.name,
                source_file=excluded.source_file,
                inserted_at=excluded.inserted_at
            """,
            rows,
        )
        conn.execute(
            "INSERT INTO ingested_sources(source_path, fingerprint, source_rows, observation_rows, ingested_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, source_rows=excluded.source_rows, "
            "observation_rows=excluded.observation_rows, ingested_at=excluded.ingested_at",
            (source_key, fingerprint, source_rows, len(rows), now),
        )
        conn.commit()
        return source_rows, len(rows), True


def _history_source_files(paths: PipelineFiles) -> list[Path]:
    files = sorted(paths.combined_history_dir.glob("combined_scrape_*.csv"))
    # The weekly pack already contains a dated history copy of latest. Do not
    # ingest combined_scrape_latest.csv as a second snapshot when dated history
    # exists. Keep it only as a first-run fallback for projects with no history.
    if not files and paths.combined_latest.exists():
        files.append(paths.combined_latest)
    # Preserve order while avoiding duplicate paths.
    seen: set[Path] = set()
    result: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def _existing_schema_version(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if not exists:
                return 0
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def _remove_sqlite_files(db_path: Path) -> None:
    """Best-effort removal helper retained for maintenance/debugging only.

    Normal schema migration no longer deletes the SQLite file because Windows
    prevents unlinking a database that is open in another process (for example
    the pricing editor, a SQLite viewer, or a sync/indexing process).
    """
    for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except TypeError:  # Python < 3.8 compatibility fallback
            if candidate.exists():
                candidate.unlink()


def _rebuild_history_schema_in_place(db_path: Path) -> None:
    """Reset the derived analytical cache without deleting the DB file.

    The market-history database is fully rebuildable from dated combined scrape
    CSVs. Rebuilding the tables in place avoids WinError 32 on Windows when some
    other process has the SQLite file open. Existing connections can keep their
    file handle while this process obtains the normal SQLite write lock.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        # A passive checkpoint is harmless if the old DB used WAL and helps keep
        # the reset compact. Failure is non-fatal; DROP/CREATE below is the real
        # migration operation.
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(
            """
            DROP TABLE IF EXISTS market_observations;
            DROP TABLE IF EXISTS ingested_sources;
            DROP TABLE IF EXISTS metadata;
            """
        )
        conn.commit()
        _init_schema(conn)


def _ensure_current_history_semantics(paths: PipelineFiles) -> None:
    """Rebuild the derived DB once when observation-date semantics change.

    Schema v2 records the dated combined-scrape snapshot as observed_date. The
    database is a rebuildable analytical cache. Rebuild its tables in place
    rather than deleting the file so migration is robust on Windows/OneDrive.
    """
    db_path = paths.market_history_db
    version = _existing_schema_version(db_path)
    if version is None or version == SCHEMA_VERSION:
        return
    print(
        f"Market history DB version {version} -> {SCHEMA_VERSION}: "
        "rebuilding derived history with snapshot dates..."
    )
    _rebuild_history_schema_in_place(db_path)


def update_market_history(
    paths: PipelineFiles = FILES,
    *,
    force: bool = False,
) -> HistoryUpdateStats:
    _ensure_current_history_semantics(paths)
    stats = HistoryUpdateStats(db_path=paths.market_history_db)
    for path in _history_source_files(paths):
        stats.scanned_files += 1
        source_rows, observation_rows, imported = ingest_combined_csv(
            path,
            db_path=paths.market_history_db,
            force=force,
        )
        if imported:
            stats.imported_files += 1
            stats.source_rows += source_rows
            stats.observation_rows += observation_rows
        else:
            stats.skipped_files += 1
    return stats


def history_db_summary(db_path: str | Path | None = None) -> dict[str, Any]:
    db_path = Path(db_path or FILES.market_history_db)
    if not db_path.exists():
        return {
            "observations": 0,
            "products": 0,
            "providers": 0,
            "countries": 0,
            "date_min": "",
            "date_max": "",
        }
    with _connect(db_path) as conn:
        _init_schema(conn)
        row = conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT product_key), COUNT(DISTINCT provider),
                   COUNT(DISTINCT iso), MIN(observed_date), MAX(observed_date)
            FROM market_observations
            """
        ).fetchone()
    return {
        "observations": int(row[0] or 0),
        "products": int(row[1] or 0),
        "providers": int(row[2] or 0),
        "countries": int(row[3] or 0),
        "date_min": row[4] or "",
        "date_max": row[5] or "",
    }


def latest_product_changes(
    db_path: str | Path | None = None,
    *,
    material_threshold: float = MATERIAL_CHANGE_PCT,
) -> pd.DataFrame:
    db_path = Path(db_path or FILES.market_history_db)
    if not db_path.exists():
        return pd.DataFrame()

    query = """
    WITH sequenced AS (
        SELECT
            product_key, provider, iso, iso3, country, plan, plan_type,
            days, gb, currency, price AS CurrentPrice,
            observed_date AS CurrentDate,
            LAG(price) OVER (PARTITION BY product_key ORDER BY observed_date) AS PreviousPrice,
            LAG(observed_date) OVER (PARTITION BY product_key ORDER BY observed_date) AS PreviousDate,
            ROW_NUMBER() OVER (PARTITION BY product_key ORDER BY observed_date DESC) AS rn_desc
        FROM market_observations
    )
    SELECT * FROM sequenced
    WHERE rn_desc = 1 AND PreviousPrice IS NOT NULL AND PreviousPrice > 0 AND CurrentPrice > 0
    """
    with _connect(db_path) as conn:
        _init_schema(conn)
        df = pd.read_sql_query(query, conn)
    if df.empty:
        return df
    df["PctChange"] = df["CurrentPrice"] / df["PreviousPrice"] - 1.0
    df["AbsPctChange"] = df["PctChange"].abs()
    df["Material"] = df["AbsPctChange"] >= float(material_threshold)
    df["Direction"] = "FLAT"
    df.loc[df["PctChange"] >= float(material_threshold), "Direction"] = "UP"
    df.loc[df["PctChange"] <= -float(material_threshold), "Direction"] = "DOWN"
    return df


def _historical_change_for_window(
    observations: pd.DataFrame,
    *,
    days_back: int,
    material_threshold: float = MATERIAL_CHANGE_PCT,
) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame()
    obs = observations.copy()
    obs["observed_date"] = pd.to_datetime(obs["observed_date"], errors="coerce")
    obs["price"] = pd.to_numeric(obs["price"], errors="coerce")
    obs = obs.dropna(subset=["observed_date", "price"])
    obs = obs[obs["price"] > 0].sort_values(["product_key", "observed_date"])

    rows: list[dict[str, Any]] = []
    for product_key, group in obs.groupby("product_key", sort=False):
        latest = group.iloc[-1]
        target_date = latest["observed_date"] - pd.Timedelta(days=days_back)
        prior_candidates = group[group["observed_date"] <= target_date]
        if prior_candidates.empty:
            continue
        prior = prior_candidates.iloc[-1]
        if float(prior["price"]) <= 0:
            continue
        pct = float(latest["price"]) / float(prior["price"]) - 1.0
        rows.append(
            {
                "product_key": product_key,
                "provider": latest["provider"],
                "iso": latest["iso"],
                "iso3": latest.get("iso3", ""),
                "country": latest.get("country", ""),
                "plan": latest.get("plan", ""),
                "plan_type": latest.get("plan_type", ""),
                "days": latest.get("days"),
                "gb": latest.get("gb"),
                "currency": latest.get("currency", ""),
                "CurrentPrice": float(latest["price"]),
                "PreviousPrice": float(prior["price"]),
                "CurrentDate": latest["observed_date"].date().isoformat(),
                "PreviousDate": prior["observed_date"].date().isoformat(),
                "PctChange": pct,
                "AbsPctChange": abs(pct),
                "Material": abs(pct) >= material_threshold,
                "Direction": "UP" if pct >= material_threshold else ("DOWN" if pct <= -material_threshold else "FLAT"),
            }
        )
    return pd.DataFrame(rows)


def window_product_changes(
    db_path: str | Path | None = None,
    *,
    days_back: int = 28,
    material_threshold: float = MATERIAL_CHANGE_PCT,
) -> pd.DataFrame:
    """Compare each product's latest price with its latest observation at
    least ``days_back`` days earlier.

    This is intentionally implemented in SQLite rather than loading the full
    observation table and looping over thousands of pandas groups.  With the
    product/date index, the history dashboard stays fast as the DB grows.
    """
    db_path = Path(db_path or FILES.market_history_db)
    if not db_path.exists():
        return pd.DataFrame()

    modifier = f"-{max(int(days_back), 0)} days"
    query = """
    WITH latest_ranked AS (
        SELECT
            product_key, provider, iso, iso3, country, plan, plan_type,
            days, gb, currency, price, observed_date,
            ROW_NUMBER() OVER (
                PARTITION BY product_key ORDER BY observed_date DESC
            ) AS rn_latest
        FROM market_observations
        WHERE price > 0
    ), candidate_pairs AS (
        SELECT
            l.product_key, l.provider, l.iso, l.iso3, l.country, l.plan, l.plan_type,
            l.days, l.gb, l.currency,
            l.price AS CurrentPrice, l.observed_date AS CurrentDate,
            p.price AS PreviousPrice, p.observed_date AS PreviousDate,
            ROW_NUMBER() OVER (
                PARTITION BY l.product_key ORDER BY p.observed_date DESC
            ) AS rn_prior
        FROM latest_ranked AS l
        JOIN market_observations AS p
          ON p.product_key = l.product_key
         AND p.price > 0
         AND p.observed_date <= date(l.observed_date, ?)
        WHERE l.rn_latest = 1
    )
    SELECT
        product_key, provider, iso, iso3, country, plan, plan_type,
        days, gb, currency, CurrentPrice, CurrentDate, PreviousPrice, PreviousDate
    FROM candidate_pairs
    WHERE rn_prior = 1
    """
    with _connect(db_path) as conn:
        _init_schema(conn)
        df = pd.read_sql_query(query, conn, params=(modifier,))
    if df.empty:
        return df

    df["PctChange"] = df["CurrentPrice"] / df["PreviousPrice"] - 1.0
    df["AbsPctChange"] = df["PctChange"].abs()
    df["Material"] = df["AbsPctChange"] >= float(material_threshold)
    df["Direction"] = "FLAT"
    df.loc[df["PctChange"] >= float(material_threshold), "Direction"] = "UP"
    df.loc[df["PctChange"] <= -float(material_threshold), "Direction"] = "DOWN"
    return df


def print_history_summary(stats: HistoryUpdateStats) -> None:
    summary = history_db_summary(stats.db_path)
    print()
    print("Market History DB")
    print(f"- DB: {stats.db_path}")
    print(f"- Source files scanned: {stats.scanned_files}")
    print(f"- Newly ingested/updated: {stats.imported_files}")
    print(f"- Already up to date: {stats.skipped_files}")
    print(f"- Stored observations: {summary['observations']:,}")
    print(f"- Comparable product series: {summary['products']:,}")
    print(f"- Providers: {summary['providers']:,}")
    print(f"- Countries: {summary['countries']:,}")
    if summary["date_min"] or summary["date_max"]:
        print(f"- Coverage: {summary['date_min'] or '?'} to {summary['date_max'] or '?'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build/update the local SQLite competitor market-history database."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-read all combined-scrape history files even if already ingested.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    stats = update_market_history(FILES, force=args.force)
    print_history_summary(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
