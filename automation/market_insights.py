from __future__ import annotations

from datetime import datetime
from html import escape
import json
from string import Template
from pathlib import Path
from typing import Any

import math
import sqlite3
import pandas as pd

try:
    from pipeline_files import FILES, PipelineFiles
except ImportError:
    from automation.pipeline_files import FILES, PipelineFiles

try:
    from market_history import (
        MATERIAL_CHANGE_PCT,
        history_db_summary,
        latest_product_changes,
        stable_product_key,
        update_market_history,
        window_product_changes,
    )
except ImportError:
    from automation.market_history import (
        MATERIAL_CHANGE_PCT,
        history_db_summary,
        latest_product_changes,
        stable_product_key,
        update_market_history,
        window_product_changes,
    )


MIN_MATERIAL_CHANGE_PCT = MATERIAL_CHANGE_PCT

# Temporal data-quality guardrails.  These deliberately target very large,
# uncorroborated jumps rather than normal commercial repricing.
EXTREME_UP_PCT = 1.00       # +100%
EXTREME_DOWN_PCT = -0.50    # -50%
PEER_CONFIRM_PCT = 0.20     # peers need a meaningful same-direction move
MIN_CONFIRMING_PEERS = 2
MIN_PROVIDER_BROAD_MOVES = 8
MIN_PROVIDER_BROAD_COUNTRIES = 3
PROVIDER_BROAD_CONFIRM_PCT = 0.20
MAX_CURRENT_AGE_DAYS = 14   # old observations are not "latest" market movement
HISTORICAL_BASELINE_DAYS = 56
HISTORICAL_MIN_OBSERVATIONS = 3
TREND_WINDOWS = (("latest", "Latest snapshot", None), ("28", "4 weeks", 28), ("56", "8 weeks", 56), ("84", "12 weeks", 84))
DASHBOARD_VERSION = "history-charts-v2"


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


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "t", "yes", "y"}


def collect_market_changes(paths: PipelineFiles = FILES) -> pd.DataFrame:
    """Return the latest two observations per stable competitor product."""
    update_market_history(paths)
    return latest_product_changes(paths.market_history_db, material_threshold=MIN_MATERIAL_CHANGE_PCT)


def _current_cross_sectional_outlier_keys(paths: PipelineFiles) -> tuple[set[str], pd.DataFrame]:
    """Return current products already flagged by the market outlier annotator."""
    path = paths.market_outlier_audit
    if not path.exists():
        return set(), pd.DataFrame()
    try:
        audit = pd.read_csv(path, low_memory=False)
    except Exception:
        return set(), pd.DataFrame()
    if audit.empty:
        return set(), audit

    if "RowFlag" in audit.columns:
        audit = audit[audit["RowFlag"].map(_boolish)].copy()
    if audit.empty:
        return set(), audit

    keys: set[str] = set()
    for _, row in audit.iterrows():
        iso = _text(row.get("ISO")) or _text(row.get("ISO3"))
        keys.add(
            stable_product_key(
                row.get("Provider"),
                iso,
                row.get("Plan"),
                row.get("Days"),
                row.get("GB"),
                row.get("Currency"),
            )
        )
    return keys, audit


def _annotate_temporal_quality(
    changes: pd.DataFrame,
    *,
    current_outlier_keys: set[str],
    latest_market_date: str,
) -> pd.DataFrame:
    """Classify suspicious historical movements without deleting raw observations.

    A very large one-provider jump is excluded from headline/trend analytics unless
    at least two peer providers in the same country/currency/product type/duration
    move materially in the same direction.  Current cross-sectional outliers are
    always excluded as a second independent quality gate.
    """
    if changes.empty:
        return changes.copy()

    df = changes.copy()
    df["PctChange"] = pd.to_numeric(df.get("PctChange"), errors="coerce")
    df["days"] = pd.to_numeric(df.get("days"), errors="coerce")
    df["gb"] = pd.to_numeric(df.get("gb"), errors="coerce")
    df["TemporalAnomaly"] = False
    df["CurrentMarketOutlier"] = df.get("product_key", "").astype(str).isin(current_outlier_keys)
    df["PeerProviderCount"] = 0
    df["ConfirmingPeerCount"] = 0
    df["PeerMedianChangePct"] = pd.NA
    df["ProviderMoveCount"] = 0
    df["ProviderCountryCount"] = 0
    df["ProviderMedianChangePct"] = pd.NA
    df["BroadProviderMove"] = False
    df["AnomalyReason"] = ""

    grouping = ["iso", "currency", "plan_type", "days"]
    grouped: dict[tuple[Any, ...], pd.DataFrame] = {
        key: group for key, group in df.groupby(grouping, dropna=False, sort=False)
    }

    extreme_mask = (df["PctChange"] >= EXTREME_UP_PCT) | (df["PctChange"] <= EXTREME_DOWN_PCT)
    for idx in df.index[extreme_mask]:
        row = df.loc[idx]
        key = tuple(row.get(col) for col in grouping)
        group = grouped.get(key, pd.DataFrame())
        peers = group[group["provider"].astype(str) != str(row.get("provider", ""))].copy()

        # For capped products, corroboration must also come from roughly
        # comparable allowances.  This prevents (for example) a 50 GB product
        # from validating a suspicious jump on a 2 GB product merely because
        # both happen to be seven-day offers. Unlimited has no GB dimension.
        if _text(row.get("plan_type")).lower() != "unlimited":
            row_gb = _num(row.get("gb"))
            if row_gb is not None and row_gb > 0 and not peers.empty:
                peer_gb = pd.to_numeric(peers.get("gb"), errors="coerce")
                ratio = peer_gb / row_gb
                peers = peers[ratio.between(0.5, 2.0, inclusive="both")].copy()

        if peers.empty:
            peer_by_provider = pd.Series(dtype=float)
        else:
            peer_by_provider = (
                peers.groupby("provider", dropna=False)["PctChange"].median().dropna()
            )
        peer_count = int(len(peer_by_provider))
        pct = float(row["PctChange"])
        if pct > 0:
            confirming = int((peer_by_provider >= PEER_CONFIRM_PCT).sum())
            peer_median = float(peer_by_provider.median()) if peer_count else math.nan
            median_confirms = peer_count >= MIN_CONFIRMING_PEERS and peer_median >= PEER_CONFIRM_PCT
        else:
            confirming = int((peer_by_provider <= -PEER_CONFIRM_PCT).sum())
            peer_median = float(peer_by_provider.median()) if peer_count else math.nan
            median_confirms = peer_count >= MIN_CONFIRMING_PEERS and peer_median <= -PEER_CONFIRM_PCT

        peer_corroborated = confirming >= MIN_CONFIRMING_PEERS or median_confirms

        provider_group = df[df["provider"].astype(str) == str(row.get("provider", ""))].copy()
        if pct > 0:
            provider_moves = provider_group[pd.to_numeric(provider_group["PctChange"], errors="coerce") >= PROVIDER_BROAD_CONFIRM_PCT]
        else:
            provider_moves = provider_group[pd.to_numeric(provider_group["PctChange"], errors="coerce") <= -PROVIDER_BROAD_CONFIRM_PCT]
        provider_move_count = int(provider_moves.get("product_key", pd.Series(dtype=str)).astype(str).nunique()) if not provider_moves.empty else 0
        provider_country_count = int(provider_moves.get("iso", pd.Series(dtype=str)).astype(str).nunique()) if not provider_moves.empty else 0
        provider_median = float(pd.to_numeric(provider_moves.get("PctChange"), errors="coerce").median()) if not provider_moves.empty else math.nan
        provider_broad = (
            provider_move_count >= MIN_PROVIDER_BROAD_MOVES
            and provider_country_count >= MIN_PROVIDER_BROAD_COUNTRIES
            and ((pct > 0 and provider_median >= PROVIDER_BROAD_CONFIRM_PCT) or (pct < 0 and provider_median <= -PROVIDER_BROAD_CONFIRM_PCT))
        )

        corroborated = peer_corroborated or provider_broad
        df.at[idx, "PeerProviderCount"] = peer_count
        df.at[idx, "ConfirmingPeerCount"] = confirming
        df.at[idx, "PeerMedianChangePct"] = peer_median if math.isfinite(peer_median) else pd.NA
        df.at[idx, "ProviderMoveCount"] = provider_move_count
        df.at[idx, "ProviderCountryCount"] = provider_country_count
        df.at[idx, "ProviderMedianChangePct"] = provider_median if math.isfinite(provider_median) else pd.NA
        df.at[idx, "BroadProviderMove"] = provider_broad
        if not corroborated:
            df.at[idx, "TemporalAnomaly"] = True
            if peer_count:
                df.at[idx, "AnomalyReason"] = (
                    f"extreme uncorroborated move; {peer_count} peer provider(s), "
                    f"peer median {peer_median:+.1%}; provider move not broad enough "
                    f"({provider_move_count} products / {provider_country_count} countries)"
                )
            else:
                df.at[idx, "AnomalyReason"] = (
                    "extreme move with no peer-provider corroboration; provider move not broad enough "
                    f"({provider_move_count} products / {provider_country_count} countries)"
                )

    # Cross-sectional outlier status is independent from the temporal jump test.
    current_flag = df["CurrentMarketOutlier"]
    df.loc[current_flag & df["AnomalyReason"].eq(""), "AnomalyReason"] = "current price is a cross-sectional market outlier"
    df.loc[current_flag & df["AnomalyReason"].ne("") & ~df["AnomalyReason"].str.contains("cross-sectional", na=False), "AnomalyReason"] += "; current price is also a cross-sectional market outlier"

    latest_ts = pd.to_datetime(latest_market_date, errors="coerce")
    current_ts = pd.to_datetime(df.get("CurrentDate"), errors="coerce")
    if pd.isna(latest_ts):
        df["ObservationAgeDays"] = pd.NA
        df["FreshEnough"] = True
    else:
        age = (latest_ts - current_ts).dt.days
        df["ObservationAgeDays"] = age
        df["FreshEnough"] = age.le(MAX_CURRENT_AGE_DAYS) | age.isna()

    df["Trusted"] = ~df["TemporalAnomaly"] & ~df["CurrentMarketOutlier"] & df["FreshEnough"]
    return df


