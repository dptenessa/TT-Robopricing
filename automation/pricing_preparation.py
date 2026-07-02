import numpy as np
import pandas as pd
import requests

from config import (
    UTILIZATION_OF_GB_IN_PRACTICE,
    K,
    DAYS_RANGE,
    PACKAGE_CONFIG,
    STRATEGY_MAP,
)
try:
    from config import DEFAULT_EUR_TO_USD
except Exception:
    DEFAULT_EUR_TO_USD = 1.10

from currency_support import (
    DEFAULT_CURRENCY,
    add_currency_price_columns,
    currency_price_column,
    normalize_currency,
)

# =================================================================
# DATA PREPARATION
# =================================================================

def get_eur_to_usd() -> float:
    url = "https://api.frankfurter.app/latest?from=EUR&to=USD"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        return float(data["rates"]["USD"])
    except Exception:
        return float(DEFAULT_EUR_TO_USD)

def normalize_iso(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.upper()


def prepare_market_data(
    market_file: str,
    currency: str = DEFAULT_CURRENCY,
    eur_to_usd: float = DEFAULT_EUR_TO_USD,
) -> pd.DataFrame:
    df = pd.read_csv(market_file)
    currency = normalize_currency(currency)

    required_cols = ["ISO", "GB", "Days", "Price"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Market file missing required columns: {missing}")

    df = df.copy()
    df = add_currency_price_columns(
        df,
        currency_hint=currency,
        eur_to_usd=eur_to_usd,
        fill_missing_with_conversion=True,
    )

    df["ISO"] = normalize_iso(df["ISO"])

    if "ISO3" in df.columns:
        df["ISO3"] = normalize_iso(df["ISO3"])

    df["GB"] = pd.to_numeric(df["GB"], errors="coerce")
    df["Days"] = pd.to_numeric(df["Days"], errors="coerce")
    df["Price"] = pd.to_numeric(df[currency_price_column(currency)], errors="coerce")
    df["Currency"] = currency

    df["GB"] = df["GB"].fillna(0)
    df.loc[df["GB"] == 0, "GB"] = 3 * df["Days"]

    df = df[
        (df["GB"] > 0) &
        (df["Price"] > 0) &
        df[["GB", "Days", "Price"]].notna().all(axis=1)
    ].copy()

    df["iso_key"] = normalize_iso(df["ISO"])
    df["ReferenceProvider"] = df["Provider"].astype(str).str.lower()
    df["Type"] = "market_price"

    if "Country" not in df.columns:
        df["Country"] = df["ISO"]

    return df


def prepare_ppg_data(ppg_file: str) -> pd.DataFrame:
    if str(ppg_file).lower().endswith(".csv"):
        df = pd.read_csv(ppg_file)
    else:
        df = pd.read_excel(ppg_file)

    iso_col = None
    for candidate in ["ISO", "ISO_Code_A2", "ISO_A2", "Country_Code_A2"]:
        if candidate in df.columns:
            iso_col = candidate
            break

    if iso_col is None:
        raise ValueError(
            "PPG file must contain ISO / ISO_Code_A2 / ISO_A2 / Country_Code_A2 column."
        )

    required_cols = [iso_col, "Min of Min"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"PPG file missing required columns: {missing}")

    df = df.copy()
    df["ISO"] = normalize_iso(df[iso_col])
    df["iso_key"] = normalize_iso(df["ISO"])

    if "country" not in df.columns:
        df["country"] = df["ISO"]

    return df


def get_countries_to_process(ppg_df: pd.DataFrame) -> list[str]:
    country_series = ppg_df["ISO"].dropna().astype(str).str.strip().str.upper()
    countries = sorted(country_series[country_series != ""].unique())
    return countries


def load_market_data_for_country(market_df: pd.DataFrame, country: str) -> pd.DataFrame:
    if not country:
        raise ValueError("Country ISO must be provided explicitly.")

    country_key = country.strip().upper()

    df = market_df.loc[market_df["iso_key"] == country_key].copy()
    df = df.reset_index(drop=True)
    df["ref_id"] = df.index

    return df


def build_ht_cost_curves(
    country: str,
    ppg_df: pd.DataFrame,
    eur_to_usd: float,
    currency: str = DEFAULT_CURRENCY,
) -> pd.DataFrame:
    if not country:
        raise ValueError("Country ISO must be provided explicitly.")

    currency = normalize_currency(currency)
    country_key = country.strip().upper()
    match = ppg_df.loc[ppg_df["iso_key"] == country_key]

    if match.empty:
        raise ValueError(f"Could not find wholesale cost (PPG) for ISO '{country}'.")

    ws_cost_per_gb_eur = float(match["Min of Min"].iloc[0])

    cost_per_gb = ws_cost_per_gb_eur * eur_to_usd if currency == "USD" else ws_cost_per_gb_eur

    country_name = match["country"].iloc[0] if "country" in match.columns else country

    rows = []

    for pkg_name, cfg in PACKAGE_CONFIG.items():
        avg_daily_gb = cfg["avg_daily"]
        daily_std_gb = cfg["daily_std"]

        for days in DAYS_RANGE:
            gb = avg_daily_gb * days + K * daily_std_gb * np.sqrt(days)
            cost = gb * UTILIZATION_OF_GB_IN_PRACTICE * cost_per_gb

            rows.append({
                "Provider": "HT",
                "Plan": pkg_name,
                "ISO": country_key,
                "Country": country_name,
                "Days": days,
                "GB": gb,
                "Cost": cost,
                "Price": np.nan,
                "Currency": currency,
                "Type": "cost",
                "EUR_TO_USD": eur_to_usd,
            })

    return pd.DataFrame(rows)


def build_unified_df(market_df_country: pd.DataFrame, ht_df: pd.DataFrame) -> pd.DataFrame:
    market_out = market_df_country.assign(Cost=np.nan, Plan=np.nan)
    unified_df = pd.concat([market_out, ht_df], ignore_index=True, sort=False)
    unified_df["GB"] = np.ceil(unified_df["GB"] / 0.25) * 0.25

    if "ISO" not in unified_df.columns:
        unified_df["ISO"] = pd.NA

    if "Country" not in unified_df.columns:
        unified_df["Country"] = unified_df["ISO"]

    return unified_df


def build_market_with_ht_costs(
    market_df: pd.DataFrame,
    country: str,
    ppg_df: pd.DataFrame,
    eur_to_usd: float,
    currency: str = DEFAULT_CURRENCY,
) -> pd.DataFrame:
    market_country = load_market_data_for_country(market_df, country)
    ht_df = build_ht_cost_curves(country, ppg_df, eur_to_usd, currency=currency)
    return build_unified_df(market_country, ht_df)
