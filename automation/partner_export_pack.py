from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from currency_support import CURRENCIES, normalize_currency


PLAN_PACKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Basic", ("Basic",)),
    ("Medium", ("Medium", "Moderate")),
    ("Large", ("Large",)),
    ("Unlimited", ("Unlimited",)),
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
    return series.fillna(False).astype(str).str.strip().str.lower().isin({"true", "t", "yes", "y", "1"})


def _below_cost_mask(df: pd.DataFrame) -> pd.Series:
    if "IsBelowCostFloor" in df.columns:
        return _bool_series(df["IsBelowCostFloor"])
    if "IsBelowCalculatedCostFloor" in df.columns:
        return _bool_series(df["IsBelowCalculatedCostFloor"])
    if "Is_Below_Cost_Floor" in df.columns:
        return _bool_series(df["Is_Below_Cost_Floor"])
    raise ValueError("Partner export requires IsBelowCostFloor so below-cost prices can be removed.")


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

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zip_file:
        for currency in currencies:
            currency = normalize_currency(currency)
            currency_dir = local_export_dir / currency
            country_prices = _read_required_csv(currency_dir / "HT_prices_last_export.csv")
            region_prices = _read_required_csv(currency_dir / "Region_prices.csv")
            merged = pd.concat([country_prices, region_prices], ignore_index=True, sort=False)

            if "Plan" not in merged.columns:
                raise ValueError(f"Missing Plan column in {currency} export.")

            below_cost = _below_cost_mask(merged)
            removed_by_plan = (
                merged.loc[below_cost]
                .assign(_plan_key=_plan_key(merged.loc[below_cost, "Plan"]))
                .groupby("_plan_key")
                .size()
                .to_dict()
            )
            merged = merged.loc[~below_cost].copy()
            plan_values = _plan_key(merged["Plan"])
            for pack_name, source_plans in PLAN_PACKS:
                wanted = {plan.lower() for plan in source_plans}
                out = merged.loc[plan_values.isin(wanted)].copy()
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
