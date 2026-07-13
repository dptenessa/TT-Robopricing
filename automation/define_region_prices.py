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

# Regional anchor guardrails.
# A country cannot define regional prices when its Unlimited 30-day USD price
# is above either the absolute ceiling or the relative median ceiling.
REGIONAL_ANCHOR_UNLIMITED_30D_CAP_USD = 300.0
REGIONAL_ANCHOR_MEDIAN_MULTIPLIER = 2.0

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


def _required_columns(fieldnames: list[str]) -> None:
    required = {"ISO", "Days", "Plan", "FinalPriceAfterPromo"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")


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
        return list(reader), list(reader.fieldnames or [])


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



def _is_unlimited_30d_sku(sku_key: tuple[str, str, str]) -> bool:
    _provider, plan, days = sku_key
    plan_text = str(plan).strip().lower()

    try:
        days_value = float(days)
    except (TypeError, ValueError):
        return False

    return "unlimited" in plan_text and days_value == 30.0


def _country_covers_anchor_prices(
    country: str,
    sku_keys: set[tuple[str, str, str]],
    anchor_prices_by_currency: dict[
        str,
        dict[tuple[str, str, str], float],
    ],
    floor_by_currency_country_sku: dict[
        str,
        dict[tuple[str, tuple[str, str, str]], float],
    ],
    required_currencies: list[str],
) -> bool:
    """
    Return True only when every anchor SKU passes this country's floor
    in every required currency.
    """
    if not sku_keys:
        return False

    for sku_key in sku_keys:
        for currency in required_currencies:
            anchor_price = anchor_prices_by_currency.get(
                currency,
                {},
            ).get(sku_key)

            required_floor = floor_by_currency_country_sku.get(
                currency,
                {},
            ).get((country, sku_key))

            if anchor_price is None or required_floor is None:
                return False

            if anchor_price < required_floor:
                return False

    return True


def _build_region_pricing_decisions(
    rows_by_currency: dict[str, list[dict[str, Any]]],
    regions_data: dict[str, Any],
) -> tuple[
    dict[str, dict[str, dict[tuple[str, str, str], float]]],
    dict[str, dict[str, dict[tuple[str, str, str], dict[str, Any]]]],
    dict[str, list[str]],
    set[str],
]:
    """
    Select one commercially reasonable anchor country per region.

    Anchor requirements:
    - The country has every regional SKU in USD and EUR.
    - Every own USD and EUR price passes its own corresponding cost floor.
    - Unlimited 30-day USD is not an extreme outlier:
      <= absolute cap and <= regional median * multiplier.

    Selection:
    - Highest safe country coverage.
    - Then lowest Unlimited 30-day USD price.
    - Then lowest total USD curve price.
    - Then country code for deterministic output.

    The selected anchor supplies every USD and EUR regional price.
    """
    normalized_rows_by_currency = {
        normalize_currency(currency): rows
        for currency, rows in rows_by_currency.items()
        if rows
    }

    required_currencies = ["USD", "EUR"]

    missing_required = [
        currency
        for currency in required_currencies
        if currency not in normalized_rows_by_currency
    ]
    if missing_required:
        raise ValueError(
            "Regional price generation requires both USD and EUR exports. "
            f"Missing: {', '.join(missing_required)}"
        )

    # Build fast indexes by currency + country + SKU.
    price_by_currency_country_sku: dict[
        str,
        dict[tuple[str, tuple[str, str, str]], float],
    ] = {}
    row_by_currency_country_sku: dict[
        str,
        dict[tuple[str, tuple[str, str, str]], dict[str, Any]],
    ] = {}
    floor_by_currency_country_sku: dict[
        str,
        dict[tuple[str, tuple[str, str, str]], float],
    ] = {}

    for currency in required_currencies:
        price_index: dict[
            tuple[str, tuple[str, str, str]],
            float,
        ] = {}
        row_index: dict[
            tuple[str, tuple[str, str, str]],
            dict[str, Any],
        ] = {}
        floor_index: dict[
            tuple[str, tuple[str, str, str]],
            float,
        ] = {}

        for row in normalized_rows_by_currency[currency]:
            country = _country_code(row.get("ISO", ""))
            if not country:
                continue

            sku_key = _regional_sku_key(row)
            index_key = (country, sku_key)

            price = round_regular_price(_row_final_price(row))
            previous_price = price_index.get(index_key)

            # Duplicate country/SKU rows: retain the highest customer price.
            if previous_price is None or price > previous_price:
                price_index[index_key] = price
                row_index[index_key] = row

            floor = _cost_floor_for_currency(row, currency)
            if floor is not None:
                previous_floor = floor_index.get(index_key)

                # Duplicate country/SKU floors: retain the strictest floor.
                if previous_floor is None or floor > previous_floor:
                    floor_index[index_key] = float(floor)

        price_by_currency_country_sku[currency] = price_index
        row_by_currency_country_sku[currency] = row_index
        floor_by_currency_country_sku[currency] = floor_index

    regional_prices_by_region: dict[
        str,
        dict[str, dict[tuple[str, str, str], float]],
    ] = {}
    regional_source_rows_by_region: dict[
        str,
        dict[str, dict[tuple[str, str, str], dict[str, Any]]],
    ] = {}
    eligible_countries_by_region: dict[str, list[str]] = {}
    excluded_countries: set[str] = set()

    for region_name in region_names(regions_data):
        configured_countries = _unique_ordered(
            code
            for code in (
                _country_code(country)
                for country in resolve_region_ordered(
                    region_name,
                    regions_data,
                )
            )
            if code
        )

        if not configured_countries:
            continue

        # Determine the regional SKU universe from all configured countries.
        all_sku_keys: set[tuple[str, str, str]] = set()

        for currency in required_currencies:
            currency_prices = price_by_currency_country_sku[currency]

            for indexed_country, sku_key in currency_prices.keys():
                if indexed_country in configured_countries:
                    all_sku_keys.add(sku_key)

        if not all_sku_keys:
            continue

        ordered_sku_keys = sorted(
            all_sku_keys,
            key=lambda sku: (
                str(sku[0]).strip().lower(),
                str(sku[1]).strip().lower(),
                float(sku[2]),
            ),
        )

        unlimited_30d_keys = {
            sku_key
            for sku_key in ordered_sku_keys
            if _is_unlimited_30d_sku(sku_key)
        }

        if not unlimited_30d_keys:
            # An anchor cannot be commercially screened without the agreed
            # benchmark product.
            excluded_countries.update(configured_countries)
            continue

        # First identify technically valid anchor candidates.
        technically_valid_candidates: list[str] = []

        for country in configured_countries:
            candidate_valid = True

            for sku_key in ordered_sku_keys:
                for currency in required_currencies:
                    own_price = price_by_currency_country_sku[
                        currency
                    ].get((country, sku_key))

                    own_floor = floor_by_currency_country_sku[
                        currency
                    ].get((country, sku_key))

                    if (
                        own_price is None
                        or own_floor is None
                        or own_price < own_floor
                    ):
                        candidate_valid = False
                        break

                if not candidate_valid:
                    break

            if candidate_valid:
                technically_valid_candidates.append(country)

        if not technically_valid_candidates:
            excluded_countries.update(configured_countries)
            continue

        # Use the highest Unlimited 30-day SKU price when multiple matching
        # Unlimited rows exist.
        unlimited_30d_usd_by_country: dict[str, float] = {}

        for country in technically_valid_candidates:
            prices = [
                price_by_currency_country_sku["USD"][
                    (country, sku_key)
                ]
                for sku_key in unlimited_30d_keys
                if (country, sku_key)
                in price_by_currency_country_sku["USD"]
            ]

            if prices:
                unlimited_30d_usd_by_country[country] = max(prices)

        if not unlimited_30d_usd_by_country:
            excluded_countries.update(configured_countries)
            continue

        sorted_benchmark_prices = sorted(
            unlimited_30d_usd_by_country.values()
        )
        midpoint = len(sorted_benchmark_prices) // 2

        if len(sorted_benchmark_prices) % 2:
            regional_median = sorted_benchmark_prices[midpoint]
        else:
            regional_median = (
                sorted_benchmark_prices[midpoint - 1]
                + sorted_benchmark_prices[midpoint]
            ) / 2.0

        relative_ceiling = (
            regional_median
            * REGIONAL_ANCHOR_MEDIAN_MULTIPLIER
        )
        effective_ceiling = min(
            REGIONAL_ANCHOR_UNLIMITED_30D_CAP_USD,
            relative_ceiling,
        )

        reasonable_candidates = [
            country
            for country in technically_valid_candidates
            if unlimited_30d_usd_by_country.get(
                country,
                float("inf"),
            ) <= effective_ceiling
        ]

        if not reasonable_candidates:
            excluded_countries.update(configured_countries)
            continue

        scored_candidates: list[
            tuple[
                int,
                float,
                float,
                str,
                dict[str, dict[tuple[str, str, str], float]],
                list[str],
            ]
        ] = []

        for candidate in reasonable_candidates:
            anchor_prices_by_currency: dict[
                str,
                dict[tuple[str, str, str], float],
            ] = {
                currency: {
                    sku_key: price_by_currency_country_sku[
                        currency
                    ][(candidate, sku_key)]
                    for sku_key in ordered_sku_keys
                }
                for currency in required_currencies
            }

            covered_countries = [
                country
                for country in configured_countries
                if _country_covers_anchor_prices(
                    country,
                    all_sku_keys,
                    anchor_prices_by_currency,
                    floor_by_currency_country_sku,
                    required_currencies,
                )
            ]

            coverage_count = len(covered_countries)
            unlimited_30d_usd = (
                unlimited_30d_usd_by_country[candidate]
            )
            total_usd_curve_price = sum(
                anchor_prices_by_currency["USD"].values()
            )

            scored_candidates.append(
                (
                    -coverage_count,
                    unlimited_30d_usd,
                    total_usd_curve_price,
                    candidate,
                    anchor_prices_by_currency,
                    covered_countries,
                )
            )

        scored_candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3],
            )
        )

        (
            _negative_coverage,
            _benchmark_price,
            _total_curve_price,
            selected_anchor,
            selected_prices,
            eligible_countries,
        ) = scored_candidates[0]

        source_rows_by_currency: dict[
            str,
            dict[tuple[str, str, str], dict[str, Any]],
        ] = {
            currency: {
                sku_key: row_by_currency_country_sku[
                    currency
                ][(selected_anchor, sku_key)]
                for sku_key in ordered_sku_keys
            }
            for currency in required_currencies
        }

        regional_prices_by_region[region_name] = selected_prices
        regional_source_rows_by_region[
            region_name
        ] = source_rows_by_currency
        eligible_countries_by_region[
            region_name
        ] = eligible_countries

        excluded_countries.update(
            country
            for country in configured_countries
            if country not in eligible_countries
        )

    return (
        regional_prices_by_region,
        regional_source_rows_by_region,
        eligible_countries_by_region,
        excluded_countries,
    )


