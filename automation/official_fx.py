from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    from config import DEFAULT_EUR_TO_USD
except Exception:
    DEFAULT_EUR_TO_USD = 1.10


ECB_DAILY_XML_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


@dataclass(frozen=True)
class OfficialFxRate:
    rate: float
    date: str
    source: str = "ECB"
    status: str = "online"
    fetched_at: str = ""
    error: str = ""

    @property
    def label(self) -> str:
        suffix = f" ({self.source} {self.date}, {self.status})"
        return f"{self.rate:.4f}{suffix}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cube_attr(element: ET.Element, name: str) -> str | None:
    return element.attrib.get(name)


def parse_ecb_eur_usd_xml(xml_text: str) -> OfficialFxRate:
    root = ET.fromstring(xml_text)
    rate_date = ""
    usd_rate: float | None = None

    for element in root.iter():
        time_value = _cube_attr(element, "time")
        if time_value:
            rate_date = time_value

        if _cube_attr(element, "currency") == "USD":
            rate_value = _cube_attr(element, "rate")
            if rate_value is None:
                continue
            usd_rate = float(rate_value)
            break

    if usd_rate is None:
        raise ValueError("ECB feed did not contain a USD rate.")
    if not rate_date:
        raise ValueError("ECB feed did not contain a rate date.")

    return OfficialFxRate(
        rate=usd_rate,
        date=rate_date,
        source="ECB",
        status="online",
        fetched_at=_now_utc(),
    )


def fetch_ecb_eur_usd(timeout_seconds: float = 4.0) -> OfficialFxRate:
    request = Request(
        ECB_DAILY_XML_URL,
        headers={"User-Agent": "TT-Robopricing/1.0"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        xml_text = response.read().decode("utf-8")
    return parse_ecb_eur_usd_xml(xml_text)


def load_cached_rate(cache_path: str | Path) -> OfficialFxRate | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return OfficialFxRate(
        rate=float(data["rate"]),
        date=str(data.get("date", "")),
        source=str(data.get("source", "ECB")),
        status="cached",
        fetched_at=str(data.get("fetched_at", "")),
        error=str(data.get("error", "")),
    )


def save_cached_rate(cache_path: str | Path, rate: OfficialFxRate) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(rate), indent=2), encoding="utf-8")


def get_official_eur_usd(
    cache_path: str | Path,
    *,
    fallback_rate: float = DEFAULT_EUR_TO_USD,
    timeout_seconds: float = 4.0,
) -> OfficialFxRate:
    try:
        rate = fetch_ecb_eur_usd(timeout_seconds=timeout_seconds)
        save_cached_rate(cache_path, rate)
        return rate
    except Exception as exc:
        cached = load_cached_rate(cache_path)
        if cached is not None:
            return OfficialFxRate(
                rate=cached.rate,
                date=cached.date,
                source=cached.source,
                status="cached",
                fetched_at=cached.fetched_at,
                error=str(exc),
            )
        return OfficialFxRate(
            rate=float(fallback_rate or DEFAULT_EUR_TO_USD),
            date="unavailable",
            source="ECB",
            status="fallback",
            fetched_at=_now_utc(),
            error=str(exc),
        )
