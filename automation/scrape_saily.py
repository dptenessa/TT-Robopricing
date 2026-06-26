#!/usr/bin/env python3
# v2.2 - Austria debug version

import csv
import json
import os
import random
import re
import time
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup
from curl_cffi import requests

try:
    import pycountry
except ImportError:
    pycountry = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


OUTPUT_CURRENT_CSV = "outputs/saily_current.csv"
OUTPUT_PREVIOUS_CSV = "outputs/saily_previous.csv"
WHITELIST_XLSX = "inputs/WS_PPG.csv"
COUNTRIES_URL = "https://saily.com/all-destinations/"
SITEMAP_URL = "https://saily.com/sitemap.xml"

# Austria-only test
AUSTRIA_ONLY = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://saily.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}

PLAYWRIGHT = None
PLAYWRIGHT_BROWSER = None
PLAYWRIGHT_PAGE = None

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
    "source",
]


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def polite_sleep(min_s: float = 0.8, max_s: float = 1.8) -> None:
    time.sleep(random.uniform(min_s, max_s))


def normalize_country_key(value: str) -> str:
    return str(value).strip().lower().replace("&", "and").replace(" ", "-")


def safe_str(v):
    if pd.isna(v):
        return ""
    return str(v).strip()


def get_iso2_from_row(row) -> str:
    possible_cols = [
        "ISO_Code_A2", "ISO_A2", "ISO2", "Alpha2", "iso2", "country_code", "CountryCode"
    ]
    for col in possible_cols:
        if col in row and pd.notna(row[col]) and str(row[col]).strip():
            return str(row[col]).strip().upper()
    return ""


def get_iso2_from_country_name(country_name: str) -> str:
    if not pycountry:
        return ""

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
    }

    if country_name in manual_map:
        return manual_map[country_name]

    try:
        match = pycountry.countries.lookup(country_name)
        return getattr(match, "alpha_2", "") or ""
    except Exception:
        return ""


