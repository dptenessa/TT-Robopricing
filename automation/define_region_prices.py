from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

try:
    from config import INPUT_REGIONS, OUTPUT_NAME
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    INPUT_REGIONS = PROJECT_ROOT / "inputs" / "regions.yaml"
    OUTPUT_NAME = "region_prices_current.csv"

# Regional eligibility and anchor guardrails.
REGIONAL_MIN_VALID_SKUS = 50

# Unlimited 30-day is only a relative outlier guardrail when present.
# There is no fixed absolute ceiling, and a missing Unlimited 30-day SKU
# does not automatically disqualify an anchor candidate.
REGIONAL_ANCHOR_MEDIAN_MULTIPLIER = 2.0
REGION_MEMBERSHIP_OUTPUT_NAME = "region_membership_current.json"
REGION_COUNTRY_EXCLUSIONS_NAME = "region_country_exclusions.json"

try:
    from currency_support import CURRENCIES, DEFAULT_CURRENCY, DEFAULT_EUR_TO_USD, normalize_currency
except Exception:
    CURRENCIES = ("USD", "EUR")
    DEFAULT_CURRENCY = "USD"
    DEFAULT_EUR_TO_USD = 1.10

    def normalize_currency(value: Any, default: str = DEFAULT_CURRENCY) -> str:
        currency = str(value or "").strip().upper()
        return currency if currency in CURRENCIES else default


class NoBooleanSafeLoader(yaml.SafeLoader):
    pass


for first_letter, mappings in list(NoBooleanSafeLoader.yaml_implicit_resolvers.items()):
    NoBooleanSafeLoader.yaml_implicit_resolvers[first_letter] = [
        (tag, regexp)
        for tag, regexp in mappings
        if tag != "tag:yaml.org,2002:bool"
    ]


@dataclass(frozen=True)
class RegionGenerationResult:
    currency: str
    input_csv: Path
    output_csv: Path
    rows_written: int
    excluded_countries: tuple[str, ...]


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=NoBooleanSafeLoader)
    return data or {}


def parse_bool(value: Any) -> bool:
    return str(value).strip().upper() in {"TRUE", "T", "YES", "Y", "1"}


def parse_price(value: Any, default: float = 0.0) -> float:
    text = str(value if value is not None else "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return default


def round_regular_price(price: float) -> float:
    return round(float(price) * 20) / 20


def convert_price(value: float, from_currency: str, to_currency: str, eur_to_usd: float) -> float:
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)
    if from_currency == to_currency:
        return float(value)
    rate = float(eur_to_usd or DEFAULT_EUR_TO_USD)
    if rate <= 0:
        rate = DEFAULT_EUR_TO_USD
    if from_currency == "EUR" and to_currency == "USD":
        return float(value) * rate
    return float(value) / rate