def generate_region_prices(
    input_csv: str | Path,
    output_folder: str | Path | None = None,
    *,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
    currency: str | None = None,
    regional_prices_by_region: dict[str, dict[str, dict[tuple[str, str, str], float]]] | None = None,
    regional_source_rows_by_region: dict[
        str,
        dict[str, dict[tuple[str, str, str], dict[str, Any]]],
    ] | None = None,
    eligible_countries_by_region: dict[str, list[str]] | None = None,
    excluded_countries: Iterable[str] | None = None,
) -> RegionGenerationResult:
    input_csv = Path(input_csv)
    output_folder = Path(output_folder) if output_folder else input_csv.parent
    regions_yaml = Path(regions_yaml)

    if not input_csv.exists():
        raise FileNotFoundError(f"Pricing CSV not found: {input_csv}")
    if not regions_yaml.exists():
        raise FileNotFoundError(f"regions.yaml not found: {regions_yaml}")

    regions_data = load_yaml(regions_yaml)
    rows, fieldnames = _read_pricing_rows(input_csv)

    if not rows:
        raise ValueError("Pricing file is empty.")
    _required_columns(fieldnames)

    detected_currency = normalize_currency(currency or rows[0].get("Currency") or DEFAULT_CURRENCY)

    if (
        regional_prices_by_region is None
        or regional_source_rows_by_region is None
        or eligible_countries_by_region is None
    ):
        (
            regional_prices_by_region,
            regional_source_rows_by_region,
            eligible_countries_by_region,
            calculated_excluded_countries,
        ) = _build_region_pricing_decisions({detected_currency: rows}, regions_data)

        if excluded_countries is None:
            excluded_countries = calculated_excluded_countries

    excluded_country_set = {
        code
        for code in (_country_code(country) for country in (excluded_countries or []))
        if code
    }

    output_rows: list[dict[str, Any]] = []

    for region_name in region_names(regions_data):
        eligible_countries = eligible_countries_by_region.get(region_name, [])
        if not eligible_countries:
            continue

        region_prices = regional_prices_by_region.get(region_name, {})
        region_source_rows = regional_source_rows_by_region.get(region_name, {})
        current_currency_prices = region_prices.get(detected_currency, {})
        current_currency_source_rows = region_source_rows.get(detected_currency, {})

        ordered_current_currency_prices = sorted(
            current_currency_prices.items(),
            key=lambda item: (
                str(item[0][0]).strip().lower(),
                str(item[0][1]).strip().lower(),
                float(item[0][2]),
            ),
        )

        for sku_key, final_price in ordered_current_currency_prices:
            max_row = current_currency_source_rows.get(sku_key)
            if max_row is None:
                continue

            _provider, plan, days = sku_key
            final_price = round_regular_price(final_price)
            new_row = dict(max_row)

            _set_if_present(new_row, fieldnames, "Country", region_name)
            _set_if_present(new_row, fieldnames, "ISO", region_name)
            _set_if_present(new_row, fieldnames, "ISO3", "")
            _set_if_present(new_row, fieldnames, "Currency", detected_currency)
            _set_if_present(new_row, fieldnames, "PricingUnitIdUsed", region_name)
            _set_if_present(new_row, fieldnames, "PricingSourceUsed", "region_max")
            _set_if_present(new_row, fieldnames, "PricingRegionUsed", region_name)
            _set_if_present(new_row, fieldnames, "PricingUnitCountriesUsed", json.dumps(eligible_countries))
            _set_if_present(new_row, fieldnames, "PromoScopeKey", f"{region_name}|{plan}|{days}")
            _set_if_present(new_row, fieldnames, "Price", final_price)
            _set_if_present(new_row, fieldnames, "FinalPriceAfterPromo", final_price)

            usd_price = region_prices.get("USD", {}).get(sku_key)
            eur_price = region_prices.get("EUR", {}).get(sku_key)

            if usd_price is not None:
                _set_if_present(new_row, fieldnames, "Price_USD", round_regular_price(usd_price))
            if eur_price is not None:
                _set_if_present(new_row, fieldnames, "Price_EUR", round_regular_price(eur_price))

            _set_if_present(new_row, fieldnames, "CalculatedCostFloor", "")
            _set_if_present(new_row, fieldnames, "CostFloor_USD", "")
            _set_if_present(new_row, fieldnames, "CostFloor_EUR", "")
            _set_if_present(new_row, fieldnames, "IsBelowCostFloor", False)
            _set_if_present(new_row, fieldnames, "USD_IsBelowCostFloor", False)
            _set_if_present(new_row, fieldnames, "EUR_IsBelowCostFloor", False)
            _set_if_present(new_row, fieldnames, "IsPartnerExportBlocked", False)
            _set_if_present(new_row, fieldnames, "PartnerExportBlockReason", "")

            output_rows.append(new_row)

    output_folder.mkdir(parents=True, exist_ok=True)
    output_path = output_folder / output_name

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    return RegionGenerationResult(
        currency=detected_currency,
        input_csv=input_csv,
        output_csv=output_path,
        rows_written=len(output_rows),
        excluded_countries=tuple(sorted(excluded_country_set)),
    )


