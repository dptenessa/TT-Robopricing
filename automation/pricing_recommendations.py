from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    from pricing_book import read_pricing_workbook
except ImportError:
    from automation.pricing_book import read_pricing_workbook

try:
    from currency_support import (
        CURRENCIES,
        DEFAULT_EUR_TO_USD,
        add_currency_price_columns,
        normalize_currency,
    )
except ImportError:
    from automation.currency_support import (
        CURRENCIES,
        DEFAULT_EUR_TO_USD,
        add_currency_price_columns,
        normalize_currency,
    )

try:
    from pipeline_files import FILES, PipelineFiles
except ImportError:
    from automation.pipeline_files import FILES, PipelineFiles


ANCHOR_DAYS = (1, 3, 7, 10, 15, 30)


@dataclass(frozen=True)
class RecommendationConfig:
    anchor_days: tuple[int, ...] = ANCHOR_DAYS
    min_provider_neighbors: int = 2
    max_provider_neighbors: int = 5
    max_distance: float = 0.80
    days_weight: float = 1.8
    gb_weight: float = 1.0
    # We do not chase the absolute market median. A point is considered only
    # when its market position differs materially from its neighboring anchors.
    local_position_deadband_pct: float = 0.12
    market_sanity_deadband_pct: float = 0.05
    portfolio_contradiction_pct: float = 0.08
    metric_agreement_band_pct: float = 0.05
    move_fraction_toward_target: float = 0.35
    min_abs_move: float = 0.10
    max_abs_move: float = 0.50
    max_move_pct: float = 0.05
    price_step: float = 0.05
    ppg_weight_capped: float = 0.80
    ppg_weight_unlimited: float = 0.0
    capped_gb_ratio_min: float = 0.50
    capped_gb_ratio_max: float = 2.00
    min_multi_country_market_coverage: float = 0.50
    max_required_market_countries: int = 8
    require_full_coverage_up_to_countries: int = 5


DEFAULT_CONFIG = RecommendationConfig()


@dataclass
class MarketReference:
    provider_count: int = 0
    neighbor_count: int = 0
    country_count: int = 0
    exact_day_provider_count: int = 0
    median_price: float | None = None
    median_ppg: float | None = None
    target_price: float | None = None
    providers: str = ""
    priority_country: str = ""
    secondary_country_count: int = 0
    secondary_provider_count: int = 0
    secondary_target_price: float | None = None
    secondary_providers: str = ""
    priority_country_missing: bool = False
    match_mode: str = ""
    duration_min: float | None = None
    duration_max: float | None = None


@dataclass
class MarketCountryData:
    provider: np.ndarray
    plan_nonblank: np.ndarray
    is_unlimited: np.ndarray
    days: np.ndarray
    gb: np.ndarray
    price_eur: np.ndarray
    price_usd: np.ndarray


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() in {
        "true", "t", "yes", "y", "1"
    }


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text


def _number(value: Any) -> float | None:
    value = pd.to_numeric(value, errors="coerce")
    if pd.isna(value):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _round_step(value: float, step: float = 0.05) -> float:
    if step <= 0:
        return float(value)
    return round(float(value) / step) * step


def _floor_step(value: float, step: float = 0.05) -> float:
    if step <= 0:
        return float(value)
    return math.floor((float(value) + 1e-12) / step) * step


def _ceil_step(value: float, step: float = 0.05) -> float:
    if step <= 0:
        return float(value)
    return math.ceil((float(value) - 1e-12) / step) * step


def _is_unlimited(plan: Any) -> bool:
    return "unlimited" in _text(plan).lower()


