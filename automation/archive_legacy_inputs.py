from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUTS = PROJECT_ROOT / "inputs"
ARCHIVE = PROJECT_ROOT / "archive" / "legacy_inputs_before_destinations_refactor"

# Confirmed obsolete after destinations.yaml + external sales-master migration.
LEGACY_ITEMS = [
    "pricing_units.json",
    "pricing_units_ISO3.json",
    "regions.yaml",
    "export-destination-table_reviewed.json",
    "region_country_exclusions.json",
    "Regions_countries_list.xlsx",
    "sales_volumes_last_month_test.xlsx",
    "Connections_2026-05-26.csv",
    "Countries with 4G-5G Coverage.xlsx",
    "T_Cost_Crodis.xlsx",
    "old files",
]

KEEP = {
    "WS_PPG.csv",
    "destinations.yaml",
    "promos.json",
    # Still used by the live Vodafone PDF scraper. Do not archive yet.
    "Charges.pdf",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive obsolete T-Travel input files after the destinations.yaml refactor."
    )
    parser.add_argument("--apply", action="store_true", help="Actually move the files. Default is dry-run.")
    args = parser.parse_args()

    print(f"Inputs:  {INPUTS}")
    print(f"Archive: {ARCHIVE}")
    print("Mode:    " + ("APPLY" if args.apply else "DRY RUN"))
    print()

    found = []
    missing = []
    for name in LEGACY_ITEMS:
        src = INPUTS / name
        if src.exists():
            found.append((name, src))
        else:
            missing.append(name)

    for name, src in found:
        print(f"ARCHIVE: {name}")
        if args.apply:
            ARCHIVE.mkdir(parents=True, exist_ok=True)
            dst = ARCHIVE / name
            if dst.exists():
                raise FileExistsError(f"Archive target already exists: {dst}")
            shutil.move(str(src), str(dst))

    for name in missing:
        print(f"SKIP missing: {name}")

    if not args.apply:
        print("\nDry run only. Re-run with --apply when the list looks correct.")
        return 0

    print("\nRemaining inputs:")
    remaining = sorted(p.name for p in INPUTS.iterdir())
    for name in remaining:
        marker = "OK" if name in KEEP else "REVIEW"
        print(f"  [{marker}] {name}")

    unexpected = [name for name in remaining if name not in KEEP]
    if unexpected:
        print("\nCleanup completed, but these remaining inputs still need review:")
        for name in unexpected:
            print(f"  - {name}")
    else:
        print("\nCleanup OK. Only the three canonical inputs plus live Charges.pdf remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