def generate_region_prices_for_export_folder(
    export_dir: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
) -> list[RegionGenerationResult]:
    export_dir = Path(export_dir)
    regions_yaml = Path(regions_yaml)

    results: list[RegionGenerationResult] = []
    currency_inputs: list[tuple[str, Path]] = []
    rows_by_currency: dict[str, list[dict[str, Any]]] = {}

    for currency in currencies:
        normalized_currency = normalize_currency(currency)
        input_csv = export_dir / normalized_currency / "manual_prices_current.csv"

        if not input_csv.exists():
            continue

        rows, fieldnames = _read_pricing_rows(input_csv)
        if not rows:
            continue

        _required_columns(fieldnames)
        currency_inputs.append((normalized_currency, input_csv))
        rows_by_currency[normalized_currency] = rows

    if not currency_inputs:
        raise FileNotFoundError(
            f"No manual_prices_current.csv files found under: {export_dir}"
        )

    regions_data = load_yaml(regions_yaml)

    (
        regional_prices_by_region,
        regional_source_rows_by_region,
        eligible_countries_by_region,
        excluded_countries,
    ) = _build_region_pricing_decisions(rows_by_currency, regions_data)

    for currency, input_csv in currency_inputs:
        results.append(
            generate_region_prices(
                input_csv,
                input_csv.parent,
                regions_yaml=regions_yaml,
                output_name=output_name,
                currency=currency,
                regional_prices_by_region=regional_prices_by_region,
                regional_source_rows_by_region=regional_source_rows_by_region,
                eligible_countries_by_region=eligible_countries_by_region,
                excluded_countries=excluded_countries,
            )
        )

    return results


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