def _aggregate_changes(changes: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if changes.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for key, group in changes.groupby(by, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        material = group[group["Material"]]
        up = int((material["Direction"] == "UP").sum())
        down = int((material["Direction"] == "DOWN").sum())
        changed = int(len(material))
        row = {col: value for col, value in zip(by, key)}
        row.update(
            {
                "MatchedProducts": int(len(group)),
                "ChangedProducts": changed,
                "UpProducts": up,
                "DownProducts": down,
                "BreadthPct": ((up - down) / changed) if changed else 0.0,
                "MedianChangePct": float(material["PctChange"].median()) if not material.empty else 0.0,
                "MeanChangePct": float(material["PctChange"].mean()) if not material.empty else 0.0,
                "Providers": int(group["provider"].nunique()) if "provider" in group.columns else 1,
                "LatestDataDate": _text(group.get("CurrentDate", pd.Series(dtype=str)).max()),
                "PreviousDataDate": _text(group.get("PreviousDate", pd.Series(dtype=str)).max()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _merge_country_trends(latest: pd.DataFrame, four_week: pd.DataFrame) -> pd.DataFrame:
    latest_agg = _aggregate_changes(latest, ["iso", "country"]) if not latest.empty else pd.DataFrame()
    four_agg = _aggregate_changes(four_week, ["iso", "country"]) if not four_week.empty else pd.DataFrame()
    if latest_agg.empty and four_agg.empty:
        return pd.DataFrame()

    def rename_latest(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns={
            "MatchedProducts": "LatestMatchedProducts",
            "ChangedProducts": "LatestChangedProducts",
            "UpProducts": "LatestUpProducts",
            "DownProducts": "LatestDownProducts",
            "BreadthPct": "LatestBreadthPct",
            "MedianChangePct": "LatestMedianChangePct",
            "MeanChangePct": "LatestMeanChangePct",
            "Providers": "LatestProviders",
            "PreviousDataDate": "PreviousDataDate",
        })

    def rename_four(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns={
            "MatchedProducts": "FourWeekMatchedProducts",
            "ChangedProducts": "FourWeekChangedProducts",
            "UpProducts": "FourWeekUpProducts",
            "DownProducts": "FourWeekDownProducts",
            "BreadthPct": "FourWeekBreadthPct",
            "MedianChangePct": "FourWeekMedianChangePct",
            "MeanChangePct": "FourWeekMeanChangePct",
            "Providers": "FourWeekProviders",
            "LatestDataDate": "FourWeekLatestDataDate",
            "PreviousDataDate": "FourWeekBaselineDate",
        })

    if latest_agg.empty:
        merged = rename_four(four_agg)
    elif four_agg.empty:
        merged = rename_latest(latest_agg)
    else:
        merged = rename_latest(latest_agg).merge(rename_four(four_agg), on=["iso", "country"], how="outer")

    int_cols = [
        "LatestMatchedProducts", "LatestChangedProducts", "LatestUpProducts", "LatestDownProducts", "LatestProviders",
        "FourWeekMatchedProducts", "FourWeekChangedProducts", "FourWeekUpProducts", "FourWeekDownProducts", "FourWeekProviders",
    ]
    for col in int_cols:
        if col not in merged.columns:
            merged[col] = 0
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0).astype(int)

    for col in ["LatestMedianChangePct", "FourWeekMedianChangePct", "LatestBreadthPct", "FourWeekBreadthPct"]:
        if col not in merged.columns:
            merged[col] = pd.NA
        merged[col] = pd.to_numeric(merged[col], errors="coerce")

    if "LatestDataDate" not in merged.columns:
        merged["LatestDataDate"] = ""
    if "FourWeekLatestDataDate" not in merged.columns:
        merged["FourWeekLatestDataDate"] = ""

    four_week_usable = merged["FourWeekMatchedProducts"] >= 2
    merged["TrendChangePct"] = merged["LatestMedianChangePct"]
    merged.loc[four_week_usable, "TrendChangePct"] = merged.loc[four_week_usable, "FourWeekMedianChangePct"]
    merged["TrendBreadthPct"] = merged["LatestBreadthPct"]
    merged.loc[four_week_usable, "TrendBreadthPct"] = merged.loc[four_week_usable, "FourWeekBreadthPct"]
    merged["TrendWindow"] = "latest"
    merged.loc[four_week_usable, "TrendWindow"] = "4 weeks"
    return merged


def _provider_trends(latest: pd.DataFrame, four_week: pd.DataFrame) -> pd.DataFrame:
    latest_agg = _aggregate_changes(latest, ["provider"]) if not latest.empty else pd.DataFrame()
    four_agg = _aggregate_changes(four_week, ["provider"]) if not four_week.empty else pd.DataFrame()
    if latest_agg.empty and four_agg.empty:
        return pd.DataFrame()
    if not latest_agg.empty:
        latest_agg = latest_agg.rename(columns={
            "provider": "Provider", "MedianChangePct": "LatestMedianChangePct",
            "BreadthPct": "LatestBreadthPct", "ChangedProducts": "LatestChangedProducts",
            "UpProducts": "LatestUpProducts", "DownProducts": "LatestDownProducts",
            "MatchedProducts": "LatestMatchedProducts", "LatestDataDate": "LatestDataDate",
        })
    if not four_agg.empty:
        four_agg = four_agg.rename(columns={
            "provider": "Provider", "MedianChangePct": "FourWeekMedianChangePct",
            "BreadthPct": "FourWeekBreadthPct", "ChangedProducts": "FourWeekChangedProducts",
            "UpProducts": "FourWeekUpProducts", "DownProducts": "FourWeekDownProducts",
            "MatchedProducts": "FourWeekMatchedProducts", "LatestDataDate": "FourWeekLatestDataDate",
        })
    if latest_agg.empty:
        return four_agg
    if four_agg.empty:
        return latest_agg
    keep_latest = [c for c in latest_agg.columns if c in {
        "Provider", "LatestMedianChangePct", "LatestBreadthPct", "LatestChangedProducts", "LatestUpProducts",
        "LatestDownProducts", "LatestMatchedProducts", "LatestDataDate"
    }]
    keep_four = [c for c in four_agg.columns if c in {
        "Provider", "FourWeekMedianChangePct", "FourWeekBreadthPct", "FourWeekChangedProducts", "FourWeekUpProducts",
        "FourWeekDownProducts", "FourWeekMatchedProducts", "FourWeekLatestDataDate"
    }]
    return latest_agg[keep_latest].merge(four_agg[keep_four], on="Provider", how="outer")


def _provider_coverage(paths: PipelineFiles, latest_market_date: str) -> pd.DataFrame:
    db_path = paths.market_history_db
    if not db_path.exists():
        return pd.DataFrame()
    query = """
    WITH daily AS (
        SELECT provider, observed_date,
               COUNT(DISTINCT iso) AS countries,
               COUNT(DISTINCT product_key) AS products
        FROM market_observations
        GROUP BY provider, observed_date
    ), ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY provider ORDER BY observed_date DESC) AS rn
        FROM daily
    )
    SELECT provider,
           MAX(CASE WHEN rn=1 THEN observed_date END) AS LatestDate,
           MAX(CASE WHEN rn=1 THEN countries END) AS LatestCountries,
           MAX(CASE WHEN rn=1 THEN products END) AS LatestProducts,
           MAX(CASE WHEN rn=2 THEN observed_date END) AS PreviousDate,
           MAX(CASE WHEN rn=2 THEN countries END) AS PreviousCountries,
           MAX(CASE WHEN rn=2 THEN products END) AS PreviousProducts
    FROM ranked
    WHERE rn <= 2
    GROUP BY provider
    ORDER BY provider
    """
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn)
    if df.empty:
        return df
    df = df.rename(columns={"provider": "Provider"})
    latest_ts = pd.to_datetime(latest_market_date, errors="coerce")
    provider_ts = pd.to_datetime(df["LatestDate"], errors="coerce")
    if pd.isna(latest_ts):
        df["AgeDays"] = pd.NA
    else:
        df["AgeDays"] = (latest_ts - provider_ts).dt.days
    df["ProductDeltaPct"] = (
        pd.to_numeric(df["LatestProducts"], errors="coerce") /
        pd.to_numeric(df["PreviousProducts"], errors="coerce").replace(0, pd.NA) - 1.0
    )
    df["CountryDeltaPct"] = (
        pd.to_numeric(df["LatestCountries"], errors="coerce") /
        pd.to_numeric(df["PreviousCountries"], errors="coerce").replace(0, pd.NA) - 1.0
    )
    def status(age: Any) -> str:
        age_num = _num(age)
        if age_num is None:
            return "unknown"
        if age_num <= 7:
            return "current"
        if age_num <= MAX_CURRENT_AGE_DAYS:
            return "watch"
        return "stale"
    df["Freshness"] = df["AgeDays"].map(status)
    return df


def _recommendation_aggregates(recommendations: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if recommendations.empty:
        empty = pd.DataFrame()
        return empty, empty, empty
    rec = recommendations.copy()
    rec["Actionable"] = rec.get("Actionable", False).map(_boolish)
    rec = rec[rec["Actionable"]].copy()
    if rec.empty:
        empty = pd.DataFrame()
        return empty, empty, empty
    rec["Direction"] = rec.get("Direction", "").fillna("").astype(str).str.upper()
    rec["Mechanism"] = rec.get("Mechanism", "").fillna("").astype(str).str.upper()
    rec["Confidence"] = rec.get("Confidence", "").fillna("").astype(str).str.upper()
    rec["MarketGapPct"] = pd.to_numeric(rec.get("MarketGapPct"), errors="coerce")
    rec["Days"] = pd.to_numeric(rec.get("Days"), errors="coerce")

    by_unit = rec.groupby(["PricingUnitIdUsed"], dropna=False).agg(
        Recommendations=("Direction", "size"),
        Up=("Direction", lambda s: int((s == "UP").sum())),
        Down=("Direction", lambda s: int((s == "DOWN").sum())),
        HighConfidence=("Confidence", lambda s: int((s == "HIGH").sum())),
        MedianMarketGapPct=("MarketGapPct", "median"),
    ).reset_index()
    by_unit["NetSignal"] = by_unit["Up"] - by_unit["Down"]

    by_days = rec.groupby(["Days"], dropna=False).agg(
        Recommendations=("Direction", "size"),
        Up=("Direction", lambda s: int((s == "UP").sum())),
        Down=("Direction", lambda s: int((s == "DOWN").sum())),
    ).reset_index().sort_values("Days")

    by_plan = rec.groupby(["Plan"], dropna=False).agg(
        Recommendations=("Direction", "size"),
        Up=("Direction", lambda s: int((s == "UP").sum())),
        Down=("Direction", lambda s: int((s == "DOWN").sum())),
    ).reset_index().sort_values("Recommendations", ascending=False)
    return by_unit, by_days, by_plan


def _fmt_pct(value: Any, digits: int = 1) -> str:
    val = _num(value)
    if val is None or not math.isfinite(val):
        return "—"
    return f"{val * 100:.{digits}f}%"


def _direction_class(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "neutral"
    if value > 0.005:
        return "up"
    if value < -0.005:
        return "down"
    return "neutral"


def _table_html(df: pd.DataFrame, columns: list[tuple[str, str, str]], table_id: str, limit: int = 100) -> str:
    if df.empty:
        return '<div class="empty">No data available.</div>'
    view = df.head(limit)
    head = "".join(f"<th>{escape(label)}</th>" for _, label, _ in columns)
    body_rows = []
    for _, row in view.iterrows():
        cells = []
        for col, _label, kind in columns:
            value = row.get(col, "")
            cls = ""
            if kind == "pct":
                text = _fmt_pct(value)
                cls = _direction_class(_num(value))
            elif kind == "num":
                num = _num(value)
                text = "—" if num is None else f"{num:,.0f}"
            elif kind == "dec":
                num = _num(value)
                text = "—" if num is None else f"{num:,.2f}"
            elif kind == "date":
                text = _text(value) or "—"
            else:
                text = _text(value) or "—"
            cells.append(f'<td class="{cls}">{escape(text)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<input class="table-search" type="search" placeholder="Filter table…" '
        f'oninput="filterTable(\'{table_id}\', this.value)">'
        f'<div class="table-wrap"><table id="{table_id}"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def _material_median(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    material = df[df["Material"]]
    if material.empty:
        return 0.0
    value = pd.to_numeric(material["PctChange"], errors="coerce").median()
    return 0.0 if pd.isna(value) else float(value)



def _historical_median_changes(
    paths: PipelineFiles = FILES,
    *,
    days_back: int = HISTORICAL_BASELINE_DAYS,
    min_observations: int = HISTORICAL_MIN_OBSERVATIONS,
    material_threshold: float = MIN_MATERIAL_CHANGE_PCT,
) -> pd.DataFrame:
    """Compare each current product price with its recent historical median.

    The baseline contains prior observations only; the current observation is
    deliberately excluded.  This keeps a bad scrape suspicious even when the
    same bad value appears in two consecutive snapshots.
    """
    db_path = paths.market_history_db
    if not db_path.exists():
        return pd.DataFrame()

    modifier = f"-{max(int(days_back), 1)} days"
    query = """
    WITH latest AS (
        SELECT product_key, MAX(observed_date) AS CurrentDate
        FROM market_observations
        WHERE price > 0
        GROUP BY product_key
    )
    SELECT
        cur.product_key, cur.provider, cur.iso, cur.iso3, cur.country,
        cur.plan, cur.plan_type, cur.days, cur.gb, cur.currency,
        cur.price AS CurrentPrice, cur.observed_date AS CurrentDate,
        hist.price AS HistoricalPrice, hist.observed_date AS HistoricalDate
    FROM latest AS l
    JOIN market_observations AS cur
      ON cur.product_key = l.product_key
     AND cur.observed_date = l.CurrentDate
     AND cur.price > 0
    JOIN market_observations AS hist
      ON hist.product_key = l.product_key
     AND hist.price > 0
     AND hist.observed_date < l.CurrentDate
     AND hist.observed_date >= date(l.CurrentDate, ?)
    ORDER BY cur.product_key, hist.observed_date
    """
    with sqlite3.connect(db_path) as conn:
        rows = pd.read_sql_query(query, conn, params=(modifier,))
    if rows.empty:
        return pd.DataFrame()

    rows["HistoricalPrice"] = pd.to_numeric(rows["HistoricalPrice"], errors="coerce")
    rows["CurrentPrice"] = pd.to_numeric(rows["CurrentPrice"], errors="coerce")
    rows["HistoricalDate"] = pd.to_datetime(rows["HistoricalDate"], errors="coerce")
    rows = rows.dropna(subset=["HistoricalPrice", "CurrentPrice", "HistoricalDate"])
    rows = rows[(rows["HistoricalPrice"] > 0) & (rows["CurrentPrice"] > 0)]

    output: list[dict[str, Any]] = []
    for product_key, group in rows.groupby("product_key", sort=False):
        if len(group) < int(min_observations):
            continue
        first = group.iloc[-1]
        prices = pd.to_numeric(group["HistoricalPrice"], errors="coerce").dropna()
        if len(prices) < int(min_observations):
            continue
        baseline = float(prices.median())
        if baseline <= 0:
            continue
        current = float(first["CurrentPrice"])
        pct = current / baseline - 1.0
        mad = float((prices - baseline).abs().median()) if len(prices) else 0.0
        output.append({
            "product_key": product_key,
            "provider": first.get("provider", ""),
            "iso": first.get("iso", ""),
            "iso3": first.get("iso3", ""),
            "country": first.get("country", ""),
            "plan": first.get("plan", ""),
            "plan_type": first.get("plan_type", ""),
            "days": first.get("days"),
            "gb": first.get("gb"),
            "currency": first.get("currency", ""),
            "CurrentPrice": current,
            "CurrentDate": _text(first.get("CurrentDate")),
            "PreviousPrice": baseline,
            "PreviousDate": group["HistoricalDate"].min().date().isoformat(),
            "BaselineEndDate": group["HistoricalDate"].max().date().isoformat(),
            "BaselineMedianPrice": baseline,
            "HistoryObservations": int(len(prices)),
            "BaselineMAD": mad,
            "ComparisonMethod": f"{int(days_back)}d historical median",
            "PctChange": pct,
            "AbsPctChange": abs(pct),
            "Material": abs(pct) >= float(material_threshold),
            "Direction": "UP" if pct >= float(material_threshold) else ("DOWN" if pct <= -float(material_threshold) else "FLAT"),
        })
    return pd.DataFrame(output)


def current_quality_exclusions(paths: PipelineFiles = FILES) -> pd.DataFrame:
    """Return current products excluded by the temporal/cross-sectional guard.

    The preferred temporal test is current price versus the previous 56-day
    historical median (minimum three prior observations).  Products without
    enough history fall back to latest-versus-previous comparison.
    """
    update_market_history(paths)
    history = history_db_summary(paths.market_history_db)
    latest_market_date = _text(history.get("date_max"))
    current_outlier_keys, _ = _current_cross_sectional_outlier_keys(paths)

    robust = _historical_median_changes(paths)
    robust_q = _annotate_temporal_quality(
        robust,
        current_outlier_keys=current_outlier_keys,
        latest_market_date=latest_market_date,
    ) if not robust.empty else robust
    if not robust_q.empty:
        robust_q["QualityBaseline"] = robust_q.get("ComparisonMethod", f"{HISTORICAL_BASELINE_DAYS}d historical median")

    latest = latest_product_changes(paths.market_history_db, material_threshold=MIN_MATERIAL_CHANGE_PCT)
    latest_q = _annotate_temporal_quality(
        latest,
        current_outlier_keys=current_outlier_keys,
        latest_market_date=latest_market_date,
    ) if not latest.empty else latest
    if not latest_q.empty:
        latest_q["QualityBaseline"] = "previous observation (fallback)"

    if robust_q.empty:
        quality = latest_q.copy()
    elif latest_q.empty:
        quality = robust_q.copy()
    else:
        robust_keys = set(robust_q["product_key"].astype(str))
        fallback = latest_q[~latest_q["product_key"].astype(str).isin(robust_keys)].copy()
        quality = pd.concat([robust_q, fallback], ignore_index=True, sort=False)

    if quality.empty:
        return quality
    quality["QualityExcluded"] = (
        quality.get("TemporalAnomaly", False).map(_boolish)
        | quality.get("CurrentMarketOutlier", False).map(_boolish)
    )
    return quality


def _apply_current_quality_to_window(
    changes: pd.DataFrame,
    *,
    quality: pd.DataFrame,
    current_outlier_keys: set[str],
    latest_market_date: str,
) -> pd.DataFrame:
    if changes.empty:
        return changes.copy()
    out = changes.copy()
    temporal_keys: set[str] = set()
    if not quality.empty and "TemporalAnomaly" in quality.columns:
        temporal_keys = set(
            quality.loc[quality["TemporalAnomaly"].map(_boolish), "product_key"].astype(str)
        )
    keys = out.get("product_key", pd.Series("", index=out.index)).astype(str)
    out["TemporalAnomaly"] = keys.isin(temporal_keys)
    out["CurrentMarketOutlier"] = keys.isin(current_outlier_keys)
    latest_ts = pd.to_datetime(latest_market_date, errors="coerce")
    current_ts = pd.to_datetime(out.get("CurrentDate"), errors="coerce")
    if pd.isna(latest_ts):
        out["ObservationAgeDays"] = pd.NA
        out["FreshEnough"] = True
    else:
        age = (latest_ts - current_ts).dt.days
        out["ObservationAgeDays"] = age
        out["FreshEnough"] = age.le(MAX_CURRENT_AGE_DAYS) | age.isna()
    out["Trusted"] = ~out["TemporalAnomaly"] & ~out["CurrentMarketOutlier"] & out["FreshEnough"]
    return out


def _window_payload(changes: pd.DataFrame, label: str) -> dict[str, Any]:
    trusted = changes[changes.get("Trusted", True)].copy() if not changes.empty else changes
    material = trusted[trusted.get("Material", False)].copy() if not trusted.empty else pd.DataFrame()
    country = _aggregate_changes(trusted, ["iso", "country"]) if not trusted.empty else pd.DataFrame()
    provider = _aggregate_changes(trusted, ["provider"]) if not trusted.empty else pd.DataFrame()

    if not country.empty:
        country = country.rename(columns={"Providers": "ProviderCount"})
        country = country.sort_values("MedianChangePct", key=lambda s: s.abs(), ascending=False)
    if not provider.empty:
        provider = provider.rename(columns={"provider": "Provider"})
        provider = provider.sort_values("MedianChangePct", key=lambda s: s.abs(), ascending=False)
    if not material.empty:
        material = material.sort_values("AbsPctChange", ascending=False)

    def records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
        if df.empty:
            return []
        view = df.head(limit) if limit else df
        return json.loads(view.to_json(orient="records", date_format="iso"))

    return {
        "label": label,
        "matched": int(len(trusted)),
        "changed": int(len(material)),
        "up": int((material.get("Direction", pd.Series(dtype=str)) == "UP").sum()) if not material.empty else 0,
        "down": int((material.get("Direction", pd.Series(dtype=str)) == "DOWN").sum()) if not material.empty else 0,
        "median": _material_median(trusted),
        "countries": records(country, 300),
        "providers": records(provider, 100),
        "moves": records(material, 300),
    }


def _history_timeline(paths: PipelineFiles) -> list[dict[str, Any]]:
    if not paths.market_history_db.exists():
        return []
    query = """
    SELECT observed_date AS Date,
           COUNT(*) AS Observations,
           COUNT(DISTINCT product_key) AS Products,
           COUNT(DISTINCT provider) AS Providers,
           COUNT(DISTINCT iso) AS Countries
    FROM market_observations
    GROUP BY observed_date
    ORDER BY observed_date
    """
    with sqlite3.connect(paths.market_history_db) as conn:
        df = pd.read_sql_query(query, conn)
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records"))

def market_insights_is_stale(
    paths: PipelineFiles = FILES,
    *,
    recommendations_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> bool:
    rec_dir = paths.work_dir / "pricing_recommendations"
    recommendations_path = Path(recommendations_path or (rec_dir / "recommendations_latest.csv"))
    output_path = Path(output_path or (rec_dir / "market_insights_latest.html"))
    if not output_path.exists():
        return True

    # Do not rely only on mtimes. ZIP extraction / OneDrive can preserve timestamps,
    # which can make an HTML generated by an older dashboard version look current.
    # The version marker makes stale detection deterministic.
    try:
        html_head = output_path.read_text(encoding="utf-8", errors="ignore")[:250000]
        if DASHBOARD_VERSION not in html_head:
            return True
    except Exception:
        return True

    output_mtime = output_path.stat().st_mtime
    for source in (Path(__file__), recommendations_path, paths.market_history_db, paths.market_outlier_audit):
        if source.exists() and source.stat().st_mtime > output_mtime:
            return True
    return False


def generate_market_insights(
    paths: PipelineFiles = FILES,
    *,
    recommendations_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    rec_dir = paths.work_dir / "pricing_recommendations"
    recommendations_path = Path(recommendations_path or (rec_dir / "recommendations_latest.csv"))
    output_path = Path(output_path or (rec_dir / "market_insights_latest.html"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    history_stats = update_market_history(paths)
    history = history_db_summary(paths.market_history_db)
    latest_market_date = _text(history.get("date_max"))
    recs = pd.read_csv(recommendations_path, low_memory=False) if recommendations_path.exists() else pd.DataFrame()
    current_outlier_keys, current_outlier_audit = _current_cross_sectional_outlier_keys(paths)

    quality = current_quality_exclusions(paths)
    temporal_anomaly_count = int(quality.get("TemporalAnomaly", pd.Series(dtype=bool)).map(_boolish).sum()) if not quality.empty else 0
    quality_exclusions = quality[quality.get("QualityExcluded", False).map(_boolish)].copy() if not quality.empty else pd.DataFrame()
    cross_outlier_count = len(current_outlier_keys)

    raw_windows: dict[str, pd.DataFrame] = {}
    for key, _label, days_back in TREND_WINDOWS:
        if days_back is None:
            raw = latest_product_changes(paths.market_history_db, material_threshold=MIN_MATERIAL_CHANGE_PCT)
        else:
            raw = window_product_changes(paths.market_history_db, days_back=days_back, material_threshold=MIN_MATERIAL_CHANGE_PCT)
        raw_windows[key] = _apply_current_quality_to_window(
            raw,
            quality=quality,
            current_outlier_keys=current_outlier_keys,
            latest_market_date=latest_market_date,
        )

    window_data = {
        key: _window_payload(raw_windows[key], label)
        for key, label, _days_back in TREND_WINDOWS
    }
    default_key = "56" if window_data.get("56", {}).get("matched", 0) else ("28" if window_data.get("28", {}).get("matched", 0) else "latest")
    default_payload = window_data.get(default_key, {})

    provider_coverage = _provider_coverage(paths, latest_market_date)
    by_unit, by_days, by_plan = _recommendation_aggregates(recs)
    unit_sorted = by_unit.sort_values(["Recommendations", "NetSignal"], ascending=[False, False]) if not by_unit.empty else by_unit

    actionable = recs[recs.get("Actionable", False).map(_boolish)].copy() if not recs.empty and "Actionable" in recs.columns else pd.DataFrame()
    rec_up = int((actionable.get("Direction", pd.Series(dtype=str)).astype(str).str.upper() == "UP").sum()) if not actionable.empty else 0
    rec_down = int((actionable.get("Direction", pd.Series(dtype=str)).astype(str).str.upper() == "DOWN").sum()) if not actionable.empty else 0

    stale_comparisons = 0
    latest_q = raw_windows.get("latest", pd.DataFrame())
    if not latest_q.empty and "FreshEnough" in latest_q.columns:
        stale_comparisons = int((~latest_q["FreshEnough"].map(_boolish)).sum())

    if not quality_exclusions.empty:
        quality_exclusions = quality_exclusions.sort_values("AbsPctChange", ascending=False)
    quality_exclusion_count = int(quality_exclusions["product_key"].astype(str).nunique()) if not quality_exclusions.empty and "product_key" in quality_exclusions.columns else int(len(quality_exclusions))

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    default_median = float(default_payload.get("median", 0.0) or 0.0)
    direction_word = "rising" if default_median > 0.005 else ("falling" if default_median < -0.005 else "broadly stable")
    direction_cls = _direction_class(default_median)
    quality_note = (
        f"{temporal_anomaly_count:,} temporal anomaly product(s) · "
        f"{cross_outlier_count:,} current cross-sectional outlier product(s) · "
        f"{stale_comparisons:,} stale latest comparison(s) excluded from trend KPIs"
    )

    timeline = _history_timeline(paths)
    dashboard_json = json.dumps({"windows": window_data, "timeline": timeline}, ensure_ascii=False).replace("</", "<\\/")

    quality_table = _table_html(
        quality_exclusions,
        [
            ("provider", "Provider", "text"), ("iso", "ISO", "text"), ("country", "Country", "text"),
            ("plan", "Plan", "text"), ("days", "Days", "dec"), ("gb", "GB", "dec"),
            ("currency", "Currency", "text"), ("PreviousPrice", "Baseline", "dec"),
            ("CurrentPrice", "Current", "dec"), ("PctChange", "Vs baseline", "pct"),
            ("HistoryObservations", "History obs", "num"), ("QualityBaseline", "Baseline method", "text"),
            ("PeerProviderCount", "Peers", "num"), ("PeerMedianChangePct", "Peer median", "pct"),
            ("AnomalyReason", "Why excluded", "text"), ("PreviousDate", "Baseline from", "date"),
            ("BaselineEndDate", "Baseline to", "date"), ("CurrentDate", "Current date", "date"),
        ],
        "quality_exclusions",
        300,
    )
    coverage_table = _table_html(
        provider_coverage,
        [("Provider","Provider","text"),("Freshness","Freshness","text"),("LatestDate","Latest date","date"),("AgeDays","Age days","num"),("LatestCountries","Countries","num"),("LatestProducts","Products","num"),("CountryDeltaPct","Country Δ","pct"),("ProductDeltaPct","Product Δ","pct"),("PreviousDate","Previous date","date")],
        "coverage",
        50,
    )
    days_table = _table_html(by_days, [("Days","Days","dec"),("Recommendations","Total","num"),("Up","Up","num"),("Down","Down","num")], "days", 30)
    plans_table = _table_html(by_plan, [("Plan","Plan","text"),("Recommendations","Total","num"),("Up","Up","num"),("Down","Down","num")], "plans", 30)
    units_table = _table_html(unit_sorted, [("PricingUnitIdUsed","Pricing unit","text"),("Recommendations","Recommendations","num"),("Up","Up","num"),("Down","Down","num"),("HighConfidence","High confidence","num"),("MedianMarketGapPct","Median market gap","pct")], "units", 100)

    template = Template(r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T-Travel Market Insights</title>
<style>
:root{--magenta:#e20074;--bg:#f4f6f8;--card:#fff;--text:#1f2933;--muted:#66727f;--green:#177a3f;--red:#b42318;--orange:#b54708;--line:#d8dee4;--dark:#20242a;}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Segoe UI,Arial,sans-serif}
header{background:var(--dark);color:#fff;padding:20px 28px;border-bottom:5px solid var(--magenta)} header h1{margin:0;font-size:25px} header .sub{margin-top:4px;color:#cbd2d9}.version{display:inline-block;margin-left:8px;padding:2px 7px;border:1px solid #65707b;border-radius:999px;font-size:11px;color:#e8edf2;vertical-align:2px}
main{max-width:1550px;margin:0 auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(5,minmax(180px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.kpi .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}.kpi .value{font-size:28px;font-weight:700;margin-top:4px}.up{color:var(--green);font-weight:600}.down{color:var(--red);font-weight:600}.neutral{color:var(--muted)}.warn{color:var(--orange);font-weight:600}
section{margin-top:18px}h2{font-size:18px;margin:0 0 10px}h3{font-size:15px;margin:0 0 8px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0 0 14px}.toolbar label{font-weight:600}.toolbar select{padding:8px 34px 8px 10px;border:1px solid var(--line);border-radius:7px;background:#fff}
.table-wrap{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:7px}table{width:100%;border-collapse:collapse;background:#fff}th{position:sticky;top:0;background:#f0f2f4;text-align:left;padding:8px;border-bottom:1px solid var(--line);font-size:12px;white-space:nowrap}td{padding:7px 8px;border-bottom:1px solid #edf0f2;white-space:nowrap}tr:hover td{background:#fafbfc}.table-search{width:100%;max-width:330px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;margin:0 0 8px}.note{color:var(--muted);font-size:12px;margin-top:8px}.empty{color:var(--muted);padding:18px 0}.method{border-left:4px solid var(--magenta);padding-left:12px}
.chart{min-height:260px}.bar-row{display:grid;grid-template-columns:minmax(95px,150px) 1fr 64px;gap:8px;align-items:center;margin:7px 0}.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}.bar-track{height:16px;background:#eef1f4;border-radius:8px;position:relative;overflow:hidden}.bar-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#b9c1c9}.bar-fill{position:absolute;top:2px;bottom:2px;border-radius:6px;background:var(--magenta)}.bar-value{text-align:right;font-variant-numeric:tabular-nums;font-size:12px}.chart-titleline{display:flex;justify-content:space-between;gap:10px;align-items:end;margin-bottom:8px}.chart-sub{font-size:12px;color:var(--muted)}
.spark-wrap{height:250px;position:relative}.spark-wrap svg{width:100%;height:100%;display:block}.axis-text{font-size:10px;fill:#74808c}.timeline-line{fill:none;stroke:var(--magenta);stroke-width:3}.timeline-area{fill:rgba(226,0,116,.08)}.timeline-dot{fill:var(--magenta)}
.dynamic-table input{margin-bottom:8px}.pill{display:inline-block;border:1px solid var(--line);background:#f7f8fa;padding:3px 7px;border-radius:999px;color:var(--muted);font-size:11px}
@media(max-width:1100px){.grid{grid-template-columns:repeat(2,1fr)}.three,.chart-grid{grid-template-columns:1fr}}@media(max-width:750px){.grid,.two,.three,.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header><h1>T-Travel Market Insights <span class="version">$dashboard_version</span></h1><div class="sub">Generated $generated · trusted SQLite market history + current pricing recommendations</div></header>
<main>
<div class="toolbar card"><label for="windowSelect">Market trend window</label><select id="windowSelect"></select><span class="pill">Changes are always measured against the latest available price</span><span class="note" style="margin:0">Use longer windows to separate a one-week shock from a sustained market move.</span></div>
<div class="grid">
 <div class="card kpi"><div class="label" id="directionLabel">Selected market direction</div><div class="value $direction_cls" id="directionValue">$direction_word</div><div class="note" id="directionNote">Median trusted material movement: $default_median</div></div>
 <div class="card kpi"><div class="label">Selected trusted moves</div><div class="value"><span class="up" id="upMoves">↑ $default_up</span> <span class="down" id="downMoves">↓ $default_down</span></div><div class="note" id="moveNote">$default_changed changed of $default_matched trusted product comparisons</div></div>
 <div class="card kpi"><div class="label">Data-quality exclusions</div><div class="value warn">$quality_exclusion_count</div><div class="note">$quality_note</div></div>
 <div class="card kpi"><div class="label">Historical observations</div><div class="value">$history_observations</div><div class="note">$history_products comparable product series · $history_min → $history_max</div></div>
 <div class="card kpi"><div class="label">Our recommendations</div><div class="value"><span class="up">↑ $rec_up</span> <span class="down">↓ $rec_down</span></div><div class="note">Exact-duration anchors only</div></div>
</div>

<section class="card method"><h2>Historical quality guard</h2><div>The primary anomaly check compares each current competitor price with the median of its prior <b>$baseline_days days</b> of history, requiring at least <b>$baseline_min observations</b>. This keeps a repeated bad scrape suspicious even if the immediately previous snapshot is already wrong. Where there is not enough history, the engine falls back to latest-vs-previous. Extreme moves of ≥+$extreme_up or ≤$extreme_down are excluded unless peer providers corroborate the direction <b>or</b> the same provider shows a broad same-direction repricing across at least $broad_moves products in $broad_countries countries. Current cross-sectional outliers remain a separate exclusion gate.</div><div class="note">Raw observations are never deleted. The selectable market windows below are for analysis; the 8-week historical median guard is the data-quality baseline used by recommendations.</div></section>

<section class="chart-grid">
 <div class="card chart"><div class="chart-titleline"><div><h2>Market movement by time window</h2><div class="chart-sub">Median material price change</div></div></div><div id="windowChart"></div></div>
 <div class="card chart"><div class="chart-titleline"><div><h2>History coverage</h2><div class="chart-sub">Stored competitor observations per snapshot date</div></div></div><div class="spark-wrap" id="timelineChart"></div></div>
</section>

<section class="chart-grid">
 <div class="card chart"><div class="chart-titleline"><div><h2>Largest country movements</h2><div class="chart-sub" id="countryChartSub"></div></div></div><div id="countryChart"></div></div>
 <div class="card chart"><div class="chart-titleline"><div><h2>Largest provider movements</h2><div class="chart-sub" id="providerChartSub"></div></div></div><div id="providerChart"></div></div>
</section>

<section class="two">
 <div class="card dynamic-table"><h2>Countries with rising competitor prices</h2><input class="table-search" id="risingSearch" type="search" placeholder="Filter table…"><div class="table-wrap"><table><thead><tr><th>ISO</th><th>Country</th><th>Median change</th><th>Breadth</th><th>Changed</th><th>Providers</th><th>Latest data</th><th>Baseline data</th></tr></thead><tbody id="risingBody"></tbody></table></div><div class="note">Breadth = (products up − products down) / changed products.</div></div>
 <div class="card dynamic-table"><h2>Countries with falling competitor prices</h2><input class="table-search" id="fallingSearch" type="search" placeholder="Filter table…"><div class="table-wrap"><table><thead><tr><th>ISO</th><th>Country</th><th>Median change</th><th>Breadth</th><th>Changed</th><th>Providers</th><th>Latest data</th><th>Baseline data</th></tr></thead><tbody id="fallingBody"></tbody></table></div><div class="note">Only trusted, fresh observations contribute.</div></div>
</section>

<section class="three">
 <div class="card dynamic-table"><h2>Competitor movement</h2><input class="table-search" id="providerSearch" type="search" placeholder="Filter provider…"><div class="table-wrap"><table><thead><tr><th>Provider</th><th>Median change</th><th>Breadth</th><th>Changed</th><th>Up</th><th>Down</th><th>Latest data</th></tr></thead><tbody id="providerBody"></tbody></table></div></div>
 <div class="card"><h2>Recommendations by duration</h2>$days_table</div>
 <div class="card"><h2>Recommendations by plan</h2>$plans_table</div>
</section>

<section class="card dynamic-table"><h2>Largest trusted competitor moves</h2><input class="table-search" id="movesSearch" type="search" placeholder="Filter move…"><div class="table-wrap"><table><thead><tr><th>Provider</th><th>ISO</th><th>Country</th><th>Plan</th><th>Days</th><th>GB</th><th>Previous</th><th>Current</th><th>Change</th><th>Previous date</th><th>Current date</th></tr></thead><tbody id="movesBody"></tbody></table></div><div class="note">The table follows the selected time window. Product identity is provider + market + plan + duration + allowance + currency.</div></section>

<section class="card"><h2>Data quality — excluded suspicious current prices</h2>$quality_table<div class="note">For historical-median rows, “Baseline” is the median of prior observations, not a single snapshot. These rows remain in SQLite for audit/review and are excluded only from trusted analytics/recommendations.</div></section>
<section class="card"><h2>Provider coverage & freshness</h2>$coverage_table<div class="note">Freshness is measured against the newest observation date in the DB: current ≤7 days, watch 8–14 days, stale &gt;14 days. Coverage deltas can reveal partial/failed scrapes.</div></section>
<section class="card"><h2>Pricing units with the most signals</h2>$units_table<div class="note">Recommendation signals use the current quality-filtered market and are separate from the historical trend window selected above.</div></section>
<section class="card"><h2>History database</h2><div class="note">Local SQLite DB: $history_db<br>Coverage: $history_min to $history_max · $history_providers providers · $history_countries countries · $history_products comparable product series.<br>Current cross-sectional outlier audit: $audit_rows row(s). On this refresh, $imported source file(s) were new/changed and $skipped were already indexed.</div></section>
</main>
<script id="dashboardData" type="application/json">$dashboard_json</script>
<script>
const dashboard=JSON.parse(document.getElementById('dashboardData').textContent);const windows=dashboard.windows||{};const selector=document.getElementById('windowSelect');
function pct(v,d=1){if(v===null||v===undefined||!isFinite(Number(v)))return '—';return (Number(v)*100).toFixed(d)+'%'}function n(v){if(v===null||v===undefined||v==='')return '—';const x=Number(v);return isFinite(x)?x.toLocaleString(undefined,{maximumFractionDigits:2}):String(v)}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
Object.entries(windows).forEach(([key,w])=>{const o=document.createElement('option');o.value=key;o.textContent=w.label;selector.appendChild(o)});selector.value='$default_key';
function barChart(id,rows,labelKey,valueKey,maxRows=12){const el=document.getElementById(id);el.innerHTML='';const vals=(rows||[]).filter(r=>isFinite(Number(r[valueKey]))&&Math.abs(Number(r[valueKey]))>=0.0001).sort((a,b)=>Math.abs(Number(b[valueKey]))-Math.abs(Number(a[valueKey]))).slice(0,maxRows);if(!vals.length){el.innerHTML='<div class="empty">No material movement in this view.</div>';return}const max=Math.max(...vals.map(r=>Math.abs(Number(r[valueKey]))),0.01);vals.forEach(r=>{const v=Number(r[valueKey]);const row=document.createElement('div');row.className='bar-row';const label=document.createElement('div');label.className='bar-label';label.title=String(r[labelKey]??'');label.textContent=String(r[labelKey]??'');const track=document.createElement('div');track.className='bar-track';const zero=document.createElement('div');zero.className='bar-zero';track.appendChild(zero);const fill=document.createElement('div');fill.className='bar-fill';const width=Math.min(49,Math.abs(v)/max*49);fill.style.width=width+'%';fill.style.left=(v>=0?50:50-width)+'%';fill.style.background=v>=0?'var(--green)':'var(--red)';track.appendChild(fill);const value=document.createElement('div');value.className='bar-value '+(v>=0?'up':'down');value.textContent=pct(v);row.append(label,track,value);el.appendChild(row)})}
function renderWindowChart(){const rows=Object.values(windows).map(w=>({Label:w.label,Median:w.median}));barChart('windowChart',rows,'Label','Median',10)}
function renderTimeline(){const el=document.getElementById('timelineChart');const data=dashboard.timeline||[];if(data.length<2){el.innerHTML='<div class="empty">More snapshot dates are needed for a history chart.</div>';return}const W=720,H=220,p=28;const ys=data.map(d=>Number(d.Observations)||0);const max=Math.max(...ys,1),min=Math.min(...ys,0);const range=Math.max(max-min,1);const pts=data.map((d,i)=>{const x=p+(W-2*p)*(i/(data.length-1));const y=H-p-(H-2*p)*((ys[i]-min)/range);return [x,y]});const line=pts.map((q,i)=>(i?'L':'M')+q[0].toFixed(1)+','+q[1].toFixed(1)).join(' ');const area='M'+pts[0][0]+','+(H-p)+' '+pts.map(q=>'L'+q[0].toFixed(1)+','+q[1].toFixed(1)).join(' ')+' L'+pts[pts.length-1][0]+','+(H-p)+' Z';const first=esc(data[0].Date),last=esc(data[data.length-1].Date);el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none"><path class="timeline-area" d="'+area+'"></path><path class="timeline-line" d="'+line+'"></path><text class="axis-text" x="'+p+'" y="'+(H-8)+'">'+first+'</text><text class="axis-text" text-anchor="end" x="'+(W-p)+'" y="'+(H-8)+'">'+last+'</text><text class="axis-text" x="4" y="'+(p+4)+'">'+max.toLocaleString()+'</text><text class="axis-text" x="4" y="'+(H-p)+'">'+min.toLocaleString()+'</text></svg>'}
function tableRows(id,rows,cols,searchId,limit=120){const body=document.getElementById(id);const q=(document.getElementById(searchId)?.value||'').toLowerCase();body.innerHTML='';(rows||[]).filter(r=>JSON.stringify(r).toLowerCase().includes(q)).slice(0,limit).forEach(r=>{const tr=document.createElement('tr');cols.forEach(c=>{const td=document.createElement('td');let v=r[c[0]];td.textContent=c[1]==='pct'?pct(v):c[1]==='num'?n(v):(v??'—');if(c[1]==='pct'&&isFinite(Number(v)))td.className=Number(v)>0.005?'up':(Number(v)<-0.005?'down':'neutral');tr.appendChild(td)});body.appendChild(tr)});if(!body.children.length)body.innerHTML='<tr><td colspan="12" class="neutral">No rows for this view/filter.</td></tr>'}
function renderSelected(){const w=windows[selector.value];if(!w)return;const med=Number(w.median)||0;document.getElementById('directionLabel').textContent=w.label+' market direction';const dv=document.getElementById('directionValue');dv.textContent=med>0.005?'Rising':(med<-0.005?'Falling':'Broadly stable');dv.className='value '+(med>0.005?'up':(med<-0.005?'down':'neutral'));document.getElementById('directionNote').textContent='Median trusted material movement: '+pct(med);document.getElementById('upMoves').textContent='↑ '+Number(w.up||0).toLocaleString();document.getElementById('downMoves').textContent='↓ '+Number(w.down||0).toLocaleString();document.getElementById('moveNote').textContent=Number(w.changed||0).toLocaleString()+' changed of '+Number(w.matched||0).toLocaleString()+' trusted product comparisons';const countries=w.countries||[];const rising=countries.filter(r=>Number(r.MedianChangePct)>0).sort((a,b)=>Number(b.MedianChangePct)-Number(a.MedianChangePct));const falling=countries.filter(r=>Number(r.MedianChangePct)<0).sort((a,b)=>Number(a.MedianChangePct)-Number(b.MedianChangePct));barChart('countryChart',countries.map(r=>({...r,Label:(r.iso||'')+' '+(r.country||'')})),'Label','MedianChangePct',12);barChart('providerChart',w.providers||[],'Provider','MedianChangePct',12);document.getElementById('countryChartSub').textContent=w.label+' · absolute largest median moves';document.getElementById('providerChartSub').textContent=w.label+' · absolute largest median moves';const ccols=[['iso','text'],['country','text'],['MedianChangePct','pct'],['BreadthPct','pct'],['ChangedProducts','num'],['ProviderCount','num'],['LatestDataDate','text'],['PreviousDataDate','text']];tableRows('risingBody',rising,ccols,'risingSearch',120);tableRows('fallingBody',falling,ccols,'fallingSearch',120);tableRows('providerBody',w.providers||[],[['Provider','text'],['MedianChangePct','pct'],['BreadthPct','pct'],['ChangedProducts','num'],['UpProducts','num'],['DownProducts','num'],['LatestDataDate','text']],'providerSearch',120);tableRows('movesBody',w.moves||[],[['provider','text'],['iso','text'],['country','text'],['plan','text'],['days','num'],['gb','num'],['PreviousPrice','num'],['CurrentPrice','num'],['PctChange','pct'],['PreviousDate','text'],['CurrentDate','text']],'movesSearch',250)}
selector.addEventListener('change',renderSelected);['risingSearch','fallingSearch','providerSearch','movesSearch'].forEach(id=>document.getElementById(id).addEventListener('input',renderSelected));renderWindowChart();renderTimeline();renderSelected();
function filterTable(id,q){q=q.toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(r=>{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'})}
</script>
</body></html>''')

    html = template.safe_substitute(
        generated=escape(generated), dashboard_version=escape(DASHBOARD_VERSION), direction_cls=direction_cls,
        direction_word=escape(direction_word.title()), default_median=_fmt_pct(default_median),
        default_up=f"{int(default_payload.get('up', 0)):,}", default_down=f"{int(default_payload.get('down', 0)):,}",
        default_changed=f"{int(default_payload.get('changed', 0)):,}", default_matched=f"{int(default_payload.get('matched', 0)):,}",
        quality_exclusion_count=f"{quality_exclusion_count:,}", quality_note=escape(quality_note),
        history_observations=f"{int(history.get('observations', 0)):,}", history_products=f"{int(history.get('products', 0)):,}",
        history_min=escape(history.get('date_min') or '—'), history_max=escape(history.get('date_max') or '—'),
        rec_up=f"{rec_up:,}", rec_down=f"{rec_down:,}", baseline_days=HISTORICAL_BASELINE_DAYS,
        baseline_min=HISTORICAL_MIN_OBSERVATIONS, extreme_up=f"{EXTREME_UP_PCT:.0%}", extreme_down=f"{abs(EXTREME_DOWN_PCT):.0%}", broad_moves=MIN_PROVIDER_BROAD_MOVES, broad_countries=MIN_PROVIDER_BROAD_COUNTRIES,
        days_table=days_table, plans_table=plans_table, quality_table=quality_table, coverage_table=coverage_table, units_table=units_table,
        history_db=escape(str(paths.market_history_db)), history_providers=f"{int(history.get('providers', 0)):,}", history_countries=f"{int(history.get('countries', 0)):,}",
        audit_rows=f"{len(current_outlier_audit):,}", imported=history_stats.imported_files, skipped=history_stats.skipped_files,
        dashboard_json=dashboard_json, default_key=default_key,
    )
    output_path.write_text(html, encoding="utf-8")
    print(f"Market insights generator: {DASHBOARD_VERSION} ({Path(__file__).resolve()})")
    print(f"Saved: {output_path}")
    return output_path


if __name__ == "__main__":
    generate_market_insights()
