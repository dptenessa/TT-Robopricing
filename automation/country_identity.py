"""Canonical country identity shared by all competitor scrapers.

Provider names and slugs are presentation labels. Matching and output identity
come from the active PPG whitelist's ISO-2/ISO-3 codes.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import pycountry
except ImportError:  # PPG names and ISO codes still provide a useful registry.
    pycountry = None


ALIASES_BY_ISO3 = {
    "AFG": ("afganistan",),
    "ARE": ("uae", "united arab emirates"),
    "BES": ("netherlands antilles",),
    "BOL": ("bolivia", "plurinational state of bolivia"),
    "BIH": ("bosnia and herzegowina",),
    "BRN": ("brunei", "brunei darussalam"),
    "CIV": ("ivory coast", "cote d ivoire"),
    "CZE": ("czech republic", "czechia"),
    "GBR": ("uk", "u k", "great britain", "britain", "england"),
    "IRN": ("iran", "persia"),
    "SLE": ("sierra leon",),
    "LBY": ("libyan arab jamahiriya",),
    "KOR": ("south korea", "republic of korea"),
    "MKD": ("macedonia the former yugoslav republic of",),
    "LAO": ("laos", "lao people s democratic republic"),
    "MDA": ("moldova", "moldova republic of"),
    "PSE": ("palestine", "state of palestine", "palestinian territor"),
    "PRK": ("north korea", "dpr korea"),
    "COD": ("congo the democratic republic", "democratic republic of congo"),
    "RUS": ("russia", "russian federation"),
    "SYR": ("syria", "syrian arab republic"),
    "TZA": ("tanzania", "tanzania united republic of"),
    "TUR": ("turkey", "turkiye"),
    "TWN": ("taiwan", "taiwan province of china"),
    "USA": ("usa", "us", "u s a", "u s", "united states", "united states of america", "america"),
    "VGB": ("br virgin islands", "british virgin islands"),
    "SVK": ("slovakia slovak republic",),
    "STP": ("sao tome and principe",),
    "VEN": ("venezuela", "bolivarian republic of venezuela"),
    "VNM": ("vietnam", "viet nam"),
}


def normalize_country_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


class CountryRegistry:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.by_iso3: dict[str, dict[str, str]] = {}
        self.by_alias: dict[str, dict[str, str]] = {}

        for row in rows:
            iso2 = row.get("ISO_Code_A2", "").strip().upper()
            iso3 = row.get("ISO_Code_A3", "").strip().upper()
            country = row.get("country", "").strip()
            if not iso2 or not iso3 or not country:
                continue
            identity = {"iso2": iso2, "iso3": iso3, "country": country}
            self.by_iso3[iso3] = identity
            aliases = {country, iso2, iso3}

            if pycountry is not None:
                item = pycountry.countries.get(alpha_3=iso3)
                if item:
                    aliases.add(item.name)
                    aliases.add(getattr(item, "official_name", ""))
                    aliases.add(getattr(item, "common_name", ""))

            aliases.update(ALIASES_BY_ISO3.get(iso3, ()))
            for alias in aliases:
                key = normalize_country_key(alias)
                if key:
                    self.by_alias[key] = identity

    def resolve(self, value: Any) -> dict[str, str] | None:
        key = normalize_country_key(value)
        return self.by_alias.get(key)


@lru_cache(maxsize=8)
def load_country_registry(path: str = "inputs/WS_PPG.csv") -> CountryRegistry:
    ppg_path = Path(path)
    if not ppg_path.exists():
        raise FileNotFoundError(f"Country whitelist not found: {ppg_path}")
    with ppg_path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"ISO_Code_A2", "ISO_Code_A3", "country"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Country whitelist is missing columns: {', '.join(sorted(missing))}")
        return CountryRegistry(list(reader))


def resolve_country(value: Any, path: str = "inputs/WS_PPG.csv") -> dict[str, str] | None:
    """Resolve a provider name, slug, ISO-2, or ISO-3 to a PPG country."""
    return load_country_registry(path).resolve(value)


def allowed_iso3(path: str = "inputs/WS_PPG.csv") -> set[str]:
    return set(load_country_registry(path).by_iso3)