def detect_dialect(path: str | Path) -> csv.Dialect:
    sample = Path(path).read_text(encoding="utf-8-sig")[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        return csv.excel


def _direct_region_countries(regions_data: dict[str, Any]) -> set[str]:
    countries: set[str] = set()
    if isinstance(regions_data.get("countries"), list):
        countries.update(str(country).strip() for country in regions_data["countries"])
    for spec in (regions_data.get("regions") or {}).values():
        if isinstance(spec, dict):
            items = spec.get("countries", [])
        else:
            items = spec or []
        countries.update(str(country).strip() for country in items if str(country).strip() != "*")
    for spec in (regions_data.get("base") or {}).values():
        countries.update(str(country).strip() for country in spec)
    return {country for country in countries if country}


def resolve_region(name: str, regions_data: dict[str, Any], seen: set[str] | None = None) -> set[str]:
    seen = seen or set()
    name = str(name).strip()
    if name in seen:
        raise ValueError(f"Circular region reference detected: {name}")
    seen.add(name)

    regions = regions_data.get("regions") or {}
    if name in regions:
        spec = regions[name]
        items = spec.get("countries", []) if isinstance(spec, dict) else spec
        return _resolve_region_items(items or [], regions_data, seen)

    base = regions_data.get("base") or {}
    derived = regions_data.get("derived") or {}
    if name in base:
        return {str(country).strip() for country in base[name] if str(country).strip()}
    if name in derived:
        return _resolve_region_items(derived[name] or [], regions_data, seen)

    return {name} if name else set()


def resolve_region_ordered(name: str, regions_data: dict[str, Any], seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    name = str(name).strip()
    if name in seen:
        raise ValueError(f"Circular region reference detected: {name}")
    seen.add(name)

    regions = regions_data.get("regions") or {}
    if name in regions:
        spec = regions[name]
        items = spec.get("countries", []) if isinstance(spec, dict) else spec
        return _resolve_region_items_ordered(items or [], regions_data, seen)

    base = regions_data.get("base") or {}
    derived = regions_data.get("derived") or {}
    if name in base:
        return _unique_ordered(str(country).strip() for country in base[name] if str(country).strip())
    if name in derived:
        return _resolve_region_items_ordered(derived[name] or [], regions_data, seen)

    return [name] if name else []


def _resolve_region_items(items: Iterable[Any], regions_data: dict[str, Any], seen: set[str]) -> set[str]:
    result: set[str] = set()
    region_names_set = set((regions_data.get("regions") or {}).keys())
    region_names_set.update((regions_data.get("base") or {}).keys())
    region_names_set.update((regions_data.get("derived") or {}).keys())

    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item == "*":
            result.update(_direct_region_countries(regions_data))
        elif item in region_names_set:
            result.update(resolve_region(item, regions_data, seen.copy()))
        else:
            result.add(item)
    return result


def _unique_ordered(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item).strip()
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _resolve_region_items_ordered(items: Iterable[Any], regions_data: dict[str, Any], seen: set[str]) -> list[str]:
    result: list[str] = []
    region_names_set = set((regions_data.get("regions") or {}).keys())
    region_names_set.update((regions_data.get("base") or {}).keys())
    region_names_set.update((regions_data.get("derived") or {}).keys())

    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item == "*":
            result.extend(sorted(_direct_region_countries(regions_data)))
        elif item in region_names_set:
            result.extend(resolve_region_ordered(item, regions_data, seen.copy()))
        else:
            result.append(item)
    return _unique_ordered(result)


def region_names(regions_data: dict[str, Any]) -> list[str]:
    helper_regions_to_skip = {"EU_EXTRA", "EUROPA_EXTRA"}
    if regions_data.get("regions"):
        names = list((regions_data.get("regions") or {}).keys())
    else:
        names = list((regions_data.get("base") or {}).keys()) + list((regions_data.get("derived") or {}).keys())
    return [
        str(name).strip()
        for name in names
        if str(name).strip() and str(name).strip() not in helper_regions_to_skip
    ]



def load_region_country_exclusions(path: str | Path) -> dict[str, list[str]]:
    """Load persistent country-to-region exclusions.

    File format:
    {
      "MA": ["AFRICA"],
      "CU": ["GLOBAL", "CARIBBEAN"]
    }
    """
    exclusion_path = Path(path)
    if not exclusion_path.exists():
        return {}

    try:
        data = json.loads(exclusion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for country, regions in data.items():
        country_code = _country_code(country)
        if not country_code:
            continue
        if not isinstance(regions, list):
            continue
        normalized_regions = _unique_ordered(
            str(region).strip()
            for region in regions
            if str(region).strip()
        )
        if normalized_regions:
            normalized[country_code] = normalized_regions
    return dict(sorted(normalized.items()))


def save_region_country_exclusions(
    path: str | Path,
    exclusions: dict[str, list[str]],
) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    normalized = {
        _country_code(country): _unique_ordered(
            str(region).strip()
            for region in regions
            if str(region).strip()
        )
        for country, regions in exclusions.items()
        if _country_code(country)
    }
    normalized = {
        country: regions
        for country, regions in sorted(normalized.items())
        if regions
    }

    output_path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def configured_regions_for_country(
    country: str,
    regions_data: dict[str, Any],
) -> list[str]:
    country_code = _country_code(country)
    if not country_code:
        return []
    return [
        region_name
        for region_name in region_names(regions_data)
        if country_code in {
            _country_code(item)
            for item in resolve_region_ordered(region_name, regions_data)
        }
    ]


def _required_columns(fieldnames: list[str]) -> None:
    required = {
        "ISO", "Days", "Plan", "GB",
        "Price_USD", "Price_EUR",
        "CostFloor_USD", "CostFloor_EUR",
    }
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"Missing required consolidated pricing columns: {sorted(missing)}")

    for currency in CURRENCIES:
        final_col = f"FinalPriceAfterPromo_{currency}"
        if final_col not in fieldnames:
            raise ValueError(f"Missing required consolidated pricing column: {final_col}")


def _currency_rows_from_consolidated(
    rows: list[dict[str, Any]],
    currency: str,
) -> list[dict[str, Any]]:
    """Create an in-memory one-currency view without persisting duplicate files."""
    currency = normalize_currency(currency)
    out: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["Currency"] = currency
        row["Price"] = source.get(f"Price_{currency}", "")
        row["FinalPriceAfterPromo"] = source.get(
            f"FinalPriceAfterPromo_{currency}",
            source.get(f"Price_{currency}", ""),
        )
        row["IsBelowCostFloor"] = source.get(f"{currency}_IsBelowCostFloor", False)
        out.append(row)
    return out

def _is_below_cost(row: dict[str, Any]) -> bool:
    return (
        parse_bool(row.get("IsPartnerExportBlocked"))
        or parse_bool(row.get("USD_IsBelowCostFloor"))
        or parse_bool(row.get("EUR_IsBelowCostFloor"))
        or parse_bool(row.get("IsBelowCostFloor"))
        or parse_bool(row.get("IsBelowCalculatedCostFloor"))
        or parse_bool(row.get("Is_Below_Cost_Floor"))
    )


def _country_code(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    return "" if not text or text.lower() == "nan" else text.upper()


def _excluded_countries_from_rows(rows: list[dict[str, Any]]) -> set[str]:
    return {
        code
        for row in rows
        for code in [_country_code(row.get("ISO", ""))]
        if code and _is_below_cost(row)
    }


def _read_pricing_rows(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    dialect = detect_dialect(path)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        rows = [
            row for row in reader
            if any(str(value if value is not None else "").strip() for value in row.values())
        ]
        return rows, list(reader.fieldnames or [])


def _row_final_price(row: dict[str, Any]) -> float:
    final_price = parse_price(row.get("FinalPriceAfterPromo"), default=float("nan"))
    if final_price == final_price:
        return final_price
    return parse_price(row.get("Price"))


def _set_if_present(row: dict[str, Any], fieldnames: list[str], key: str, value: Any) -> None:
    if key in fieldnames:
        row[key] = value


def _rate_for_row(row: dict[str, Any]) -> float:
    rate = parse_price(row.get("EUR_TO_USD"), DEFAULT_EUR_TO_USD)
    return rate if rate > 0 else DEFAULT_EUR_TO_USD


def _normalized_key_part(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "nan":
        return ""
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
        return f"{number:g}"
    except ValueError:
        return text


def _regional_sku_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _normalized_key_part(row.get("Provider", "")),
        _normalized_key_part(row.get("Plan", "")),
        _normalized_key_part(row.get("Days", "")),
    )


def _cost_floor_for_currency(row: dict[str, Any], currency: str) -> float | None:
    currency = normalize_currency(currency)
    floor = parse_price(row.get(f"CostFloor_{currency}"), default=float("nan"))
    if floor == floor:
        return float(floor)

    row_currency = normalize_currency(row.get("Currency") or DEFAULT_CURRENCY)
    if row_currency == currency:
        floor = parse_price(row.get("CalculatedCostFloor"), default=float("nan"))
        if floor == floor:
            return float(floor)
    return None



def _below_cost_override_key(row: dict[str, Any]) -> str:
    return "|".join([
        _country_code(row.get("ISO", "")),
        str(row.get("Plan", "") or "").strip().upper(),
        _normalized_key_part(row.get("Days", "")),
        _normalized_key_part(row.get("GB", "")),
    ])


def _is_unlimited_30d_sku(sku_key: tuple[str, str, str]) -> bool:
    _provider, plan, days = sku_key
    plan_text = str(plan).strip().lower()

    try:
        days_value = float(days)
    except (TypeError, ValueError):
        return False

    return "unlimited" in plan_text and days_value == 30.0


def _country_valid_skus(
    country: str,
    sku_keys: set[tuple[str, str, str]],
    price_by_currency_country_sku: dict[str, dict[tuple[str, tuple[str, str, str]], float]],
    floor_by_currency_country_sku: dict[str, dict[tuple[str, tuple[str, str, str]], float]],
    required_currencies: list[str],
    below_cost_override_country_skus: set[tuple[str, tuple[str, str, str]]] | None = None,
) -> set[tuple[str, str, str]]:
    """Return SKUs whose own prices pass own floors in all currencies."""
    valid_skus: set[tuple[str, str, str]] = set()
    overrides = below_cost_override_country_skus or set()
    for sku_key in sku_keys:
        overridden = (country, sku_key) in overrides
        if all(
            price_by_currency_country_sku.get(currency, {}).get((country, sku_key)) is not None
            and (
                overridden
                or (
                    floor_by_currency_country_sku.get(currency, {}).get((country, sku_key)) is not None
                    and price_by_currency_country_sku[currency][(country, sku_key)]
                    >= floor_by_currency_country_sku[currency][(country, sku_key)]
                )
            )
            for currency in required_currencies
        ):
            valid_skus.add(sku_key)
    return valid_skus


def _anchor_supported_skus(
    anchor_prices_by_currency: dict[str, dict[tuple[str, str, str], float]],
    candidate_sku_keys: set[tuple[str, str, str]],
    eligible_countries: list[str],
    floor_by_currency_country_sku: dict[str, dict[tuple[str, tuple[str, str, str]], float]],
    required_currencies: list[str],
    below_cost_override_country_skus: set[tuple[str, tuple[str, str, str]]] | None = None,
) -> set[tuple[str, str, str]]:
    """Keep only SKUs whose anchor price passes every eligible country's floor."""
    supported: set[tuple[str, str, str]] = set()
    overrides = below_cost_override_country_skus or set()
    for sku_key in candidate_sku_keys:
        ok = True
        for country in eligible_countries:
            overridden = (country, sku_key) in overrides
            for currency in required_currencies:
                anchor_price = anchor_prices_by_currency.get(currency, {}).get(sku_key)
                required_floor = floor_by_currency_country_sku.get(currency, {}).get((country, sku_key))
                if anchor_price is None or (not overridden and (required_floor is None or anchor_price < required_floor)):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            supported.add(sku_key)
    return supported

def _build_region_pricing_decisions(
    rows_by_currency: dict[str, list[dict[str, Any]]],
    regions_data: dict[str, Any],
    region_country_exclusions: dict[str, list[str]] | None = None,
    allow_below_cost_keys: set[str] | None = None,
) -> tuple[
    dict[str, dict[str, dict[tuple[str, str, str], float]]],
    dict[str, dict[str, dict[tuple[str, str, str], dict[str, Any]]]],
    dict[str, list[str]],
    set[str],
]:
    """
    Country eligibility:
    - At least REGIONAL_MIN_VALID_SKUS unique Provider + Plan + Days SKUs.
    - Each counted SKU has valid prices above floor in both USD and EUR.

    Regional SKU eligibility:
    - A failed SKU is removed from the region, not the country.
    - The anchor price must pass every eligible country's floor in both currencies.

    Anchor guardrail:
    - No fixed Unlimited 30-day ceiling.
    - When present, Unlimited 30-day USD must not exceed regional median times
      REGIONAL_ANCHOR_MEDIAN_MULTIPLIER.
    - Missing Unlimited 30-day does not disqualify the candidate.
    """
    exclusions_by_country = {
        _country_code(country): set(regions)
        for country, regions in (region_country_exclusions or {}).items()
        if _country_code(country)
    }

    allow_below_cost_keys = {
        str(key).strip().upper()
        for key in (allow_below_cost_keys or set())
        if str(key).strip()
    }

    normalized_rows_by_currency = {
        normalize_currency(currency): rows
        for currency, rows in rows_by_currency.items()
        if rows
    }
    required_currencies = ["USD", "EUR"]
    missing_required = [c for c in required_currencies if c not in normalized_rows_by_currency]
    if missing_required:
        raise ValueError(
            "Regional price generation requires both USD and EUR exports. "
            f"Missing: {', '.join(missing_required)}"
        )

    price_by_currency_country_sku = {}
    row_by_currency_country_sku = {}
    floor_by_currency_country_sku = {}
    below_cost_override_country_skus: set[tuple[str, tuple[str, str, str]]] = set()

    for currency in required_currencies:
        price_index = {}
        row_index = {}
        floor_index = {}
        for row in normalized_rows_by_currency[currency]:
            country = _country_code(row.get("ISO", ""))
            if not country:
                continue
            sku_key = _regional_sku_key(row)
            index_key = (country, sku_key)
            if _below_cost_override_key(row).upper() in allow_below_cost_keys:
                below_cost_override_country_skus.add(index_key)
            price = round_regular_price(_row_final_price(row))
            if index_key not in price_index or price > price_index[index_key]:
                price_index[index_key] = price
                row_index[index_key] = row
            floor = _cost_floor_for_currency(row, currency)
            if floor is not None and (index_key not in floor_index or floor > floor_index[index_key]):
                floor_index[index_key] = float(floor)
        price_by_currency_country_sku[currency] = price_index
        row_by_currency_country_sku[currency] = row_index
        floor_by_currency_country_sku[currency] = floor_index

    regional_prices_by_region = {}
    regional_source_rows_by_region = {}
    eligible_countries_by_region = {}
    excluded_countries: set[str] = set()

    for region_name in region_names(regions_data):
        configured_countries = _unique_ordered(
            code
            for code in (
                _country_code(country)
                for country in resolve_region_ordered(region_name, regions_data)
            )
            if code and region_name not in exclusions_by_country.get(code, set())
        )
        if not configured_countries:
            continue

        all_sku_keys: set[tuple[str, str, str]] = set()
        for currency in required_currencies:
            for indexed_country, sku_key in price_by_currency_country_sku[currency].keys():
                if indexed_country in configured_countries:
                    all_sku_keys.add(sku_key)
        if not all_sku_keys:
            excluded_countries.update(configured_countries)
            continue

        ordered_sku_keys = sorted(
            all_sku_keys,
            key=lambda sku: (str(sku[0]).lower(), str(sku[1]).lower(), float(sku[2])),
        )
        valid_skus_by_country = {
            country: _country_valid_skus(
                country,
                all_sku_keys,
                price_by_currency_country_sku,
                floor_by_currency_country_sku,
                required_currencies,
                below_cost_override_country_skus,
            )
            for country in configured_countries
        }
        eligible_countries = [
            country
            for country in configured_countries
            if len(valid_skus_by_country[country]) >= REGIONAL_MIN_VALID_SKUS
        ]
        excluded_countries.update(c for c in configured_countries if c not in eligible_countries)
        if not eligible_countries:
            continue

        unlimited_30d_keys = {sku for sku in ordered_sku_keys if _is_unlimited_30d_sku(sku)}
        unlimited_30d_usd_by_country = {}
        for country in eligible_countries:
            prices = [
                price_by_currency_country_sku["USD"][(country, sku)]
                for sku in unlimited_30d_keys
                if (country, sku) in price_by_currency_country_sku["USD"]
                and sku in valid_skus_by_country[country]
            ]
            if prices:
                unlimited_30d_usd_by_country[country] = max(prices)

        candidates = list(eligible_countries)
        if unlimited_30d_usd_by_country:
            vals = sorted(unlimited_30d_usd_by_country.values())
            mid = len(vals) // 2
            median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
            ceiling = median * REGIONAL_ANCHOR_MEDIAN_MULTIPLIER
            candidates = [
                c for c in candidates
                if c not in unlimited_30d_usd_by_country
                or unlimited_30d_usd_by_country[c] <= ceiling
            ]
        if not candidates:
            continue

        scored = []
        for candidate in candidates:
            candidate_skus = set.intersection(*[
                {
                    sku_key
                    for indexed_country, sku_key in price_by_currency_country_sku[currency].keys()
                    if indexed_country == candidate
                }
                for currency in required_currencies
            ])
            anchor_prices = {
                currency: {
                    sku: price_by_currency_country_sku[currency][(candidate, sku)]
                    for sku in candidate_skus
                }
                for currency in required_currencies
            }
            supported_skus = _anchor_supported_skus(
                anchor_prices,
                candidate_skus,
                eligible_countries,
                floor_by_currency_country_sku,
                required_currencies,
                below_cost_override_country_skus,
            )
            selected_prices = {
                currency: {sku: anchor_prices[currency][sku] for sku in supported_skus}
                for currency in required_currencies
            }
            scored.append((
                -len(supported_skus),
                unlimited_30d_usd_by_country.get(candidate, float("inf")),
                sum(selected_prices["USD"].values()),
                candidate,
                selected_prices,
                supported_skus,
            ))

        scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        _, _, _, selected_anchor, selected_prices, selected_skus = scored[0]
        if not selected_skus:
            continue

        source_rows = {
            currency: {
                sku: row_by_currency_country_sku[currency][(selected_anchor, sku)]
                for sku in selected_skus
            }
            for currency in required_currencies
        }
        regional_prices_by_region[region_name] = selected_prices
        regional_source_rows_by_region[region_name] = source_rows
        eligible_countries_by_region[region_name] = eligible_countries

    return (
        regional_prices_by_region,
        regional_source_rows_by_region,
        eligible_countries_by_region,
        excluded_countries,
    )

def _region_membership_payload(
    eligible_countries_by_region: dict[str, list[str]],
    regions_data: dict[str, Any],
) -> dict[str, Any]:
    managed_regions = region_names(regions_data)
    regions = {
        region: _unique_ordered(eligible_countries_by_region.get(region, []))
        for region in managed_regions
    }

    countries: dict[str, list[str]] = defaultdict(list)
    for region in managed_regions:
        for country in regions.get(region, []):
            countries[country].append(region)

    return {
        "regions": regions,
        "countries": dict(sorted(countries.items())),
    }


def save_region_membership_snapshot(
    path: str | Path,
    eligible_countries_by_region: dict[str, list[str]],
    regions_data: dict[str, Any],
) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _region_membership_payload(
        eligible_countries_by_region,
        regions_data,
    )
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


CONSOLIDATED_REGION_COLUMNS: tuple[str, ...] = (
    "Provider",
    "ReferenceProvider",
    "Country",
    "ISO",
    "ISO3",
    "GB",
    "Days",
    "Price_USD",
    "Price_EUR",
    "Plan",
    "PricingUnitIdUsed",
    "PricingSourceUsed",
    "PricingRegionUsed",
    "PricingUnitCountriesUsed",
    "PromoScopeKey",
    "PromoCode",
    "PromoType",
    "PromoValue",
    "PromoLabel",
    "PromoBasePrice",
    "FinalPriceAfterPromo_EUR",
    "FinalPriceAfterPromo_USD",
    "CostFloor_USD",
    "CostFloor_EUR",
    "USD_IsBelowCostFloor",
    "EUR_IsBelowCostFloor",
    "IsPartnerExportBlocked",
    "PartnerExportBlockReason",
)


def _write_consolidated_region_prices(
    *,
    output_path: Path,
    regional_prices_by_region: dict[str, dict[str, dict[tuple[str, str, str], float]]],
    regional_source_rows_by_region: dict[str, dict[str, dict[tuple[str, str, str], dict[str, Any]]]],
    eligible_countries_by_region: dict[str, list[str]],
    regions_data: dict[str, Any],
    excluded_countries: Iterable[str] | None = None,
) -> RegionGenerationResult:
    output_rows: list[dict[str, Any]] = []

    for region_name in region_names(regions_data):
        eligible_countries = eligible_countries_by_region.get(region_name, [])
        if not eligible_countries:
            continue

        region_prices = regional_prices_by_region.get(region_name, {})
        source_rows = regional_source_rows_by_region.get(region_name, {})
        usd_prices = region_prices.get("USD", {})
        eur_prices = region_prices.get("EUR", {})
        sku_keys = sorted(
            set(usd_prices) & set(eur_prices),
            key=lambda sku: (str(sku[0]).lower(), str(sku[1]).lower(), float(sku[2])),
        )

        for sku_key in sku_keys:
            _provider, plan, days = sku_key
            source = (source_rows.get("EUR", {}).get(sku_key)
                      or source_rows.get("USD", {}).get(sku_key)
                      or {})
            price_usd = round_regular_price(usd_prices[sku_key])
            price_eur = round_regular_price(eur_prices[sku_key])

            output_rows.append({
                "Provider": source.get("Provider", "HT") or "HT",
                "ReferenceProvider": "",
                "Country": region_name,
                "ISO": region_name,
                "ISO3": "",
                "GB": source.get("GB", ""),
                "Days": source.get("Days", days),
                "Price_USD": price_usd,
                "Price_EUR": price_eur,
                "Plan": source.get("Plan", plan),
                "PricingUnitIdUsed": region_name,
                "PricingSourceUsed": "region_max",
                "PricingRegionUsed": region_name,
                "PricingUnitCountriesUsed": json.dumps(eligible_countries),
                "PromoScopeKey": f"{region_name}|{plan}|{days}",
                "PromoCode": "",
                "PromoType": "",
                "PromoValue": "",
                "PromoLabel": "",
                "PromoBasePrice": "",
                "FinalPriceAfterPromo_EUR": price_eur,
                "FinalPriceAfterPromo_USD": price_usd,
                "CostFloor_USD": "",
                "CostFloor_EUR": "",
                "USD_IsBelowCostFloor": False,
                "EUR_IsBelowCostFloor": False,
                "IsPartnerExportBlocked": False,
                "PartnerExportBlockReason": "",
            })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(CONSOLIDATED_REGION_COLUMNS))
        writer.writeheader()
        writer.writerows(output_rows)

    excluded_country_set = {
        code
        for code in (_country_code(country) for country in (excluded_countries or []))
        if code
    }
    return RegionGenerationResult(
        currency="ALL",
        input_csv=output_path.parent / "manual_prices_current.csv",
        output_csv=output_path,
        rows_written=len(output_rows),
        excluded_countries=tuple(sorted(excluded_country_set)),
    )


def generate_region_prices(
    input_csv: str | Path,
    output_folder: str | Path | None = None,
    *,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
    currency: str | None = None,
    region_country_exclusions: dict[str, list[str]] | None = None,
    allow_below_cost_keys: set[str] | None = None,
    **_legacy_kwargs,
) -> RegionGenerationResult:
    """Generate one consolidated regional pricing file from one consolidated manual file."""
    input_csv = Path(input_csv)
    output_folder = Path(output_folder) if output_folder else input_csv.parent
    regions_yaml = Path(regions_yaml)
    if not input_csv.exists():
        raise FileNotFoundError(f"Pricing CSV not found: {input_csv}")
    if not regions_yaml.exists():
        raise FileNotFoundError(f"regions.yaml not found: {regions_yaml}")

    rows, fieldnames = _read_pricing_rows(input_csv)
    if not rows:
        raise ValueError("Pricing file is empty.")
    _required_columns(fieldnames)

    regions_data = load_yaml(regions_yaml)
    rows_by_currency = {
        currency_code: _currency_rows_from_consolidated(rows, currency_code)
        for currency_code in CURRENCIES
    }
    (
        regional_prices_by_region,
        regional_source_rows_by_region,
        eligible_countries_by_region,
        excluded_countries,
    ) = _build_region_pricing_decisions(
        rows_by_currency,
        regions_data,
        region_country_exclusions=region_country_exclusions,
        allow_below_cost_keys=allow_below_cost_keys,
    )

    return _write_consolidated_region_prices(
        output_path=output_folder / output_name,
        regional_prices_by_region=regional_prices_by_region,
        regional_source_rows_by_region=regional_source_rows_by_region,
        eligible_countries_by_region=eligible_countries_by_region,
        regions_data=regions_data,
        excluded_countries=excluded_countries,
    )


def generate_region_prices_for_export_folder(
    export_dir: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
    region_exclusions_json: str | Path | None = None,
    allow_below_cost_keys: set[str] | None = None,
) -> list[RegionGenerationResult]:
    export_dir = Path(export_dir)
    regions_yaml = Path(regions_yaml)
    input_csv = export_dir / "manual_prices_current.csv"
    if not input_csv.exists():
        raise FileNotFoundError(f"Consolidated manual pricing file not found: {input_csv}")

    rows, fieldnames = _read_pricing_rows(input_csv)
    if not rows:
        raise ValueError("Pricing file is empty.")
    _required_columns(fieldnames)

    regions_data = load_yaml(regions_yaml)
    exclusion_path = (
        Path(region_exclusions_json)
        if region_exclusions_json is not None
        else regions_yaml.with_name(REGION_COUNTRY_EXCLUSIONS_NAME)
    )
    region_country_exclusions = load_region_country_exclusions(exclusion_path)
    rows_by_currency = {
        currency_code: _currency_rows_from_consolidated(rows, currency_code)
        for currency_code in CURRENCIES
    }

    (
        regional_prices_by_region,
        regional_source_rows_by_region,
        eligible_countries_by_region,
        excluded_countries,
    ) = _build_region_pricing_decisions(
        rows_by_currency,
        regions_data,
        region_country_exclusions=region_country_exclusions,
        allow_below_cost_keys=allow_below_cost_keys,
    )

    save_region_membership_snapshot(
        export_dir / REGION_MEMBERSHIP_OUTPUT_NAME,
        eligible_countries_by_region,
        regions_data,
    )

    result = _write_consolidated_region_prices(
        output_path=export_dir / output_name,
        regional_prices_by_region=regional_prices_by_region,
        regional_source_rows_by_region=regional_source_rows_by_region,
        eligible_countries_by_region=eligible_countries_by_region,
        regions_data=regions_data,
        excluded_countries=excluded_countries,
    )
    return [result]

def _pick_input_csv() -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Select pricing CSV file",
        filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")],
    )
    root.destroy()
    return path


def _pick_output_folder() -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title=f"Select folder to save {OUTPUT_NAME}")
    root.destroy()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate region prices from an exported HT prices CSV.")
    parser.add_argument("--input", dest="input_csv", help="Input manual_prices_current.csv")
    parser.add_argument("--output-folder", help=f"Folder where {OUTPUT_NAME} will be written")
    parser.add_argument("--regions-yaml", default=str(INPUT_REGIONS), help="Path to regions.yaml")
    parser.add_argument("--currency", choices=list(CURRENCIES), help="Currency of the input export")
    args = parser.parse_args()

    input_csv = args.input_csv or _pick_input_csv()
    if not input_csv:
        print("No input CSV selected. Exiting.")
        return

    output_folder = args.output_folder or _pick_output_folder()
    if not output_folder:
        print("No output folder selected. Exiting.")
        return

    result = generate_region_prices(
        input_csv,
        output_folder,
        regions_yaml=args.regions_yaml,
        currency=args.currency,
    )
    print(f"Saved: {result.output_csv}")
    print(f"Excluded countries due to cost floor: {list(result.excluded_countries)}")
    print(f"Rows written: {result.rows_written}")


if __name__ == "__main__":
    main()
