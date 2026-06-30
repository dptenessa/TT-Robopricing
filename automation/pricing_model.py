import json
import numpy as np
import pandas as pd

from pricing_preparation import PACKAGE_CONFIG, STRATEGY_MAP

pd.set_option("future.no_silent_downcasting", True)

from config import (
    VAT,
    COUNTRY_SURFACE_MIN_ROWS,
    BLEND_SURFACE_MIN_ROWS,
    DAYS_LOG_OFFSET,
    GB_LOG_OFFSET,
    MIN_REL_STEP_GROWTH,
    MIN_ABS_STEP_GROWTH,
    CONCAVITY_DECAY_FACTOR,
    PROMO_CHECK_DAYS,
    PROMO_EPSILON,
    PROMO_LOW_PPG_MAX_LOWER_PCT,
    GB_TOLERANCE_RATIO,
    PROMO_TARGET_COMPETITOR_RANK,
    PROMO_TARGET_POSITION,
    PROMO_TARGET_MARGIN_PCT,
    PROMOS_PATH_DEFAULT
)
try:
    from config import DEFAULT_EUR_TO_USD
except Exception:
    DEFAULT_EUR_TO_USD = 1.10

from currency_support import DEFAULT_CURRENCY, convert_price, normalize_currency

def round_price_to_5_cents(price):
    return np.round(np.asarray(price, dtype=float) * 20) / 20


def IPG_fee(price):
    ipg_fee = (
                .9 * .2
                + .9 * .005 * price
                + .25 * .1
                + .00275 * .1
                + .05
                + .005 * price
                + .02 * .65 * price
                + .01 * .35 * price
                + .03
                + .002 * price
                + 20 * .01
            )
    return ipg_fee

def filter_extreme_low_ppg_outliers(
    relevant_comp: pd.DataFrame,
    ht_ppg: float,
    max_lower_pct: float = PROMO_LOW_PPG_MAX_LOWER_PCT,
) -> pd.DataFrame:
    """
    Remove competitor rows whose PricePerGB is too far below HT's own PricePerGB.

    max_lower_pct = 0.25 means:
    discard competitor offers with PricePerGB more than 25% below HT PricePerGB.
    """
    comp = relevant_comp.copy()

    if comp.empty or "PricePerGB" not in comp.columns:
        return comp

    if pd.isna(ht_ppg) or float(ht_ppg) <= 0:
        return comp

    min_allowed_ppg = float(ht_ppg) * (1.0 - max_lower_pct)

    return comp.loc[comp["PricePerGB"] >= min_allowed_ppg].copy()

def enforce_minimal_monotonicity(prices):
    prices = np.asarray(prices, dtype=float).copy()

    if len(prices) == 0:
        return prices

    for i in range(1, len(prices)):
        min_next = max(
            prices[i - 1] * (1.0 + MIN_REL_STEP_GROWTH),
            prices[i - 1] + MIN_ABS_STEP_GROWTH
        )
        prices[i] = max(prices[i], min_next)

    return prices


def enforce_concave_duration_curve(prices, days, decay_factor=CONCAVITY_DECAY_FACTOR):
    prices = np.asarray(prices, dtype=float).copy()
    days = np.asarray(days, dtype=float)

    if len(prices) < 3:
        return prices

    day_deltas = np.diff(days)
    if np.any(day_deltas <= 0):
        return prices

    slopes = np.diff(prices) / day_deltas

    if not np.any(slopes[1:] > slopes[:-1]):
        return prices

    new_prices = [prices[0]]
    prev_slope = slopes[0]

    for i in range(1, len(prices)):
        delta_days = days[i] - days[i - 1]
        curr_slope = (prices[i] - prices[i - 1]) / delta_days

        if curr_slope > prev_slope:
            curr_slope = prev_slope * decay_factor

        min_next = max(
            new_prices[-1] * (1.0 + MIN_REL_STEP_GROWTH),
            new_prices[-1] + MIN_ABS_STEP_GROWTH
        )
        next_price = max(new_prices[-1] + curr_slope * delta_days, min_next)

        realized_slope = (next_price - new_prices[-1]) / delta_days

        new_prices.append(next_price)
        prev_slope = min(prev_slope, realized_slope)

    return np.array(new_prices, dtype=float)


