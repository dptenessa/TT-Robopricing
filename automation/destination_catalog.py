from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class NoBooleanSafeLoader(yaml.SafeLoader):
    pass


for first_letter, mappings in list(NoBooleanSafeLoader.yaml_implicit_resolvers.items()):
    NoBooleanSafeLoader.yaml_implicit_resolvers[first_letter] = [
        (tag, regexp)
        for tag, regexp in mappings
        if tag != "tag:yaml.org,2002:bool"
    ]


def load_destination_catalog(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Destination catalogue not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle, Loader=NoBooleanSafeLoader) or {}
    destinations = data.get("destinations")
    if not isinstance(destinations, dict):
        raise ValueError(f"destinations.yaml must contain a 'destinations' mapping: {path}")
    return data


def destination_specs(path: str | Path) -> dict[str, dict[str, Any]]:
    data = load_destination_catalog(path)
    out: dict[str, dict[str, Any]] = {}
    for raw_code, raw_spec in data.get("destinations", {}).items():
        code = str(raw_code).strip().upper()
        if not code or not isinstance(raw_spec, dict):
            continue
        spec = dict(raw_spec)
        spec["type"] = str(spec.get("type", "")).strip().lower()
        spec["partner_class"] = str(spec.get("partner_class", "")).strip().upper()
        spec["coverage"] = [
            str(x).strip().upper() for x in (spec.get("coverage") or []) if str(x).strip()
        ]
        spec["members"] = [
            str(x).strip().upper() for x in (spec.get("members") or []) if str(x).strip()
        ]
        spec["parent_regions"] = [
            str(x).strip().upper() for x in (spec.get("parent_regions") or []) if str(x).strip()
        ]
        translations = spec.get("translations") or {}
        spec["translations"] = {
            str(lang).strip(): str(value).strip()
            for lang, value in translations.items()
            if str(lang).strip() and str(value).strip()
        } if isinstance(translations, dict) else {}
        out[code] = spec
    return out


def country_destination_specs(path: str | Path) -> dict[str, dict[str, Any]]:
    return {
        code: spec
        for code, spec in destination_specs(path).items()
        if spec.get("type") == "country"
    }


def region_destination_specs(path: str | Path) -> dict[str, dict[str, Any]]:
    return {
        code: spec
        for code, spec in destination_specs(path).items()
        if spec.get("type") in {"region", "global"}
    }


def recommendation_primary_countries(path: str | Path) -> dict[str, str]:
    """Commercial destination ID is now the recommendation-driving country.

    A country destination may technically cover multiple countries (for example
    ES covers ES+PT), but its own ISO is always the commercial market signal.
    """
    out: dict[str, str] = {}
    for code, spec in country_destination_specs(path).items():
        if len(code) != 2:
            continue
        coverage = set(spec.get("coverage") or [])
        if coverage and code not in coverage:
            raise ValueError(
                f"Country destination {code!r} must include itself in coverage: {sorted(coverage)}"
            )
        out[code] = code
    return out


def legacy_regions_payload(path: str | Path) -> dict[str, Any]:
    """Expose destinations.yaml in the old regions.yaml shape for legacy logic."""
    specs = destination_specs(path)
    countries = sorted(code for code, spec in specs.items() if spec.get("type") == "country")
    regions: dict[str, dict[str, Any]] = {}
    for code, spec in specs.items():
        if spec.get("type") not in {"region", "global"}:
            continue
        translations = spec.get("translations") or {}
        regions[code] = {
            "region_type": spec.get("partner_class") or ("GLOBAL" if spec.get("type") == "global" else "LARGE_REGION"),
            "region_product_name": translations.get("en") or code,
            "countries": list(spec.get("members") or []),
        }
    return {
        "metadata": {
            "coding": "ISO2",
            "country_count": len(countries),
            "region_count": len(regions),
            "source": "destinations.yaml",
        },
        "countries": countries,
        "regions": regions,
    }


def partner_offer_ids(path: str | Path) -> dict[str, dict[bool, str]]:
    data = load_destination_catalog(path)
    raw = data.get("partner_classes") or {}
    if not isinstance(raw, dict):
        raise ValueError("destinations.yaml partner_classes must be a mapping")
    out: dict[str, dict[bool, str]] = {}
    for raw_class, raw_values in raw.items():
        if not isinstance(raw_values, dict):
            continue
        cls = str(raw_class).strip().upper()
        limited = str(raw_values.get("limited", "")).strip()
        unlimited = str(raw_values.get("unlimited", "")).strip()
        if limited and unlimited:
            out[cls] = {False: limited, True: unlimited}
    return out
