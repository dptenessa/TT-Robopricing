#!/usr/bin/env python3

from __future__ import annotations

import csv
import os
import random
import re
import time
from datetime import date
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import pycountry
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)


OUTPUT_CURRENT_CSV = "scrapes/airalo_current.csv"
OUTPUT_PREVIOUS_CSV = "scrapes/airalo_previous.csv"
WHITELIST_XLSX = "inputs/WS_PPG.csv"

TEST_MODE = False
TEST_LIMIT = 5
HEADED = False
CURRENCY = "USD"
MAX_PER_MINUTE = 60

REGIONAL_SLUGS = {
    "africa",
    "africa-safari",
    "asia",
    "caribbean-islands",
    "europe",
    "global",
    "latin-america",
    "middle-east-and-north-africa",
    "north-america",
    "oceania",
    "world",
}

BASE_URL = "https://www.airalo.com"
COUNTRIES_URL = "https://www.airalo.com/local-esim"

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


def human_wait(page, min_ms=150, max_ms=350):
    page.wait_for_timeout(random.randint(min_ms, max_ms))


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def polite_sleep(min_s: float = 0.3, max_s: float = 0.9) -> None:
    time.sleep(random.uniform(min_s, max_s))


def clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def extract_offer_ponder(special_offer: str) -> str:
    if not special_offer:
        return ""
    m = re.search(r"(\d+)\s*%", special_offer)
    return m.group(1) if m else ""


def iso2_to_iso3(iso2: str | None) -> str | None:
    if not iso2:
        return None
    country = pycountry.countries.get(alpha_2=iso2.upper())
    return country.alpha_3 if country else None


def iso2_to_country_name(iso2: str | None) -> str | None:
    if not iso2:
        return None
    country = pycountry.countries.get(alpha_2=iso2.upper())
    return country.name if country else None


def country_name_to_iso2(name: str | None) -> str | None:
    if not name:
        return None

    name = name.strip()

    manual_map = {
        "United States": "US",
        "United Kingdom": "GB",
        "South Korea": "KR",
        "North Korea": "KP",
        "Vietnam": "VN",
        "Russia": "RU",
        "Moldova": "MD",
        "Bolivia": "BO",
        "Venezuela": "VE",
        "Iran": "IR",
        "Laos": "LA",
        "Tanzania": "TZ",
        "Syria": "SY",
        "Taiwan": "TW",
        "Brunei": "BN",
        "Czech Republic": "CZ",
        "Palestine": "PS",
        "Kosovo": "XK",
        "Turkey": "TR",
        "Türkiye": "TR",
        "Ivory Coast": "CI",
        "Cape Verde": "CV",
        "Hong Kong": "HK",
        "Macau": "MO",
        "Réunion": "RE",
        "Reunion": "RE",
        "French Guiana": "GF",
        "French Polynesia": "PF",
        "Guadeloupe": "GP",
        "Martinique": "MQ",
        "Mayotte": "YT",
        "New Caledonia": "NC",
        "Saint Barthelemy": "BL",
        "Saint Martin": "MF",
        "Saint Pierre and Miquelon": "PM",
        "Isle of Man": "IM",
        "Jersey": "JE",
        "Guernsey": "GG",
        "Åland Islands": "AX",
        "Aland Islands": "AX",
        "Democratic Republic of Congo": "CD",
        "Congo": "CG",
        "Eswatini": "SZ",
        "Swaziland": "SZ",
        "Vatican": "VA",
        "Timor-Leste": "TL",
        "Northern Cyprus": "CY",
        "Virgin Islands (U.S.)": "VI",
            }

    if name in manual_map:
        return manual_map[name]

    country = pycountry.countries.get(name=name)
    if country:
        return country.alpha_2

    try:
        country = pycountry.countries.search_fuzzy(name)[0]
        return country.alpha_2
    except Exception:
        return None


