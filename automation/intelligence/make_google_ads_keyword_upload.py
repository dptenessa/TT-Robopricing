#!/usr/bin/env python3
"""Create a Google Ads Keyword Planner upload file for one origin market."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True, help="Origin ISO2, for example DE or US")
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parent / "inputs" / "travel_demand")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    origin = args.origin.strip().upper()

    alias_rows = rows(args.input_dir / "destination_aliases.csv")
    names = {}
    for row in alias_rows:
        code = str(row.get("destination_iso2") or "").strip().upper()
        name = str(row.get("country_name") or "").strip()
        if code and name:
            names[code] = name

    templates = []
    template_path = args.input_dir / "origin_keyword_templates.csv"
    if template_path.exists():
        for row in rows(template_path):
            code = str(row.get("origin_iso2") or "").strip().upper()
            template = str(row.get("template") or "").strip()
            if code in {origin, "DEFAULT"} and template:
                templates.append(template)

    if not templates:
        templates = (
            ["{d} urlaub", "{d} reise", "flug {d}", "fluege {d}"]
            if origin == "DE" else
            ["{d} travel", "flights {d}", "{d} vacation", "trip to {d}"]
            if origin == "US" else
            ["travel {d}", "flights {d}", "holiday {d}"]
        )

    keywords = []
    for code, name in sorted(names.items()):
        for template in templates:
            keyword = template.replace("{d}", name).strip()
            if keyword:
                keywords.append(keyword)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Google Ads is more reliable with plain UTF-8 CSV without a BOM.
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Keyword"])
        writer.writerows([[keyword] for keyword in dict.fromkeys(keywords)])

    print(f"Wrote {len(dict.fromkeys(keywords))} keywords for {origin} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
