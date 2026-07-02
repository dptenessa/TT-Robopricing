#!/usr/bin/env python3

import csv
import html
import os
import random
import re
import time
from datetime import date
from typing import Any
import pandas as pd

import pycountry
import requests
from bs4 import BeautifulSoup


OUTPUT_CURRENT_CSV = "scrapes/holafly_current.csv"
OUTPUT_PREVIOUS_CSV = "scrapes/holafly_previous.csv"
WHITELIST_XLSX = "inputs/WS_PPG.csv"

TEST_MODE = False
TEST_LIMIT = 2
CURRENCY = "USD"  # use "USD" or "EUR"

COUNTRIES_URL = "https://esim.holafly.com/shop/countries/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )
}

CSV_FIELDNAMES = [
    "Provider",
    "ProviderCountry",
    "ISO",
    "Country",
    "URL",
    "Plan",
    "GB",
    "Days",
    "Price",
    "Currency",
    "SpecialOffer",
    "OfferPonder",
    "PriceDate",
    "ISO3",
    "variant_id",
    "name",
    "eur_price",
    "usd_price",
]


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def polite_sleep(min_s: float = 1.2, max_s: float = 2.8) -> None:
    time.sleep(random.uniform(min_s, max_s))


def get_country_rows(
    session: requests.Session,
    url: str,
    iso3: str | None = None,
) -> list[dict[str, Any]]:
    page_html = fetch_html(session, url)
    props_list = extract_all_props(page_html)

    print(f"Found {len(props_list)} astro-island props blocks on {url}")

    all_rows: list[dict[str, Any]] = []

    for idx, props in enumerate(props_list):
        rows = extract_variants_from_text(props)
        print(f"astro-island #{idx}: props length={len(props)}, parsed_rows={len(rows)}")
        all_rows.extend(rows)

    best_by_days: dict[int, dict[str, Any]] = {}

    for row in all_rows:
        days = row["days"]
        if days not in best_by_days:
            best_by_days[days] = row

    return sorted(best_by_days.values(), key=lambda x: x["days"])


def country_name_to_iso3(name: str) -> str | None:
    cleaned = name.strip()

    aliases = {
        "usa": "USA",
        "united states": "USA",
        "uk": "GBR",
        "united kingdom": "GBR",
        "uae": "ARE",
        "south korea": "KOR",
        "north korea": "PRK",
        "czech republic": "CZE",
        "russia": "RUS",
        "vietnam": "VNM",
    }

    key = cleaned.lower()

    if key in aliases:
        return aliases[key]

    try:
        return pycountry.countries.lookup(cleaned).alpha_3
    except LookupError:
        return None


def fetch_html(
    session: requests.Session,
    url: str,
    max_retries: int = 4,
) -> str:
    delay = 2.0

    for attempt in range(max_retries):
        try:
            r = session.get(url, timeout=30)

            if r.status_code == 200:
                return r.text

            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == max_retries - 1:
                    r.raise_for_status()
                time.sleep(delay + random.random())
                delay *= 2
                continue

            r.raise_for_status()

        except requests.RequestException:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay + random.random())
            delay *= 2

    raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries")


def iso3_to_iso2(iso3: str | None) -> str | None:
    if not iso3:
        return None
    country = pycountry.countries.get(alpha_3=iso3)
    return country.alpha_2 if country else None


def iso2_to_country_name(iso2: str | None) -> str | None:
    if not iso2:
        return None
    country = pycountry.countries.get(alpha_2=iso2)
    return country.name if country else None


def load_allowed_iso3(filepath: str) -> set[str]:
    COLUMN_NAME = "ISO_Code_A3"

    df = pd.read_csv(filepath, encoding="utf-8-sig")

    print("Columns found:", list(df.columns))

    if COLUMN_NAME not in df.columns:
        raise ValueError(
            f"Column '{COLUMN_NAME}' not found in columns: {list(df.columns)}"
        )

    allowed: set[str] = set()

    for value in df[COLUMN_NAME]:
        if pd.isna(value):
            continue

        code = str(value).strip().upper()

        if code:
            allowed.add(code)

    return allowed