def fit_log_price_surface(country_fit: pd.DataFrame):
    log_days = np.log(country_fit["Days"].to_numpy(dtype=float) + DAYS_LOG_OFFSET)
    log_gb = np.log(country_fit["GB"].to_numpy(dtype=float) + GB_LOG_OFFSET)

    mean_log_days = log_days.mean()
    mean_log_gb = log_gb.mean()

    x_days = log_days - mean_log_days
    x_gb = log_gb - mean_log_gb
    interaction = x_days * x_gb

    y = np.log(country_fit["Price"].to_numpy(dtype=float))

    X = np.column_stack([
        np.ones(len(x_days)),
        x_days,
        x_gb,
        interaction
    ])

    coef, *_ = np.linalg.lstsq(X, y, rcond=None)

    feature_means = {
        "mean_log_days": mean_log_days,
        "mean_log_gb": mean_log_gb,
    }

    return coef, feature_means


def predict_log_price_from_surface(
    coef: np.ndarray,
    feature_means: dict,
    days_arr,
    gb_arr,
    overall_factor: float = 1.0
):
    days_arr = np.asarray(days_arr, dtype=float)
    gb_arr = np.asarray(gb_arr, dtype=float)

    log_days = np.log(days_arr + DAYS_LOG_OFFSET)
    log_gb = np.log(gb_arr + GB_LOG_OFFSET)

    x_days = log_days - feature_means["mean_log_days"]
    x_gb = log_gb - feature_means["mean_log_gb"]
    interaction = x_days * x_gb

    intercept, coef_days, coef_gb, coef_interaction = coef

    log_price = (
        intercept
        + coef_days * x_days
        + coef_gb * x_gb
        + coef_interaction * interaction
    )

    if overall_factor != 1.0:
        log_price = log_price + np.log(overall_factor)

    price = np.exp(log_price)
    return log_price, price


def fit_global_competition_surface(market_df: pd.DataFrame):
    all_country = market_df.loc[
        (market_df["Provider"].astype(str).str.lower() != "ht") &
        (market_df["GB"] > 0) &
        (market_df["Price"] > 0)
    ].copy()

    if all_country.empty:
        raise ValueError("No global country data available to fit fallback surface.")

    coef, feature_means = fit_log_price_surface(all_country)
    return coef, feature_means, len(all_country)


