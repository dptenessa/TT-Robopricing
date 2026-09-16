from __future__ import annotations

from datetime import datetime
from html import escape
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
MAX_CURRENT_AGE_DAYS = 14   # old observations are not "latest" market movement


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

        corroborated = confirming >= MIN_CONFIRMING_PEERS or median_confirms
        df.at[idx, "PeerProviderCount"] = peer_count
        df.at[idx, "ConfirmingPeerCount"] = confirming
        df.at[idx, "PeerMedianChangePct"] = peer_median if math.isfinite(peer_median) else pd.NA
        if not corroborated:
            df.at[idx, "TemporalAnomaly"] = True
            if peer_count:
                df.at[idx, "AnomalyReason"] = (
                    f"extreme uncorroborated move; {peer_count} peer provider(s), "
                    f"peer median {peer_median:+.1%}"
                )
            else:
                df.at[idx, "AnomalyReason"] = "extreme move with no peer-provider corroboration"

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
    output_mtime = output_path.stat().st_mtime
    for source in (recommendations_path, paths.market_history_db, paths.market_outlier_audit):
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
    raw_latest = latest_product_changes(paths.market_history_db, material_threshold=MIN_MATERIAL_CHANGE_PCT)
    raw_four_week = window_product_changes(paths.market_history_db, days_back=28, material_threshold=MIN_MATERIAL_CHANGE_PCT)
    current_outlier_keys, current_outlier_audit = _current_cross_sectional_outlier_keys(paths)

    latest_q = _annotate_temporal_quality(
        raw_latest,
        current_outlier_keys=current_outlier_keys,
        latest_market_date=latest_market_date,
    )
    four_week_q = _annotate_temporal_quality(
        raw_four_week,
        current_outlier_keys=current_outlier_keys,
        latest_market_date=latest_market_date,
    )
    latest = latest_q[latest_q.get("Trusted", True)].copy() if not latest_q.empty else latest_q
    four_week = four_week_q[four_week_q.get("Trusted", True)].copy() if not four_week_q.empty else four_week_q

    countries = _merge_country_trends(latest, four_week)
    providers = _provider_trends(latest, four_week)
    provider_coverage = _provider_coverage(paths, latest_market_date)
    by_unit, by_days, by_plan = _recommendation_aggregates(recs)

    latest_material = latest[latest["Material"]].copy() if not latest.empty else pd.DataFrame()
    latest_matched = int(len(latest))
    latest_changed = int(len(latest_material))
    latest_up = int((latest_material["Direction"] == "UP").sum()) if not latest_material.empty else 0
    latest_down = int((latest_material["Direction"] == "DOWN").sum()) if not latest_material.empty else 0
    latest_median = _material_median(latest)
    four_week_median = _material_median(four_week)

    temporal_anomalies = latest_q[latest_q.get("TemporalAnomaly", False)].copy() if not latest_q.empty else pd.DataFrame()
    cross_outlier_count = len(current_outlier_keys)
    temporal_anomaly_count = int(len(temporal_anomalies))
    stale_comparisons = int((~latest_q.get("FreshEnough", pd.Series(True, index=latest_q.index))).sum()) if not latest_q.empty else 0

    anomaly_rows = latest_q[
        latest_q.get("TemporalAnomaly", False) | latest_q.get("CurrentMarketOutlier", False)
    ].copy() if not latest_q.empty else pd.DataFrame()
    if not anomaly_rows.empty:
        anomaly_rows = anomaly_rows.sort_values("AbsPctChange", ascending=False)
    # A product may be both a temporal anomaly and a current cross-sectional
    # outlier.  Count it once in the headline KPI while still reporting the two
    # diagnostic categories separately below.
    quality_exclusion_count = int(
        len(anomaly_rows.drop_duplicates(subset=["product_key"]))
        if not anomaly_rows.empty and "product_key" in anomaly_rows.columns
        else len(anomaly_rows)
    )

    actionable = recs[recs.get("Actionable", False).map(_boolish)].copy() if not recs.empty and "Actionable" in recs.columns else pd.DataFrame()
    rec_up = int((actionable.get("Direction", pd.Series(dtype=str)).astype(str).str.upper() == "UP").sum()) if not actionable.empty else 0
    rec_down = int((actionable.get("Direction", pd.Series(dtype=str)).astype(str).str.upper() == "DOWN").sum()) if not actionable.empty else 0

    if not countries.empty:
        evidence = (countries["FourWeekChangedProducts"] >= 2) | (countries["LatestChangedProducts"] >= 2)
        rising = countries[(countries["TrendChangePct"] > 0) & evidence].copy()
        falling = countries[(countries["TrendChangePct"] < 0) & evidence].copy()
        rising = rising.sort_values(["TrendChangePct", "FourWeekChangedProducts", "LatestChangedProducts"], ascending=[False, False, False])
        falling = falling.sort_values(["TrendChangePct", "FourWeekChangedProducts", "LatestChangedProducts"], ascending=[True, False, False])
    else:
        rising = falling = pd.DataFrame()

    provider_sorted = providers.copy()
    if not provider_sorted.empty:
        sort_col = "FourWeekChangedProducts" if "FourWeekChangedProducts" in provider_sorted.columns else "LatestChangedProducts"
        provider_sorted = provider_sorted.sort_values(sort_col, ascending=False)
    unit_sorted = by_unit.sort_values(["Recommendations", "NetSignal"], ascending=[False, False]) if not by_unit.empty else by_unit

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    direction_basis = four_week_median if not four_week.empty else latest_median
    direction_word = "rising" if direction_basis > 0.005 else ("falling" if direction_basis < -0.005 else "broadly stable")
    direction_cls = _direction_class(direction_basis)

    quality_note = (
        f"{temporal_anomaly_count:,} extreme uncorroborated latest move(s) · "
        f"{cross_outlier_count:,} current cross-sectional outlier product(s) · "
        f"{stale_comparisons:,} stale latest comparison(s) excluded from current trend KPIs"
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T-Travel Market Insights</title>
<style>
:root{{--magenta:#e20074;--bg:#f4f6f8;--card:#fff;--text:#1f2933;--muted:#66727f;--green:#177a3f;--red:#b42318;--orange:#b54708;--line:#d8dee4;}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Segoe UI,Arial,sans-serif}}
header{{background:#20242a;color:#fff;padding:20px 28px;border-bottom:5px solid var(--magenta)}}
header h1{{margin:0;font-size:25px}} header .sub{{margin-top:4px;color:#cbd2d9}}
main{{max-width:1550px;margin:0 auto;padding:20px}} .grid{{display:grid;grid-template-columns:repeat(5,minmax(180px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}}
.kpi .label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}} .kpi .value{{font-size:28px;font-weight:700;margin-top:4px}}
.up{{color:var(--green);font-weight:600}} .down{{color:var(--red);font-weight:600}} .neutral{{color:var(--muted)}} .warn{{color:var(--orange);font-weight:600}}
section{{margin-top:18px}} h2{{font-size:18px;margin:0 0 10px}} h3{{font-size:15px;margin:0 0 8px}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .three{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}}
.table-wrap{{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:7px}} table{{width:100%;border-collapse:collapse;background:#fff}}
th{{position:sticky;top:0;background:#f0f2f4;text-align:left;padding:8px;border-bottom:1px solid var(--line);font-size:12px;white-space:nowrap}}
td{{padding:7px 8px;border-bottom:1px solid #edf0f2;white-space:nowrap}} tr:hover td{{background:#fafbfc}}
.table-search{{width:100%;max-width:330px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;margin:0 0 8px}}
.note{{color:var(--muted);font-size:12px;margin-top:8px}} .empty{{color:var(--muted);padding:18px 0}}
.method{{border-left:4px solid var(--magenta);padding-left:12px}}
@media(max-width:1100px){{.grid{{grid-template-columns:repeat(2,1fr)}} .three{{grid-template-columns:1fr}}}}
@media(max-width:750px){{.grid,.two,.three{{grid-template-columns:1fr}}}}
</style>
<script>
function filterTable(id,q){{q=q.toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(r=>{{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';}})}}
</script>
</head>
<body>
<header><h1>T-Travel Market Insights</h1><div class="sub">Generated {escape(generated)} · trusted SQLite market history + current pricing recommendations</div></header>
<main>
<div class="grid">
 <div class="card kpi"><div class="label">4-week market direction</div><div class="value {direction_cls}">{escape(direction_word.title())}</div><div class="note">Median trusted material movement: {_fmt_pct(four_week_median if not four_week.empty else latest_median)}</div></div>
 <div class="card kpi"><div class="label">Latest trusted moves</div><div class="value"><span class="up">↑ {latest_up:,}</span> <span class="down">↓ {latest_down:,}</span></div><div class="note">{latest_changed:,} changed of {latest_matched:,} fresh/trusted product comparisons</div></div>
 <div class="card kpi"><div class="label">Data-quality exclusions</div><div class="value warn">{quality_exclusion_count:,}</div><div class="note">{escape(quality_note)}</div></div>
 <div class="card kpi"><div class="label">Historical observations</div><div class="value">{history['observations']:,}</div><div class="note">{history['products']:,} comparable product series · {history['date_min'] or '—'} → {history['date_max'] or '—'}</div></div>
 <div class="card kpi"><div class="label">Our recommendations</div><div class="value"><span class="up">↑ {rec_up:,}</span> <span class="down">↓ {rec_down:,}</span></div><div class="note">Exact-duration anchors only</div></div>
</div>

<section class="card method"><h2>How this report and recommendation engine now protect against bad market data</h2>
<div>Raw observations are always retained in SQLite. Headline market trends exclude (1) current cross-sectional outliers, (2) extreme uncorroborated temporal jumps of ≥+{EXTREME_UP_PCT:.0%} or ≤{EXTREME_DOWN_PCT:.0%}, and (3) observations older than {MAX_CURRENT_AGE_DAYS} days relative to the freshest market snapshot. Very large moves are retained when at least {MIN_CONFIRMING_PEERS} peer providers corroborate the direction.</div>
<div class="note">Recommendations use exact duration only. Capped plans use 80% price/GB + 20% total package price; Unlimited uses total price only. If fewer than two exact-duration providers are available, no market-driven recommendation is produced.</div></section>

<section class="two">
 <div class="card"><h2>Countries with rising competitor prices</h2>{_table_html(rising, [("iso","ISO","text"),("country","Country","text"),("TrendChangePct","Trend","pct"),("TrendBreadthPct","Breadth","pct"),("TrendWindow","Window","text"),("LatestProviders","Latest providers","num"),("LatestChangedProducts","Latest changed","num"),("FourWeekChangedProducts","4-week changed","num"),("LatestDataDate","Latest data","date")], "rising", 80)}<div class="note">Breadth = (products up − products down) / changed products. +100% means all changed products moved up.</div></div>
 <div class="card"><h2>Countries with falling competitor prices</h2>{_table_html(falling, [("iso","ISO","text"),("country","Country","text"),("TrendChangePct","Trend","pct"),("TrendBreadthPct","Breadth","pct"),("TrendWindow","Window","text"),("LatestProviders","Latest providers","num"),("LatestChangedProducts","Latest changed","num"),("FourWeekChangedProducts","4-week changed","num"),("LatestDataDate","Latest data","date")], "falling", 80)}<div class="note">Only trusted/fresh observations contribute to the trend tables.</div></div>
</section>

<section class="three">
 <div class="card"><h2>Competitor movement</h2>{_table_html(provider_sorted, [("Provider","Provider","text"),("LatestMedianChangePct","Latest","pct"),("LatestBreadthPct","Latest breadth","pct"),("FourWeekMedianChangePct","4-week","pct"),("FourWeekBreadthPct","4-week breadth","pct"),("LatestChangedProducts","Latest changed","num"),("LatestDataDate","Latest data","date")], "providers", 40)}</div>
 <div class="card"><h2>Recommendations by duration</h2>{_table_html(by_days, [("Days","Days","dec"),("Recommendations","Total","num"),("Up","Up","num"),("Down","Down","num")], "days", 30)}</div>
 <div class="card"><h2>Recommendations by plan</h2>{_table_html(by_plan, [("Plan","Plan","text"),("Recommendations","Total","num"),("Up","Up","num"),("Down","Down","num")], "plans", 30)}</div>
</section>

<section class="card"><h2>Provider coverage & freshness</h2>{_table_html(provider_coverage, [("Provider","Provider","text"),("Freshness","Freshness","text"),("LatestDate","Latest date","date"),("AgeDays","Age days","num"),("LatestCountries","Countries","num"),("LatestProducts","Products","num"),("CountryDeltaPct","Country Δ","pct"),("ProductDeltaPct","Product Δ","pct"),("PreviousDate","Previous date","date")], "coverage", 50)}<div class="note">Freshness is measured against the newest observation date in the DB: current ≤7 days, watch 8–14 days, stale &gt;14 days. Coverage deltas help reveal partial/failed scrapes.</div></section>

<section class="card"><h2>Data quality — excluded suspicious movements</h2>{_table_html(anomaly_rows, [("provider","Provider","text"),("iso","ISO","text"),("country","Country","text"),("plan","Plan","text"),("days","Days","dec"),("gb","GB","dec"),("currency","Currency","text"),("PreviousPrice","Previous","dec"),("CurrentPrice","Current","dec"),("PctChange","Change","pct"),("PeerProviderCount","Peers","num"),("PeerMedianChangePct","Peer median","pct"),("AnomalyReason","Why excluded","text"),("PreviousDate","Previous date","date"),("CurrentDate","Current date","date")], "anomalies", 250)}<div class="note">These rows remain in SQLite for audit/review; they are excluded only from trusted trend calculations. Cross-sectional outliers are also excluded from recommendations.</div></section>

<section class="card"><h2>Largest trusted latest competitor moves</h2>{_table_html(latest_material.sort_values("AbsPctChange", ascending=False) if not latest_material.empty else latest_material, [("provider","Provider","text"),("iso","ISO","text"),("country","Country","text"),("plan","Plan","text"),("days","Days","dec"),("gb","GB","dec"),("currency","Currency","text"),("PreviousPrice","Previous","dec"),("CurrentPrice","Current","dec"),("PctChange","Change","pct"),("PreviousDate","Previous date","date"),("CurrentDate","Current date","date")], "moves", 250)}<div class="note">Stable commercial-product matching uses provider + market + plan + duration + allowance + currency. A ±{MIN_MATERIAL_CHANGE_PCT*100:.0f}% threshold defines a material move.</div></section>

<section class="card"><h2>Pricing units with the most signals</h2>{_table_html(unit_sorted, [("PricingUnitIdUsed","Pricing unit","text"),("Recommendations","Recommendations","num"),("Up","Up","num"),("Down","Down","num"),("HighConfidence","High confidence","num"),("MedianMarketGapPct","Median market gap","pct")], "units", 100)}<div class="note">Recommendation signals use the current cross-sectional quality-filtered market and are separate from historical movement.</div></section>

<section class="card"><h2>History database</h2><div class="note">Local SQLite DB: {escape(str(paths.market_history_db))}<br>Coverage: {escape(history['date_min'] or '—')} to {escape(history['date_max'] or '—')} · {history['providers']:,} providers · {history['countries']:,} countries · {history['products']:,} comparable product series.<br>Current cross-sectional outlier audit: {len(current_outlier_audit):,} row(s). On this refresh, {history_stats.imported_files} source file(s) were new/changed and {history_stats.skipped_files} were already indexed.</div></section>
</main></body></html>"""
    output_path.write_text(html, encoding="utf-8")
    print(f"Saved: {output_path}")
    return output_path


if __name__ == "__main__":
    generate_market_insights()