def slug_to_country_name(slug: str) -> str:
    manual_slug_map = {
        "united-states": "United States",
        "united-kingdom": "United Kingdom",
        "south-korea": "South Korea",
        "north-korea": "North Korea",
        "north-macedonia": "North Macedonia",
        "czech-republic": "Czech Republic",
        "bosnia-and-herzegovina": "Bosnia and Herzegovina",
        "central-african-republic": "Central African Republic",
        "dominican-republic": "Dominican Republic",
        "el-salvador": "El Salvador",
        "hong-kong": "Hong Kong",
        "papua-new-guinea": "Papua New Guinea",
        "trinidad-and-tobago": "Trinidad and Tobago",
        "united-arab-emirates": "United Arab Emirates",
        "ivory-coast": "Ivory Coast",
        "cape-verde": "Cape Verde",
        "french-guiana": "French Guiana",
        "french-polynesia": "French Polynesia",
        "new-caledonia": "New Caledonia",
        "isle-of-man": "Isle of Man",
        "aland-islands": "Åland Islands",
        "democratic-republic-of-congo": "Democratic Republic of Congo",
        "saint-barthelemy": "Saint Barthelemy",
        "saint-lucia": "Saint Lucia",
        "saint-martin": "Saint Martin",
        "saint-pierre-and-miquelon": "Saint Pierre and Miquelon",
        "turkey": "Turkey",
        "turkiye": "Türkiye",
    }
    return manual_slug_map.get(slug, slug.replace("-", " ").strip().title())


def load_allowed_iso3(filepath: str) -> set[str]:
    column_name = "ISO_Code_A3"
    df = pd.read_csv(filepath, encoding="utf-8-sig")

    if column_name not in df.columns:
        raise RuntimeError(f"Column '{column_name}' not found in CSV. Headers: {list(df.columns)}")

    return {
        str(value).strip().upper()
        for value in df[column_name]
        if pd.notna(value) and str(value).strip()
    }


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


def setup_page(page) -> None:
    def route_handler(route):
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    page.route("**/*", route_handler)
    page.set_default_timeout(60000)
    page.set_default_navigation_timeout(90000)


def goto_with_retry(page, url: str, tries: int = 3) -> None:
    last_err: Optional[Exception] = None

    for attempt in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            try:
                page.locator("body").wait_for(state="visible", timeout=10000)
            except Exception:
                pass

            human_wait(page, 300, 700)
            return

        except Exception as e:
            last_err = e
            if attempt < tries - 1:
                time.sleep(random.uniform(1.0, 2.0))

    raise RuntimeError(f"Failed to load {url} after {tries} tries: {last_err}")


def get_visible_tab_labels(page) -> list[str]:
    labels = []

    try:
        tabs = page.locator('button[role="tab"]')
        for i in range(tabs.count()):
            txt = clean(tabs.nth(i).inner_text(timeout=1000))
            if txt:
                labels.append(txt)
    except Exception:
        pass

    return labels


def click_airalo_tab_by_label(page, label: str) -> bool:
    try:
        tabs = page.locator('button[role="tab"]')

        for i in range(tabs.count()):
            tab = tabs.nth(i)
            txt = clean(tab.inner_text(timeout=1000))

            if txt.lower() == label.lower():
                tab.click(timeout=5000, force=True)
                human_wait(page, 400, 900)
                return True

    except Exception:
        pass

    return False


def accept_cookies(page) -> bool:
    selectors = [
        "#onetrust-accept-btn-handler",
        "#didomi-notice-agree-button",
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button:has-text("Agree")',
        'button:has-text("Allow all")',
    ]

    human_wait(page, 800, 1600)

    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue

            for i in range(loc.count()):
                btn = loc.nth(i)
                try:
                    if btn.is_visible():
                        print(f"[cookies] clicking {sel}")
                        btn.click(timeout=5000, force=True)
                        human_wait(page, 800, 1600)
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    return False


def set_currency_cookie(context, currency: str) -> None:
    context.add_cookies([
        {
            "name": "currency",
            "value": currency.upper(),
            "domain": ".airalo.com",
            "path": "/",
        },
        {
            "name": "selected_currency",
            "value": currency.upper(),
            "domain": ".airalo.com",
            "path": "/",
        },
    ])


def get_country_links(page) -> list[dict[str, str]]:
    goto_with_retry(page, COUNTRIES_URL)
    accept_cookies(page)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        full_url = urljoin(BASE_URL, href)

        if not full_url.startswith(BASE_URL):
            continue

        path = full_url.replace(BASE_URL, "").split("?")[0].rstrip("/")

        if not path.endswith("-esim"):
            continue

        slug = path.strip("/").replace("-esim", "")

        if not slug or slug in {"local"}:
            continue

        if full_url in seen:
            continue

        seen.add(full_url)

        title_el = a.select_one('[data-testid="locations-details_title"]')
        if title_el:
            provider_country = clean(title_el.get_text(" ", strip=True))
        else:
            provider_country = clean(a.get_text(" ", strip=True))

        if not provider_country or len(provider_country) > 80:
            provider_country = slug_to_country_name(slug)

        results.append({
            "slug": slug,
            "url": full_url,
            "provider_country": provider_country,
        })

    results = sorted(results, key=lambda x: x["provider_country"].lower())
    print(f"[links] found {len(results)} country links")
    return results