def _parse_country_codes(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return sorted({str(x).strip().upper() for x in parsed if str(x).strip()})
    except Exception:
        pass

    normalized = text
    for sep in (";", "/", "|"):
        normalized = normalized.replace(sep, ",")
    normalized = normalized.replace("[", "").replace("]", "")
    normalized = normalized.replace('"', "").replace("'", "")
    return sorted({part.strip().upper() for part in normalized.split(",") if part.strip()})


def _iso3_to_iso2(value: Any) -> str:
    code = _text(value).upper()
    if len(code) == 2:
        return code
    if len(code) != 3:
        return code
    try:
        import pycountry  # type: ignore

        country = pycountry.countries.get(alpha_3=code)
        return str(country.alpha_2).upper() if country else code
    except Exception:
        return code


def _infer_eur_to_usd(raw_market: pd.DataFrame, fallback: float = DEFAULT_EUR_TO_USD) -> float:
    lower = {str(c).strip().lower(): c for c in raw_market.columns}
    usd_col = lower.get("price_usd") or lower.get("usd_price")
    eur_col = lower.get("price_eur") or lower.get("eur_price")
    if usd_col is not None and eur_col is not None:
        usd = pd.to_numeric(raw_market[usd_col], errors="coerce")
        eur = pd.to_numeric(raw_market[eur_col], errors="coerce")
        ratio = (usd / eur).replace([np.inf, -np.inf], np.nan).dropna()
        ratio = ratio[(ratio > 0.5) & (ratio < 2.0)]
        if len(ratio) >= 5:
            return float(ratio.median())
    return float(fallback)


def _load_promos(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        code = _text(item.get("promo_code"))
        ptype = _text(item.get("promo_type")).lower()
        if ptype in {"percentage", "%"}:
            ptype = "percent"
        if ptype not in {"percent", "absolute"}:
            continue
        value = _number(item.get("promo_value"))
        if not code or value is None:
            continue
        out.append({
            "promo_code": code,
            "promo_type": ptype,
            "promo_value": float(value),
            "label": _text(item.get("label")) or code,
        })
    return out


def _load_priority_countries(path: str | Path | None) -> dict[str, str]:
    """Load explicit priority countries for shared pricing units.

    A multi-country pricing unit without ``priority_country`` is intentionally
    left without recommendations. This is safer than silently reverting to an
    equal-weight country average.
    """
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read pricing units JSON: {path}") from exc

    if not isinstance(data, list):
        raise ValueError(f"pricing_units.json must contain a list: {path}")

    priorities: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        unit_id = _text(item.get("pricing_unit_id"))
        covered = {str(x).strip().upper() for x in item.get("area_covered", []) if str(x).strip()}
        priority = _text(item.get("priority_country")).upper()
        if not unit_id or not priority:
            continue
        if covered and priority not in covered:
            raise ValueError(
                f"priority_country {priority!r} is not in area_covered for pricing unit {unit_id!r}"
            )
        priorities[unit_id] = priority
    return priorities


def _promo_final(list_price: float, promo_type: str, promo_value: float, step: float) -> float:
    promo_type = _text(promo_type).lower()
    if promo_type in {"percentage", "%"}:
        promo_type = "percent"
    if promo_type == "percent":
        raw = float(list_price) * (1.0 - float(promo_value) / 100.0)
    elif promo_type == "absolute":
        raw = float(list_price) - float(promo_value)
    else:
        raw = float(list_price)
    return max(_floor_step(raw, step), 0.0)


def _list_price_for_target_net(
    target_net: float,
    promo_type: str,
    promo_value: float,
    step: float,
) -> float:
    promo_type = _text(promo_type).lower()
    if promo_type in {"percentage", "%"}:
        promo_type = "percent"
    if promo_type == "percent" and float(promo_value) < 100:
        raw = float(target_net) / (1.0 - float(promo_value) / 100.0)
    elif promo_type == "absolute":
        raw = float(target_net) + float(promo_value)
    else:
        raw = float(target_net)
    return max(_round_step(raw, step), 0.0)



def _exclude_temporal_market_anomalies(
    raw_market: pd.DataFrame,
    paths: PipelineFiles = FILES,
) -> tuple[pd.DataFrame, int]:
    """Exclude current competitor rows rejected by the shared history guard.

    The shared quality classifier prefers a 56-day historical median with at
    least three prior observations and falls back to latest-vs-previous only
    when the product series is still too sparse.  Raw/history data is retained;
    only the in-memory recommendation input is filtered.
    """
    if raw_market.empty:
        return raw_market, 0

    try:
        try:
            import market_insights as mi
            from market_history import stable_product_key
        except ImportError:
            from automation import market_insights as mi
            from automation.market_history import stable_product_key

        quality = mi.current_quality_exclusions(paths)
        if quality.empty or "QualityExcluded" not in quality.columns:
            return raw_market, 0

        suspicious = quality[quality["QualityExcluded"].map(_boolish)].copy()
        if suspicious.empty:
            return raw_market, 0

        suspicious_keys = set(suspicious.get("product_key", pd.Series(dtype=str)).astype(str))
        suspicious_keys.discard("")
        if not suspicious_keys:
            return raw_market, 0

        work = raw_market.copy()
        for col, default in (
            ("Provider", ""), ("ISO", ""), ("ISO3", ""), ("Plan", ""),
            ("Days", pd.NA), ("GB", pd.NA), ("Currency", "USD"),
        ):
            if col not in work.columns:
                work[col] = default

        def row_key(row: pd.Series) -> str:
            iso = _text(row.get("ISO")) or _text(row.get("ISO3"))
            return stable_product_key(
                row.get("Provider"), iso, row.get("Plan"), row.get("Days"),
                row.get("GB"), row.get("Currency"),
            )

        current_keys = work.apply(row_key, axis=1)
        remove = current_keys.isin(suspicious_keys)
        excluded = int(remove.sum())
        if excluded:
            print(
                f"Historical outlier guard: excluded {excluded} current market row(s) "
                "from recommendations."
            )
            preview_cols = [c for c in ["Provider", "ISO", "ISO3", "Plan", "Days", "GB", "Price"] if c in work.columns]
            for _, row in work.loc[remove, preview_cols].head(8).iterrows():
                print("  - " + " | ".join(f"{c}={row.get(c)}" for c in preview_cols))

        return raw_market.loc[~remove].copy(), excluded
    except Exception as exc:
        print(f"Historical outlier guard warning: {exc}")
        return raw_market, 0

def _prepare_market(raw: pd.DataFrame, eur_to_usd: float) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    df.columns = df.columns.astype(str).str.strip()

    if "ISO" not in df.columns:
        df["ISO"] = ""
    if "ISO3" not in df.columns:
        df["ISO3"] = ""
    if "Provider" not in df.columns:
        df["Provider"] = ""
    if "Plan" not in df.columns:
        df["Plan"] = ""
    if "Currency" not in df.columns:
        df["Currency"] = "USD"

    df = add_currency_price_columns(
        df,
        currency_hint="USD",
        eur_to_usd=eur_to_usd,
        fill_missing_with_conversion=True,
    )

    df["ISO"] = df["ISO"].map(lambda x: _text(x).upper())
    iso3 = df["ISO3"].map(lambda x: _text(x).upper())
    missing_iso = df["ISO"].eq("")
    if missing_iso.any():
        df.loc[missing_iso, "ISO"] = iso3.loc[missing_iso].map(_iso3_to_iso2)

    df["Provider"] = df["Provider"].map(_text)
    df["Plan"] = df["Plan"].map(_text)
    df["Days"] = pd.to_numeric(df.get("Days"), errors="coerce")
    df["GB"] = pd.to_numeric(df.get("GB"), errors="coerce")
    df["Price_EUR"] = pd.to_numeric(df.get("Price_EUR"), errors="coerce")
    df["Price_USD"] = pd.to_numeric(df.get("Price_USD"), errors="coerce")

    if "UseForPricing" in df.columns:
        use = df["UseForPricing"].map(_boolish)
        df = df[use].copy()

    # Defensive quality gate: historical versions of outlier_removal.py kept
    # RowFlag rows marked UseForPricing=True.  Never let an explicitly flagged
    # cross-sectional market outlier enter a recommendation reference, even if
    # the annotation file came from one of those older runs.
    if "RowFlag" in df.columns:
        flagged = df["RowFlag"].map(_boolish)
        df = df[~flagged].copy()

    df = df[
        df["ISO"].ne("")
        & df["Provider"].ne("")
        & ~df["Provider"].str.lower().eq("ht")
        & df["Days"].notna()
        & df["GB"].notna()
        & (df["Days"] > 0)
        & (df["GB"] > 0)
        & (df["Price_EUR"].notna() | df["Price_USD"].notna())
    ].copy()

    df["is_unlimited"] = df["Plan"].str.lower().str.contains("unlimited", na=False)
    return df.reset_index(drop=True)


def _consolidate_price_book(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate country rows into one editable pricing-unit scope.

    Most price-book rows are already unique by pricing unit / plan / day / GB,
    so the common path stays vectorized. Only genuinely duplicated scopes are
    aggregated in Python.
    """
    if df.empty:
        return pd.DataFrame()

    work = df.copy()
    work.columns = work.columns.astype(str).str.strip()
    text_cols = [
        "PricingUnitIdUsed", "Plan", "PricingSourceUsed", "PricingUnitCountriesUsed",
        "Country", "ISO", "PromoCode", "PromoType", "PromoLabel",
    ]
    for col in text_cols:
        if col not in work.columns:
            work[col] = ""
        work[col] = work[col].map(_text)

    numeric_cols = [
        "Days", "GB", "Price_EUR", "Price_USD", "FinalPriceAfterPromo_EUR",
        "FinalPriceAfterPromo_USD", "CostFloor_EUR", "CostFloor_USD", "PromoValue",
    ]
    for col in numeric_cols:
        if col not in work.columns:
            work[col] = np.nan
        work[col] = pd.to_numeric(work[col], errors="coerce")

    if "AllowBelowCost" not in work.columns:
        work["AllowBelowCost"] = False
    work["AllowBelowCost"] = work["AllowBelowCost"].map(_boolish)

    work = work[
        work["PricingUnitIdUsed"].ne("")
        & work["Plan"].ne("")
        & work["Days"].notna()
        & work["GB"].notna()
    ].copy()

    group_cols = ["PricingUnitIdUsed", "Plan", "Days", "GB"]
    dup_mask = work.duplicated(group_cols, keep=False)
    records: list[dict[str, Any]] = []

    def row_countries(row: pd.Series) -> str:
        countries = set(_parse_country_codes(row.get("PricingUnitCountriesUsed", "")))
        iso = _text(row.get("ISO", "")).upper()
        if len(iso) == 2:
            countries.add(iso)
        return ",".join(sorted(countries))

    singles = work.loc[~dup_mask].copy()
    if not singles.empty:
        singles["Countries"] = singles.apply(row_countries, axis=1)
        singles["Label"] = np.where(
            singles["PricingSourceUsed"].str.lower().eq("region_max"),
            singles["PricingUnitIdUsed"],
            singles["Country"].where(singles["Country"].ne(""), singles["PricingUnitIdUsed"]),
        )
        for _, row in singles.iterrows():
            records.append({
                "Country": row["Label"],
                "Countries": row["Countries"],
                "ISO": _text(row.get("ISO")).upper(),
                "PricingUnitIdUsed": _text(row.get("PricingUnitIdUsed")),
                "PricingSourceUsed": _text(row.get("PricingSourceUsed")),
                "Plan": _text(row.get("Plan")),
                "Days": float(row["Days"]),
                "GB": float(row["GB"]),
                "Price_EUR": _number(row.get("Price_EUR")),
                "Price_USD": _number(row.get("Price_USD")),
                "FinalPriceAfterPromo_EUR": _number(row.get("FinalPriceAfterPromo_EUR")) or _number(row.get("Price_EUR")),
                "FinalPriceAfterPromo_USD": _number(row.get("FinalPriceAfterPromo_USD")) or _number(row.get("Price_USD")),
                "CostFloor_EUR": _number(row.get("CostFloor_EUR")),
                "CostFloor_USD": _number(row.get("CostFloor_USD")),
                "AllowBelowCost": bool(row.get("AllowBelowCost", False)),
                "PromoCode": _text(row.get("PromoCode")),
                "PromoType": _text(row.get("PromoType")),
                "PromoValue": float(_number(row.get("PromoValue")) or 0.0),
                "PriceConflict": False,
            })

    duplicates = work.loc[dup_mask].copy()
    for key, group in duplicates.groupby(group_cols, dropna=False, sort=False):
        unit_id, plan, days, gb = key
        countries: set[str] = set()
        for value in group["PricingUnitCountriesUsed"]:
            countries.update(_parse_country_codes(value))
        countries.update(
            iso for iso in group["ISO"].map(lambda x: _text(x).upper())
            if len(iso) == 2
        )

        def unique_numeric(col: str) -> list[float]:
            values = pd.to_numeric(group[col], errors="coerce").dropna().astype(float)
            return sorted({round(v, 8) for v in values.tolist()})

        price_conflict = False
        prices: dict[str, float | None] = {}
        finals: dict[str, float | None] = {}
        floors: dict[str, float | None] = {}
        for currency in CURRENCIES:
            pvals = unique_numeric(f"Price_{currency}")
            fvals = unique_numeric(f"FinalPriceAfterPromo_{currency}")
            floor_vals = pd.to_numeric(group[f"CostFloor_{currency}"], errors="coerce").dropna()
            if len(pvals) > 1 or len(fvals) > 1:
                price_conflict = True
            prices[currency] = float(np.median(pvals)) if pvals else None
            finals[currency] = float(np.median(fvals)) if fvals else prices[currency]
            floors[currency] = float(floor_vals.max()) if not floor_vals.empty else None

        promo_codes = [x for x in group["PromoCode"].map(_text).unique().tolist() if x]
        promo_types = [x for x in group["PromoType"].map(_text).unique().tolist() if x]
        promo_values = pd.to_numeric(group["PromoValue"], errors="coerce").dropna().astype(float).unique().tolist()
        promo_code = promo_codes[0] if len(promo_codes) == 1 else ""
        promo_type = promo_types[0] if len(promo_types) == 1 else ""
        promo_value = float(promo_values[0]) if len(promo_values) == 1 else 0.0
        if len(promo_codes) > 1 or len(promo_types) > 1 or len(promo_values) > 1:
            price_conflict = True

        source_values = [x for x in group["PricingSourceUsed"].map(_text).unique().tolist() if x]
        source = source_values[0] if source_values else ""
        labels = [x for x in group["Country"].map(_text).unique().tolist() if x]
        label = unit_id if source.lower() == "region_max" else (labels[0] if len(labels) == 1 else unit_id)

        records.append({
            "Country": label,
            "Countries": ",".join(sorted(countries)),
            "ISO": _text(group["ISO"].iloc[0]).upper(),
            "PricingUnitIdUsed": _text(unit_id),
            "PricingSourceUsed": source,
            "Plan": _text(plan),
            "Days": float(days),
            "GB": float(gb),
            "Price_EUR": prices["EUR"],
            "Price_USD": prices["USD"],
            "FinalPriceAfterPromo_EUR": finals["EUR"],
            "FinalPriceAfterPromo_USD": finals["USD"],
            "CostFloor_EUR": floors["EUR"],
            "CostFloor_USD": floors["USD"],
            "AllowBelowCost": bool(group["AllowBelowCost"].all()),
            "PromoCode": promo_code,
            "PromoType": promo_type,
            "PromoValue": promo_value,
            "PriceConflict": bool(price_conflict),
        })

    return pd.DataFrame(records)

def _distance(ht_days: float, ht_gb: float, cand_days: pd.Series, cand_gb: pd.Series, cfg: RecommendationConfig) -> np.ndarray:
    day_ratio = np.log(cand_days.astype(float).to_numpy() / max(float(ht_days), 1e-9))
    gb_ratio = np.log(cand_gb.astype(float).to_numpy() / max(float(ht_gb), 1e-9))
    return np.sqrt(cfg.days_weight * day_ratio ** 2 + cfg.gb_weight * gb_ratio ** 2)



def _select_market_neighbors(
    data: MarketCountryData,
    *,
    ht_unlimited: bool,
    days: float,
    gb: float,
    cfg: RecommendationConfig,
) -> tuple[list[tuple[str, int]], str, tuple[float, float]]:
    """Select exact-duration competitor products for one country.

    Recommendation evidence is deliberately strict:
    1. same product type (Unlimited only with Unlimited; capped excludes Unlimited),
    2. exact duration only -- no duration bracketing/fallback,
    3. for capped products, allowance must stay within 0.5x-2.0x and the
       closest allowance is retained per provider,
    4. one closest product per provider is retained, up to max_provider_neighbors.

    If fewer than ``min_provider_neighbors`` providers exist at the exact
    duration, callers receive the available rows but will treat the market
    reference as insufficient evidence.
    """
    if len(data.days) == 0:
        return [], "exact_day", (float(days), float(days))

    finite = np.isfinite(data.days) & (data.days > 0)
    if ht_unlimited:
        type_mask = data.plan_nonblank & data.is_unlimited
        allowance_mask = np.ones(len(data.days), dtype=bool)
        gb_distance = np.zeros(len(data.days), dtype=float)
    else:
        type_mask = ~(data.plan_nonblank & data.is_unlimited)
        ratios = data.gb / max(float(gb), 1e-9)
        allowance_mask = (
            np.isfinite(ratios)
            & (ratios >= cfg.capped_gb_ratio_min)
            & (ratios <= cfg.capped_gb_ratio_max)
        )
        gb_distance = np.abs(np.log(np.maximum(ratios, 1e-12)))

    exact = finite & type_mask & allowance_mask & np.isclose(data.days, float(days), atol=1e-9)

    def best_per_provider(mask: np.ndarray, score: np.ndarray) -> list[tuple[str, int]]:
        candidates = np.flatnonzero(mask & np.isfinite(score))
        best_by_provider: dict[str, int] = {}
        for i in candidates:
            provider = str(data.provider[i])
            prev = best_by_provider.get(provider)
            if prev is None or score[i] < score[prev] - 1e-12:
                best_by_provider[provider] = int(i)
            elif prev is not None and math.isclose(float(score[i]), float(score[prev]), abs_tol=1e-12):
                # Stable tie-break: lower EUR price when similarity is identical.
                if data.price_eur[i] < data.price_eur[prev]:
                    best_by_provider[provider] = int(i)
        return sorted(best_by_provider.items(), key=lambda item: (score[item[1]], item[0]))

    # On the same duration, allowance proximity is the only similarity dimension
    # for capped products; Unlimited products are equivalent on allowance.
    exact_score = gb_distance if not ht_unlimited else np.zeros(len(data.days), dtype=float)
    exact_best = best_per_provider(exact, exact_score)
    return exact_best[: cfg.max_provider_neighbors], "exact_day", (float(days), float(days))


def _build_market_index(market: pd.DataFrame) -> dict[str, MarketCountryData]:
    out: dict[str, MarketCountryData] = {}
    if market.empty:
        return out
    for iso, group in market.groupby("ISO", sort=False):
        out[str(iso).upper()] = MarketCountryData(
            provider=group["Provider"].astype(str).to_numpy(),
            plan_nonblank=group["Plan"].astype(str).str.strip().ne("").to_numpy(dtype=bool),
            is_unlimited=group["is_unlimited"].to_numpy(dtype=bool),
            days=group["Days"].to_numpy(dtype=float),
            gb=group["GB"].to_numpy(dtype=float),
            price_eur=group["Price_EUR"].to_numpy(dtype=float),
            price_usd=group["Price_USD"].to_numpy(dtype=float),
        )
    return out


def _aggregate_market_references(
    market_index: dict[str, MarketCountryData],
    countries: list[str],
    plan: str,
    days: float,
    gb: float,
    cfg: RecommendationConfig,
) -> dict[str, MarketReference]:
    empty = {currency: MarketReference() for currency in CURRENCIES}
    if not market_index or not countries:
        return empty

    ht_unlimited = _is_unlimited(plan)
    country_price_refs: dict[str, list[float]] = {c: [] for c in CURRENCIES}
    country_ppg_refs: dict[str, list[float]] = {c: [] for c in CURRENCIES}
    eligible_country_count = 0
    provider_union: set[str] = set()
    exact_provider_union: set[str] = set()
    total_neighbors = 0
    match_modes: list[str] = []
    duration_windows: list[tuple[float, float]] = []

    for iso in {c.upper() for c in countries if c}:
        data = market_index.get(iso)
        if data is None or len(data.days) == 0:
            continue

        best, match_mode, duration_window = _select_market_neighbors(
            data,
            ht_unlimited=ht_unlimited,
            days=days,
            gb=gb,
            cfg=cfg,
        )
        if len(best) < cfg.min_provider_neighbors:
            continue

        indices = np.array([idx for _provider, idx in best], dtype=int)
        providers = [provider for provider, _idx in best]
        provider_union.update(providers)
        exact_provider_union.update(
            provider
            for provider, idx in best
            if math.isclose(float(data.days[idx]), float(days), abs_tol=1e-9)
        )
        total_neighbors += len(best)
        eligible_country_count += 1
        match_modes.append(match_mode)
        duration_windows.append(duration_window)

        for currency in CURRENCIES:
            prices = data.price_eur[indices] if currency == "EUR" else data.price_usd[indices]
            valid = np.isfinite(prices) & (prices > 0)
            if valid.sum() < cfg.min_provider_neighbors:
                continue
            valid_prices = prices[valid]
            country_price_refs[currency].append(float(np.median(valid_prices)))
            if not ht_unlimited:
                valid_gb = data.gb[indices][valid]
                country_ppg_refs[currency].append(float(np.median(valid_prices / valid_gb)))

    requested_country_count = len({c.upper() for c in countries if c})
    if requested_country_count > 1:
        if requested_country_count <= cfg.require_full_coverage_up_to_countries:
            required_countries = requested_country_count
        else:
            required_countries = min(
                cfg.max_required_market_countries,
                max(2, int(math.ceil(requested_country_count * cfg.min_multi_country_market_coverage))),
            )
    else:
        required_countries = 1

    provider_count = len(provider_union)
    providers_text = ", ".join(sorted(provider_union)[:10])
    match_mode = match_modes[0] if match_modes and len(set(match_modes)) == 1 else ("mixed" if match_modes else "")
    duration_min = min((x[0] for x in duration_windows), default=None)
    duration_max = max((x[1] for x in duration_windows), default=None)

    if eligible_country_count < required_countries or provider_count < cfg.min_provider_neighbors:
        return {
            currency: MarketReference(
                provider_count=provider_count,
                neighbor_count=total_neighbors,
                country_count=eligible_country_count,
                exact_day_provider_count=len(exact_provider_union),
                providers=providers_text,
                match_mode=match_mode,
                duration_min=duration_min,
                duration_max=duration_max,
            )
            for currency in CURRENCIES
        }

    refs: dict[str, MarketReference] = {}
    for currency in CURRENCIES:
        price_ok = len(country_price_refs[currency]) >= required_countries
        ppg_ok = ht_unlimited or len(country_ppg_refs[currency]) >= required_countries
        if not price_ok or not ppg_ok:
            refs[currency] = MarketReference(
                provider_count=provider_count,
                neighbor_count=total_neighbors,
                country_count=eligible_country_count,
                exact_day_provider_count=len(exact_provider_union),
                providers=providers_text,
                match_mode=match_mode,
                duration_min=duration_min,
                duration_max=duration_max,
            )
            continue

        median_price = float(np.median(country_price_refs[currency]))
        if ht_unlimited:
            median_ppg = None
            target_price = median_price
        else:
            median_ppg = float(np.median(country_ppg_refs[currency]))
            ppg_equivalent = median_ppg * float(gb)
            target_price = (1.0 - cfg.ppg_weight_capped) * median_price + cfg.ppg_weight_capped * ppg_equivalent

        refs[currency] = MarketReference(
            provider_count=provider_count,
            neighbor_count=total_neighbors,
            country_count=eligible_country_count,
            exact_day_provider_count=len(exact_provider_union),
            median_price=median_price,
            median_ppg=median_ppg,
            target_price=float(target_price),
            providers=providers_text,
            match_mode=match_mode,
            duration_min=duration_min,
            duration_max=duration_max,
        )
    return refs

def _market_references(
    market_index: dict[str, MarketCountryData],
    countries: list[str],
    plan: str,
    days: float,
    gb: float,
    cfg: RecommendationConfig,
    *,
    pricing_unit_id: str = "",
    priority_countries: dict[str, str] | None = None,
) -> dict[str, MarketReference]:
    """Build the market reference for one editable pricing scope.

    Single-country scopes use that country's market normally. Multi-country
    scopes use the explicitly configured priority country as the primary pricing
    signal. The other member countries are retained only as a corroborating /
    contradiction guardrail; they are never averaged equally into the primary
    signal.
    """
    countries = sorted({c.upper() for c in countries if c})
    if len(countries) <= 1:
        return _aggregate_market_references(market_index, countries, plan, days, gb, cfg)

    priority = _text((priority_countries or {}).get(pricing_unit_id, "")).upper()
    if not priority or priority not in countries:
        return {
            currency: MarketReference(priority_country_missing=True)
            for currency in CURRENCIES
        }

    primary = _aggregate_market_references(market_index, [priority], plan, days, gb, cfg)
    secondary_countries = [c for c in countries if c != priority]
    secondary = _aggregate_market_references(
        market_index, secondary_countries, plan, days, gb, cfg
    ) if secondary_countries else {currency: MarketReference() for currency in CURRENCIES}

    refs: dict[str, MarketReference] = {}
    for currency in CURRENCIES:
        main = primary[currency]
        other = secondary[currency]
        refs[currency] = MarketReference(
            provider_count=main.provider_count,
            neighbor_count=main.neighbor_count,
            country_count=main.country_count,
            exact_day_provider_count=main.exact_day_provider_count,
            median_price=main.median_price,
            median_ppg=main.median_ppg,
            target_price=main.target_price,
            providers=main.providers,
            priority_country=priority,
            secondary_country_count=other.country_count,
            secondary_provider_count=other.provider_count,
            secondary_target_price=other.target_price,
            secondary_providers=other.providers,
            match_mode=main.match_mode,
            duration_min=main.duration_min,
            duration_max=main.duration_max,
        )
    return refs


def _confidence(ref: MarketReference, metric_agreement: bool, gap_pct: float) -> str:
    if ref.provider_count < 2:
        return "NONE"
    score = 0
    if ref.provider_count >= 4:
        score += 2
    elif ref.provider_count >= 3:
        score += 1
    if ref.exact_day_provider_count >= 3:
        score += 2
    elif ref.exact_day_provider_count >= 1:
        score += 1
    if ref.country_count >= 2:
        score += 1
    if metric_agreement:
        score += 1
    if abs(gap_pct) >= 0.20:
        score += 1
    if score >= 5:
        return "HIGH"
    if score >= 3:
        return "MEDIUM"
    return "LOW"


def _suggested_net_price(current_net: float, target_price: float, cfg: RecommendationConfig) -> float:
    """Move only a small fraction toward the local benchmark.

    The move is always capped by both an absolute amount and 5% of the current
    net price. This is deliberately conservative: the engine is an adviser, not
    an automatic repricer.
    """
    gap = float(target_price) - float(current_net)
    if math.isclose(gap, 0.0, abs_tol=1e-12):
        return _round_step(current_net, cfg.price_step)

    cap = min(cfg.max_abs_move, abs(float(current_net)) * cfg.max_move_pct)
    if cap < cfg.price_step / 2:
        return _round_step(current_net, cfg.price_step)

    desired = max(cfg.min_abs_move, abs(gap) * cfg.move_fraction_toward_target)
    move = min(abs(gap), cap, desired)
    suggested = float(current_net) + math.copysign(move, gap)
    # Round toward the current price so rounding itself can never make the
    # recommendation larger than the configured cap.
    if gap > 0:
        suggested = _floor_step(suggested, cfg.price_step)
    else:
        suggested = _ceil_step(suggested, cfg.price_step)
    return max(float(suggested), 0.0)


def _net_value(row: pd.Series, currency: str) -> float | None:
    return _number(row.get(f"FinalPriceAfterPromo_{currency}"))


def _list_value(row: pd.Series, currency: str) -> float | None:
    return _number(row.get(f"Price_{currency}"))


def _local_signal(
    position: float | None,
    deviation: float | None,
    portfolio_deviation: float | None,
    cfg: RecommendationConfig,
) -> str:
    """Return UP/DOWN only for a material local anomaly with market sanity."""
    if position is None or deviation is None:
        return "NONE"
    if abs(deviation) < cfg.local_position_deadband_pct:
        return "NONE"

    direction = "DOWN" if deviation > 0 else "UP"
    if direction == "UP" and position >= 1.0 - cfg.market_sanity_deadband_pct:
        return "NONE"
    if direction == "DOWN" and position <= 1.0 + cfg.market_sanity_deadband_pct:
        return "NONE"

    if portfolio_deviation is not None:
        if direction == "UP" and portfolio_deviation > cfg.portfolio_contradiction_pct:
            return "NONE"
        if direction == "DOWN" and portfolio_deviation < -cfg.portfolio_contradiction_pct:
            return "NONE"
    return direction


def _build_scope_indexes(scopes: pd.DataFrame) -> tuple[dict[tuple[str, str], pd.DataFrame], dict[tuple[str, float], pd.DataFrame]]:
    by_unit_plan = {
        (str(unit), str(plan)): group.sort_values("Days").copy()
        for (unit, plan), group in scopes.groupby(["PricingUnitIdUsed", "Plan"], sort=False)
    }
    by_unit_day = {
        (str(unit), float(day)): group.sort_values("GB").copy()
        for (unit, day), group in scopes.groupby(["PricingUnitIdUsed", "Days"], sort=False)
    }
    return by_unit_plan, by_unit_day


def _guardrail_bounds(
    by_unit_plan: dict[tuple[str, str], pd.DataFrame],
    by_unit_day: dict[tuple[str, float], pd.DataFrame],
    row: pd.Series,
    currency: str,
    cfg: RecommendationConfig,
) -> tuple[float | None, float | None, list[str]]:
    unit = _text(row.get("PricingUnitIdUsed"))
    plan = _text(row.get("Plan"))
    days = float(row.get("Days"))
    gb = float(row.get("GB"))
    step = cfg.price_step
    reasons: list[str] = []
    lower: float | None = None
    upper: float | None = None

    same_plan = by_unit_plan.get((unit, plan), pd.DataFrame())
    if not same_plan.empty:
        before = same_plan[same_plan["Days"] < days]
        after = same_plan[same_plan["Days"] > days]
        if not before.empty:
            v = _net_value(before.iloc[-1], currency)
            if v is not None:
                lower = v + step
                reasons.append("duration_lower")
        if not after.empty:
            v = _net_value(after.iloc[0], currency)
            if v is not None:
                upper = v - step
                reasons.append("duration_upper")

    same_day = by_unit_day.get((unit, days), pd.DataFrame())
    if not same_day.empty:
        lower_plan = same_day[same_day["GB"] < gb]
        upper_plan = same_day[same_day["GB"] > gb]
        if not lower_plan.empty:
            v = _net_value(lower_plan.iloc[-1], currency)
            if v is not None:
                candidate = v + step
                lower = candidate if lower is None else max(lower, candidate)
                reasons.append("plan_lower")
        if not upper_plan.empty:
            v = _net_value(upper_plan.iloc[0], currency)
            if v is not None:
                candidate = v - step
                upper = candidate if upper is None else min(upper, candidate)
                reasons.append("plan_upper")

    if not _boolish(row.get("AllowBelowCost")):
        floor = _number(row.get(f"CostFloor_{currency}"))
        if floor is not None:
            floor = _ceil_step(floor, step)
            lower = floor if lower is None else max(lower, floor)
            reasons.append("cost_floor")

    if lower is not None and upper is not None and lower > upper + 1e-9:
        return None, None, ["guardrail_conflict"]
    return lower, upper, reasons

def _apply_bounds_directional(
    current: float,
    suggested: float,
    direction: str,
    lower: float | None,
    upper: float | None,
    cfg: RecommendationConfig,
) -> float:
    """Use guardrails only to cap a proposed move, never to create a bigger move.

    Existing curves may already violate one of the monotonicity bounds. The
    recommendation engine must not "repair" such legacy issues by jumping the
    price to the guardrail. It may only make the small proposed move if that move
    does not worsen the relevant constraint.
    """
    current = float(current)
    out = float(suggested)
    eps = cfg.price_step / 2

    if str(direction).upper() == "UP":
        if upper is not None:
            if current >= upper - eps:
                return _round_step(current, cfg.price_step)
            out = min(out, upper)
    elif str(direction).upper() == "DOWN":
        if lower is not None:
            if current <= lower + eps:
                return _round_step(current, cfg.price_step)
            out = max(out, lower)

    return max(_round_step(out, cfg.price_step), 0.0)


def _choose_promo(
    list_price: float,
    current_net: float,
    desired_net: float,
    floor: float | None,
    allow_below_cost: bool,
    promos: Iterable[dict[str, Any]],
    cfg: RecommendationConfig,
) -> tuple[str, float | None]:
    candidates: list[tuple[float, float, str]] = []
    for promo in promos:
        final = _promo_final(
            list_price,
            _text(promo.get("promo_type")),
            float(promo.get("promo_value", 0) or 0),
            cfg.price_step,
        )
        if final >= current_net - cfg.price_step / 2:
            continue
        if floor is not None and not allow_below_cost and final < floor - 1e-9:
            continue
        distance = abs(final - desired_net)
        overshoot = max(desired_net - final, 0.0)
        candidates.append((distance + 0.35 * overshoot, final, _text(promo.get("promo_code"))))
    if not candidates:
        return "", None
    candidates.sort(key=lambda x: (x[0], -x[1], x[2]))
    _, final, code = candidates[0]
    return code, float(final)


def _build_curve_expected_map(anchors: pd.DataFrame) -> dict[tuple[int, str], float]:
    expected: dict[tuple[int, str], float] = {}
    for _, group in anchors.groupby(["PricingUnitIdUsed", "Plan"], sort=False):
        group = group.sort_values("Days")
        indices = list(group.index)
        for pos in range(1, len(indices) - 1):
            idx0, idx1, idx2 = indices[pos - 1], indices[pos], indices[pos + 1]
            r0, r1, r2 = group.loc[idx0], group.loc[idx1], group.loc[idx2]
            d0, d1, d2 = float(r0["Days"]), float(r1["Days"]), float(r2["Days"])
            if math.isclose(d0, d2):
                continue
            t = (d1 - d0) / (d2 - d0)
            for currency in CURRENCIES:
                y0 = _net_value(r0, currency)
                y2 = _net_value(r2, currency)
                if y0 is None or y2 is None:
                    continue
                expected[(idx1, currency)] = float(y0 + t * (y2 - y0))
    return expected

def generate_recommendations(
    price_book_df: pd.DataFrame,
    market_df: pd.DataFrame,
    *,
    promos: Iterable[dict[str, Any]] | None = None,
    priority_countries: dict[str, str] | None = None,
    eur_to_usd: float = DEFAULT_EUR_TO_USD,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """Build conservative anchor recommendations without regression.

    Core principle: do not chase the absolute competitor median. Instead, compare
    each anchor's *market position* (HT net price / local market reference) with
    the market position of neighboring HT anchors. A recommendation appears only
    when one anchor is materially out of line with its own nearby anchors and the
    absolute market relationship points in the same direction.
    """
    promos = list(promos or [])
    scopes = _consolidate_price_book(price_book_df)
    # Locked geographical regions currently have no defensible competitor
    # benchmark. Exclude them entirely until a dedicated region methodology is
    # defined. Shared regulatory pricing units (json_unit) remain eligible.
    if not scopes.empty:
        scopes = scopes[
            ~scopes["PricingSourceUsed"].astype(str).str.strip().str.lower().eq("region_max")
        ].copy()
    market = _prepare_market(market_df, eur_to_usd)
    market_index = _build_market_index(market)
    by_unit_plan, by_unit_day = _build_scope_indexes(scopes)

    if scopes.empty:
        return pd.DataFrame()

    anchors = scopes[
        scopes["Days"].round(8).isin([float(x) for x in config.anchor_days])
    ].copy()
    if anchors.empty:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []

    # Phase 1: calculate the market reference for every anchor/currency. No
    # recommendation is made yet; this keeps evidence separate from decisions.
    for idx, row in anchors.iterrows():
        countries = [x for x in _text(row.get("Countries")).split(",") if x]
        if not countries:
            iso = _text(row.get("ISO")).upper()
            countries = [iso] if len(iso) == 2 else []

        gb = float(row["GB"])
        refs = _market_references(
            market_index,
            countries,
            _text(row["Plan"]),
            float(row["Days"]),
            gb,
            config,
            pricing_unit_id=_text(row.get("PricingUnitIdUsed")),
            priority_countries=priority_countries,
        )

        for currency in CURRENCIES:
            current_list = _number(row.get(f"Price_{currency}"))
            current_net = _number(row.get(f"FinalPriceAfterPromo_{currency}"))
            floor = _number(row.get(f"CostFloor_{currency}"))
            if current_list is None or current_net is None or current_net <= 0:
                continue

            ref = refs[currency]
            current_ppg = current_net / gb if gb > 0 else np.nan
            current_list_ppg = current_list / gb if gb > 0 else np.nan
            has_active_promo = (
                current_list > 0
                and current_net < current_list - config.price_step / 2
                and (
                    bool(_text(row.get("PromoCode")))
                    or bool(_text(row.get("PromoType")))
                    or float(_number(row.get("PromoValue")) or 0.0) > 0
                )
            )
            promo_discount_pct = (
                1.0 - current_net / current_list
                if has_active_promo and current_list > 0
                else 0.0
            )

            # Customer-price view: what the customer pays today.
            market_position = (
                current_net / ref.target_price
                if ref.target_price is not None and ref.target_price > 0
                else np.nan
            )
            market_gap = market_position - 1.0 if np.isfinite(market_position) else np.nan
            price_gap = (
                current_net / ref.median_price - 1.0
                if ref.median_price is not None and ref.median_price > 0
                else np.nan
            )
            ppg_gap = (
                current_ppg / ref.median_ppg - 1.0
                if ref.median_ppg is not None and ref.median_ppg > 0
                else np.nan
            )

            # Structural view: underlying list price before an intentional promo.
            list_market_position = (
                current_list / ref.target_price
                if ref.target_price is not None and ref.target_price > 0
                else np.nan
            )
            list_market_gap = (
                list_market_position - 1.0
                if np.isfinite(list_market_position)
                else np.nan
            )
            list_price_gap = (
                current_list / ref.median_price - 1.0
                if ref.median_price is not None and ref.median_price > 0
                else np.nan
            )
            list_ppg_gap = (
                current_list_ppg / ref.median_ppg - 1.0
                if ref.median_ppg is not None and ref.median_ppg > 0
                else np.nan
            )

            price_sign = (
                1 if np.isfinite(price_gap) and price_gap > config.metric_agreement_band_pct
                else -1 if np.isfinite(price_gap) and price_gap < -config.metric_agreement_band_pct
                else 0
            )
            if _is_unlimited(row["Plan"]):
                ppg_gap = np.nan
                list_ppg_gap = np.nan
                ppg_sign = 0
                metric_agreement = price_sign != 0
            else:
                ppg_sign = (
                    1 if np.isfinite(ppg_gap) and ppg_gap > config.metric_agreement_band_pct
                    else -1 if np.isfinite(ppg_gap) and ppg_gap < -config.metric_agreement_band_pct
                    else 0
                )
                metric_agreement = price_sign == ppg_sign and price_sign != 0

            list_price_sign = (
                1 if np.isfinite(list_price_gap) and list_price_gap > config.metric_agreement_band_pct
                else -1 if np.isfinite(list_price_gap) and list_price_gap < -config.metric_agreement_band_pct
                else 0
            )
            if _is_unlimited(row["Plan"]):
                list_ppg_sign = 0
                list_metric_agreement = list_price_sign != 0
            else:
                list_ppg_sign = (
                    1 if np.isfinite(list_ppg_gap) and list_ppg_gap > config.metric_agreement_band_pct
                    else -1 if np.isfinite(list_ppg_gap) and list_ppg_gap < -config.metric_agreement_band_pct
                    else 0
                )
                list_metric_agreement = list_price_sign == list_ppg_sign and list_price_sign != 0

            records.append({
                "_anchor_index": idx,
                "Country": row["Country"],
                "Countries": row["Countries"],
                "ISO": row["ISO"],
                "PricingUnitIdUsed": row["PricingUnitIdUsed"],
                "PricingSourceUsed": row["PricingSourceUsed"],
                "Plan": row["Plan"],
                "Days": int(round(float(row["Days"]))),
                "GB": float(row["GB"]),
                "Currency": currency,
                "CurrentListPrice": current_list,
                "CurrentNetPrice": current_net,
                "CurrentPricePerGB": current_ppg,
                "CurrentPromoCode": row.get("PromoCode", ""),
                "CurrentPromoType": row.get("PromoType", ""),
                "CurrentPromoValue": row.get("PromoValue", 0.0),
                "HasActivePromo": bool(has_active_promo),
                "PromoDiscountPct": promo_discount_pct,
                "CostFloor": floor,
                "AllowBelowCost": bool(row.get("AllowBelowCost", False)),
                "MarketProviderCount": ref.provider_count,
                "MarketNeighborCount": ref.neighbor_count,
                "MarketCountryCount": ref.country_count,
                "PriorityCountry": ref.priority_country,
                "PriorityCountryMissing": bool(ref.priority_country_missing),
                "SecondaryMarketCountryCount": ref.secondary_country_count,
                "SecondaryMarketProviderCount": ref.secondary_provider_count,
                "SecondaryMarketTargetPrice": ref.secondary_target_price,
                "SecondaryMarketPosition": (
                    current_net / ref.secondary_target_price
                    if ref.secondary_target_price is not None and ref.secondary_target_price > 0
                    else np.nan
                ),
                "SecondaryListMarketPosition": (
                    current_list / ref.secondary_target_price
                    if ref.secondary_target_price is not None and ref.secondary_target_price > 0
                    else np.nan
                ),
                "NeighborSecondaryMarketPosition": np.nan,
                "SecondaryPositionDeviationPct": np.nan,
                "SecondarySignal": "NONE",
                "NeighborSecondaryListMarketPosition": np.nan,
                "SecondaryListPositionDeviationPct": np.nan,
                "SecondaryListSignal": "NONE",
                "ExactDayProviderCount": ref.exact_day_provider_count,
                "MarketMatchMode": ref.match_mode,
                "MarketDurationMin": ref.duration_min,
                "MarketDurationMax": ref.duration_max,
                "MarketPricePerGBWeight": (0.0 if _is_unlimited(row["Plan"]) else config.ppg_weight_capped),
                "MarketGBRatioMin": (np.nan if _is_unlimited(row["Plan"]) else config.capped_gb_ratio_min),
                "MarketGBRatioMax": (np.nan if _is_unlimited(row["Plan"]) else config.capped_gb_ratio_max),
                "MarketMedianPrice": ref.median_price,
                "MarketMedianPricePerGB": ref.median_ppg,
                "MarketTargetPrice": ref.target_price,
                "MarketPosition": market_position,
                "MarketGapPct": market_gap,
                "PriceGapPct": price_gap,
                "PricePerGBGapPct": ppg_gap,
                "MetricAgreement": bool(metric_agreement),
                "ListMarketPosition": list_market_position,
                "ListMarketGapPct": list_market_gap,
                "ListPriceGapPct": list_price_gap,
                "ListPricePerGBGapPct": list_ppg_gap,
                "ListMetricAgreement": bool(list_metric_agreement),
                "NeighborProviders": ref.providers,
                "PriceConflict": bool(row.get("PriceConflict", False)),
                "NeighborAnchorCount": 0,
                "NeighborMarketPosition": np.nan,
                "PositionDeviationPct": np.nan,
                "NeighborListMarketPosition": np.nan,
                "ListPositionDeviationPct": np.nan,
                "PortfolioMarketPosition": np.nan,
                "PortfolioDeviationPct": np.nan,
                "PortfolioListMarketPosition": np.nan,
                "PortfolioListDeviationPct": np.nan,
                "NetSignalDirection": "NONE",
                "ListSignalDirection": "NONE",
                "DecisionBasis": "NONE",
                "SignalDirection": "NONE",
                "Direction": "NONE",
                "Confidence": "NONE",
                "SuggestedNetPrice": np.nan,
                "SuggestedNetDelta": np.nan,
                "GuardrailLower": np.nan,
                "GuardrailUpper": np.nan,
                "GuardrailTags": "",
                "Reason": (
                    "priority_country_missing"
                    if ref.priority_country_missing
                    else "insufficient_market_neighbors" if not np.isfinite(market_position)
                    else "within_local_position_band"
                ),
                "Mechanism": "NONE",
                "SuggestedListPrice": np.nan,
                "SuggestedPromoCode": "",
                "SuggestedPromoFinalPrice": np.nan,
                "Actionable": False,
            })

    out = pd.DataFrame(records)
    if out.empty:
        return out

    # Phase 2: compare each anchor's market position with neighboring anchors of
    # the same plan. This makes the system robust to a broad market shift: if all
    # competitor prices move together, the relative positions stay similar and
    # the engine normally recommends nothing.
    for _, group in out.groupby(["PricingUnitIdUsed", "Plan", "Currency"], sort=False):
        group = group.sort_values("Days")
        indices = list(group.index)
        for pos, idx in enumerate(indices):
            current_position = _number(out.at[idx, "MarketPosition"])
            if current_position is None or int(out.at[idx, "MarketProviderCount"]) < config.min_provider_neighbors:
                continue

            # Use a robust local benchmark from up to four nearest *other*
            # anchors. Two-point benchmarks can propagate one bad anchor into
            # false recommendations on both sides; the median of several nearby
            # anchors isolates a single anomaly instead of echoing it.
            candidates: list[tuple[int, float]] = []
            for j, other_idx in enumerate(indices):
                if other_idx == idx:
                    continue
                value = _number(out.at[other_idx, "MarketPosition"])
                if value is None or int(out.at[other_idx, "MarketProviderCount"]) < config.min_provider_neighbors:
                    continue
                candidates.append((abs(j - pos), value))
            candidates.sort(key=lambda item: item[0])
            neighbor_positions = [value for _distance, value in candidates[:4]]

            if len(neighbor_positions) < 2:
                continue

            neighbor_position = float(np.median(neighbor_positions))
            if neighbor_position <= 0:
                continue
            deviation = current_position / neighbor_position - 1.0
            out.at[idx, "NeighborAnchorCount"] = len(neighbor_positions)
            out.at[idx, "NeighborMarketPosition"] = neighbor_position
            out.at[idx, "PositionDeviationPct"] = deviation

            list_current = _number(out.at[idx, "ListMarketPosition"])
            list_candidates: list[tuple[int, float]] = []
            for j, other_idx in enumerate(indices):
                if other_idx == idx:
                    continue
                value = _number(out.at[other_idx, "ListMarketPosition"])
                if value is None or int(out.at[other_idx, "MarketProviderCount"]) < config.min_provider_neighbors:
                    continue
                list_candidates.append((abs(j - pos), value))
            list_candidates.sort(key=lambda item: item[0])
            list_neighbor_positions = [value for _distance, value in list_candidates[:4]]
            if list_current is not None and len(list_neighbor_positions) >= 2:
                list_neighbor = float(np.median(list_neighbor_positions))
                if list_neighbor > 0:
                    out.at[idx, "NeighborListMarketPosition"] = list_neighbor
                    out.at[idx, "ListPositionDeviationPct"] = list_current / list_neighbor - 1.0

            secondary_current = _number(out.at[idx, "SecondaryMarketPosition"])
            if secondary_current is not None:
                secondary_candidates: list[tuple[int, float]] = []
                for j, other_idx in enumerate(indices):
                    if other_idx == idx:
                        continue
                    value = _number(out.at[other_idx, "SecondaryMarketPosition"])
                    if value is None:
                        continue
                    secondary_candidates.append((abs(j - pos), value))
                secondary_candidates.sort(key=lambda item: item[0])
                secondary_positions = [value for _distance, value in secondary_candidates[:4]]
                if len(secondary_positions) >= 2:
                    secondary_neighbor = float(np.median(secondary_positions))
                    if secondary_neighbor > 0:
                        out.at[idx, "NeighborSecondaryMarketPosition"] = secondary_neighbor
                        out.at[idx, "SecondaryPositionDeviationPct"] = (
                            secondary_current / secondary_neighbor - 1.0
                        )

            secondary_list_current = _number(out.at[idx, "SecondaryListMarketPosition"])
            if secondary_list_current is not None:
                secondary_list_candidates: list[tuple[int, float]] = []
                for j, other_idx in enumerate(indices):
                    if other_idx == idx:
                        continue
                    value = _number(out.at[other_idx, "SecondaryListMarketPosition"])
                    if value is None:
                        continue
                    secondary_list_candidates.append((abs(j - pos), value))
                secondary_list_candidates.sort(key=lambda item: item[0])
                secondary_list_positions = [value for _distance, value in secondary_list_candidates[:4]]
                if len(secondary_list_positions) >= 2:
                    secondary_list_neighbor = float(np.median(secondary_list_positions))
                    if secondary_list_neighbor > 0:
                        out.at[idx, "NeighborSecondaryListMarketPosition"] = secondary_list_neighbor
                        out.at[idx, "SecondaryListPositionDeviationPct"] = (
                            secondary_list_current / secondary_list_neighbor - 1.0
                        )

    # Phase 3: portfolio context. Other plans at the same duration are a
    # secondary check only; they can strengthen a signal or veto an obvious
    # contradiction, but they do not create a recommendation by themselves.
    for _, group in out.groupby(["PricingUnitIdUsed", "Days", "Currency"], sort=False):
        group = group.sort_values("GB")
        indices = list(group.index)
        for pos, idx in enumerate(indices):
            current_position = _number(out.at[idx, "MarketPosition"])
            if current_position is None:
                continue
            neighbors: list[float] = []
            if pos > 0:
                value = _number(out.at[indices[pos - 1], "MarketPosition"])
                if value is not None:
                    neighbors.append(value)
            if pos < len(indices) - 1:
                value = _number(out.at[indices[pos + 1], "MarketPosition"])
                if value is not None:
                    neighbors.append(value)
            if not neighbors:
                continue
            portfolio_position = float(np.median(neighbors))
            if portfolio_position <= 0:
                continue
            out.at[idx, "PortfolioMarketPosition"] = portfolio_position
            out.at[idx, "PortfolioDeviationPct"] = current_position / portfolio_position - 1.0

            list_current = _number(out.at[idx, "ListMarketPosition"])
            if list_current is not None:
                list_neighbors: list[float] = []
                if pos > 0:
                    value = _number(out.at[indices[pos - 1], "ListMarketPosition"])
                    if value is not None:
                        list_neighbors.append(value)
                if pos < len(indices) - 1:
                    value = _number(out.at[indices[pos + 1], "ListMarketPosition"])
                    if value is not None:
                        list_neighbors.append(value)
                if list_neighbors:
                    list_portfolio = float(np.median(list_neighbors))
                    if list_portfolio > 0:
                        out.at[idx, "PortfolioListMarketPosition"] = list_portfolio
                        out.at[idx, "PortfolioListDeviationPct"] = list_current / list_portfolio - 1.0

    # Phase 4: turn evidence into a conservative signal and suggested net price.
    #
    # The engine now keeps two views separate:
    #   * NET  = what the customer pays today, including an active promo.
    #   * LIST = the underlying structural price before that promo.
    # An active promo may explain a very cheap net price; in that case we do not
    # raise the list price merely to offset an intentional promotion.
    for idx, rec in out.iterrows():
        if int(rec.get("MarketProviderCount", 0)) < config.min_provider_neighbors:
            continue

        net_position = _number(rec.get("MarketPosition"))
        net_deviation = _number(rec.get("PositionDeviationPct"))
        net_portfolio = _number(rec.get("PortfolioDeviationPct"))
        list_position = _number(rec.get("ListMarketPosition"))
        list_deviation = _number(rec.get("ListPositionDeviationPct"))
        list_portfolio = _number(rec.get("PortfolioListDeviationPct"))

        net_signal = _local_signal(net_position, net_deviation, net_portfolio, config)
        list_signal = _local_signal(list_position, list_deviation, list_portfolio, config)

        # Shared pricing unit guardrail, separately for net and list views.
        secondary_position = _number(rec.get("SecondaryMarketPosition"))
        secondary_deviation = _number(rec.get("SecondaryPositionDeviationPct"))
        secondary_signal = _local_signal(secondary_position, secondary_deviation, None, config)
        out.at[idx, "SecondarySignal"] = secondary_signal
        if net_signal != "NONE" and secondary_signal not in {"NONE", net_signal}:
            net_signal = "NONE"
            net_secondary_conflict = True
        else:
            net_secondary_conflict = False

        secondary_list_position = _number(rec.get("SecondaryListMarketPosition"))
        secondary_list_deviation = _number(rec.get("SecondaryListPositionDeviationPct"))
        secondary_list_signal = _local_signal(
            secondary_list_position, secondary_list_deviation, None, config
        )
        out.at[idx, "SecondaryListSignal"] = secondary_list_signal
        if list_signal != "NONE" and secondary_list_signal not in {"NONE", list_signal}:
            list_signal = "NONE"
            list_secondary_conflict = True
        else:
            list_secondary_conflict = False

        out.at[idx, "NetSignalDirection"] = net_signal
        out.at[idx, "ListSignalDirection"] = list_signal

        has_active_promo = bool(rec.get("HasActivePromo", False))
        decision = "NONE"
        basis = "NONE"

        if has_active_promo:
            # A cheap promotional net price is intentional unless the underlying
            # list price independently says that the anchor is structurally cheap.
            if net_signal == "UP":
                if list_signal == "UP":
                    decision = "UP"
                    basis = "LIST_AND_NET"
                elif list_signal == "NONE":
                    out.at[idx, "Reason"] = (
                        "secondary_markets_contradict_priority"
                        if net_secondary_conflict
                        else "active_promo_explains_low_net"
                    )
                    continue
                else:
                    out.at[idx, "Reason"] = "list_net_signals_conflict"
                    continue
            elif net_signal == "DOWN":
                if list_signal == "DOWN":
                    decision = "DOWN"
                    basis = "LIST_AND_NET"
                elif list_signal == "NONE":
                    # Customer price is locally high, but the underlying list
                    # structure is not. This is a promo-only correction.
                    decision = "DOWN"
                    basis = "NET_ONLY_PROMO"
                else:
                    out.at[idx, "Reason"] = "list_net_signals_conflict"
                    continue
            else:
                if list_signal != "NONE":
                    out.at[idx, "Reason"] = (
                        "secondary_markets_contradict_priority"
                        if list_secondary_conflict
                        else "list_signal_masked_by_active_promo"
                    )
                elif net_secondary_conflict or list_secondary_conflict:
                    out.at[idx, "Reason"] = "secondary_markets_contradict_priority"
                else:
                    out.at[idx, "Reason"] = "within_local_position_band"
                continue
        else:
            # Without a material promo list and net should normally agree. Keep
            # the net signal as the customer-facing decision, but veto an actual
            # opposite structural signal.
            if net_signal == "NONE":
                if net_secondary_conflict or list_secondary_conflict:
                    out.at[idx, "Reason"] = "secondary_markets_contradict_priority"
                else:
                    out.at[idx, "Reason"] = "within_local_position_band"
                continue
            if list_signal not in {"NONE", net_signal}:
                out.at[idx, "Reason"] = "list_net_signals_conflict"
                continue
            decision = net_signal
            basis = "LIST_AND_NET" if list_signal == net_signal else "NET_ONLY"

        out.at[idx, "SignalDirection"] = decision
        out.at[idx, "DecisionBasis"] = basis

        # Confidence remains driven by observable market evidence, with one
        # extra point when list and net independently agree.
        deviation = net_deviation
        score = 0
        provider_count = int(rec.get("MarketProviderCount", 0))
        exact_day = int(rec.get("ExactDayProviderCount", 0))
        neighbor_count = int(rec.get("NeighborAnchorCount", 0))
        if provider_count >= 4:
            score += 2
        elif provider_count >= 3:
            score += 1
        if exact_day >= 2:
            score += 1
        if neighbor_count >= 2:
            score += 1
        if deviation is not None and abs(deviation) >= 0.20:
            score += 1
        if bool(rec.get("MetricAgreement", False)):
            score += 1
        if _text(out.at[idx, "SecondarySignal"]).upper() == decision:
            score += 1
        if net_portfolio is not None:
            supports = (
                (decision == "UP" and net_portfolio < -0.05)
                or (decision == "DOWN" and net_portfolio > 0.05)
            )
            if supports:
                score += 1

        confidence = "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW"
        out.at[idx, "Confidence"] = confidence
        if confidence == "LOW":
            out.at[idx, "Reason"] = "local_signal_low_confidence"
            out.at[idx, "SignalDirection"] = "NONE"
            out.at[idx, "DecisionBasis"] = "NONE"
            continue

        market_target = _number(rec.get("MarketTargetPrice"))
        current_net = float(rec["CurrentNetPrice"])
        neighbor_position = _number(rec.get("NeighborMarketPosition"))
        if market_target is None or market_target <= 0 or neighbor_position is None:
            continue

        # The exact action is still expressed as a conservative movement of the
        # customer net price. If LIST_AND_NET is chosen, Phase 6 converts this
        # back to a list price while preserving the existing promo.
        local_target_net = market_target * neighbor_position
        raw_suggested = _suggested_net_price(current_net, local_target_net, config)

        anchor_row = anchors.loc[int(rec["_anchor_index"])]
        lower, upper, guardrail_tags = _guardrail_bounds(
            by_unit_plan,
            by_unit_day,
            anchor_row,
            str(rec["Currency"]),
            config,
        )
        if "guardrail_conflict" in guardrail_tags:
            out.at[idx, "Reason"] = "guardrail_conflict"
            continue
        suggested_net = _apply_bounds_directional(
            current_net, raw_suggested, decision, lower, upper, config
        )

        if decision == "UP" and suggested_net <= current_net + config.price_step / 2:
            out.at[idx, "Reason"] = "upward_move_blocked_by_guardrail"
            continue
        if decision == "DOWN" and suggested_net >= current_net - config.price_step / 2:
            out.at[idx, "Reason"] = "downward_move_blocked_by_guardrail"
            continue

        out.at[idx, "Direction"] = decision
        out.at[idx, "SuggestedNetPrice"] = suggested_net
        out.at[idx, "SuggestedNetDelta"] = suggested_net - current_net
        out.at[idx, "GuardrailLower"] = lower if lower is not None else np.nan
        out.at[idx, "GuardrailUpper"] = upper if upper is not None else np.nan
        out.at[idx, "GuardrailTags"] = ",".join(guardrail_tags)
        out.at[idx, "Reason"] = "local_market_position_anomaly"

    # Phase 5: choose mechanism. NET_ONLY_PROMO can never become a structural
    # list-price move. Consecutive DOWN signals become a list-price segment only
    # when both list and net independently support the structural direction.
    for _, group in out.groupby(["PricingUnitIdUsed", "Plan", "Currency"], sort=False):
        group = group.sort_values("Days")
        indices = list(group.index)
        directions = group["Direction"].tolist()
        structural_down = [
            directions[pos] == "DOWN"
            and _text(group.iloc[pos].get("DecisionBasis")) == "LIST_AND_NET"
            and _text(group.iloc[pos].get("ListSignalDirection")) == "DOWN"
            for pos in range(len(indices))
        ]
        for pos, idx in enumerate(indices):
            direction = directions[pos]
            basis = _text(out.at[idx, "DecisionBasis"])
            if direction == "UP":
                out.at[idx, "Mechanism"] = "LIST_PRICE"
            elif direction == "DOWN":
                if basis == "NET_ONLY_PROMO":
                    out.at[idx, "Mechanism"] = "PROMO"
                    continue
                prev_down = pos > 0 and structural_down[pos - 1]
                next_down = pos < len(directions) - 1 and structural_down[pos + 1]
                out.at[idx, "Mechanism"] = "LIST_PRICE_SEGMENT" if (structural_down[pos] and (prev_down or next_down)) else "PROMO"

    # Phase 6: convert net recommendation into an exact action that Step 2 can
    # apply. Existing promos are preserved for list-price moves; isolated down
    # signals use an approved promo only when it remains conservative and safe.
    for idx, rec in out.iterrows():
        if rec["Direction"] == "NONE" or pd.isna(rec["SuggestedNetPrice"]):
            continue
        if bool(rec.get("PriceConflict", False)):
            out.at[idx, "Reason"] = "current_scope_has_conflicting_prices"
            out.at[idx, "Direction"] = "NONE"
            out.at[idx, "Mechanism"] = "NONE"
            continue

        mechanism = str(rec["Mechanism"])
        current_list = float(rec["CurrentListPrice"])
        current_net = float(rec["CurrentNetPrice"])
        desired_net = float(rec["SuggestedNetPrice"])
        promo_type = _text(rec.get("CurrentPromoType"))
        promo_value = float(rec.get("CurrentPromoValue", 0.0) or 0.0)

        if mechanism in {"LIST_PRICE", "LIST_PRICE_SEGMENT"}:
            new_list = _list_price_for_target_net(
                desired_net, promo_type, promo_value, config.price_step
            )
            if abs(new_list - current_list) >= config.price_step / 2:
                out.at[idx, "SuggestedListPrice"] = new_list
                out.at[idx, "Actionable"] = True
                out.at[idx, "Reason"] = (
                    "consecutive_anchor_local_signal"
                    if mechanism == "LIST_PRICE_SEGMENT"
                    else "local_signal_list_price"
                )
            else:
                out.at[idx, "Direction"] = "NONE"
                out.at[idx, "Mechanism"] = "NONE"
                out.at[idx, "Reason"] = "list_price_rounding_removed_move"
        elif mechanism == "PROMO":
            code, promo_final = _choose_promo(
                current_list,
                current_net,
                desired_net,
                _number(rec.get("CostFloor")),
                bool(rec.get("AllowBelowCost", False)),
                promos,
                config,
            )
            # Do not let a promo convert a conservative <=5% recommendation into
            # a much larger price move.
            if code and promo_final is not None:
                promo_move_pct = abs(promo_final - current_net) / max(current_net, 1e-9)
                if promo_move_pct <= 0.06:
                    out.at[idx, "SuggestedPromoCode"] = code
                    out.at[idx, "SuggestedPromoFinalPrice"] = promo_final
                    out.at[idx, "SuggestedNetPrice"] = promo_final
                    out.at[idx, "SuggestedNetDelta"] = promo_final - current_net
                    out.at[idx, "Actionable"] = True
                    out.at[idx, "Reason"] = "isolated_anchor_local_signal"
                else:
                    out.at[idx, "Direction"] = "NONE"
                    out.at[idx, "Mechanism"] = "NONE"
                    out.at[idx, "Reason"] = "safe_promo_would_move_too_far"
            else:
                out.at[idx, "Direction"] = "NONE"
                out.at[idx, "Mechanism"] = "NONE"
                out.at[idx, "Reason"] = "isolated_down_signal_but_no_safe_promo"

    out = out.drop(columns=["_anchor_index"], errors="ignore")
    out = out.sort_values(
        ["Country", "PricingUnitIdUsed", "Plan", "Days", "Currency"],
        kind="stable",
    ).reset_index(drop=True)
    return out


def build_summary(recommendations: pd.DataFrame) -> pd.DataFrame:
    if recommendations.empty:
        return pd.DataFrame(columns=["Metric", "Value"])

    actionable = recommendations[recommendations["Actionable"].map(_boolish)]
    rows = [
        ("Anchor rows evaluated", len(recommendations)),
        ("Actionable recommendations", len(actionable)),
        ("Up recommendations", int((actionable["Direction"] == "UP").sum())),
        ("Down recommendations", int((actionable["Direction"] == "DOWN").sum())),
        ("List-price recommendations", int(actionable["Mechanism"].isin(["LIST_PRICE", "LIST_PRICE_SEGMENT"]).sum())),
        ("Promo recommendations", int((actionable["Mechanism"] == "PROMO").sum())),
        ("High confidence", int((actionable["Confidence"] == "HIGH").sum())),
        ("Medium confidence", int((actionable["Confidence"] == "MEDIUM").sum())),
        ("Low confidence", int((actionable["Confidence"] == "LOW").sum())),
        ("Pricing units with recommendations", int(actionable["PricingUnitIdUsed"].nunique())),
        ("Active-promo anchor rows", int(recommendations.get("HasActivePromo", pd.Series(False, index=recommendations.index)).map(_boolish).sum())),
        ("Promo-explained UP signals suppressed", int((recommendations.get("Reason", pd.Series("", index=recommendations.index)) == "active_promo_explains_low_net").sum())),
        ("List/net conflicts suppressed", int((recommendations.get("Reason", pd.Series("", index=recommendations.index)) == "list_net_signals_conflict").sum())),
        ("Net-only promo recommendations", int((actionable.get("DecisionBasis", pd.Series("", index=actionable.index)) == "NET_ONLY_PROMO").sum())),
    ]
    return pd.DataFrame(rows, columns=["Metric", "Value"])




def _rec_key(unit: Any, plan: Any, days: Any, gb: Any) -> tuple[str, str, float, float]:
    def number(value: Any) -> float:
        out = pd.to_numeric(value, errors="coerce")
        return -1.0 if pd.isna(out) else round(float(out), 6)
    return (
        str(unit if unit is not None else "").strip(),
        str(plan if plan is not None else "").strip(),
        number(days),
        number(gb),
    )


def annotate_price_book_with_recommendations(
    price_book_path: str | Path,
    recommendations: pd.DataFrame,
) -> Path:
    """Write one disposable Recommendation column into the canonical workbook.

    The column is advisory only. It is deliberately outside PRICING_COLUMNS, so
    pricing_book.read_pricing_workbook() ignores it when rebuilding canonical data.
    Shared pricing units therefore display the same recommendation on every member
    country row, while EUR/USD signals are consolidated into one compact cell.
    """
    price_book_path = Path(price_book_path)
    if not price_book_path.exists() or price_book_path.suffix.lower() != ".xlsx":
        return price_book_path

    rec = recommendations.copy() if recommendations is not None else pd.DataFrame()
    if not rec.empty:
        rec["Actionable"] = rec.get("Actionable", False).map(_boolish)
        rec = rec[rec["Actionable"]].copy()

    grouped: dict[tuple[str, str, float, float], list[dict[str, Any]]] = {}
    if not rec.empty:
        for _, row in rec.iterrows():
            key = _rec_key(row.get("PricingUnitIdUsed"), row.get("Plan"), row.get("Days"), row.get("GB"))
            grouped.setdefault(key, []).append(row.to_dict())

    wb = load_workbook(price_book_path)
    try:
        sheet_name = "Pricing" if "Pricing" in wb.sheetnames else wb.sheetnames[0]
        ws = wb[sheet_name]
        headers = {str(c.value or "").strip(): c.column for c in ws[1]}
        required = ["PricingUnitIdUsed", "Plan", "Days", "GB"]
        if any(name not in headers for name in required):
            return price_book_path

        rec_col = headers.get("Recommendation")
        if not rec_col:
            rec_col = ws.max_column + 1
            ws.cell(row=1, column=rec_col, value="Recommendation")

        header = ws.cell(row=1, column=rec_col)
        header.fill = PatternFill("solid", fgColor="E20074")
        header.font = Font(color="FFFFFF", bold=True)
        header.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(rec_col)].hidden = False
        ws.column_dimensions[get_column_letter(rec_col)].width = 18

        green_fill = PatternFill("solid", fgColor="E2F0D9")
        red_fill = PatternFill("solid", fgColor="FCE4D6")
        orange_fill = PatternFill("solid", fgColor="FFF2CC")
        green_font = Font(color="177A3F", bold=True)
        red_font = Font(color="B42318", bold=True)
        orange_font = Font(color="B54708", bold=True)
        neutral_font = Font(color="666666")

        for r in range(2, ws.max_row + 1):
            key = _rec_key(
                ws.cell(r, headers["PricingUnitIdUsed"]).value,
                ws.cell(r, headers["Plan"]).value,
                ws.cell(r, headers["Days"]).value,
                ws.cell(r, headers["GB"]).value,
            )
            rows = grouped.get(key, [])
            cell = ws.cell(r, rec_col)
            cell.value = ""
            cell.fill = PatternFill(fill_type=None)
            cell.font = neutral_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if not rows:
                continue

            parts = []
            directions = set()
            for item in sorted(rows, key=lambda x: str(x.get("Currency", ""))):
                direction = str(item.get("Direction", "")).upper()
                mechanism = str(item.get("Mechanism", "")).upper()
                currency = str(item.get("Currency", "")).upper()
                if direction not in {"UP", "DOWN"}:
                    continue
                directions.add(direction)
                arrow = "▲" if direction == "UP" else "▼"
                mech = "L" if mechanism == "LIST_PRICE" else ("P" if mechanism == "PROMO" else "")
                parts.append(f"{arrow}{mech} {currency}".strip())
            if not parts:
                continue

            # Collapse the common dual-currency case to a compact single indicator.
            if len(parts) == 2:
                left = parts[0].split()[0]
                right = parts[1].split()[0]
                if left == right:
                    value = left
                else:
                    value = " | ".join(parts)
            else:
                value = " | ".join(parts)
            cell.value = value
            if directions == {"UP"}:
                cell.fill, cell.font = green_fill, green_font
            elif directions == {"DOWN"}:
                cell.fill, cell.font = red_fill, red_font
            else:
                cell.fill, cell.font = orange_fill, orange_font

        wb.save(price_book_path)
    finally:
        wb.close()
    return price_book_path


def recommendation_outputs_are_stale(
    paths: PipelineFiles = FILES,
    *,
    price_book_path: str | Path | None = None,
    recommendations_path: str | Path | None = None,
) -> bool:
    price_book_path = Path(price_book_path or (paths.base_dir / "outputs" / "manual_prices" / "current" / "manual_prices_current.xlsx"))
    recommendations_path = Path(recommendations_path or (paths.work_dir / "pricing_recommendations" / "recommendations_latest.csv"))
    if not recommendations_path.exists():
        return True
    inputs = [Path(__file__), price_book_path, paths.market_annotated, paths.promos_json, paths.pricing_units_json]
    latest_input = max((p.stat().st_mtime for p in inputs if p.exists()), default=0.0)
    return recommendations_path.stat().st_mtime + 0.001 < latest_input


def generate_recommendations_from_files(
    paths: PipelineFiles = FILES,
    *,
    price_book_path: str | Path | None = None,
    market_path: str | Path | None = None,
    promos_path: str | Path | None = None,
    pricing_units_path: str | Path | None = None,
    output_path: str | Path | None = None,
    summary_path: str | Path | None = None,
    eur_to_usd: float | None = None,
    config: RecommendationConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    default_price_book = paths.base_dir / "outputs" / "manual_prices" / "current" / "manual_prices_current.xlsx"
    legacy_price_book = paths.base_dir / "outputs" / "manual_prices" / "current" / "manual_prices_current.csv"
    if price_book_path is None:
        price_book_path = default_price_book if default_price_book.exists() else legacy_price_book
    price_book_path = Path(price_book_path)
    market_path = Path(market_path or paths.market_annotated)
    promos_path = Path(promos_path or paths.promos_json)
    pricing_units_path = Path(pricing_units_path or (paths.base_dir / "inputs" / "pricing_units.json"))
    recommendation_dir = paths.work_dir / "pricing_recommendations"
    output_path = Path(output_path or recommendation_dir / "recommendations_latest.csv")
    summary_path = Path(summary_path or recommendation_dir / "recommendations_summary_latest.csv")

    if not price_book_path.exists():
        raise FileNotFoundError(f"Current price book not found: {price_book_path}")
    if not market_path.exists():
        raise FileNotFoundError(f"Current annotated market not found: {market_path}")

    prices = read_pricing_workbook(price_book_path) if price_book_path.suffix.lower() == ".xlsx" else pd.read_csv(price_book_path)
    raw_market = pd.read_csv(market_path, low_memory=False)
    raw_market, temporal_outliers_excluded = _exclude_temporal_market_anomalies(raw_market, paths)
    rate = float(eur_to_usd or _infer_eur_to_usd(raw_market))
    promos = _load_promos(promos_path)
    priority_countries = _load_priority_countries(pricing_units_path)

    recommendations = generate_recommendations(
        prices,
        raw_market,
        promos=promos,
        priority_countries=priority_countries,
        eur_to_usd=rate,
        config=config,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = build_summary(recommendations)
    # Annotate first, then timestamp the CSVs. This ordering is intentional:
    # the disposable Excel annotation must not make the workbook look newer
    # than the recommendation output and trigger a pointless recalculation on
    # every editor launch.
    if price_book_path.suffix.lower() == ".xlsx":
        annotate_price_book_with_recommendations(price_book_path, recommendations)
    recommendations.to_csv(output_path, index=False)
    summary.to_csv(summary_path, index=False)
    try:
        try:
            from market_insights import generate_market_insights
        except ImportError:
            from automation.market_insights import generate_market_insights
        generate_market_insights(paths, recommendations_path=output_path)
    except Exception as exc:
        # Recommendation generation must remain usable even if the optional HTML
        # insight view cannot be produced from an incomplete scrape history.
        print(f"Market insights warning: {exc}")

    print()
    print("Pricing Recommendations")
    print(f"Current price book: {price_book_path}")
    print(f"Current market:     {market_path}")
    print(f"Pricing units:      {pricing_units_path}")
    print(f"Priority countries: {len(priority_countries)} configured")
    print(f"EUR/USD used:       {rate:.4f}")
    print(f"Temporal outliers:  {temporal_outliers_excluded} excluded")
    if not summary.empty:
        for _, row in summary.iterrows():
            print(f"- {row['Metric']}: {row['Value']}")
    print(f"Saved: {output_path}")
    print(f"Saved: {summary_path}")
    return recommendations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate conservative non-regression pricing recommendations from the current price book and current market."
    )
    parser.add_argument("--price-book", default=None, help="Current manual_prices_current.xlsx")
    parser.add_argument("--market", default=None, help="market_prices_annotated_latest.csv")
    parser.add_argument("--promos", default=None, help="promos.json")
    parser.add_argument("--pricing-units", default=None, help="pricing_units.json with optional priority_country per shared unit")
    parser.add_argument("--output", default=None, help="Output recommendations CSV")
    parser.add_argument("--summary", default=None, help="Output summary CSV")
    parser.add_argument("--eur-usd", type=float, default=None, help="Override EUR/USD conversion rate")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    generate_recommendations_from_files(
        price_book_path=args.price_book,
        market_path=args.market,
        promos_path=args.promos,
        pricing_units_path=args.pricing_units,
        output_path=args.output,
        summary_path=args.summary,
        eur_to_usd=args.eur_usd,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