def get_country_links(session: requests.Session) -> list[dict[str, str]]:
    html_text = fetch_html(session, COUNTRIES_URL)
    soup = BeautifulSoup(html_text, "html.parser")

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for a in soup.select("a.card-flag__link"):
        href = a.get("href", "").strip()
        title = a.get("title", "").strip()

        m = re.search(r"^/esim-([^/]+)/?$", href)
        if not m:
            continue

        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)

        provider_country = title.replace("eSIM for ", "").strip() if title else slug

        results.append({
            "slug": slug,
            "url": f"https://esim.holafly.com{href}",
            "provider_country": provider_country,
        })

    return results


def extract_all_props(page_html: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    props_list: list[str] = []

    for island in soup.find_all("astro-island"):
        props = island.get("props")
        if props:
            props_list.append(html.unescape(props))

    return props_list


def extract_variants_from_text(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    results.extend(extract_variants_from_current_props(text))
    if results:
        return results

    starts = [m.start() for m in re.finditer(r'"variantId":\[0,"?\d+"?\]', text)]
    if not starts:
        return results

    starts.append(len(text))

    for i in range(len(starts) - 1):
        block = text[starts[i]:starts[i + 1]]

        variant_match = re.search(r'"variantId":\[0,"?(\d+)"?\]', block)
        name_match = re.search(r'"name":\[0,"([^"]+)"\]', block)
        iso3_match = re.search(r'"isocode":\[0,"([A-Z]{3})"\]', block)
        days_match = re.search(r'"days":\[0,(\d+)\]', block)

        if not (variant_match and name_match and iso3_match and days_match):
            continue

        days = int(days_match.group(1))

        if days > 30:
            continue

        currencies: dict[str, float] = {}

        for cur, value in re.findall(r'"([A-Z]{3})":\[0,([\d.]+)\]', block):
            currencies[cur] = float(value)

        if not currencies:
            continue

        iso3 = iso3_match.group(1)
        iso2 = iso3_to_iso2(iso3)

        results.append({
            "days": days,
            "variant_id": variant_match.group(1),
            "name": name_match.group(1),
            "iso3": iso3,
            "iso2": iso2,
            "country": iso2_to_country_name(iso2),
            "eur_price": currencies.get("EUR"),
            "usd_price": currencies.get("USD"),
            "cad_price": currencies.get("CAD"),
            "all_prices": currencies,
        })

    return results


def extract_variants_from_current_props(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    pattern = re.compile(
        r'"name":\[0,"(?P<name>[^"]+)"\],'
        r'"isocode":\[0,"(?P<iso3>[A-Z]{3})"\],'
        r'"days":\[0,(?P<days>\d+)\],'
        r'"gigas":\[0,"(?P<gigas>[^"]+)"\],'
        r'"currencies":\[0,\{(?P<currencies>.*?)\}\]',
        re.S,
    )

    seen: set[tuple[str, int, str]] = set()

    for match in pattern.finditer(text):
        days = int(match.group("days"))
        if days > 30:
            continue

        iso3 = match.group("iso3")
        gigas = match.group("gigas")
        dedupe_key = (iso3, days, gigas)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        currencies: dict[str, float] = {}
        for cur, value in re.findall(
            r'"([A-Z]{3})":\[0,(\d+(?:\.\d+)?)\]',
            match.group("currencies"),
        ):
            currencies[cur] = float(value)

        if not currencies:
            continue

        iso2 = iso3_to_iso2(iso3)
        results.append({
            "days": days,
            "variant_id": f"holafly-{iso3}-{gigas}-{days}",
            "name": match.group("name"),
            "iso3": iso3,
            "iso2": iso2,
            "country": iso2_to_country_name(iso2),
            "eur_price": currencies.get("EUR"),
            "usd_price": currencies.get("USD"),
            "cad_price": currencies.get("CAD"),
            "all_prices": currencies,
        })

    return results


def write_rows_to_csv(rows: list[dict], filename: str) -> None:
    ensure_parent_dir(filename)

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDNAMES,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        
def rotate_and_write_csv(rows: list[dict], current_file: str, previous_file: str) -> None:
    ensure_parent_dir(current_file)
    ensure_parent_dir(previous_file)

    tmp_file = current_file + ".tmp"

    write_rows_to_csv(rows, tmp_file)

    if os.path.exists(current_file):
        os.replace(current_file, previous_file)

    os.replace(tmp_file, current_file)


def main() -> None:
    allowed_iso3 = load_allowed_iso3(WHITELIST_XLSX)
    print(f"Loaded {len(allowed_iso3)} allowed ISO3 codes from {WHITELIST_XLSX}")

    session = build_session()
    countries = get_country_links(session)
    print(f"Found {len(countries)} country links")
    
    all_current_rows: list[dict[str, Any]] = []
    allowed_country_count = 0
    skipped_not_whitelisted = 0
    failed_countries = 0

    if TEST_MODE:
        countries = countries[: max(TEST_LIMIT * 5, TEST_LIMIT)]
        print(f"TEST_MODE enabled: probing first {len(countries)} country pages")

    for i, country_info in enumerate(countries, start=1):
        slug = country_info["slug"]
        url = country_info["url"]
        provider_country = country_info["provider_country"]

        print(f"\n[{i}/{len(countries)}] Scraping: {slug}")

        try:
            iso3_guess = country_name_to_iso3(provider_country)
            rows = get_country_rows(session, url, iso3=iso3_guess)
            usd_count = sum(1 for r in rows if r.get("usd_price") is not None)
            eur_count = sum(1 for r in rows if r.get("eur_price") is not None)

            print(
                f"[CURRENCY CHECK] {slug} | "
                f"USD={usd_count} | "
                f"EUR={eur_count} | "
                f"BOTH={'YES' if usd_count > 0 and eur_count > 0 else 'NO'}"
            )

            if not rows:
                print(f"No variants found for {slug}")
                continue

            valid_rows = [row for row in rows if row.get("iso3") in allowed_iso3]

            if not valid_rows:
                found_iso3 = sorted({row.get("iso3") for row in rows if row.get("iso3")})
                skipped_not_whitelisted += 1
                print(
                    f"Skipping {slug} - no whitelisted ISO3 found. "
                    f"Parsed ISO3s: {found_iso3 or ['NONE']}"
                )
                continue

            allowed_country_count += 1

            for row in valid_rows:
                days = row.get("days")
                price = row.get("usd_price") if CURRENCY == "USD" else row.get("eur_price")
                iso2 = row.get("iso2")

                if price is None:
                    print(f"Skipping row for {provider_country} {days} days - no {CURRENCY} price")
                    continue

                output_row = {
                    "Provider": "holafly",
                    "ProviderCountry": provider_country,
                    "ISO": iso2,
                    "Country": row.get("country") or iso2_to_country_name(iso2),
                    "URL": url,
                    "Plan": "Unlimited",
                    "GB": "Unlimited",
                    "Days": days,
                    "Price": price,
                    "Currency": CURRENCY,
                    "SpecialOffer": "",
                    "OfferPonder": "",
                    "PriceDate": date.today().isoformat(),
                    "ISO3": row.get("iso3"),
                    "variant_id": row.get("variant_id"),
                    "name": row.get("name"),
                    "eur_price": row.get("eur_price"),
                    "usd_price": row.get("usd_price"),
                }

                all_current_rows.append(output_row)

                print(
                    f"{output_row['ProviderCountry']:20} | "
                    f"{output_row['ISO3']} | "
                    f"{output_row['ISO']} | "
                    f"{output_row['Country']} | "
                    f"{output_row['Days']:>2} days | "
                    f"{CURRENCY} {output_row['Price']}"
                )

            if TEST_MODE and allowed_country_count >= TEST_LIMIT:
                print(f"\nTest mode reached limit of {TEST_LIMIT} allowed country/countries.")
                break

            polite_sleep()

        except Exception as e:
            failed_countries += 1
            print(f"Error scraping {slug}: {e}")
            continue
        
    if not all_current_rows:
        raise RuntimeError(
            f"No output rows generated. "
            f"country_links={len(countries)}, "
            f"allowed_iso3_count={len(allowed_iso3)}, "
            f"skipped_not_whitelisted={skipped_not_whitelisted}, "
            f"failed_countries={failed_countries}"
        )

    rotate_and_write_csv(all_current_rows, OUTPUT_CURRENT_CSV, OUTPUT_PREVIOUS_CSV)

    print(f"\nCurrent scrape rows: {len(all_current_rows)}")
    print(f"Current CSV file: {OUTPUT_CURRENT_CSV}")
    print(f"Previous CSV file: {OUTPUT_PREVIOUS_CSV}")
    print(
        f"Summary: scraped_allowed_countries={allowed_country_count}, "
        f"skipped_not_whitelisted={skipped_not_whitelisted}, "
        f"failed_countries={failed_countries}"
    )


if __name__ == "__main__":
    main()