def detect_currency_from_text(text: str) -> str:
    upper = text.upper()
    if "USD" in upper or "US$" in upper or "$" in text:
        return "USD"
    if "EUR" in upper or "€" in text:
        return "EUR"
    return CURRENCY


def make_row(
    provider_country: str,
    url: str,
    plan: str,
    gb: str,
    days: int,
    price: float,
    currency: str,
    source_text: str,
    variant_id: str = "",
    special_offer: str = "",
) -> dict:
    return {
        "provider_country": provider_country,
        "url": url,
        "plan": plan,
        "gb": gb,
        "days": days,
        "price": price,
        "currency": currency,
        "special_offer": special_offer,
        "offer_ponder": extract_offer_ponder(special_offer),
        "source": "playwright",
        "variant_id": variant_id,
        "name": source_text,
        "slug": variant_id,
        "eur_price": price if currency == "EUR" else None,
        "usd_price": price if currency == "USD" else None,
    }


def merge_currency_rows(usd_rows: list[dict], eur_rows: list[dict]) -> list[dict]:
    merged = {}

    for row in usd_rows:
        key = (row["plan"], str(row["gb"]), int(row["days"]))
        merged[key] = row.copy()
        merged[key]["usd_price"] = row["price"]
        merged[key]["eur_price"] = None
        merged[key]["currency"] = "USD"
        merged[key]["price"] = row["price"]

    for row in eur_rows:
        key = (row["plan"], str(row["gb"]), int(row["days"]))

        if key not in merged:
            merged[key] = row.copy()
            merged[key]["usd_price"] = None

        merged[key]["eur_price"] = row["price"]

    return dedupe_rows(list(merged.values()))



def dedupe_rows(rows: list[dict]) -> list[dict]:
    dedup: dict[tuple, dict] = {}

    for row in rows:
        price = row.get("price")
        if price is None:
            continue

        key = (
            row.get("plan"),
            str(row.get("gb")),
            int(row.get("days")),
            row.get("currency"),
        )

        old = dedup.get(key)
        if old is None:
            dedup[key] = row
            continue

        if len(str(row.get("name", ""))) < len(str(old.get("name", ""))):
            dedup[key] = row

    def gb_sort_value(gb: str) -> float:
        txt = str(gb).strip().upper()
        if txt == "UNLIMITED":
            return 999999.0
        txt = txt.replace("GB", "").strip()
        try:
            return float(txt)
        except Exception:
            return 999998.0

    def sort_key(x: dict):
        plan_order = 1 if x["plan"] == "Unlimited" else 0
        return (plan_order, int(x["days"]), gb_sort_value(str(x["gb"])))

    return sorted(dedup.values(), key=sort_key)


def scrape_country(page, url: str, provider_country: str) -> list[dict]:
    goto_with_retry(page, url)
    accept_cookies(page)

    rows: list[dict] = []
    standard_rows = []
    unlimited_rows = []

    tab_labels = get_visible_tab_labels(page)
    print(f"[tabs] {provider_country}: {tab_labels}")

    has_standard_unlimited = (
        "Standard" in tab_labels
        and "Unlimited" in tab_labels
    )

    if has_standard_unlimited:
        if click_airalo_tab_by_label(page, "Standard"):
            standard_rows = extract_data_only_rows(page, provider_country, url)
            for r in standard_rows:
                r["plan"] = "Standard"
            rows.extend(standard_rows)

        if click_airalo_tab_by_label(page, "Unlimited"):
            unlimited_rows = extract_data_only_rows(page, provider_country, url)
            for r in unlimited_rows:
                r["plan"] = "Unlimited"
                r["gb"] = "Unlimited"
            rows.extend(unlimited_rows)

        print(
            f"[extract] {provider_country}: "
            f"standard={len(standard_rows)}, unlimited={len(unlimited_rows)}"
        )

    else:
        # Example: tabs are Data / Data+Voice+SMS.
        # We only want the Data tab/content.
        if "Data" in tab_labels:
            click_airalo_tab_by_label(page, "Data")

        data_rows = extract_data_only_rows(page, provider_country, url)
        for r in data_rows:
            if r.get("gb") == "Unlimited":
                r["plan"] = "Unlimited"
            else:
                r["plan"] = "Standard"

        rows.extend(data_rows)
        print(f"[extract] {provider_country}: data={len(data_rows)}")

    if not rows:
        debug_file = f"scrapes/airalo_debug_{provider_country.lower().replace(' ', '_')}.html"
        ensure_parent_dir(debug_file)
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"[debug] wrote {debug_file}")

    return dedupe_rows(rows)


