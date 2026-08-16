from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import yaml

from currency_support import CURRENCIES, normalize_currency
from plan_labels import PARTNER_PLAN_PACKS, partner_display_plan_label


PARTNER_DROP_COLUMNS: tuple[str, ...] = (
    "EUR_TO_USD",
    "COST_EUR_TO_USD",
    "Price_USD",
    "Price_EUR",
    "usd_price",
    "eur_price",
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

PARTNER_DIFF_KEY_COLUMNS: tuple[str, ...] = ("ISO", "Plan", "Days", "GB")
PARTNER_DIFF_VALUE_COLUMNS: tuple[str, ...] = (
    "Country",
    "Price",
    "FinalPriceAfterPromo",
    "PromoCode",
    "PromoType",
    "PromoValue",
    "PromoCurrency",
    "PricingUnitCountriesUsed",
)
PARTNER_DIFF_OUTPUT_COLUMNS: tuple[str, ...] = (
    "ChangeType",
    "Currency",
    "PreviousDate",
    "CurrentDate",
    "ChangedFields",
    "ISO",
    "Country",
    "Plan",
    "Days",
    "GB",
    "PreviousPrice",
    "CurrentPrice",
    "PriceChange",
    "PreviousFinalPriceAfterPromo",
    "CurrentFinalPriceAfterPromo",
    "FinalPriceAfterPromoChange",
    "PreviousPromoCode",
    "CurrentPromoCode",
    "PreviousPricingUnitCountriesUsed",
    "CurrentPricingUnitCountriesUsed",
)

PARTNER_PRICE_OUTPUT_COLUMNS: tuple[str, ...] = (
    "Destination",
    "ocsOfferValidityPeriod",
    "price",
    "ocsOfferId",
    "ocsCountries",
    "ocsOfferLevelQuota",
    "ocsOfferLevelQuotaUom",
    "ocsOfferValidityUnits",
    "ListPrice",
)

OCS_OFFER_ID_COUNTRY_LIMITED = "190447115"
OCS_OFFER_ID_COUNTRY_UNLIMITED = "190447245"
OCS_OFFER_ID_GLOBAL_LIMITED = "190447135"
OCS_OFFER_ID_GLOBAL_UNLIMITED = "190447265"
OCS_OFFER_ID_LARGE_REGION_LIMITED = "190447125"
OCS_OFFER_ID_LARGE_REGION_UNLIMITED = "190447255"
OCS_OFFER_ID_MEDIUM_REGION_LIMITED = "190640635"
OCS_OFFER_ID_MEDIUM_REGION_UNLIMITED = "190640665"

OCS_OFFER_IDS: dict[str, dict[bool, str]] = {
    "COUNTRY": {
        False: OCS_OFFER_ID_COUNTRY_LIMITED,
        True: OCS_OFFER_ID_COUNTRY_UNLIMITED,
    },
    "GLOBAL": {
        False: OCS_OFFER_ID_GLOBAL_LIMITED,
        True: OCS_OFFER_ID_GLOBAL_UNLIMITED,
    },
    "LARGE_REGION": {
        False: OCS_OFFER_ID_LARGE_REGION_LIMITED,
        True: OCS_OFFER_ID_LARGE_REGION_UNLIMITED,
    },
    "MEDIUM_REGION": {
        False: OCS_OFFER_ID_MEDIUM_REGION_LIMITED,
        True: OCS_OFFER_ID_MEDIUM_REGION_UNLIMITED,
    },
}

REGION_MEMBERSHIP_CURRENT_NAME = "region_membership_current.json"
REGION_MEMBERSHIP_HISTORY_PREFIX = "region_membership_"
REGION_CHANGES_MEMBER_NAME = "TT_region_changes.csv"
DESTINATION_TABLE_MEMBER_NAME = "export-destination-table.json"


@dataclass(frozen=True)
class PartnerPackFile:
    currency: str
    pack: str
    member_name: str
    rows_written: int
    rows_removed_below_cost: int


@dataclass(frozen=True)
class PartnerPackDiffFile:
    currency: str
    member_name: str
    rows_written: int


@dataclass(frozen=True)
class PartnerPackResult:
    zip_path: Path
    files: list[PartnerPackFile]
    diff_files: list[PartnerPackDiffFile]


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


def _read_ppg_country_names(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Authoritative PPG file not found: {path}")

    ppg = pd.read_csv(path)
    ppg.columns = ppg.columns.astype(str).str.strip()

    iso_col = next(
        (
            col for col in ["ISO_Code_A2", "ISO", "ISO_A2", "Country_Code_A2"]
            if col in ppg.columns
        ),
        None,
    )
    country_col = next(
        (
            col for col in ppg.columns
            if str(col).strip().lower() in {
                "country",
                "country name",
                "country_name",
                "destination",
            }
        ),
        None,
    )

    if iso_col is None or country_col is None:
        raise ValueError(
            "PPG must contain an ISO A2 column and a canonical country-name column."
        )

    ppg["_iso"] = (
        ppg[iso_col]
        .astype("string")
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )
    ppg["_country"] = (
        ppg[country_col]
        .astype("string")
        .fillna("")
        .astype(str)
        .str.strip()
    )
    ppg = ppg[
        ppg["_iso"].str.len().eq(2)
        & ppg["_country"].ne("")
    ].copy()

    conflicts = (
        ppg.groupby("_iso")["_country"]
        .nunique(dropna=True)
        .loc[lambda values: values > 1]
    )
    if not conflicts.empty:
        raise ValueError(
            "PPG contains conflicting country names for ISO codes: "
            + ", ".join(conflicts.index.tolist())
        )

    return (
        ppg.drop_duplicates(subset=["_iso"], keep="last")
        .set_index("_iso")["_country"]
        .to_dict()
    )


def _validate_canonical_country_names(
    df: pd.DataFrame,
    ppg_country_names: dict[str, str],
    *,
    currency: str,
) -> None:
    """Country rows must already carry the current PPG name.

    Region/global rows are deliberately excluded because their ISO field is a
    region code rather than an ISO-3166 alpha-2 country code.
    """
    mismatches: list[str] = []

    for _, row in df.iterrows():
        iso = _country_code(row.get("ISO", ""))
        if len(iso) != 2:
            continue

        expected = str(ppg_country_names.get(iso, "")).strip()
        actual = str(row.get("Country", "") if row.get("Country", "") is not None else "").strip()
        if actual.lower() == "nan":
            actual = ""

        if not expected:
            mismatches.append(f"{iso}: missing in PPG")
        elif actual != expected:
            mismatches.append(f"{iso}: export={actual!r}, PPG={expected!r}")

    if mismatches:
        preview = "; ".join(mismatches[:12])
        if len(mismatches) > 12:
            preview += f"; ... and {len(mismatches) - 12} more"
        raise ValueError(
            f"{currency} partner export contains non-canonical country names. "
            "The pipeline must be corrected upstream; partner export will not "
            f"silently rewrite them. {preview}"
        )


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


def _history_root_for(local_export_dir: Path) -> Path:
    return local_export_dir.parent / "history"


def _membership_history_path(local_export_dir: Path, timestamp: str) -> Path:
    return _history_root_for(local_export_dir) / f"{REGION_MEMBERSHIP_HISTORY_PREFIX}{timestamp}.json"


def _read_membership_snapshot(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Required region membership snapshot not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid region membership snapshot: {path}")
    return data


def _membership_snapshot_from_region_prices(
    local_export_dir: Path,
    timestamp: str,
    currencies: list[str],
) -> dict[str, object]:
    """Reconstruct legacy membership history from saved regional price CSVs.

    This is used for exports created before region_membership_YYYYMMDD.json
    snapshots existed. The first available currency history file is enough
    because regional membership is generated consistently across currencies.
    """
    history_root = _history_root_for(local_export_dir)
    region_members: dict[str, set[str]] = {}

    for currency in currencies:
        path = history_root / currency / f"region_prices_{timestamp}.csv"
        if not path.exists():
            continue

        df = pd.read_csv(path)
        if df.empty:
            continue

        for _, row in df.iterrows():
            region = _country_code(
                row.get("ISO", "")
                or row.get("Country", "")
                or row.get("PricingRegionUsed", "")
            )
            if not region:
                continue

            countries = _country_list(row.get("PricingUnitCountriesUsed", ""))
            if countries:
                region_members.setdefault(region, set()).update(countries)

        if region_members:
            break

    if not region_members:
        raise FileNotFoundError(
            "No historical region membership snapshot or usable regional "
            f"price CSV was found for {timestamp}."
        )

    regions = {
        region: sorted(countries)
        for region, countries in sorted(region_members.items())
    }
    country_members: dict[str, list[str]] = {}
    for region, countries in regions.items():
        for country in countries:
            country_members.setdefault(country, []).append(region)

    snapshot: dict[str, object] = {
        "generated_from": f"historical region_prices_{timestamp}.csv",
        "regions": regions,
        "countries": {
            country: sorted(region_list)
            for country, region_list in sorted(country_members.items())
        },
    }

    history_path = _membership_history_path(local_export_dir, timestamp)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return snapshot


def _read_or_rebuild_membership_snapshot(
    local_export_dir: Path,
    timestamp: str,
    currencies: list[str],
) -> dict[str, object]:
    path = _membership_history_path(local_export_dir, timestamp)
    if path.exists():
        return _read_membership_snapshot(path)
    return _membership_snapshot_from_region_prices(
        local_export_dir,
        timestamp,
        currencies,
    )


def _region_catalog(regions_yaml: Path) -> dict[str, dict[str, str]]:
    """Load the authoritative commercial metadata for every managed region."""
    if not regions_yaml.exists():
        raise FileNotFoundError(f"regions.yaml not found: {regions_yaml}")

    data = yaml.safe_load(regions_yaml.read_text(encoding="utf-8")) or {}
    regions = data.get("regions") or {}
    if not isinstance(regions, dict):
        raise ValueError("regions.yaml must contain a 'regions' mapping.")

    allowed_types = {"GLOBAL", "LARGE_REGION", "MEDIUM_REGION"}
    catalog: dict[str, dict[str, str]] = {}

    for raw_code, raw_spec in regions.items():
        code = _country_code(raw_code)
        if not code:
            continue
        if not isinstance(raw_spec, dict):
            raise ValueError(
                f"Region {code!r} must be a mapping with region_type, "
                "region_product_name and countries."
            )

        region_type = str(raw_spec.get("region_type", "")).strip().upper()
        product_name = str(raw_spec.get("region_product_name", "")).strip()

        if region_type not in allowed_types:
            raise ValueError(
                f"Region {code!r} has unsupported region_type {region_type!r}. "
                f"Expected one of: {', '.join(sorted(allowed_types))}."
            )
        if not product_name:
            raise ValueError(
                f"Region {code!r} is missing region_product_name in regions.yaml."
            )

        catalog[code] = {
            "region_type": region_type,
            "region_product_name": product_name,
        }

    return catalog


def _managed_region_codes(region_catalog: dict[str, dict[str, str]]) -> list[str]:
    return list(region_catalog.keys())


def _region_code_for_row(
    row: pd.Series,
    region_catalog: dict[str, dict[str, str]],
) -> str:
    for field in ("ISO", "PricingUnitIdUsed", "PricingRegionUsed", "Country"):
        candidate = _country_code(row.get(field, ""))
        if candidate in region_catalog:
            return candidate
    return ""


def _partner_destination_value(
    row: pd.Series,
    region_catalog: dict[str, dict[str, str]],
) -> str:
    """Country names come from the validated country pipeline; regions from YAML."""
    iso = _country_code(row.get("ISO", ""))

    if len(iso) == 2:
        value = str(row.get("Country", "") if row.get("Country", "") is not None else "").strip()
        return "" if value.lower() == "nan" else value

    region_code = _region_code_for_row(row, region_catalog)
    if not region_code:
        raise ValueError(
            "Regional partner row could not be matched to regions.yaml. "
            f"ISO={row.get('ISO', '')!r}, Country={row.get('Country', '')!r}, "
            f"PricingUnitIdUsed={row.get('PricingUnitIdUsed', '')!r}, "
            f"PricingRegionUsed={row.get('PricingRegionUsed', '')!r}."
        )
    return region_catalog[region_code]["region_product_name"]


def _membership_pairs(snapshot: dict[str, object]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    regions = snapshot.get("regions", {})
    if not isinstance(regions, dict):
        return pairs
    for region, countries in regions.items():
        if not isinstance(countries, list):
            continue
        for country in countries:
            region_code = _country_code(region)
            country_code = _country_code(country)
            if region_code and country_code:
                pairs.add((region_code, country_code))
    return pairs


def _region_change_table(
    previous: dict[str, object],
    current: dict[str, object],
    *,
    previous_date: str,
    current_date: str,
) -> pd.DataFrame:
    previous_pairs = _membership_pairs(previous)
    current_pairs = _membership_pairs(current)
    rows: list[dict[str, str]] = []

    for region, country in sorted(current_pairs - previous_pairs):
        rows.append({
            "ChangeType": "Added",
            "Region": region,
            "CountryCode": country,
            "PreviousDate": previous_date,
            "CurrentDate": current_date,
        })
    for region, country in sorted(previous_pairs - current_pairs):
        rows.append({
            "ChangeType": "Removed",
            "Region": region,
            "CountryCode": country,
            "PreviousDate": previous_date,
            "CurrentDate": current_date,
        })

    return pd.DataFrame(
        rows,
        columns=[
            "ChangeType",
            "Region",
            "CountryCode",
            "PreviousDate",
            "CurrentDate",
        ],
    )


def _updated_destination_table(
    source_path: Path,
    membership_snapshot: dict[str, object],
    managed_regions: list[str],
) -> list[object]:
    if not source_path.exists():
        raise FileNotFoundError(f"Destination table JSON not found: {source_path}")
    data = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Destination table JSON must contain a list.")

    countries_map = membership_snapshot.get("countries", {})
    if not isinstance(countries_map, dict):
        countries_map = {}

    managed_set = set(managed_regions)
    for item in data:
        if not isinstance(item, dict):
            continue

        code = _country_code(item.get("code", ""))
        if code:
            item["destination"] = code

        if len(code) != 2:
            continue

        existing = item.get("regions", [])
        existing_regions = [
            str(region).strip()
            for region in existing
            if str(region).strip()
        ] if isinstance(existing, list) else []
        unmanaged = [region for region in existing_regions if region not in managed_set]
        actual = countries_map.get(code, [])
        actual_regions = [
            region
            for region in managed_regions
            if region in set(actual if isinstance(actual, list) else [])
        ]
        item["regions"] = unmanaged + actual_regions

    return data


def _load_export_tables(
    local_export_dir: Path,
    currencies: list[str],
    *,
    timestamp: str | None = None,
) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    tables: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for currency in currencies:
        if timestamp:
            currency_dir = _history_root_for(local_export_dir) / currency
            country_path = currency_dir / f"manual_prices_{timestamp}.csv"
            region_path = currency_dir / f"region_prices_{timestamp}.csv"
        else:
            currency_dir = local_export_dir / currency
            country_path = currency_dir / "manual_prices_current.csv"
            region_path = currency_dir / "region_prices_current.csv"

        tables[currency] = (_read_required_csv(country_path), _read_required_csv(region_path))
    return tables


def _shared_filter_rules(
    export_tables: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
) -> set[str]:
    """
    Return price-scope keys that must be blocked consistently across currencies.

    Important:
    Regional country membership is already validated by
    define_region_prices.py and stored in PricingUnitCountriesUsed.
    Do not derive a global country exclusion list from individual country
    prices here, because that would incorrectly remove countries from valid
    regional packs.
    """
    shared_blocked_price_keys: set[str] = set()

    for country_prices, region_prices in export_tables.values():
        for df in (country_prices, region_prices):
            below_cost = _below_cost_mask(df)
            shared_blocked_price_keys.update(
                key
                for key in _price_scope_keys(df.loc[below_cost])
                if key and not key.startswith("|")
            )

    return shared_blocked_price_keys


def _clean_partner_table(
    country_prices: pd.DataFrame,
    region_prices: pd.DataFrame,
    *,
    currency: str,
    shared_blocked_price_keys: set[str],
) -> tuple[pd.DataFrame, dict[str, int]]:
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

    # Preserve PricingUnitCountriesUsed exactly as generated by the regional
    # eligibility logic. The regional generator has already checked every
    # included country against the regional max prices in both currencies.
    merged["Plan"] = merged["Plan"].map(partner_display_plan_label)
    return merged, removed_by_plan


def _partner_output_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[col for col in PARTNER_DROP_COLUMNS if col in df.columns])


def _partner_ocs_countries(row: pd.Series) -> str:
    countries = _country_list(row.get("PricingUnitCountriesUsed", ""))
    if not countries:
        fallback = _country_code(row.get("ISO", ""))
        countries = [fallback] if fallback else []
    return "-".join(countries)


def _partner_ocs_offer_id(
    row: pd.Series,
    region_catalog: dict[str, dict[str, str]],
) -> str:
    is_unlimited = str(row.get("Plan", "")).strip().lower() == "unlimited"
    iso = _country_code(row.get("ISO", ""))

    if len(iso) == 2:
        offer_type = "COUNTRY"
    else:
        region_code = _region_code_for_row(row, region_catalog)
        if not region_code:
            raise ValueError(
                "Cannot determine OCS offer type for regional row because its "
                "region code is not present in regions.yaml. "
                f"ISO={row.get('ISO', '')!r}, Country={row.get('Country', '')!r}."
            )
        offer_type = region_catalog[region_code]["region_type"]

    try:
        return OCS_OFFER_IDS[offer_type][is_unlimited]
    except KeyError as exc:
        raise ValueError(
            f"No OCS offer ID configured for type {offer_type!r}, "
            f"unlimited={is_unlimited}."
        ) from exc

def _partner_number_text(value: object) -> str:
    if pd.isna(value):
        return ""
    number = pd.to_numeric(value, errors="coerce")
    if pd.notna(number):
        number = float(number)
        if number.is_integer():
            return str(int(number))
        return f"{number:.4f}".rstrip("0").rstrip(".")
    text = str(value if value is not None else "").strip()
    return "" if text.lower() == "nan" else text


def _partner_price_value(row: pd.Series) -> str:
    final_price = pd.to_numeric(row.get("FinalPriceAfterPromo", pd.NA), errors="coerce")
    if pd.notna(final_price):
        return _partner_number_text(final_price)
    return _partner_number_text(row.get("Price", ""))


def _partner_list_price_value(row: pd.Series) -> str:
    """Return the original price only when a promo changed the net price."""
    list_price = pd.to_numeric(row.get("Price", pd.NA), errors="coerce")
    net_price = pd.to_numeric(row.get("FinalPriceAfterPromo", pd.NA), errors="coerce")

    if pd.isna(list_price) or pd.isna(net_price):
        return ""

    # Price and FinalPriceAfterPromo are already expressed in the currency of
    # the current export file, so no cross-currency conversion is required.
    if abs(float(list_price) - float(net_price)) <= 1e-9:
        return ""

    return _partner_number_text(list_price)


def _partner_price_output_columns(
    df: pd.DataFrame,
    region_catalog: dict[str, dict[str, str]],
) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["Destination"] = df.apply(
        lambda row: _partner_destination_value(row, region_catalog),
        axis=1,
    )
    out["ocsOfferValidityPeriod"] = df.get("Days", pd.Series("", index=df.index)).map(_partner_number_text)
    out["price"] = df.apply(_partner_price_value, axis=1)
    out["ListPrice"] = df.apply(_partner_list_price_value, axis=1)
    out["ocsCountries"] = df.apply(_partner_ocs_countries, axis=1)
    joined = out.join(df, how="left")
    out["ocsOfferId"] = joined.apply(
        lambda row: _partner_ocs_offer_id(row, region_catalog),
        axis=1,
    )
    out["ocsOfferLevelQuota"] = df.get("GB", pd.Series("", index=df.index)).map(_partner_number_text)
    unlimited_mask = df.get("Plan", pd.Series("", index=df.index)).astype(str).str.strip().str.lower().eq("unlimited")
    out.loc[unlimited_mask, "ocsOfferLevelQuota"] = "Unlimited"
    out["ocsOfferLevelQuotaUom"] = "G"
    out["ocsOfferValidityUnits"] = "D"
    return out.loc[:, list(PARTNER_PRICE_OUTPUT_COLUMNS)]


def _diff_compare_value(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() == "nan":
        return ""
    try:
        number = float(text)
        return f"{number:.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return text


def _diff_key_for_row(row: pd.Series) -> str:
    return "|".join(_norm_key_value(row.get(col, "")) for col in PARTNER_DIFF_KEY_COLUMNS)


def _index_for_diff(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in PARTNER_DIFF_KEY_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out["_diff_key_base"] = out.apply(_diff_key_for_row, axis=1)
    out["_diff_duplicate"] = out.groupby("_diff_key_base", dropna=False).cumcount().astype(str)
    out["_diff_key"] = out["_diff_key_base"] + "|" + out["_diff_duplicate"]
    return out.set_index("_diff_key", drop=False)


def _partner_diff_table(
    previous: pd.DataFrame,
    current: pd.DataFrame,
    *,
    currency: str,
    previous_date: str,
    current_date: str,
) -> pd.DataFrame:
    previous_indexed = _index_for_diff(previous)
    current_indexed = _index_for_diff(current)
    all_keys = sorted(set(previous_indexed.index) | set(current_indexed.index))
    rows: list[dict[str, object]] = []

    for key in all_keys:
        prev_row = previous_indexed.loc[key] if key in previous_indexed.index else None
        curr_row = current_indexed.loc[key] if key in current_indexed.index else None

        if prev_row is None:
            change_type = "Added"
        elif curr_row is None:
            change_type = "Removed"
        else:
            changed_fields = [
                col for col in PARTNER_DIFF_VALUE_COLUMNS
                if _diff_compare_value(prev_row.get(col, "")) != _diff_compare_value(curr_row.get(col, ""))
            ]
            if not changed_fields:
                continue
            change_type = "Changed"

        source = curr_row if curr_row is not None else prev_row
        changed_fields_text = "" if change_type != "Changed" else ", ".join(changed_fields)

        row: dict[str, object] = {
            "ChangeType": change_type,
            "Currency": currency,
            "PreviousDate": previous_date,
            "CurrentDate": current_date,
            "ChangedFields": changed_fields_text,
        }
        for col in PARTNER_DIFF_KEY_COLUMNS:
            row[col] = source.get(col, "") if source is not None else ""
        row["Country"] = source.get("Country", "") if source is not None else ""
        row.update({
            "PreviousPrice": prev_row.get("Price", "") if prev_row is not None else "",
            "CurrentPrice": curr_row.get("Price", "") if curr_row is not None else "",
            "PriceChange": _numeric_delta(prev_row, curr_row, "Price"),
            "PreviousFinalPriceAfterPromo": prev_row.get("FinalPriceAfterPromo", "") if prev_row is not None else "",
            "CurrentFinalPriceAfterPromo": curr_row.get("FinalPriceAfterPromo", "") if curr_row is not None else "",
            "FinalPriceAfterPromoChange": _numeric_delta(prev_row, curr_row, "FinalPriceAfterPromo"),
            "PreviousPromoCode": prev_row.get("PromoCode", "") if prev_row is not None else "",
            "CurrentPromoCode": curr_row.get("PromoCode", "") if curr_row is not None else "",
            "PreviousPricingUnitCountriesUsed": prev_row.get("PricingUnitCountriesUsed", "") if prev_row is not None else "",
            "CurrentPricingUnitCountriesUsed": curr_row.get("PricingUnitCountriesUsed", "") if curr_row is not None else "",
        })
        rows.append(row)

    return pd.DataFrame(rows, columns=list(PARTNER_DIFF_OUTPUT_COLUMNS))


def _numeric_delta(prev_row: pd.Series | None, curr_row: pd.Series | None, col: str) -> float | str:
    if prev_row is None or curr_row is None:
        return ""
    previous = pd.to_numeric(prev_row.get(col, ""), errors="coerce")
    current = pd.to_numeric(curr_row.get(col, ""), errors="coerce")
    if pd.isna(previous) or pd.isna(current):
        return ""
    return round(float(current) - float(previous), 4)


def build_partner_price_pack(
    local_export_dir: str | Path,
    zip_path: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
    run_date: datetime | None = None,
    compare_timestamp: str | None = None,
    current_timestamp: str | None = None,
    destination_table_json: str | Path | None = None,
    regions_yaml: str | Path | None = None,
    ppg_csv: str | Path | None = None,
) -> PartnerPackResult:
    local_export_dir = Path(local_export_dir)
    zip_path = Path(zip_path)
    run_date = run_date or datetime.now()
    current_timestamp = current_timestamp or run_date.strftime("%Y%m%d")
    project_root = local_export_dir.parents[2]
    destination_table_json = Path(
        destination_table_json
        or project_root / "inputs" / "export-destination-table_reviewed.json"
    )
    regions_yaml = Path(
        regions_yaml
        or project_root / "inputs" / "regions.yaml"
    )
    ppg_csv = Path(
        ppg_csv
        or project_root / "inputs" / "WS_PPG.csv"
    )
    ppg_country_names = _read_ppg_country_names(ppg_csv)

    region_catalog = _region_catalog(regions_yaml)

    current_membership = _read_membership_snapshot(
        local_export_dir / REGION_MEMBERSHIP_CURRENT_NAME
    )
    managed_regions = _managed_region_codes(region_catalog)
    destination_table = _updated_destination_table(
        destination_table_json,
        current_membership,
        managed_regions,
    )
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path / f"TT_prices_{run_date.strftime('%y%m%d')}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[PartnerPackFile] = []
    diff_results: list[PartnerPackDiffFile] = []
    normalized_currencies = [normalize_currency(currency) for currency in currencies]
    export_tables = _load_export_tables(local_export_dir, normalized_currencies)
    shared_blocked_price_keys = _shared_filter_rules(export_tables)
    clean_tables: dict[str, tuple[pd.DataFrame, dict[str, int]]] = {}
    for currency, (country_prices, region_prices) in export_tables.items():
        clean_tables[currency] = _clean_partner_table(
            country_prices,
            region_prices,
            currency=currency,
            shared_blocked_price_keys=shared_blocked_price_keys,
        )
        _validate_canonical_country_names(
            clean_tables[currency][0],
            ppg_country_names,
            currency=currency,
        )

    comparison_tables: dict[str, tuple[pd.DataFrame, dict[str, int]]] = {}
    if compare_timestamp:
        previous_tables = _load_export_tables(local_export_dir, normalized_currencies, timestamp=compare_timestamp)
        previous_blocked_keys = _shared_filter_rules(previous_tables)
        for currency, (country_prices, region_prices) in previous_tables.items():
            comparison_tables[currency] = _clean_partner_table(
                country_prices,
                region_prices,
                currency=currency,
                shared_blocked_price_keys=previous_blocked_keys,
            )

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zip_file:
        zip_file.writestr(
            DESTINATION_TABLE_MEMBER_NAME,
            json.dumps(destination_table, indent=2, ensure_ascii=False) + "\n",
        )

        if compare_timestamp:
            previous_membership = _read_or_rebuild_membership_snapshot(
                local_export_dir,
                compare_timestamp,
                normalized_currencies,
            )
            region_diff = _region_change_table(
                previous_membership,
                current_membership,
                previous_date=compare_timestamp,
                current_date=current_timestamp,
            )
            zip_file.writestr(
                REGION_CHANGES_MEMBER_NAME,
                region_diff.to_csv(index=False),
            )
            diff_results.append(
                PartnerPackDiffFile(
                    currency="ALL",
                    member_name=REGION_CHANGES_MEMBER_NAME,
                    rows_written=len(region_diff),
                )
            )

        for currency in normalized_currencies:
            merged, removed_by_plan = clean_tables[currency]
            plan_values = _plan_key(merged["Plan"])
            for pack_name, source_plans in PARTNER_PLAN_PACKS:
                wanted = {partner_display_plan_label(plan).lower() for plan in source_plans}
                out = merged.loc[plan_values.isin(wanted)].copy()

                out["_sort_destination"] = out.apply(
                    lambda row: _partner_destination_value(row, region_catalog),
                    axis=1,
                )
                out["_sort_plan"] = out.get(
                    "Plan",
                    pd.Series("", index=out.index),
                ).astype(str)
                out["_sort_days"] = pd.to_numeric(
                    out.get(
                        "Days",
                        pd.Series("", index=out.index),
                    ),
                    errors="coerce",
                )
                out["_sort_gb"] = pd.to_numeric(
                    out.get(
                        "GB",
                        pd.Series("", index=out.index),
                    ),
                    errors="coerce",
                )

                out = out.sort_values(
                    [
                        "_sort_destination",
                        "_sort_plan",
                        "_sort_days",
                        "_sort_gb",
                    ],
                    kind="stable",
                    na_position="last",
                ).drop(
                    columns=[
                        "_sort_destination",
                        "_sort_plan",
                        "_sort_days",
                        "_sort_gb",
                    ]
                )

                out = _partner_price_output_columns(
                    out,
                    region_catalog,
                )
                member_name = f"TT_prices_{currency}_{pack_name}.csv"
                zip_file.writestr(member_name, out.to_csv(index=False))
                results.append(
                    PartnerPackFile(
                        currency=currency,
                        pack=pack_name,
                        member_name=member_name,
                        rows_written=len(out),
                        rows_removed_below_cost=sum(
                            int(removed_by_plan.get(plan.lower(), 0)) for plan in source_plans
                        ),
                    )
                )

        if compare_timestamp:
            for currency in normalized_currencies:
                previous, _previous_removed = comparison_tables[currency]
                current, _current_removed = clean_tables[currency]
                diff = _partner_diff_table(
                    _partner_output_columns(previous),
                    _partner_output_columns(current),
                    currency=currency,
                    previous_date=compare_timestamp,
                    current_date=current_timestamp,
                )
                member_name = f"TT_price_changes_{currency}.csv"
                zip_file.writestr(member_name, diff.to_csv(index=False))
                diff_results.append(
                    PartnerPackDiffFile(
                        currency=currency,
                        member_name=member_name,
                        rows_written=len(diff),
                    )
                )

    return PartnerPackResult(zip_path=zip_path, files=results, diff_files=diff_results)