def load_country_mapping(csv_path: str):
    df = pd.read_csv(csv_path, encoding="utf-8-sig")

    required = {"country", "ISO_Code_A3"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")

    mapping = {}

    for _, row in df.iterrows():
        country = safe_str(row["country"])
        iso3 = safe_str(row["ISO_Code_A3"]).upper()

        if not country or not iso3:
            continue

        key = normalize_country_key(country)
        iso2 = get_iso2_from_row(row) or get_iso2_from_country_name(country)

        mapping[key] = {
            "country": country,
            "iso3": iso3,
            "iso2": iso2,
        }

    return mapping


def fetch_html_with_playwright(url: str) -> str:
    global PLAYWRIGHT, PLAYWRIGHT_BROWSER, PLAYWRIGHT_PAGE

    if sync_playwright is None:
        print("    [!] Playwright is not installed; browser fallback unavailable")
        return ""

    try:
        if PLAYWRIGHT_PAGE is None:
            print("    [*] Starting browser fallback for Saily")
            PLAYWRIGHT = sync_playwright().start()
            PLAYWRIGHT_BROWSER = PLAYWRIGHT.chromium.launch(headless=True)
            context = PLAYWRIGHT_BROWSER.new_context(
                locale="en-US",
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={
                    "Accept-Language": HEADERS["Accept-Language"],
                    "Referer": HEADERS["Referer"],
                },
            )
            PLAYWRIGHT_PAGE = context.new_page()

        print(f"    [*] Browser fetching: {url}")
        response = PLAYWRIGHT_PAGE.goto(url, wait_until="domcontentloaded", timeout=60_000)
        PLAYWRIGHT_PAGE.wait_for_timeout(3_000)
        status = response.status if response else None
        html = PLAYWRIGHT_PAGE.content()

        if status and status >= 400:
            print(f"    [!] Browser status: {status}")
        if "Just a moment..." in html and "Cloudflare" in html:
            print("    [!] Browser fallback still reached a Cloudflare challenge page")
            return ""
        if len(html) < 10_000:
            print(f"    [!] Browser fallback returned short HTML: {len(html)} chars")

        return html
    except Exception as e:
        print(f"    [!] Browser fallback failed: {type(e).__name__}: {e}")
        return ""


def close_browser_fallback() -> None:
    global PLAYWRIGHT, PLAYWRIGHT_BROWSER, PLAYWRIGHT_PAGE

    try:
        if PLAYWRIGHT_BROWSER is not None:
            PLAYWRIGHT_BROWSER.close()
    finally:
        if PLAYWRIGHT is not None:
            PLAYWRIGHT.stop()
        PLAYWRIGHT = None
        PLAYWRIGHT_BROWSER = None
        PLAYWRIGHT_PAGE = None


def fetch_html(url: str, max_retries: int = 4) -> str:
    delay = 2.0

    for attempt in range(max_retries):
        try:
            print(f"[*] Fetching: {url}")
            resp = requests.get(
                url,
                headers=HEADERS,
                impersonate="chrome124",
                timeout=30,
            )

            if resp.status_code == 200:
                return resp.text

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == max_retries - 1:
                    print(f"    [!] HTTP {resp.status_code} for {url}")
                    return fetch_html_with_playwright(url)
                time.sleep(delay + random.random())
                delay *= 2
                continue

            print(f"    [!] Non-200 status: {resp.status_code}")
            return fetch_html_with_playwright(url)

        except Exception as e:
            if attempt == max_retries - 1:
                print(f"    [!] Request failed: {e}")
                return fetch_html_with_playwright(url)
            time.sleep(delay + random.random())
            delay *= 2

    return ""


def get_country_links(html: str, country_mapping: dict) -> list[dict]:
    results = []
    seen = set()

    slugs = re.findall(r'/esim-([a-z0-9\-]+)/', html)

    manual_overrides = {
        "turkiye": "turkey",
        "united-kingdom": "uk",
        "united-states": "usa",
        "vietnam": "vietnam",
    }

    skip_slugs = {
        "all-destinations",
        "how-it-works",
        "about-us",
        "blog",
        "help",
    }

    for slug in set(slugs):
        if slug in skip_slugs:
            continue

        target_name = manual_overrides.get(slug, slug)
        country_info = country_mapping.get(target_name)
        if not country_info:
            continue

        full_url = f"https://saily.com/esim-{slug}/"
        if full_url in seen:
            continue

        seen.add(full_url)
        results.append({
            "url": full_url,
            "provider_country": slug.replace("-", " ").title(),
            "country": country_info["country"],
            "iso2": country_info["iso2"],
            "iso3": country_info["iso3"],
            "slug": slug,
        })

    return sorted(results, key=lambda x: x["country"])


def normalize_currency(value) -> str:
    v = str(value or "").strip().upper()
    if not v:
        return "USD"
    if v in {"$", "US$", "USD"}:
        return "USD"
    if v in {"EUR", "€"}:
        return "EUR"
    return v


def classify_plan_and_gb(data_amount_raw: str):
    value = str(data_amount_raw).strip()

    if re.search(r"unlimited\s*gb", value, re.I):
        return "Unlimited", "Unlimited"

    m = re.search(r"(\d+(?:\.\d+)?)\s*GB", value, re.I)
    if m:
        num = float(m.group(1))
        if num.is_integer():
            num = int(num)
        return "Standard", num

    return "Standard", ""


def parse_days_label(text: str):
    m = re.search(r'(\d+)\s*days?', str(text), re.I)
    if m:
        return int(m.group(1))
    return None


def build_row(
    country_info: dict,
    plan_type: str,
    gb_value,
    days_val,
    price_val: float,
    currency: str,
    variant_id: str,
    name: str,
    source: str,
) -> dict:
    currency = normalize_currency(currency)

    return {
        "Provider": "saily",
        "ProviderCountry": country_info["provider_country"],
        "ISO": country_info["iso2"],
        "Country": country_info["country"],
        "URL": country_info["url"],
        "Plan": plan_type,
        "GB": gb_value,
        "Days": days_val,
        "Price": price_val,
        "Currency": currency,
        "SpecialOffer": "",
        "OfferPonder": "",
        "PriceDate": date.today().isoformat(),
        "ISO3": country_info["iso3"],
        "variant_id": variant_id,
        "name": name,
        "eur_price": price_val if currency == "EUR" else None,
        "usd_price": price_val if currency == "USD" else None,
        "source": source,
    }


def extract_rows_from_offer_schema(html: str, soup: BeautifulSoup, country_info: dict) -> list[dict]:
    rows = []
    seen = set()

    option_map = {}
    for opt in soup.select("select option"):
        variant_id = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        days_val = parse_days_label(label)
        if variant_id and days_val is not None:
            option_map[variant_id] = {
                "days": days_val,
                "label": label,
            }

    if not option_map:
        return rows

    offer_pattern = re.compile(
        r'"@type":"Offer".{0,2000}?"priceCurrency":"(?P<currency>[A-Z]{3})".{0,500}?'
        r'"price":"(?P<price>\d+(?:\.\d+)?)".{0,500}?"sku":"(?P<sku>[a-f0-9\-]{36})"',
        re.S,
    )

    for m in offer_pattern.finditer(html):
        sku = m.group("sku")
        if sku not in option_map:
            continue

        days_val = option_map[sku]["days"]
        price_val = float(m.group("price"))
        currency = normalize_currency(m.group("currency"))

        # selector-based variants in this path are the unlimited pack
        plan_type = "Unlimited"
        gb_value = "Unlimited"

        dedupe_key = (plan_type, str(gb_value), str(days_val), f"{price_val:.2f}", currency)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        rows.append(build_row(
            country_info=country_info,
            plan_type=plan_type,
            gb_value=gb_value,
            days_val=days_val,
            price_val=price_val,
            currency=currency,
            variant_id=sku,
            name=f"Select Unlimited - {days_val} days for {price_val:.2f} {currency}.",
            source="offer_schema",
        ))

    return rows


def extract_rows_from_json_payload(main_content_html: str, country_info: dict) -> list[dict]:
    rows = []
    package_data = re.findall(r'\{[^{}]*"amount":[^{}]*"price":[^{}]*\}', main_content_html)
    seen = set()

    for item in package_data:
        try:
            plan_json = json.loads(item)

            def clean_val(val, default=None):
                if isinstance(val, list):
                    return val[0] if val else default
                return val if val is not None else default

            amount = clean_val(plan_json.get("amount"))
            unit = clean_val(plan_json.get("unit"), "GB")
            validity = clean_val(plan_json.get("validityDays"))
            price = clean_val(plan_json.get("price"))
            currency = clean_val(plan_json.get("currency"), "USD")

            if amount is None or price is None:
                continue

            data_amount = f"{amount} {unit}".strip()
            plan_type, gb_value = classify_plan_and_gb(data_amount)
            price_val = float(price)
            days_val = int(validity) if str(validity).isdigit() else validity
            currency = normalize_currency(currency)

            variant_id = f"saily-{country_info['iso3']}-{plan_type}-{gb_value}-{days_val}-{price_val:.2f}"
            dedupe_key = (plan_type, str(gb_value), str(days_val), f"{price_val:.2f}", currency)

            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            rows.append(build_row(
                country_info=country_info,
                plan_type=plan_type,
                gb_value=gb_value,
                days_val=days_val,
                price_val=price_val,
                currency=currency,
                variant_id=variant_id,
                name=f"Select {data_amount} - {days_val} days for {price_val:.2f} {currency}.",
                source="json_payload",
            ))
        except Exception:
            continue

    return rows


def extract_rows_from_visible_text(content_text: str, country_info: dict) -> list[dict]:
    rows = []
    lines = [x.strip() for x in content_text.splitlines() if x.strip()]
    seen = set()

    i = 0
    while i < len(lines):
        line = lines[i]

        if re.fullmatch(r"(\d+(?:\.\d+)?)\s*GB", line, re.I) or re.fullmatch(r"Unlimited\s*GB", line, re.I):
            data_amount = line
            days_val = None
            price_val = None
            currency = "USD"

            for j in range(i + 1, min(i + 12, len(lines))):
                if days_val is None:
                    m_days = re.fullmatch(r"(\d+)\s+days?", lines[j], re.I)
                    if m_days:
                        days_val = int(m_days.group(1))

                if price_val is None:
                    m_price = re.search(r"(?:US\$|\$|€)\s*(\d+(?:\.\d{2})?)", lines[j], re.I)
                    if m_price:
                        price_val = float(m_price.group(1))
                        currency = "EUR" if "€" in lines[j] else "USD"

                if days_val is not None and price_val is not None:
                    break

            if price_val is not None and days_val is not None:
                plan_type, gb_value = classify_plan_and_gb(data_amount)

                # Do not trust Unlimited variants from visible text
                if plan_type != "Unlimited":
                    variant_id = f"saily-{country_info['iso3']}-{plan_type}-{gb_value}-{days_val}-{price_val:.2f}"
                    dedupe_key = (plan_type, str(gb_value), str(days_val), f"{price_val:.2f}", currency)

                    if dedupe_key not in seen:
                        seen.add(dedupe_key)

                        rows.append(build_row(
                            country_info=country_info,
                            plan_type=plan_type,
                            gb_value=gb_value,
                            days_val=days_val,
                            price_val=price_val,
                            currency=currency,
                            variant_id=variant_id,
                            name=f"Select {data_amount} - {days_val} days for {price_val:.2f} {currency}.",
                            source="visible_text",
                        ))

        i += 1

    return rows


def dedupe_rows(rows: list[dict]) -> list[dict]:
    dedup = {}
    source_rank = {
        "offer_schema": 3,
        "json_payload": 2,
        "visible_text": 1,
        "": 0,
    }

    for row in rows:
        key = (
            row["ProviderCountry"],
            row["URL"],
            row["Plan"],
            str(row["GB"]),
            str(row["Days"]),
            f'{float(row["Price"]):.2f}',
            row["Currency"],
        )

        old = dedup.get(key)
        if old is None:
            dedup[key] = row
            continue

        if source_rank.get(row.get("source", ""), 0) > source_rank.get(old.get("source", ""), 0):
            dedup[key] = row

    def sort_key(x: dict):
        plan_order = 0 if x["Plan"] == "Standard" else 1
        gb_raw = x["GB"]
        try:
            gb_order = float(gb_raw)
        except Exception:
            gb_order = float("inf")
        try:
            days_order = int(x["Days"])
        except Exception:
            days_order = 999999

        return (plan_order, days_order, gb_order, float(x["Price"]))

    return sorted(dedup.values(), key=sort_key)


def scrape_country_page(html: str, country_info: dict) -> list[dict]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        main_content = soup.find("main") or soup.find("div", id="__next") or soup
        main_content_html = str(main_content)
        content_text = main_content.get_text("\n", strip=True)

        rows = []
        try:
            rows.extend(extract_rows_from_offer_schema(html, soup, country_info))
        except Exception as e:
            print(f"      [!] Offer schema extraction failed: {e}")
        rows.extend(extract_rows_from_json_payload(main_content_html, country_info))
        rows.extend(extract_rows_from_visible_text(content_text, country_info))

        return dedupe_rows(rows)

    except Exception as e:
        print(f"      [!] Error parsing {country_info['iso3']}: {e}")
        return []


def get_country_rows(country_info: dict) -> list[dict]:
    html = fetch_html(country_info["url"])
    if not html:
        return []
    return scrape_country_page(html, country_info)


def write_rows_to_csv(rows: list[dict], filename: str) -> None:
    ensure_parent_dir(filename)

    clean_rows = []
    for row in rows:
        clean_row = {k: row.get(k, "") for k in CSV_FIELDNAMES}
        clean_rows.append(clean_row)

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDNAMES,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(clean_rows)


def rotate_and_write_csv(rows: list[dict], current_file: str, previous_file: str) -> None:
    ensure_parent_dir(current_file)
    ensure_parent_dir(previous_file)

    tmp_file = current_file + ".tmp"
    write_rows_to_csv(rows, tmp_file)

    if os.path.exists(current_file):
        os.replace(current_file, previous_file)

    os.replace(tmp_file, current_file)


def main():
    try:
        country_mapping = load_country_mapping(WHITELIST_XLSX)
        print(f"Loaded {len(country_mapping)} country mappings from {WHITELIST_XLSX}")
    except Exception as e:
        print(f"[!] Excel Error: {e}")
        return

    main_html = fetch_html(COUNTRIES_URL)
    if not main_html:
        print("[!] Could not fetch Saily destinations page; trying sitemap fallback")
        main_html = fetch_html(SITEMAP_URL)
    if not main_html:
        raise RuntimeError("Could not fetch Saily destinations page or sitemap")

    countries = get_country_links(main_html, country_mapping)
    print(f"Matched {len(countries)} countries from Saily")

# DEBUG AUSTRIA-ONLY. CAN BE REMOVED LATER
    if AUSTRIA_ONLY:
        countries = [c for c in countries if c["slug"] == "austria"]
        print(f"AUSTRIA_ONLY enabled: {len(countries)} country selected")
# END DEBUG

    all_current_rows = []
    scraped_country_count = 0
    failed_countries = 0

    for i, country_info in enumerate(countries, start=1):
        print(
            f"\n[{i}/{len(countries)}] Scraping: "
            f"{country_info['slug']} | {country_info['provider_country']} | {country_info['iso3']}"
        )

        try:
            rows = get_country_rows(country_info)
            if not rows:
                print("      [-] No plans found or empty HTML returned.")
                failed_countries += 1
                continue

            scraped_country_count += 1
            all_current_rows.extend(rows)

            for row in rows:
                print(
                    f"{row['ProviderCountry']:20} | "
                    f"{row['ISO3'] or '---'} | "
                    f"{row['ISO'] or '--'} | "
                    f"{row['Country'] or row['ProviderCountry']} | "
                    f"{str(row['Plan']):10} | "
                    f"{str(row['GB']):>9} | "
                    f"{str(row['Days']):>2} days | "
                    f"{row['Currency']} {row['Price']} | "
                    f"{row.get('source', '')}"
                )

            polite_sleep()

        except Exception as e:
            failed_countries += 1
            print(f"Error scraping {country_info['slug']} ({country_info['provider_country']}): {e}")
            continue

    if not all_current_rows:
        raise RuntimeError("No variants found for any Saily country")

    rotate_and_write_csv(all_current_rows, OUTPUT_CURRENT_CSV, OUTPUT_PREVIOUS_CSV)

    print(f"\nCurrent scrape rows: {len(all_current_rows)}")
    print(f"Current CSV file: {OUTPUT_CURRENT_CSV}")
    print(f"Previous CSV file: {OUTPUT_PREVIOUS_CSV}")
    print(
        f"Summary: scraped_countries={scraped_country_count}, "
        f"failed_countries={failed_countries}"
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        close_browser_fallback()