def normalize_area_covered(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []

    if isinstance(value, list):
        return [str(x).strip().upper() for x in value if str(x).strip()]

    raise ValueError(
        "Each 'area_covered' value in pricing_units.json must be a JSON array, "
        'for example: ["ES", "PT"]'
    )


def validate_pricing_units(pricing_units_df: pd.DataFrame) -> dict[str, int]:
    iso_to_row = {}
    duplicates = {}

    for idx, row in pricing_units_df.iterrows():
        pricing_unit_id = row.get("pricing_unit_id", f"row_{idx}")
        countries = row["area_covered"]

        for iso in countries:
            if iso in iso_to_row:
                prev_idx = iso_to_row[iso]
                prev_unit = pricing_units_df.loc[prev_idx].get("pricing_unit_id", f"row_{prev_idx}")
                duplicates.setdefault(iso, []).append(prev_unit)
                duplicates[iso].append(pricing_unit_id)
            else:
                iso_to_row[iso] = idx

    if duplicates:
        messages = []
        for iso, units in duplicates.items():
            unique_units = list(dict.fromkeys(units))
            messages.append(f"{iso}: {unique_units}")
        raise ValueError(
            "pricing_units.json contains ISO codes in multiple pricing units. "
            "Each ISO must belong to only one pricing unit. Conflicts: "
            + "; ".join(messages)
        )

    return iso_to_row



def load_pricing_units(pricing_units_path: str) -> pd.DataFrame:
    with open(pricing_units_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pricing_units_df = pd.DataFrame(data)

    required_cols = {"region", "area_covered"}
    missing = required_cols - set(pricing_units_df.columns)
    if missing:
        raise ValueError(f"pricing_units.json is missing required fields: {sorted(missing)}")

    if "pricing_unit_id" not in pricing_units_df.columns:
        pricing_units_df["pricing_unit_id"] = pd.NA

    pricing_units_df["area_covered"] = pricing_units_df["area_covered"].apply(normalize_area_covered)

    validate_pricing_units(pricing_units_df)

    return pricing_units_df


def load_promos(
    promos_path: str,
    currency: str = DEFAULT_CURRENCY,
    eur_to_usd: float = DEFAULT_EUR_TO_USD,
) -> list[dict]:
    with open(promos_path, "r", encoding="utf-8") as f:
        promos = json.load(f)

    if not isinstance(promos, list):
        raise ValueError("promos.json must contain a JSON array.")

    currency = normalize_currency(currency)
    out = []
    for promo in promos:
        if not isinstance(promo, dict):
            continue

        promo_code = str(promo.get("promo_code", "")).strip()
        promo_type = str(promo.get("promo_type", "")).strip().lower()
        promo_value = promo.get("promo_value", None)
        promo_label = str(promo.get("label", "")).strip()
        promo_currency = normalize_currency(promo.get("currency", promo.get("Currency", "USD")))

        if not promo_code or promo_value is None:
            continue

        if promo_type in {"percentage", "percent", "%"}:
            promo_type = "percentage"
        elif promo_type == "absolute":
            promo_type = "absolute"
        else:
            continue

        try:
            promo_value = float(promo_value)
        except Exception:
            continue

        if promo_type == "absolute":
            promo_value = convert_price(
                promo_value,
                promo_currency,
                currency,
                eur_to_usd,
            )

        out.append({
            "promo_code": promo_code,
            "promo_type": promo_type,
            "promo_value": promo_value,
            "label": promo_label,
            "currency": currency if promo_type == "absolute" else "",
        })

    return out


def build_sku_scope_key(iso: str, plan: str, days) -> str:
    return f"{str(iso).strip().upper()}|{str(plan).strip()}|{int(days)}"


def apply_promo_to_price(base_price: float, promo_type: str, promo_value: float) -> float:
    base_price = float(base_price)
    promo_type = str(promo_type).strip().lower()
    promo_value = float(promo_value)

    if promo_type == "absolute":
        final_price = base_price - promo_value
    elif promo_type == "percentage":
        final_price = base_price * (1.0 - promo_value / 100.0)
    else:
        raise ValueError(f"Unsupported promo_type: {promo_type}")

    return max(final_price, 0.0)


def initialize_promo_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    defaults = {
        "sku_scope_key": pd.NA,
        "OverridePrice": np.nan,
        "PromoScopeKey": pd.NA,
        "PromoCode": pd.NA,
        "PromoType": pd.NA,
        "PromoValue": np.nan,
        "PromoLabel": pd.NA,
        "PromoBasePrice": np.nan,
        "FinalPriceAfterPromo": np.nan,
        "CompetitorTargetPricePerGB": np.nan,
        "CompetitorMinPricePerGB": np.nan,
        "HTPricePerGB": np.nan,
    }

    for col, value in defaults.items():
        if col not in df.columns:
            df[col] = value

    return df


def find_pricing_unit_row(pricing_units_df: pd.DataFrame, iso: str) -> pd.Series | None:
    iso = str(iso).strip().upper()

    matches = pricing_units_df[
        pricing_units_df["area_covered"].apply(lambda countries: iso in countries)
    ]

    if matches.empty:
        return None

    if len(matches) > 1:
        units = matches["pricing_unit_id"].fillna("<missing>").astype(str).tolist()
        raise ValueError(
            f"ISO {iso} appears in multiple pricing units: {units}. "
            "Each ISO must belong to only one pricing unit."
        )

    return matches.iloc[0]


def get_competition_rows_for_countries(
    market_df: pd.DataFrame,
    countries: list[str]
) -> pd.DataFrame:
    countries_set = {str(x).strip().upper() for x in countries}

    return market_df.loc[
        (market_df["ISO"].astype(str).str.upper().isin(countries_set)) &
        (market_df["Provider"].astype(str).str.lower() != "ht") &
        (market_df["GB"] > 0) &
        (market_df["Price"] > 0)
    ].copy()


def get_market_region_for_iso(market_df: pd.DataFrame, iso: str) -> str | None:
    iso = str(iso).strip().upper()

    candidates = market_df.loc[
        market_df["ISO"].astype(str).str.upper() == iso
    ].copy()

    if candidates.empty:
        return None

    for col in ["Region", "region"]:
        if col in candidates.columns:
            vals = (
                candidates[col]
                .dropna()
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .unique()
                .tolist()
            )
            if vals:
                return vals[0]

    return None


def get_market_region_countries(market_df: pd.DataFrame, region_name: str) -> list[str]:
    if region_name is None:
        return []

    region_col = None
    if "Region" in market_df.columns:
        region_col = "Region"
    elif "region" in market_df.columns:
        region_col = "region"

    if region_col is None:
        return []

    countries = (
        market_df.loc[
            market_df[region_col].astype(str).str.strip() == str(region_name).strip(),
            "ISO"
        ]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
        .tolist()
    )

    return sorted(countries)


def enforce_cross_package_monotonicity(ht_df: pd.DataFrame) -> pd.DataFrame:
    ht_df = ht_df.copy()

    if ht_df.empty:
        return ht_df

    for day, subset in ht_df.groupby("Days"):
        subset = subset.sort_values(["GB", "Plan"]).copy()

        corrected_prices = enforce_minimal_monotonicity(
            subset["Price"].to_numpy(dtype=float)
        )

        ht_df.loc[subset.index, "Price"] = corrected_prices

    return ht_df


def get_relevant_competitor_rows_for_promo(
    competition_df: pd.DataFrame,
    ht_row: pd.Series,
    gb_tolerance_ratio: float = GB_TOLERANCE_RATIO,
) -> pd.DataFrame:
    days = float(ht_row["Days"])
    ht_gb = float(ht_row["GB"])
    ht_plan = str(ht_row.get("Plan", "")).strip().lower()
    ht_is_unlimited = "unlimited" in ht_plan

    comp = competition_df.copy()

    # exact same days only
    comp = comp.loc[
        (comp["GB"] > 0) &
        (comp["Price"] > 0) &
        (comp["Days"].astype(float) == days)
    ].copy()

    if comp.empty:
        return comp

    # non-unlimited HT should not compare against competitor unlimited offers
    if "Plan" in comp.columns:
        comp_plan = comp["Plan"].fillna("").astype(str).str.strip().str.lower()
        comp_is_unlimited = comp_plan.str.contains("unlimited", na=False)

        if ht_is_unlimited:
            comp = comp.loc[comp_is_unlimited].copy()
        else:
            comp = comp.loc[~comp_is_unlimited].copy()

    if comp.empty:
        return comp

    comp["GB"] = comp["GB"].astype(float)
    comp["gb_distance_abs"] = (comp["GB"] - ht_gb).abs()
    comp["gb_distance_rel"] = comp["gb_distance_abs"] / max(ht_gb, 1e-9)

    lower_gb = ht_gb * (1.0 - gb_tolerance_ratio)
    upper_gb = ht_gb * (1.0 + gb_tolerance_ratio)

    similar = comp.loc[comp["GB"].between(lower_gb, upper_gb, inclusive="both")].copy()

    if similar.empty:
        return similar

    return similar.sort_values(["gb_distance_rel", "gb_distance_abs", "Price"])


def get_target_competitor_ppg(
    competitor_ppgs: list[float],
    rank: int = PROMO_TARGET_COMPETITOR_RANK,
    position: str = PROMO_TARGET_POSITION,
    margin_pct: float = PROMO_TARGET_MARGIN_PCT,
) -> float | None:
    clean = sorted(float(x) for x in competitor_ppgs if pd.notna(x) and float(x) > 0)

    if not clean:
        return None

    distinct = []
    for x in clean:
        if not distinct or abs(x - distinct[-1]) > PROMO_EPSILON:
            distinct.append(x)

    rank = max(1, int(rank))
    idx = min(rank - 1, len(distinct) - 1)

    target_ppg = distinct[idx]

    if position == "below":
        target_ppg = target_ppg * (1.0 - margin_pct / 100.0)
    elif position == "above":
        target_ppg = target_ppg * (1.0 + margin_pct / 100.0)
    else:
        raise ValueError("PROMO_TARGET_POSITION must be 'below' or 'above'.")

    return target_ppg


def select_best_promo_for_row(
    base_price: float,
    gb: float,
    competitor_ppgs: list[float],
    promos: list[dict],
    rank: int = PROMO_TARGET_COMPETITOR_RANK,
    position: str = PROMO_TARGET_POSITION,
    margin_pct: float = PROMO_TARGET_MARGIN_PCT,
) -> dict | None:
    if gb <= 0:
        return None

    target_ppg = get_target_competitor_ppg(
        competitor_ppgs=competitor_ppgs,
        rank=rank,
        position=position,
        margin_pct=margin_pct,
    )

    if target_ppg is None:
        return None

    target_price = target_ppg * gb
    candidates = []

    for promo in promos:
        final_price = apply_promo_to_price(
            base_price=base_price,
            promo_type=promo["promo_type"],
            promo_value=promo["promo_value"]
        )
        final_ppg = final_price / gb

        if position == "below":
            qualifies = final_ppg < target_ppg - PROMO_EPSILON
        elif position == "above":
            qualifies = final_ppg > target_ppg + PROMO_EPSILON
        else:
            raise ValueError("PROMO_TARGET_POSITION must be 'below' or 'above'.")

        if qualifies:
            distance = abs(final_price - target_price)
            candidates.append({
                "promo_code": promo["promo_code"],
                "promo_type": promo["promo_type"],
                "promo_value": promo["promo_value"],
                "label": promo["label"],
                "final_price": final_price,
                "final_ppg": final_ppg,
                "distance_to_target_price": distance,
            })

    if not candidates:
        return None

    if position == "below":
        candidates.sort(
            key=lambda x: (
                x["distance_to_target_price"],
                -x["final_price"],
                x["promo_value"],
                str(x["promo_code"])
            )
        )
    else:
        candidates.sort(
            key=lambda x: (
                x["distance_to_target_price"],
                x["final_price"],
                x["promo_value"],
                str(x["promo_code"])
            )
        )

    return candidates[0]

def apply_competitive_promos(
    df: pd.DataFrame,
    competition_df: pd.DataFrame,
    promos: list[dict],
    competitor_rank: int = PROMO_TARGET_COMPETITOR_RANK,
    target_position: str = PROMO_TARGET_POSITION,
    target_margin_pct: float = PROMO_TARGET_MARGIN_PCT,
) -> pd.DataFrame:
    df = initialize_promo_columns(df)

    if df.empty or competition_df.empty or not promos:
        return df

    ht_mask = df["Provider"].astype(str).str.lower() == "ht"
    candidate_mask = ht_mask & df["Days"].isin(PROMO_CHECK_DAYS)

    ht_candidates = df.loc[candidate_mask].copy()
    if ht_candidates.empty:
        return df

    for idx, row in ht_candidates.iterrows():
        gb = float(row["GB"])
        base_price = float(row["Price"])
        iso = str(row["ISO"]).strip().upper()
        plan = str(row["Plan"]).strip()
        days = int(row["Days"])

        if gb <= 0 or base_price <= 0:
            continue

        ht_ppg = base_price / gb

        relevant_comp = get_relevant_competitor_rows_for_promo(competition_df, row)

        if relevant_comp.empty:
            continue

        relevant_comp = relevant_comp.copy()
        relevant_comp["PricePerGB"] = (
            relevant_comp["Price"].astype(float) / relevant_comp["GB"].astype(float)
        )
        relevant_comp = relevant_comp.loc[relevant_comp["PricePerGB"] > 0].copy()

        if relevant_comp.empty:
            continue

        relevant_comp = relevant_comp.drop_duplicates(
            subset=["Plan", "GB", "Days", "Price"]
        ).copy()

        relevant_comp = filter_extreme_low_ppg_outliers(
            relevant_comp,
            ht_ppg=ht_ppg,
            max_lower_pct=PROMO_LOW_PPG_MAX_LOWER_PCT,
        )

        if relevant_comp.empty:
            continue

        competitor_ppgs = sorted(relevant_comp["PricePerGB"].astype(float).tolist())
        competitor_min_ppg = float(min(competitor_ppgs))
        target_ppg = get_target_competitor_ppg(
            competitor_ppgs=competitor_ppgs,
            rank=competitor_rank,
            position=target_position,
            margin_pct=target_margin_pct,
        )

        if target_ppg is None:
            continue

        if target_position == "below":
            already_good = ht_ppg < target_ppg - PROMO_EPSILON
        else:
            already_good = ht_ppg > target_ppg + PROMO_EPSILON

        if already_good:
            continue

        best_promo = select_best_promo_for_row(
            base_price=base_price,
            gb=gb,
            competitor_ppgs=competitor_ppgs,
            promos=promos,
            rank=competitor_rank,
            position=target_position,
            margin_pct=target_margin_pct,
        )

        if best_promo is None:
            continue

        sku_scope_key = build_sku_scope_key(iso, plan, days)

        df.at[idx, "sku_scope_key"] = sku_scope_key
        df.at[idx, "OverridePrice"] = base_price
        df.at[idx, "PromoScopeKey"] = sku_scope_key
        df.at[idx, "PromoBasePrice"] = base_price
        df.at[idx, "HTPricePerGB"] = ht_ppg
        df.at[idx, "CompetitorMinPricePerGB"] = competitor_min_ppg
        df.at[idx, "CompetitorTargetPricePerGB"] = target_ppg

        df.at[idx, "PromoCode"] = best_promo["promo_code"]
        df.at[idx, "PromoType"] = best_promo["promo_type"]
        df.at[idx, "PromoValue"] = best_promo["promo_value"]
        df.at[idx, "PromoLabel"] = best_promo["label"]
        df.at[idx, "FinalPriceAfterPromo"] = round(best_promo["final_price"], 4)

    return df


def build_ht_prices(
    df: pd.DataFrame,
    market_df: pd.DataFrame,
    pricing_units_df: pd.DataFrame,
    global_coef: np.ndarray,
    global_feature_means: dict,
    cost_multiplier: float = 1+VAT,   #VAT, cuestionable!!!!!!!!
    strategy: str = "balanced",
    promos_path: str = PROMOS_PATH_DEFAULT,
    promo_target_competitor_rank: int = PROMO_TARGET_COMPETITOR_RANK,
    promo_target_position: str = PROMO_TARGET_POSITION,
    promo_target_margin_pct: float = PROMO_TARGET_MARGIN_PCT,
) -> pd.DataFrame:
    """
    Output remains country-level.

    If ISO3 exists in pricing_units.json:
      1. use all countries in same pricing unit for reference competition
      2. if not enough data, use same JSON region
      3. if still not enough, use global
      4. if no competition data at all, use cost-plus

    If ISO3 does NOT exist in pricing_units.json:
      1. use ISO3 only
      2. if not enough data, use same market-data region
      3. if still not enough, use global
      4. if no competition data at all, use cost-plus
    """
    df = df.copy()
    if df.empty:
        raise ValueError("build_ht_prices received an empty dataframe.")

    currency = DEFAULT_CURRENCY
    if "Currency" in df.columns:
        non_empty_currency = (
            df["Currency"]
            .dropna()
            .astype(str)
            .str.strip()
            .replace("", pd.NA)
            .dropna()
        )
        if not non_empty_currency.empty:
            currency = normalize_currency(non_empty_currency.iloc[0])

    eur_to_usd = DEFAULT_EUR_TO_USD
    if "EUR_TO_USD" in df.columns:
        rates = pd.to_numeric(df["EUR_TO_USD"], errors="coerce").dropna()
        if not rates.empty and float(rates.iloc[0]) > 0:
            eur_to_usd = float(rates.iloc[0])

    promos = load_promos(promos_path, currency=currency, eur_to_usd=eur_to_usd)

    iso = df["ISO"].iloc[0] if "ISO" in df.columns else None
    country_name = df["Country"].iloc[0] if "Country" in df.columns else None

    if iso is None or pd.isna(iso) or str(iso).strip() == "":
        raise ValueError("build_ht_prices requires df to contain a valid ISO.")

    iso = str(iso).strip().upper()

    unit_row = find_pricing_unit_row(pricing_units_df, iso)

    if unit_row is not None:
        pricing_unit_id = unit_row.get("pricing_unit_id", None)
        if pd.isna(pricing_unit_id) or str(pricing_unit_id).strip() == "":
            pricing_unit_id = iso

        unit_countries = [str(x).strip().upper() for x in unit_row["area_covered"]]
        region_name = unit_row["region"]

        region_rows = pricing_units_df[
            pricing_units_df["region"].astype(str).str.strip() == str(region_name).strip()
        ]

        region_countries = []
        for covered in region_rows["area_covered"]:
            region_countries.extend([str(x).strip().upper() for x in covered])

        region_countries = sorted(set(region_countries))
        pricing_source = "json_unit"
    else:
        pricing_unit_id = iso
        unit_countries = [iso]
        region_name = get_market_region_for_iso(market_df, iso)
        region_countries = get_market_region_countries(market_df, region_name)
        pricing_source = "iso_only"

        if not region_countries:
            region_countries = [iso]

    ht_mask = df["Provider"].astype(str).str.lower() == "ht"
    ht_df = df.loc[ht_mask].copy()

    if ht_df.empty:
        raise ValueError(f"No HT rows found for ISO {iso}.")

    unit_competition = get_competition_rows_for_countries(market_df, unit_countries)
    region_competition = get_competition_rows_for_countries(market_df, region_countries)
    global_competition = market_df.loc[
        (market_df["Provider"].astype(str).str.lower() != "ht") &
        (market_df["GB"] > 0) &
        (market_df["Price"] > 0)
    ].copy()

    n_unit = len(unit_competition)
    n_region = len(region_competition)

    print(f"  Output country: {iso}")
    print(f"  Pricing source: {pricing_source}")
    print(f"  Pricing unit id: {pricing_unit_id}")
    print(f"  Unit countries: {unit_countries}")
    print(f"  Region: {region_name}")
    print(f"  Region countries: {region_countries}")
    print(f"  Competition rows in unit scope: {n_unit}")
    print(f"  Competition rows in region scope: {n_region}")

    unit_coef = None
    unit_feature_means = None
    region_coef = None
    region_feature_means = None

    if n_unit >= COUNTRY_SURFACE_MIN_ROWS:
        surface_mode = "unit"
        print("  Surface mode: unit")
        unit_coef, unit_feature_means = fit_log_price_surface(unit_competition)

    elif n_unit >= BLEND_SURFACE_MIN_ROWS and n_region >= BLEND_SURFACE_MIN_ROWS:
        surface_mode = "blend_unit_region"
        print("  Surface mode: blended unit / region")
        unit_coef, unit_feature_means = fit_log_price_surface(unit_competition)
        region_coef, region_feature_means = fit_log_price_surface(region_competition)

    elif n_region >= BLEND_SURFACE_MIN_ROWS:
        surface_mode = "region"
        print("  Surface mode: region")
        region_coef, region_feature_means = fit_log_price_surface(region_competition)

    elif n_unit > 0 or n_region > 0:
        surface_mode = "global"
        print("  Sparse unit/region data — using global surface")

    else:
        surface_mode = "cost_plus"
        print("  No competition market data available — using cost-plus pricing")

    if surface_mode == "unit":
        promo_competition_df = unit_competition.copy()
    elif surface_mode in {"blend_unit_region", "region"}:
        promo_competition_df = region_competition.copy()
    elif surface_mode == "global":
        promo_competition_df = global_competition.copy()
    else:
        promo_competition_df = unit_competition.copy()
        if promo_competition_df.empty:
            promo_competition_df = region_competition.copy()
        if promo_competition_df.empty:
            promo_competition_df = global_competition.copy()

    strat = STRATEGY_MAP.get(strategy, STRATEGY_MAP["balanced"])
    overall_factor = strat["overall"]
    package_factor = strat["plan"]

    def get_surface_price(days_arr, gb_arr):
        if surface_mode == "cost_plus":
            raise RuntimeError("get_surface_price should not be called in cost_plus mode.")

        if surface_mode == "unit":
            _, price = predict_log_price_from_surface(
                unit_coef,
                unit_feature_means,
                days_arr,
                gb_arr,
                overall_factor=overall_factor
            )
            return price

        if surface_mode == "region":
            _, price = predict_log_price_from_surface(
                region_coef,
                region_feature_means,
                days_arr,
                gb_arr,
                overall_factor=overall_factor
            )
            return price

        if surface_mode == "blend_unit_region":
            weight_unit = (n_unit - BLEND_SURFACE_MIN_ROWS) / (
                COUNTRY_SURFACE_MIN_ROWS - 1 - BLEND_SURFACE_MIN_ROWS
            )
            weight_unit = float(np.clip(weight_unit, 0.0, 1.0))

            log_unit, _ = predict_log_price_from_surface(
                unit_coef,
                unit_feature_means,
                days_arr,
                gb_arr,
                overall_factor=overall_factor
            )
            log_region, _ = predict_log_price_from_surface(
                region_coef,
                region_feature_means,
                days_arr,
                gb_arr,
                overall_factor=overall_factor
            )

            blended_log = weight_unit * log_unit + (1.0 - weight_unit) * log_region
            return np.exp(blended_log)

        _, price = predict_log_price_from_surface(
            global_coef,
            global_feature_means,
            days_arr,
            gb_arr,
            overall_factor=overall_factor
        )
        return price

    for pkg in PACKAGE_CONFIG:
        subset = ht_df[ht_df["Plan"] == pkg].sort_values(["Days", "GB"])
        if subset.empty:
            continue

        days = subset["Days"].to_numpy()
        gb = subset["GB"].to_numpy()
        cost = subset["Cost"].to_numpy()

        if surface_mode == "cost_plus":
            base_prices = cost * cost_multiplier
        else:
            base_prices = get_surface_price(days, gb) * package_factor.get(pkg, 1.0)

        adjusted_prices = enforce_minimal_monotonicity(base_prices)

        cost_floor_base = cost * cost_multiplier
        cost_floor_reference = cost_floor_base + IPG_fee(cost_floor_base)

        df.loc[subset.index, "Price"] = adjusted_prices
        df.loc[subset.index, "Cost_Floor_Reference"] = cost_floor_reference
        df.loc[subset.index, "SurfaceModeUsed"] = surface_mode
        df.loc[subset.index, "PricingSourceUsed"] = pricing_source
        df.loc[subset.index, "PricingUnitIdUsed"] = pricing_unit_id
        df.loc[subset.index, "PricingRegionUsed"] = region_name
        df.loc[subset.index, "PricingUnitCountriesUsed"] = json.dumps(unit_countries)
        df.loc[subset.index, "Currency"] = currency

    ht_mask = df["Provider"].astype(str).str.lower() == "ht"
    ht_only = df.loc[ht_mask].copy()

    if not ht_only.empty:
        ht_only = enforce_cross_package_monotonicity(ht_only)
        df.loc[ht_only.index, "Price"] = ht_only["Price"]
        
    # Round HT prices to nearest 0.10, e.g. 2.21 -> 2.20, 2.26 -> 2.30
    ht_mask = df["Provider"].astype(str).str.lower() == "ht"
    df.loc[ht_mask, "Price"] = round_price_to_5_cents(df.loc[ht_mask, "Price"])

    ht_mask = df["Provider"].astype(str).str.lower() == "ht"
    if ht_mask.any():
        final_prices = df.loc[ht_mask, "Price"].astype(float)
        floor_refs = df.loc[ht_mask, "Cost_Floor_Reference"].astype(float)

        df.loc[ht_mask, "IsBelowCostFloor"] = final_prices < floor_refs
        df.loc[ht_mask, "Cost_Floor_Gap"] = np.maximum(floor_refs - final_prices, 0.0)

    df = apply_competitive_promos(
        df=df,
        competition_df=promo_competition_df,
        promos=promos,
        competitor_rank=promo_target_competitor_rank,
        target_position=promo_target_position,
        target_margin_pct=promo_target_margin_pct,
    )

    df["IsBelowCostFloor"] = (
        df["IsBelowCostFloor"]
        .fillna(False)
        .infer_objects(copy=False)
    )

    if "ISO" not in df.columns:
        df["ISO"] = iso

    if "Country" not in df.columns:
        df["Country"] = country_name if country_name is not None else iso

    return df


def check_cross_package_monotonicity(df: pd.DataFrame) -> pd.DataFrame:
    ht = df[df["Provider"] == "HT"].copy()
    issues = []

    for day, subset in ht.groupby("Days"):
        subset = subset.sort_values(["GB", "Plan"])
        prices = subset["Price"].to_numpy()
        gbs = subset["GB"].to_numpy()
        pkgs = subset["Plan"].tolist()

        iso3 = subset["ISO3"].iloc[0] if "ISO3" in subset.columns else None
        country = subset["Country"].iloc[0] if "Country" in subset.columns else iso3

        for i in range(1, len(prices)):
            if prices[i] < prices[i - 1]:
                issues.append({
                    "ISO3": iso3,
                    "Country": country,
                    "Days": day,
                    "PrevPlan": pkgs[i - 1],
                    "PrevGB": gbs[i - 1],
                    "PrevPrice": prices[i - 1],
                    "CurrPlan": pkgs[i],
                    "CurrGB": gbs[i],
                    "CurrPrice": prices[i],
                })

    return pd.DataFrame(issues)
