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
    OUTPUT_NAME = "Region_prices.csv"

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


def _resolve_region_items(items: Iterable[Any], regions_data: dict[str, Any], seen: set[str]) -> set[str]:
    result: set[str] = set()
    region_names = set((regions_data.get("regions") or {}).keys())
    region_names.update((regions_data.get("base") or {}).keys())
    region_names.update((regions_data.get("derived") or {}).keys())

    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if item == "*":
            result.update(_direct_region_countries(regions_data))
        elif item in region_names:
            result.update(resolve_region(item, regions_data, seen.copy()))
        else:
            result.add(item)
    return result


def region_names(regions_data: dict[str, Any]) -> list[str]:
    helper_regions_to_skip = {"EU_EXTRA", "EUROPA_EXTRA"}
    if regions_data.get("regions"):
        names = list((regions_data.get("regions") or {}).keys())
    else:
        names = list((regions_data.get("base") or {}).keys()) + list((regions_data.get("derived") or {}).keys())
    return [str(name).strip() for name in names if str(name).strip() and str(name).strip() not in helper_regions_to_skip]


def _required_columns(fieldnames: list[str]) -> None:
    required = {"ISO", "Days", "Plan", "FinalPriceAfterPromo"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")


def _is_below_cost(row: dict[str, Any]) -> bool:
    return parse_bool(row.get("IsBelowCalculatedCostFloor")) or parse_bool(row.get("Is_Below_Cost_Floor"))


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


def generate_region_prices(
    input_csv: str | Path,
    output_folder: str | Path | None = None,
    *,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
    currency: str | None = None,
) -> RegionGenerationResult:
    input_csv = Path(input_csv)
    output_folder = Path(output_folder) if output_folder else input_csv.parent
    regions_yaml = Path(regions_yaml)

    if not input_csv.exists():
        raise FileNotFoundError(f"Pricing CSV not found: {input_csv}")
    if not regions_yaml.exists():
        raise FileNotFoundError(f"regions.yaml not found: {regions_yaml}")

    regions_data = load_yaml(regions_yaml)
    dialect = detect_dialect(input_csv)

    with open(input_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if not rows:
        raise ValueError("Pricing file is empty.")
    _required_columns(fieldnames)

    detected_currency = normalize_currency(currency or rows[0].get("Currency") or DEFAULT_CURRENCY)

    excluded_countries = {
        str(row.get("ISO", "")).strip()
        for row in rows
        if str(row.get("ISO", "")).strip() and _is_below_cost(row)
    }

    valid_rows = [
        row
        for row in rows
        if str(row.get("ISO", "")).strip() not in excluded_countries
    ]

    output_rows: list[dict[str, Any]] = []

    for region_name in region_names(regions_data):
        region_countries = resolve_region(region_name, regions_data)
        eligible_countries = sorted(region_countries - excluded_countries)

        region_rows = [
            row
            for row in valid_rows
            if str(row.get("ISO", "")).strip() in eligible_countries
        ]
        if not region_rows:
            continue

        grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
        for row in region_rows:
            key = (row.get("Provider", ""), row.get("Plan", ""), row.get("Days", ""))
            grouped[key].append(row)

        for (_provider, plan, days), group_rows in grouped.items():
            max_row = max(group_rows, key=_row_final_price)
            final_price = round_regular_price(_row_final_price(max_row))
            row_currency = normalize_currency(max_row.get("Currency") or detected_currency, detected_currency)
            eur_to_usd = _rate_for_row(max_row)

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

            if "Price_USD" in fieldnames:
                price_usd = final_price if detected_currency == "USD" else round_regular_price(
                    convert_price(final_price, row_currency, "USD", eur_to_usd)
                )
                _set_if_present(new_row, fieldnames, "Price_USD", price_usd)
            if "Price_EUR" in fieldnames:
                price_eur = final_price if detected_currency == "EUR" else round_regular_price(
                    convert_price(final_price, row_currency, "EUR", eur_to_usd)
                )
                _set_if_present(new_row, fieldnames, "Price_EUR", price_eur)

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
        excluded_countries=tuple(sorted(excluded_countries)),
    )


def generate_region_prices_for_export_folder(
    export_dir: str | Path,
    *,
    currencies: Iterable[str] = CURRENCIES,
    regions_yaml: str | Path = INPUT_REGIONS,
    output_name: str = OUTPUT_NAME,
    include_legacy_usd: bool = True,
) -> list[RegionGenerationResult]:
    export_dir = Path(export_dir)
    results: list[RegionGenerationResult] = []

    for currency in currencies:
        currency = normalize_currency(currency)
        input_csv = export_dir / currency / "HT_prices_last_export.csv"
        if input_csv.exists():
            results.append(
                generate_region_prices(
                    input_csv,
                    input_csv.parent,
                    regions_yaml=regions_yaml,
                    output_name=output_name,
                    currency=currency,
                )
            )

    legacy_input = export_dir / "HT_prices_last_export.csv"
    if include_legacy_usd and legacy_input.exists():
        results.append(
            generate_region_prices(
                legacy_input,
                export_dir,
                regions_yaml=regions_yaml,
                output_name=output_name,
                currency=DEFAULT_CURRENCY,
            )
        )

    if not results:
        raise FileNotFoundError(f"No HT_prices_last_export.csv files found under: {export_dir}")
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
    parser.add_argument("--input", dest="input_csv", help="Input HT_prices_last_export.csv")
    parser.add_argument("--output-folder", help="Folder where Region_prices.csv will be written")
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