def extract_single_package_from_text(
    text: str,
    provider_country: str,
    url: str,
) -> Optional[dict]:
    raw = clean(text)

    if not raw:
        return None

    currency = detect_currency_from_text(raw)

    price = None

    price_patterns = [
        r"(?:US\$|\$|USD)\s*(\d+(?:[.,]\d+)?)",
        r"(?:€|EUR)\s*(\d+(?:[.,]\d+)?)",
    ]

    for pat in price_patterns:
        m = re.search(pat, raw, flags=re.I)
        if m:
            price = float(m.group(1).replace(",", "."))
            break

    if price is None:
        return None

    days_match = re.search(r"\b(\d+)\s*days?\b", raw, flags=re.I)
    if not days_match:
        return None

    days = int(days_match.group(1))

    if re.search(r"\bUnlimited\b", raw, flags=re.I):
        return make_row(
            provider_country=provider_country,
            url=url,
            plan="Unlimited",
            gb="Unlimited",
            days=days,
            price=price,
            currency=currency,
            source_text=raw,
            variant_id=f"unlimited-{days}days",
        )

    gb_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*GB\b", raw, flags=re.I)
    if not gb_match:
        return None

    gb = gb_match.group(1).replace(",", ".")

    return make_row(
        provider_country=provider_country,
        url=url,
        plan="Standard",
        gb=gb,
        days=days,
        price=price,
        currency=currency,
        source_text=raw,
        variant_id=f"{gb}gb-{days}days",
    )


def extract_data_only_rows(page, provider_country: str, url: str) -> list[dict]:
    """
    Extract only real visible Airalo package cards.
    Avoid broad parent containers because they mix prices/days/GB from multiple cards.
    """

    card_selectors = [
        '[data-testid="sim-package-card"]',
        '[data-testid*="package-card"]',
        '[data-testid*="package_card"]',
        'button[aria-label*="Select" i]',
    ]

    all_rows: list[dict] = []

    for sel in card_selectors:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 40)

            if count == 0:
                continue

            for i in range(count):
                item = loc.nth(i)

                try:
                    if not item.is_visible():
                        continue

                    raw = clean(item.get_attribute("aria-label") or "")

                    if not raw:
                        raw = clean(item.inner_text(timeout=1000))

                    if not raw:
                        continue

                    if re.search(r"\b(calls?|minutes?|sms|texts?)\b", raw, flags=re.I):
                        continue

                    row = extract_single_package_from_text(raw, provider_country, url)

                    if row:
                        all_rows.append(row)

                except Exception:
                    continue

            if all_rows:
                break

        except Exception:
            continue

    return dedupe_rows(all_rows)


def variant_id_from_fields(plan: str, gb: str, days: int | str) -> str:
    plan_slug = re.sub(r"[^a-z0-9]+", "-", str(plan).lower()).strip("-")
    gb_slug = re.sub(r"[^a-z0-9]+", "-", str(gb).lower()).strip("-")
    days_slug = f"{days}days" if str(days) else "nodays"
    return f"{plan_slug}-{gb_slug}-{days_slug}"


