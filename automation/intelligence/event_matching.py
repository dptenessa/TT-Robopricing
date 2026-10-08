"""Shared event-to-country matching and event-quality rules.

This module keeps the opportunity builder and dashboard consistent. It avoids
substring matches (for example CH matching Chile) and distinguishes concrete
destination events from broad multi-destination travel occasions.
"""
from __future__ import annotations

import re
import unicodedata
import csv
from functools import lru_cache
from pathlib import Path
from datetime import date, timedelta

EVENT_MATCHING_VERSION = "2026-10-08-all-input-origin-aliases-v2"


COUNTRY_ALIASES = {
    "JP": ["japan", "tokyo", "osaka", "kyoto"],
    "TR": ["turkey", "türkiye", "turkiye", "istanbul", "antalya"],
    "ES": ["spain", "madrid", "barcelona", "mallorca", "ibiza", "valencia"],
    "MA": ["morocco", "marrakech", "casablanca"],
    "US": ["united states", "usa", "u.s.", "new york", "miami", "los angeles", "las vegas"],
    "MX": ["mexico", "cancun", "mexico city"],
    "CA": ["canada", "toronto", "vancouver", "montreal"],
    "TH": ["thailand", "bangkok", "phuket"],
    "AE": ["united arab emirates", "uae", "dubai", "abu dhabi"],
    "EG": ["egypt", "cairo", "hurghada", "sharm"],
    "CH": ["switzerland", "swiss", "geneva", "zurich", "basel", "bern", "lucerne", "davos"],
    "AR": ["argentina", "buenos aires", "patagonia"],
    "CL": ["chile", "santiago", "atacama"],
    "PE": ["peru", "lima", "cusco", "machu picchu"],
    "IS": ["iceland", "reykjavik"],
    "AL": ["albania", "tirana"],
    "PT": ["portugal", "lisbon", "porto", "madeira"],
    "IT": ["italy", "rome", "milan", "venice", "sicily"],
    "FR": ["france", "paris", "nice", "cannes"],
    "GB": ["united kingdom", "england", "london", "scotland", "wales"],
    "DE": ["germany", "berlin", "munich", "frankfurt"],
    "AT": ["austria", "vienna", "salzburg", "innsbruck"],
    "NL": ["netherlands", "amsterdam", "rotterdam"],
    "BE": ["belgium", "brussels", "antwerp"],
    "HR": ["croatia", "split", "dubrovnik", "zagreb", "hvar"],
    "GR": ["greece", "athens", "crete", "rhodes", "santorini"],
}

ORIGIN_ALIASES = {
    "DE": ["de", "germany", "german", "deutschland"],
    "FR": ["fr", "france", "french"],
    "NL": ["nl", "netherlands", "dutch", "holland"],
    "GB": ["gb", "uk", "united kingdom", "british", "great britain"],
    "IT": ["it", "italy", "italian"],
    "ES": ["es", "spain", "spanish"],
    "HR": ["hr", "croatia", "croatian"],
    "AT": ["at", "austria", "austrian"],
    "CH": ["ch", "switzerland", "swiss"],
}

NON_EVENT_CLASSIFICATIONS = (
    "travel article", "travel pattern", "institutional", "policy",
    "infrastructure", "regulation", "award", "not relevant",
)


def normalize(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


@lru_cache(maxsize=None)
def project_aliases(code: str, origin: bool = False) -> tuple[str, ...]:
    """Load aliases for all portfolio countries without requiring pycountry."""
    code = str(code or "").strip().upper()
    folder = Path(__file__).resolve().parent / "inputs" / "travel_demand"
    values = []
    files = [("origins.csv", "origin_iso2", "country_name")] if origin else []
    files += [("destination_aliases.csv", "destination_iso2", "country_name")]
    for filename, code_key, name_key in files:
        path = folder / filename
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get(code_key) or "").strip().upper() != code:
                        continue
                    values.extend([str(row.get(name_key) or ""), str(row.get("aliases") or "")])
        except OSError:
            continue
    return tuple(x for value in values for x in str(value).split("|") if normalize(x))


def text_match(text: object, aliases: list[str]) -> bool:
    raw = str(text or "")
    normalized = normalize(raw)
    if not normalized:
        return False
    for alias in aliases:
        a = normalize(alias)
        if not a:
            continue
        # ISO codes are accepted only as explicit uppercase tokens.
        if len(a) == 2 and a.isalpha():
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(a.upper()) + r"(?![A-Za-z0-9])", raw):
                return True
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(a).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, normalized):
            return True
    return False


def aliases_for(code: str, origin: bool = False) -> list[str]:
    code = str(code or "").strip().upper()
    source = ORIGIN_ALIASES if origin else COUNTRY_ALIASES
    aliases = list(source.get(code, []))
    aliases.append(code)
    aliases.extend(project_aliases(code, origin))
    try:
        import pycountry
        country = pycountry.countries.get(alpha_2=code)
        if country:
            for field in ("name", "official_name", "common_name"):
                value = getattr(country, field, None)
                if value:
                    aliases.append(value)
    except ImportError:
        pass
    return list(dict.fromkeys(aliases))


def destination_matches(event: dict, destination: str) -> bool:
    # Region is intentionally excluded. A European region does not mean the
    # event is relevant to every European destination.
    text = " ".join(str(event.get(k) or "") for k in ("Country", "Destination"))
    return text_match(text, aliases_for(destination))


def origin_matches(event: dict, origin: str) -> bool:
    raw = str(event.get("Origin_Markets") or "")
    if not raw.strip():
        return True
    return text_match(raw, aliases_for(origin, origin=True))


def parse_date(value: object) -> date | None:
    value = str(value or "").strip()
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def overlaps(event: dict, as_of: date, horizon_days: int = 90) -> bool:
    start = parse_date(event.get("Start_Date"))
    end = parse_date(event.get("End_Date")) or start
    if not start or not end:
        return False
    return start <= as_of + timedelta(days=horizon_days) and end >= as_of


def classify(event: dict) -> str:
    """Return specific, multi-destination, generic, or invalid."""
    if not parse_date(event.get("Start_Date")):
        return "invalid"
    classification = normalize(event.get("Content_Classification"))
    if any(marker in classification for marker in NON_EVENT_CLASSIFICATIONS):
        return "generic"

    country = str(event.get("Country") or "")
    destination = str(event.get("Destination") or "")
    notes = normalize(event.get("Notes"))
    multi_markers = ("multiple", "international", "worldwide", "host country varies")
    separators = country.count(";") + destination.count(";")
    if any(marker in normalize(country + " " + destination) for marker in multi_markers):
        return "multi-destination"
    if separators > 0 or "can be split" in notes or "split by individual" in notes:
        return "multi-destination"
    return "specific"


def scoreable(event: dict) -> bool:
    # Broad patterns remain visible in the dashboard but do not create a
    # destination-specific event score.
    return classify(event) == "specific"
