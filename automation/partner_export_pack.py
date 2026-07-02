from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from currency_support import CURRENCIES, normalize_currency
from plan_labels import PARTNER_PLAN_PACKS, display_plan_label


PARTNER_DROP_COLUMNS: tuple[str, ...] = (
    "EUR_TO_USD",
    "COST_EUR_TO_USD",
    "CalculatedCostFloor",
    "CostFloor_USD",
    "CostFloor_EUR",
    "IsBelowCostFloor",
    "IsBelowCalculatedCostFloor",
    "Is_Below_Cost_Floor",
    "USD_IsBelowCostFloor",
    "EUR_IsBelowCostFloor",
    "IsPartnerExportBlocked",
    "PartnerExportBlockReason",
    "ISO3",
    "Reference",
    "ReferenceProvider",
)


@dataclass(frozen=True)
class PartnerPackFile:
    currency: str
    pack: str
    member_name: str
    rows_written: int
    rows_removed_below_cost: int


@dataclass(frozen=True)
class PartnerPackResult:
    zip_path: Path
    files: list[PartnerPackFile]


def _read_required_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required export file not found: {path}")
    return pd.read_csv(path)


def _plan_key(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


def _bool_series(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip().str.lower().isin({"true", "t", "yes", "y", "1"})


def _below_cost_mask(df: pd.DataFrame) -> pd.Series:
    masks = []
    if "IsPartnerExportBlocked" in df.columns:
        masks.append(_bool_series(df["IsPartnerExportBlocked"]))
    if "USD_IsBelowCostFloor" in df.columns:
        masks.append(_bool_series(df["USD_IsBelowCostFloor"]))
    if "EUR_IsBelowCostFloor" in df.columns:
        masks.append(_bool_series(df["EUR_IsBelowCostFloor"]))
    if "IsBelowCostFloor" in df.columns:
        masks.append(_bool_series(df["IsBelowCostFloor"]))
    if "IsBelowCalculatedCostFloor" in df.columns:
        masks.append(_bool_series(df["IsBelowCalculatedCostFloor"]))
    if "Is_Below_Cost_Floor" in df.columns:
        masks.append(_bool_series(df["Is_Below_Cost_Floor"]))
    if masks:
        out = masks[0].copy()
        for mask in masks[1:]:
            out = out | mask
        return out
    raise ValueError("Partner export requires IsBelowCostFloor so below-cost prices can be removed.")


def _country_code(value: object) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.lower() == "nan" else text.upper()


def _norm_key_value(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "nan":
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
        return f"{number:.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return text.upper()


def _price_scope_keys(df: pd.DataFrame) -> pd.Series:
    index = df.index
    iso = df["ISO"] if "ISO" in df.columns else df.get("Country", pd.Series("", index=index))
    return (
        iso.map(_country_code)
        + "|"
        + df.get("Plan", pd.Series("", index=index)).map(_norm_key_value)
        + "|"
        + df.get("Days", pd.Series("", index=index)).map(_norm_key_value)
        + "|"
        + df.get("GB", pd.Series("", index=index)).map(_norm_key_value)
    )


def _country_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value if value is not None else "").strip()
        if not text or text.lower() == "nan":
            raw_items = []
        elif text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
                raw_items = list(parsed) if isinstance(parsed, (list, tuple, set)) else [text]
            except (ValueError, SyntaxError):
                raw_items = [text]
        elif "-" in text:
            raw_items = text.split("-")
        elif "," in text:
            raw_items = text.split(",")
        else:
            raw_items = [text]

    countries: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        code = _country_code(item).strip("'\" ")
        if code and code not in seen:
            countries.append(code)
            seen.add(code)
    return countries


def _below_cost_country_codes(country_prices: pd.DataFrame) -> set[str]:
    if "ISO" not in country_prices.columns:
        return set()
    below_cost = _below_cost_mask(country_prices)
    return {
        code
        for code in country_prices.loc[below_cost, "ISO"].map(_country_code)
        if code
    }


def _format_partner_country_lists(df: pd.DataFrame, excluded_countries: set[str]) -> pd.DataFrame:
    if "PricingUnitCountriesUsed" not in df.columns:
        return df
    df = df.copy()
    df["PricingUnitCountriesUsed"] = df["PricingUnitCountriesUsed"].map(
        lambda value: "-".join(
            country for country in _country_list(value)
            if country not in excluded_countries
        )
    )
    return df


def build_partner_price_pack(
    local_export_dir: str | Path,
    zip_path: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
    run_date: datetime | None = None,
) -> PartnerPackResult:
    local_export_dir = Path(local_export_dir)
    zip_path = Path(zip_path)
    run_date = run_date or datetime.now()
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path / f"TT_prices_{run_date.strftime('%y%m%d')}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[PartnerPackFile] = []
    normalized_currencies = [normalize_currency(currency) for currency in currencies]
    export_tables: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    shared_excluded_countries: set[str] = set()
    shared_blocked_price_keys: set[str] = set()

    for currency in normalized_currencies:
        currency_dir = local_export_dir / currency
        country_prices = _read_required_csv(currency_dir / "manual_prices_current.csv")
        region_prices = _read_required_csv(currency_dir / "region_prices_current.csv")
        export_tables[currency] = (country_prices, region_prices)
        shared_excluded_countries.update(_below_cost_country_codes(country_prices))
        for df in (country_prices, region_prices):
            below_cost = _below_cost_mask(df)
            shared_blocked_price_keys.update(
                key
                for key in _price_scope_keys(df.loc[below_cost])
                if key and not key.startswith("|")
            )

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zip_file:
        for currency in normalized_currencies:
            country_prices, region_prices = export_tables[currency]
            region_prices = _format_partner_country_lists(region_prices, shared_excluded_countries)
            frames = [df for df in (country_prices, region_prices) if not df.empty]
            merged = pd.concat(frames, ignore_index=True, sort=False) if frames else country_prices.copy()

            if "Plan" not in merged.columns:
                raise ValueError(f"Missing Plan column in {currency} export.")

            below_cost = _below_cost_mask(merged) | _price_scope_keys(merged).isin(shared_blocked_price_keys)
            removed_by_plan = (
                merged.loc[below_cost]
                .assign(_plan_key=_plan_key(merged.loc[below_cost, "Plan"]))
                .groupby("_plan_key")
                .size()
                .to_dict()
            )
            merged = merged.loc[~below_cost].copy()
            plan_values = _plan_key(merged["Plan"])
            for pack_name, source_plans in PARTNER_PLAN_PACKS:
                wanted = {plan.lower() for plan in source_plans}
                out = merged.loc[plan_values.isin(wanted)].copy()
                out["Plan"] = out["Plan"].map(display_plan_label)
                out = out.drop(columns=[col for col in PARTNER_DROP_COLUMNS if col in out.columns])
                member_name = f"TT_prices_{currency}_{pack_name}.csv"
                zip_file.writestr(member_name, out.to_csv(index=False))
                results.append(
                    PartnerPackFile(
                        currency=currency,
                        pack=pack_name,
                        member_name=member_name,
                        rows_written=len(out),
                        rows_removed_below_cost=sum(int(removed_by_plan.get(plan, 0)) for plan in wanted),
                    )
                )

    return PartnerPackResult(zip_path=zip_path, files=results)