def main() -> None:
    allowed_iso3 = load_allowed_iso3(WHITELIST_XLSX)
    print(f"Loaded {len(allowed_iso3)} allowed ISO3 codes from {WHITELIST_XLSX}")

    all_current_rows: list[dict] = []
    allowed_country_count = 0
    skipped_not_whitelisted = 0
    skipped_no_iso = 0
    failed_countries = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=not HEADED,
            args=["--no-proxy-server"],
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 1400},
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        set_currency_cookie(context, "USD")

        page = context.new_page()
        setup_page(page)

        countries = get_country_links(page)
        countries = sorted(countries, key=lambda x: x["provider_country"].lower())

        filtered_countries = []
        skipped_no_iso = 0
        skipped_not_whitelisted = 0

        for country_info in countries:
            slug = country_info["slug"]
            provider_country = country_info["provider_country"]

            if slug in REGIONAL_SLUGS:
                continue

            iso2 = country_name_to_iso2(provider_country)
            iso3 = iso2_to_iso3(iso2)

            if not iso3:
                skipped_no_iso += 1
                print(f"Skipping {slug} ({provider_country}) - could not map country to ISO3")
                continue

            if iso3 not in allowed_iso3:
                skipped_not_whitelisted += 1
                continue

            country_info["iso2"] = iso2
            country_info["iso3"] = iso3
            country_info["country_name"] = iso2_to_country_name(iso2)

            filtered_countries.append(country_info)

        countries = filtered_countries

        print(f"[filter] countries to scrape after whitelist: {len(countries)}")
        print(f"[filter] skipped_no_iso={skipped_no_iso}, skipped_not_whitelisted={skipped_not_whitelisted}")

        if TEST_MODE:
            countries = countries[: max(TEST_LIMIT * 5, TEST_LIMIT)]
            print(f"TEST_MODE enabled: probing first {len(countries)} country pages")

        window_start = time.time()
        requests_done_in_window = 0

        for i, country_info in enumerate(countries, start=1):
            if requests_done_in_window >= MAX_PER_MINUTE:
                elapsed = time.time() - window_start
                if elapsed < 60:
                    sleep_time = 60 - elapsed
                    print(f"[rate-limit] sleeping {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                window_start = time.time()
                requests_done_in_window = 0

            slug = country_info["slug"]
            url = country_info["url"]
            provider_country = country_info["provider_country"]

            if slug in REGIONAL_SLUGS:
                print(f"Skipping {slug} ({provider_country}) - regional/global offer")
                continue

            iso2 = country_info["iso2"]
            iso3 = country_info["iso3"]
            country_name = country_info["country_name"]

            print(f"\n[{i}/{len(countries)}] Scraping: {slug} | {provider_country} | {iso3}")

            try:
                set_currency_cookie(context, "USD")
                usd_rows = scrape_country(page, url, provider_country)

                set_currency_cookie(context, "EUR")
                eur_rows = scrape_country(page, url, provider_country)

                rows = merge_currency_rows(usd_rows, eur_rows)
                requests_done_in_window += 1

                if not rows:
                    print(f"No variants found for {slug}")
                    polite_sleep()
                    continue

                allowed_country_count += 1

                for row in rows:
                    currency = "USD"
                    price = row.get("usd_price") or row.get("price")

                    output_row = {
                        "Provider": "airalo",
                        "ProviderCountry": provider_country,
                        "ISO": iso2,
                        "Country": country_name,
                        "URL": url,
                        "Plan": row.get("plan"),
                        "GB": row.get("gb"),
                        "Days": row.get("days"),
                        "Price": price,
                        "Currency": currency,
                        "SpecialOffer": row.get("special_offer", ""),
                        "OfferPonder": row.get("offer_ponder", ""),
                        "PriceDate": date.today().isoformat(),
                        "ISO3": iso3,
                        "variant_id": row.get("variant_id") or variant_id_from_fields(
                            str(row.get("plan", "")),
                            str(row.get("gb", "")),
                            row.get("days", ""),
                        ),
                        "name": row.get("name", ""),
                        "eur_price": row.get("eur_price"),
                        "usd_price": row.get("usd_price"),
                    }

                    all_current_rows.append(output_row)

                    print(
                        f"{output_row['ProviderCountry']:24} | "
                        f"{str(output_row['Plan']):10} | "
                        f"{str(output_row['GB']):>10} | "
                        f"{str(output_row['Days']):>3} days | "
                        f"{output_row['Currency']} {output_row['Price']}"
                    )

                if TEST_MODE and allowed_country_count >= TEST_LIMIT:
                    print(f"\nTest mode reached limit of {TEST_LIMIT} allowed countries.")
                    break

                polite_sleep()

            except Exception as e:
                failed_countries += 1
                requests_done_in_window += 1
                print(f"Error scraping {slug} ({provider_country}): {type(e).__name__}: {e}")
                time.sleep(random.uniform(3.0, 7.0))
                continue

        context.close()
        browser.close()

    if not all_current_rows:
        raise RuntimeError("No variants found for any allowed country")

    rotate_and_write_csv(all_current_rows, OUTPUT_CURRENT_CSV, OUTPUT_PREVIOUS_CSV)

    print(f"\nCurrent scrape rows: {len(all_current_rows)}")
    print(f"Current CSV file: {OUTPUT_CURRENT_CSV}")
    print(f"Previous CSV file: {OUTPUT_PREVIOUS_CSV}")
    print(
        f"Summary: scraped_allowed_countries={allowed_country_count}, "
        f"skipped_not_whitelisted={skipped_not_whitelisted}, "
        f"skipped_no_iso={skipped_no_iso}, "
        f"failed_countries={failed_countries}"
    )


if __name__ == "__main__":
    main()
