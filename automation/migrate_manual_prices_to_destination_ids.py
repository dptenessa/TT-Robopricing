from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd
import yaml


def _root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text


def _scope_key(unit: str, plan: str, days) -> str:
    try:
        d = float(days)
        day_text = str(int(d)) if d.is_integer() else f"{d:g}"
    except Exception:
        day_text = _text(days)
    return f"{unit}|{_text(plan)}|{day_text}"


def load_catalogue(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    destinations = data.get("destinations", {})
    if not isinstance(destinations, dict):
        raise ValueError("destinations.yaml has no valid 'destinations' mapping")
    return destinations


def load_legacy_units(path: Path) -> dict[str, list[str]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for row in rows:
        unit = _text(row.get("pricing_unit_id")).upper()
        coverage = [_text(x).upper() for x in row.get("area_covered", []) if _text(x)]
        if unit and len(coverage) > 1:
            result[unit] = coverage
    return result


def read_book(path: Path) -> pd.DataFrame:
    try:
        from pricing_book import read_pricing_workbook
    except ImportError:
        from automation.pricing_book import read_pricing_workbook
    return read_pricing_workbook(path)


def write_book(df: pd.DataFrame, path: Path, promos_path: Path) -> None:
    try:
        from pricing_book import write_pricing_workbook
    except ImportError:
        from automation.pricing_book import write_pricing_workbook

    promos = []
    if promos_path.exists():
        raw = json.loads(promos_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            promos = raw
    write_pricing_workbook(df, path, promo_catalog=promos)


def migrate(df: pd.DataFrame, legacy_units: dict[str, list[str]], destinations: dict) -> tuple[pd.DataFrame, list[dict], list[str]]:
    out = df.copy()
    required = {"ISO", "PricingUnitIdUsed", "Plan", "Days"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError("Current price book is missing required column(s): " + ", ".join(missing))

    changes = []
    problems = []

    for idx, row in out.iterrows():
        old_unit = _text(row.get("PricingUnitIdUsed")).upper()
        if old_unit not in legacy_units:
            continue

        iso = _text(row.get("ISO")).upper()
        old_coverage = legacy_units[old_unit]
        if iso not in old_coverage:
            problems.append(f"row {idx + 2}: unit {old_unit} but ISO {iso or '<blank>'} is not in {old_coverage}")
            continue

        dest = destinations.get(iso)
        if not isinstance(dest, dict):
            problems.append(f"row {idx + 2}: destination {iso} missing from destinations.yaml")
            continue

        new_coverage = [_text(x).upper() for x in dest.get("coverage", []) if _text(x)]
        if set(new_coverage) != set(old_coverage):
            problems.append(
                f"row {idx + 2}: {iso} coverage in destinations.yaml is {new_coverage}, expected same set as legacy {old_coverage}"
            )
            continue

        new_unit = iso
        out.at[idx, "PricingUnitIdUsed"] = new_unit
        if "PricingUnitCountriesUsed" in out.columns:
            out.at[idx, "PricingUnitCountriesUsed"] = json.dumps(new_coverage, ensure_ascii=False)
        if "PromoScopeKey" in out.columns:
            out.at[idx, "PromoScopeKey"] = _scope_key(new_unit, row.get("Plan"), row.get("Days"))

        changes.append({
            "row": idx + 2,
            "ISO": iso,
            "old_unit": old_unit,
            "new_unit": new_unit,
            "coverage": ",".join(new_coverage),
            "plan": _text(row.get("Plan")),
            "days": row.get("Days"),
        })

    # New commercial key must be unique for a given ISO/plan/day/GB row identity.
    key_cols = [c for c in ["ISO", "PricingUnitIdUsed", "Plan", "Days", "GB"] if c in out.columns]
    dupes = out[out.duplicated(key_cols, keep=False)] if key_cols else pd.DataFrame()
    if not dupes.empty:
        sample = dupes[key_cols].head(10).to_dict("records")
        problems.append(f"duplicate rows would remain after migration on {key_cols}: {sample}")

    return out, changes, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy shared pricing-unit IDs to destination ISO IDs.")
    parser.add_argument("--apply", action="store_true", help="Write the migrated current workbook. Default is dry-run only.")
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args()

    root = Path(args.project_root).resolve() if args.project_root else _root_from_script()
    book = root / "outputs" / "manual_prices" / "current" / "manual_prices_current.xlsx"
    catalogue = root / "inputs" / "destinations.yaml"
    legacy = root / "inputs" / "pricing_units.json"
    promos = root / "inputs" / "promos.json"

    for path in (book, catalogue, legacy):
        if not path.exists():
            raise FileNotFoundError(path)

    df = read_book(book)
    destinations = load_catalogue(catalogue)
    legacy_units = load_legacy_units(legacy)
    migrated, changes, problems = migrate(df, legacy_units, destinations)

    print(f"Current price rows: {len(df)}")
    print(f"Legacy shared units found in config: {len(legacy_units)}")
    print(f"Rows that would change PricingUnitIdUsed: {len(changes)}")
    if changes:
        summary = pd.DataFrame(changes).groupby(["old_unit", "new_unit"]).size().reset_index(name="rows")
        print("\nMigration summary:")
        print(summary.to_string(index=False))

    if problems:
        print("\nSTOP - validation problems found:")
        for item in problems[:30]:
            print(" -", item)
        if len(problems) > 30:
            print(f" - ... and {len(problems) - 30} more")
        return 2

    print("\nValidation OK: no duplicate or coverage conflicts found.")
    if not args.apply:
        print("DRY RUN ONLY - nothing was changed.")
        print("Run again with --apply only after reviewing this output.")
        return 0

    backup = book.with_name("manual_prices_current_BEFORE_DESTINATION_ID_MIGRATION.xlsx")
    if not backup.exists():
        shutil.copy2(book, backup)
        print(f"Backup created: {backup}")
    else:
        print(f"Backup already exists: {backup}")

    write_book(migrated, book, promos)
    print(f"Migrated workbook saved: {book}")
    print(f"Rows migrated: {len(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
