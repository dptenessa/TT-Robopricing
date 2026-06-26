from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


CURRENCIES = ("USD", "EUR")
DEFAULT_CURRENCY = "USD"
LINKED_USD_MODE = "linked_usd"
DUAL_CURRENCY_MODE = "dual"
DEFAULT_EUR_TO_USD = 1.10


def normalize_currency(value: Any, default: str = DEFAULT_CURRENCY) -> str:
    currency = str(value or "").strip().upper()
    if currency in {"$", "US$", "USD"}:
        return "USD"
    if currency in {"EUR", "EURO", "€"}:
        return "EUR"
    return default


def other_currency(currency: str) -> str:
    currency = normalize_currency(currency)
    return "EUR" if currency == "USD" else "USD"


def currency_folder(base_dir: str | Path, currency: str) -> Path:
    return Path(base_dir) / normalize_currency(currency)


def currency_price_column(currency: str) -> str:
    return f"Price_{normalize_currency(currency)}"


def currency_final_price_column(currency: str) -> str:
    return f"FinalPriceAfterPromo_{normalize_currency(currency)}"


def convert_price(value: Any, from_currency: str, to_currency: str, eur_to_usd: float) -> float:
    if pd.isna(value):
        return np.nan

    price = float(value)
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)

    if from_currency == to_currency:
        return price

    rate = float(eur_to_usd or DEFAULT_EUR_TO_USD)
    if rate <= 0:
        rate = DEFAULT_EUR_TO_USD

    if from_currency == "EUR" and to_currency == "USD":
        return price * rate

    return price / rate


def convert_price_series(
    series: pd.Series,
    from_currency: str,
    to_currency: str,
    eur_to_usd: float,
) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)

    if from_currency == to_currency:
        return values

    rate = float(eur_to_usd or DEFAULT_EUR_TO_USD)
    if rate <= 0:
        rate = DEFAULT_EUR_TO_USD

    if from_currency == "EUR" and to_currency == "USD":
        return values * rate

    return values / rate


def add_currency_price_columns(
    df: pd.DataFrame,
    *,
    currency_hint: str | None = None,
    eur_to_usd: float = DEFAULT_EUR_TO_USD,
    fill_missing_with_conversion: bool = True,
) -> pd.DataFrame:
    out = df.copy()
    hint = normalize_currency(currency_hint or DEFAULT_CURRENCY)

    if "Currency" not in out.columns:
        out["Currency"] = hint
    else:
        out["Currency"] = out["Currency"].apply(lambda value: normalize_currency(value, hint))

    for currency in CURRENCIES:
        col = currency_price_column(currency)
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")

    legacy_price = pd.to_numeric(out.get("Price", pd.Series(np.nan, index=out.index)), errors="coerce")

    lower_cols = {str(c).lower(): c for c in out.columns}
    if "usd_price" in lower_cols:
        out["Price_USD"] = out["Price_USD"].fillna(pd.to_numeric(out[lower_cols["usd_price"]], errors="coerce"))
    if "eur_price" in lower_cols:
        out["Price_EUR"] = out["Price_EUR"].fillna(pd.to_numeric(out[lower_cols["eur_price"]], errors="coerce"))

    for currency in CURRENCIES:
        mask = out["Currency"].eq(currency) & out[currency_price_column(currency)].isna()
        out.loc[mask, currency_price_column(currency)] = legacy_price.loc[mask]

    if fill_missing_with_conversion:
        out["Price_USD"] = out["Price_USD"].fillna(
            convert_price_series(out["Price_EUR"], "EUR", "USD", eur_to_usd)
        )
        out["Price_EUR"] = out["Price_EUR"].fillna(
            convert_price_series(out["Price_USD"], "USD", "EUR", eur_to_usd)
        )

    if "Price" not in out.columns:
        out["Price"] = np.nan
    out["Price"] = pd.to_numeric(out["Price"], errors="coerce")
    out["Price"] = out["Price"].fillna(out[currency_price_column(hint)])
    out["Currency"] = out["Currency"].where(out["Currency"].isin(CURRENCIES), hint)

    return out


def row_currency_key(row: pd.Series) -> str:
    iso = str(row.get("ISO", "") or row.get("ISO3", "")).strip().upper()
    scope = str(row.get("sku_scope_key", "") or row.get("PromoScopeKey", "")).strip()
    if not scope:
        package = str(row.get("Plan", "") or row.get("Package", "")).strip()
        days = row.get("Days", "")
        try:
            days = int(float(days))
        except Exception:
            days = str(days).strip()
        scope = f"{str(row.get('PricingUnitIdUsed', '')).strip()}|{package}|{days}"
    return f"{iso}|{scope}"


def merge_currency_tables(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames = []

    for currency, df in tables.items():
        currency = normalize_currency(currency)
        if df.empty:
            continue
        work = df.copy()
        work["Currency"] = currency
        work["_currency_row_key"] = work.apply(row_currency_key, axis=1)
        frames.append(work)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True, sort=False)
    out = combined.drop_duplicates("_currency_row_key", keep="first").copy()

    for currency in CURRENCIES:
        price_col = currency_price_column(currency)
        if price_col not in combined.columns:
            continue

        currency_rows = combined[
            combined["Currency"].astype(str).str.upper().eq(currency)
        ][["_currency_row_key", price_col]].copy()

        if currency_rows.empty:
            continue

        currency_rows[price_col] = pd.to_numeric(currency_rows[price_col], errors="coerce")
        price_by_key = (
            currency_rows.dropna(subset=[price_col])
            .drop_duplicates("_currency_row_key", keep="first")
            .set_index("_currency_row_key")[price_col]
        )

        mapped = out["_currency_row_key"].map(price_by_key)
        out[price_col] = mapped.combine_first(pd.to_numeric(out.get(price_col), errors="coerce"))

    out = out.drop(columns=["_currency_row_key"])
    return out


def find_named_file(folder: str | Path, names: list[str]) -> Path | None:
    root = Path(folder)
    if not root.exists():
        return None
    for name in names:
        candidate = root / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def find_currency_file(folder: str | Path, currency: str, names: list[str]) -> Path | None:
    root = Path(folder)
    currency = normalize_currency(currency)
    in_currency_folder = find_named_file(root / currency, names)
    if in_currency_folder is not None:
        return in_currency_folder
    lower_folder = find_named_file(root / currency.lower(), names)
    if lower_folder is not None:
        return lower_folder
    if currency == DEFAULT_CURRENCY:
        return find_named_file(root, names)
    return None
