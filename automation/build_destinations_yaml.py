from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import yaml

OCS_IDS = {
    "COUNTRY": {"limited": "190447115", "unlimited": "190447245"},
    "MEDIUM_REGION": {"limited": "190640635", "unlimited": "190640665"},
    "LARGE_REGION": {"limited": "190447125", "unlimited": "190447255"},
    "GLOBAL": {"limited": "190447135", "unlimited": "190447265"},
}

EXPORT_CODE_ALIASES = {"US": "USA"}


class NoAliasSafeDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _translations(entry: dict[str, Any] | None, fallback_en: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if entry:
        raw = ((entry.get("dictionary") or {}).get("destination") or {})
        for lang, value in raw.items():
            text = str(value or "").strip()
            if text:
                result[str(lang)] = text
    result.setdefault("en", fallback_en)
    return result


def _load_ws_names(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            iso2 = str(row.get("ISO_Code_A2") or "").strip().upper()
            name = str(row.get("country") or iso2).strip()
            if iso2:
                names[iso2] = name or iso2
    return names


def build(root: Path) -> tuple[dict[str, Any], list[str]]:
    inputs = root / "inputs"
    ws_names = _load_ws_names(inputs / "WS_PPG.csv")
    pricing_units = json.loads((inputs / "pricing_units.json").read_text(encoding="utf-8"))
    regions_doc = yaml.safe_load((inputs / "regions.yaml").read_text(encoding="utf-8"))
    export_rows = json.loads((inputs / "export-destination-table_reviewed.json").read_text(encoding="utf-8"))

    export_by_code = {str(x.get("code") or "").strip().upper(): x for x in export_rows}
    shared_coverage: dict[str, list[str]] = {}
    for unit in pricing_units:
        coverage = [str(x).strip().upper() for x in unit.get("area_covered", []) if str(x).strip()]
        for iso2 in coverage:
            if iso2 in shared_coverage and shared_coverage[iso2] != coverage:
                raise ValueError(f"Country {iso2} appears in conflicting shared pricing units: {shared_coverage[iso2]} vs {coverage}")
            shared_coverage[iso2] = coverage

    all_countries = [str(x).strip().upper() for x in regions_doc.get("countries", [])]
    missing_translation_codes: list[str] = []
    destinations: dict[str, Any] = {}

    for iso2 in all_countries:
        coverage = shared_coverage.get(iso2, [iso2])
        partner_class = "MEDIUM_REGION" if len(coverage) > 1 else "COUNTRY"
        export_code = EXPORT_CODE_ALIASES.get(iso2, iso2)
        export_entry = export_by_code.get(export_code)
        if export_entry is None:
            missing_translation_codes.append(iso2)
        destinations[iso2] = {
            "type": "country",
            "coverage": coverage,
            "partner_class": partner_class,
            "translations": _translations(export_entry, ws_names.get(iso2, iso2)),
        }

    regions = regions_doc.get("regions", {}) or {}
    for region_id, region in regions.items():
        region_id = str(region_id).strip().upper()
        export_entry = export_by_code.get(region_id)
        if export_entry is None:
            missing_translation_codes.append(region_id)
        partner_class = str(region.get("region_type") or "MEDIUM_REGION").strip().upper()
        parent_regions = []
        if isinstance(export_entry, dict):
            parent_regions = [
                str(x).strip().upper()
                for x in (export_entry.get("regions") or [])
                if str(x).strip() and str(x).strip().upper() != region_id
            ]
        destinations[region_id] = {
            "type": "global" if partner_class == "GLOBAL" else "region",
            "members": [str(x).strip().upper() for x in region.get("countries", [])],
            "parent_regions": parent_regions,
            "partner_class": partner_class,
            "translations": _translations(
                export_entry,
                str(region.get("region_product_name") or region_id).strip(),
            ),
        }

    doc = {
        "schema_version": 2,
        "metadata": {
            "purpose": "Single commercial destination catalogue for T-Travel",
            "country_count": len(all_countries),
            "region_count": len(regions),
            "note": (
                "Draft migration catalogue. Not consumed by production code yet. Region-to-region hierarchy is preserved in parent_regions. "
                "Country IDs are the future commercial pricing keys; coverage controls where a country product works; "
                "region members control region membership independently."
            ),
        },
        "partner_classes": OCS_IDS,
        "destinations": destinations,
    }
    return doc, sorted(set(missing_translation_codes))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build draft inputs/destinations.yaml from current T-Travel inputs.")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite an existing destinations.yaml")
    args = parser.parse_args()

    root = Path(args.project_root).resolve() if args.project_root else Path(__file__).resolve().parents[1]
    out = root / "inputs" / "destinations.yaml"
    if out.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite existing file: {out}\nRun again with --force if intentional.")

    doc, missing = build(root)
    out.write_text(yaml.dump(doc, Dumper=NoAliasSafeDumper, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"Saved draft catalogue: {out}")
    print(f"Destinations: {len(doc['destinations'])}")
    print(f"Countries: {doc['metadata']['country_count']}; regions: {doc['metadata']['region_count']}")
    if missing:
        print("Missing full translation source (English fallback used): " + ", ".join(missing))
    else:
        print("Translation source coverage: complete")
    print("IMPORTANT: production code does not read destinations.yaml yet. This step changes no live pricing behavior.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
