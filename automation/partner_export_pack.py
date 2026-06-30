from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
    path: Path
    rows_written: int


def _read_required_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required export file not found: {path}")
    return pd.read_csv(path)


def _plan_key(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip().str.lower()


def build_partner_price_pack(
    local_export_dir: str | Path,
    pack_dir: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
) -> list[PartnerPackFile]:
    local_export_dir = Path(local_export_dir)
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)

    results: list[PartnerPackFile] = []

    for currency in currencies:
        currency = normalize_currency(currency)
        currency_dir = local_export_dir / currency
        country_prices = _read_required_csv(currency_dir / "HT_prices_last_export.csv")
        region_prices = _read_required_csv(currency_dir / "Region_prices.csv")
        merged = pd.concat([country_prices, region_prices], ignore_index=True, sort=False)

        if "Plan" not in merged.columns:
            raise ValueError(f"Missing Plan column in {currency} export.")

        plan_values = _plan_key(merged["Plan"])
        for pack_name, source_plans in PLAN_PACKS:
            wanted = {plan.lower() for plan in source_plans}
            out = merged.loc[plan_values.isin(wanted)].copy()
            output_path = pack_dir / f"HT_prices_{currency}_{pack_name}.csv"
            out.to_csv(output_path, index=False)
            results.append(
                PartnerPackFile(
                    currency=currency,
                    pack=pack_name,
                    path=output_path,
                    rows_written=len(out),
                )
            )

    return results
