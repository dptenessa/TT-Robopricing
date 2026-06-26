#!/usr/bin/env python3

from __future__ import annotations

import csv
import os
import random
import re
import time
from datetime import date
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import pandas as pd

import pycountry
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright #, TimeoutError as PWTimeoutError

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)


OUTPUT_CURRENT_CSV = "outputs/orange_current.csv"
OUTPUT_PREVIOUS_CSV = "outputs/orange_previous.csv"
WHITELIST_XLSX = "inputs/WS_PPG.csv"

TEST_MODE = False
TEST_LIMIT = 10
HEADED = False
CURRENCY = "USD"
MAX_PER_MINUTE = 20

BASE_URL = "https://travel.orange.com"
SITEMAP_URL = "https://travel.orange.com/en/site-map"


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


def polite_sleep(min_s: float = 0.1, max_s: float = 0.3) -> None:
    time.sleep(random.uniform(min_s, max_s))


def clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def parse_money(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)", text.replace("\xa0", " "))
    return float(m.group(1).replace(",", ".")) if m else None


def extract_days_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)\s*Days", text, flags=re.I)
    return int(m.group(1)) if m else None


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
    "Ivory Coast": "CI",
    "Cape Verde": "CV",
    "Faeroe Islands": "FO",
    "Faroe Islands": "FO",
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
    "Turkey": "TR",
    "Türkiye": "TR",
    "Democratic Republic of Congo": "CD",
    "Congo": "CG",
    "Swaziland or Eswatini": "SZ",
    "Eswatini": "SZ",
    "Vatican": "VA",
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


def load_allowed_iso3(filepath: str) -> set[str]:
    COLUMN_NAME = "ISO_Code_A3"

    df = pd.read_csv(filepath, encoding="utf-8-sig")

    headers = list(df.columns)

    if COLUMN_NAME not in headers:
        raise RuntimeError(
            f"Column '{COLUMN_NAME}' not found in CSV. Headers: {headers}"
        )

    allowed: set[str] = set()

    for value in df[COLUMN_NAME]:
        if pd.notna(value):
            allowed.add(str(value).strip().upper())

    return allowed


def prepare_currency_once(page, countries: list[dict[str, str]]) -> None:
    seed_url = None

    for c in countries:
        if c["slug"] not in {"europe", "world"}:
            seed_url = c["url"]
            break

    if not seed_url:
        print("[currency] no seed country found for initial currency switch")
        return

    print(f"[currency] preparing once using {seed_url}")
    goto_with_retry(page, seed_url)

    accept_cookies(page)
    wait_for_cookie_overlay_to_clear(page)

    current = detect_currency_from_page(page)
    print(f"[currency] initial detected currency: {current}")

    if current != CURRENCY:
        ok = switch_currency(page, CURRENCY)
        print(f"[currency] switch to {CURRENCY}: {'ok' if ok else 'failed'}")
    else:
        print(f"[currency] already in {CURRENCY}")

    clear_ui_overlays(page)
    human_wait(page, 800, 1600)


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
            page.goto(url, wait_until="domcontentloaded")
            human_wait(page, 1200, 2400)
            return
        except Exception as e:
            last_err = e
            if attempt < tries - 1:
                time.sleep(random.uniform(2.0, 4.0))

    raise RuntimeError(f"Failed to load {url} after {tries} tries: {last_err}")


def accept_cookies(page) -> bool:
    selectors = [
        "#didomi-notice-agree-button",
        'button[id*="didomi"][id*="agree"]',
        'button:has-text("Agree")',
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("Allow all")',
    ]

    human_wait(page, 1000, 1800)

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
                        page.wait_for_timeout(1800)
                        return True
                except Exception:
                    continue
        except Exception:
            pass

    print("[cookies] no cookie button clicked")
    return False


def wait_for_cookie_overlay_to_clear(page) -> None:
    try:
        page.locator("#didomi-popup").wait_for(state="hidden", timeout=5000)
        print("[cookies] overlay hidden")
    except Exception:
        print("[cookies] overlay still present or not found")


def detect_currency_from_page(page) -> Optional[str]:
    try:
        text = page.locator("body").inner_text() or ""
        if " USD" in text or "$" in text:
            return "USD"
        if " EUR" in text or "€" in text:
            return "EUR"
    except Exception:
        pass
    return None


def close_currency_popup(page) -> None:
    print("[currency] attempting to close popup")

    try:
        page.keyboard.press("Escape")
        human_wait(page, 1000, 1800)
    except Exception:
        pass

    try:
        page.mouse.click(20, 20)
        human_wait(page, 500, 1000)
    except Exception:
        pass

    selectors = [
        'button[aria-label="Close"]',
        'button:has-text("Close")',
        '.modal button.btn-close',
        '.offcanvas button.btn-close',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000, force=True)
                human_wait(page, 1500, 2500)
                return
        except Exception:
            pass


def clear_ui_overlays(page) -> None:
    close_currency_popup(page)
    try:
        page.locator("#didomi-popup").wait_for(state="hidden", timeout=2000)
    except Exception:
        pass


def switch_currency(page, currency_code: str) -> bool:
    currency_code = currency_code.upper().strip()

    current = detect_currency_from_page(page)
    print(f"[main] detected currency: {current}")

    if current == currency_code:
        print(f"[main] already in {currency_code}, skipping switch")
        return True

    open_selectors = [
        "body > esim-root > ng-component > div > esim-header > header > nav > div > div.d-flex.flex-column.gap-2 > div:nth-child(2) > ul > li:nth-child(1) > esim-header-currency-modal > esim-header-modal > button",
        "esim-header-currency-modal button.btn-color",
        "esim-header-currency-modal button",
    ]

    opened = False
    for sel in open_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                print(f"[currency] open with: {sel}")
                loc.first.click(timeout=4000)
                human_wait(page, 1500, 2500)
                opened = True
                break
        except Exception as e:
            print(f"[currency] failed open {sel}: {e}")

    if not opened:
        current = detect_currency_from_page(page)
        if current == currency_code:
            print(f"[currency] could not open modal, but page is already {currency_code}")
            return True
        print("[currency] could not open currency modal")
        return False

    option_selectors = (
        ['text=/\\bUSD\\b/i', 'text=/\\$\\s*USD/i']
        if currency_code == "USD"
        else ['text=/\\bEUR\\b/i', 'text=/€\\s*EUR/i']
    )

    for sel in option_selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        print(f"[currency] selecting with {sel}")
                        el.click(timeout=4000, force=True)
                        human_wait(page, 1500, 2500)
                        current = detect_currency_from_page(page)
                        if current == currency_code:
                            print(f"[currency] switched to {currency_code}")
                            close_currency_popup(page)
                            return True
                except Exception:
                    continue
        except Exception as e:
            print(f"[currency] failed choose {sel}: {e}")

    current = detect_currency_from_page(page)
    if current == currency_code:
        print(f"[currency] page is already {currency_code}")
        close_currency_popup(page)
        return True

    print(f"[currency] could not switch to {currency_code}")
    close_currency_popup(page)
    return False


def print_filter_state(page) -> None:
    for idx in [0, 1]:
        try:
            checked = page.locator(f"input#esimFilter{idx}").first.is_checked()
            print(f"[filter-state] esimFilter{idx} checked={checked}")
        except Exception as e:
            print(f"[filter-state] esimFilter{idx} error: {e}")


def click_filter(page, idx: int) -> bool:
    print(f"\n[filter] activating esimFilter{idx}")

    label = page.locator(f'label[for="esimFilter{idx}"]')
    inp = page.locator(f'input#esimFilter{idx}')

    try:
        if label.count() > 0 and label.first.is_visible():
            label.first.click(timeout=4000, force=True)
            human_wait(page, 1500, 2500)
    except Exception as e:
        print(f"[filter] label click failed for {idx}: {e}")

    try:
        checked = inp.first.is_checked()
        print(f"[filter] esimFilter{idx} checked={checked}")
        return checked
    except Exception as e:
        print(f"[filter] state read failed for {idx}: {e}")
        return False


def extract_special_offer(card) -> str:
    try:
        badge = card.locator("p.bg-supporting-yellow")
        if badge.count() > 0:
            return clean(badge.first.inner_text())
    except Exception:
        pass
    return ""


def extract_offer_ponder(special_offer: str) -> str:
    if not special_offer:
        return ""
    m = re.search(r"(\d+)\s*%", special_offer)
    return m.group(1) if m else ""


def extract_gb(card) -> str:
    try:
        h4 = card.locator("h4")
        if h4.count() > 0:
            txt = clean(h4.first.inner_text())

            # Keep Unlimited as-is
            if re.search(r"^unlimited$", txt, flags=re.I):
                return "Unlimited"

            # Remove GB if present and return just the number
            m = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*GB\b", txt, flags=re.I)
            if m:
                return m.group(1).replace(",", ".")

            # Fallback: if it's already just a number, keep it
            m = re.search(r"^[0-9]+(?:[.,][0-9]+)?$", txt)
            if m:
                return txt.replace(",", ".")

            return txt
    except Exception:
        pass
    return ""


def extract_price(card) -> tuple[Optional[float], str]:
    try:
        tag = card.locator("esim-offer-price-tag")
        if tag.count() > 0:
            txt = clean(tag.first.inner_text())
            price = parse_money(txt)
            currency = ""
            if "USD" in txt.upper() or "$" in txt:
                currency = "USD"
            elif "EUR" in txt.upper() or "€" in txt:
                currency = "EUR"
            return price, currency
    except Exception:
        pass
    return None, ""


def variant_id_from_fields(plan: str, gb: str, days: int | str, special_offer: str) -> str:
    plan_slug = re.sub(r"[^a-z0-9]+", "-", plan.lower()).strip("-")
    gb_slug = re.sub(r"[^a-z0-9]+", "-", gb.lower()).strip("-")
    days_slug = f"{days}days" if str(days) else "nodays"
    offer_slug = re.sub(r"[^a-z0-9]+", "-", special_offer.lower()).strip("-") if special_offer else "nooffer"
    return f"{plan_slug}-{gb_slug}-{days_slug}-{offer_slug}"

def normalize_provider_country(text: str, slug: str = "") -> str:
    txt = clean(text)

    replacements = [
        (r"^eSIM\s+for\s+", ""),
        (r"^Orange\s+Travel\s+eSIM\s+for\s+", ""),
    ]

    for pattern, repl in replacements:
        txt = re.sub(pattern, repl, txt, flags=re.I).strip()

    manual_text_map = {
        "Global eSIM": "World",
        "eSIM for Réunion": "Réunion",
        "eSIM for Türkiye": "Turkey",
        "eSIM for Åland Islands": "Åland Islands",
    }

    if text in manual_text_map:
        return manual_text_map[text]

    if txt:
        return txt

    if slug:
        return slug.replace("-", " ").strip().title()

    return txt

def slug_to_country_name(slug: str) -> str:
    manual_slug_map = {
        "united-states": "United States",
        "united-kingdom": "United Kingdom",
        "south-korea": "South Korea",
        "north-macedonia": "North Macedonia",
        "czech-republic": "Czech Republic",
        "ivory-coast": "Ivory Coast",
        "cape-verde": "Cape Verde",
        "faeroe-islands": "Faeroe Islands",
        "french-guiana": "French Guiana",
        "french-polynesia": "French Polynesia",
        "new-caledonia": "New Caledonia",
        "isle-of-man": "Isle of Man",
        "aland-islands": "Åland Islands",
        "democratic-republic-of-congo": "Democratic Republic of Congo",
        "swaziland-or-eswatini": "Swaziland or Eswatini",
        "turkiye": "Türkiye",
        "saint-barthelemy": "Saint Barthelemy",
        "saint-lucia": "Saint Lucia",
        "saint-martin": "Saint Martin",
        "saint-pierre-and-miquelon": "Saint Pierre and Miquelon",
        "trinidad-and-tobago": "Trinidad and Tobago",
        "united-arab-emirates": "United Arab Emirates",
        "papua-new-guinea": "Papua New Guinea",
        "bosnia-and-herzegovina": "Bosnia and Herzegovina",
        "central-african-republic": "Central African Republic",
        "dominican-republic": "Dominican Republic",
        "el-salvador": "El Salvador",
        "hong-kong": "Hong Kong",
    }
    return manual_slug_map.get(slug, slug.replace("-", " ").strip().title())


def get_country_links(page) -> list[dict[str, str]]:
    goto_with_retry(page, SITEMAP_URL)
    accept_cookies(page)
    wait_for_cookie_overlay_to_clear(page)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        raw_href = (a.get("href") or "").strip()
        if not raw_href:
            continue

        full_url = urljoin(BASE_URL, raw_href)
        parsed = urlparse(full_url)

        path = parsed.path.rstrip("/")
        parts = [p for p in path.split("/") if p]

        # Expect exactly: /en/buy-a-sim/offers/<slug>
        if len(parts) != 4:
            continue
        if parts[0] != "en" or parts[1] != "buy-a-sim" or parts[2] != "offers":
            continue

        slug = parts[3].strip()
        if not slug:
            continue

        if full_url in seen:
            continue
        seen.add(full_url)

        raw_txt = clean(a.get_text(" ", strip=True))
        raw_txt = re.sub(r"^eSIM\s+for\s+", "", raw_txt, flags=re.I).strip()
        raw_txt = re.sub(r"^Orange\s+Travel\s+eSIM\s+for\s+", "", raw_txt, flags=re.I).strip()

        provider_country = raw_txt or slug_to_country_name(slug)

        results.append({
            "slug": slug,
            "url": full_url,
            "provider_country": provider_country,
        })

    results = sorted(results, key=lambda x: x["provider_country"].lower())
    print(f"[links] found {len(results)} country links from sitemap")
    return results


def extract_by_dom_order(page, plan_name: str) -> list[dict]:
    items = page.locator("esim-offer-card, .mt-1.px-2.mb-0.fw-bold")
    count = items.count()

    rows: list[dict] = []
    current_days: Optional[int] = None

    for i in range(count):
        item = items.nth(i)

        try:
            tag = item.evaluate("(el) => el.tagName.toLowerCase()")
        except Exception:
            tag = ""

        raw = clean(item.inner_text())

        if tag != "esim-offer-card":
            days = extract_days_from_text(raw)
            if days is not None:
                current_days = days
            continue

        gb = extract_gb(item)
        price, currency = extract_price(item)
        offer = extract_special_offer(item)

        card_days = extract_days_from_text(raw)
        final_days = card_days if card_days is not None else current_days

        if not gb or price is None or final_days is None:
            continue

        rows.append({
            "plan": plan_name + "-" + str(gb),
            "gb": gb,
            "days": final_days,
            "price": price,
            "currency": currency or detect_currency_from_page(page) or CURRENCY,
            "special_offer": offer,
            "offer_ponder": extract_offer_ponder(offer),
            "raw": raw,
        })

    return rows


def dedupe_rows(rows: list[dict]) -> list[dict]:
    dedup: dict[tuple, dict] = {}

    for row in rows:
        key = (
            row["plan"],
            row["gb"],
            row["days"],
            row["currency"],
            f'{row["price"]:.2f}',
            row["special_offer"],
        )
        dedup[key] = row

    def gb_sort_value(gb: str) -> float:
        txt = str(gb).strip().upper()
        if txt == "UNLIMITED":
            return -1.0
        txt = txt.replace("GB", "").strip()
        try:
            return float(txt)
        except Exception:
            return 999999.0

    def sort_key(x: dict):
        plan_order = 0 if x["plan"] == "Data+Calls+SMS" else 1
        return (plan_order, int(x["days"]), gb_sort_value(x["gb"]))

    return sorted(dedup.values(), key=sort_key)


def scrape_country(page, url: str, provider_country: str) -> list[dict]:
    goto_with_retry(page, url)

    accept_cookies(page)
    wait_for_cookie_overlay_to_clear(page)

    current = detect_currency_from_page(page)
    print(f"[main] detected currency: {current}")

    clear_ui_overlays(page)
    human_wait(page, 800, 1600)

    rows: list[dict] = []

    filter0_exists = False
    filter1_exists = False

    try:
        filter0_exists = page.locator("input#esimFilter0").count() > 0
    except Exception:
        pass

    try:
        filter1_exists = page.locator("input#esimFilter1").count() > 0
    except Exception:
        pass

    # Normal path: page has switches
    if filter0_exists:
        if click_filter(page, 0):
            print_filter_state(page)
            rows.extend(extract_by_dom_order(page, "Data+Calls+SMS"))

    if filter1_exists:
        if click_filter(page, 1):
            print_filter_state(page)
            rows.extend(extract_by_dom_order(page, "Data"))

    # Fallback: no switches rendered, scrape whatever is already visible
    if not rows and not filter0_exists and not filter1_exists:
        print(f"[fallback] no esimFilter switches found for {provider_country}, scraping visible offers")
        rows.extend(extract_by_dom_order(page, "Data+Calls+SMS"))

    return dedupe_rows(rows)


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
        )
        page = context.new_page()
        setup_page(page)

        countries = get_country_links(page)
        countries = sorted(countries, key=lambda x: x["provider_country"].lower())
        prepare_currency_once(page, countries)

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
                    print(f"[rate-limit] sleeping {sleep_time:.1f}s to stay under {MAX_PER_MINUTE}/min")
                    time.sleep(sleep_time)
                window_start = time.time()
                requests_done_in_window = 0

            slug = country_info["slug"]
            url = country_info["url"]
            provider_country = country_info["provider_country"]

            if slug in {"europe", "world"}:
                print(f"Skipping {slug} ({provider_country}) - regional/global offer")
                continue

            iso2 = country_name_to_iso2(provider_country)
            iso3 = iso2_to_iso3(iso2)

            if not iso3:
                skipped_no_iso += 1
                print(f"Skipping {slug} ({provider_country}) - could not map country to ISO3")
                continue

            if iso3 not in allowed_iso3:
                skipped_not_whitelisted += 1
                print(f"Skipping {slug} ({provider_country}) - ISO3 {iso3} not in whitelist")
                continue

            print(f"\n[{i}/{len(countries)}] Scraping: {slug} | {provider_country} | {iso3}")

            try:
                rows = scrape_country(page, url, provider_country)
                requests_done_in_window += 1

                if not rows:
                    print(f"No variants found for {slug}")
                    polite_sleep()
                    continue

                allowed_country_count += 1
                country_name = iso2_to_country_name(iso2)

                for row in rows:
                    currency = row.get("currency") or CURRENCY
                    price = row.get("price")

                    output_row = {
                        "Provider": "orange",
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
                        "variant_id": variant_id_from_fields(
                            str(row.get("plan", "")),
                            str(row.get("gb", "")),
                            row.get("days", ""),
                            str(row.get("special_offer", "")),
                        ),
                        "name": row.get("raw", ""),
                        "eur_price": price if currency == "EUR" else None,
                        "usd_price": price if currency == "USD" else None,
                    }

                    all_current_rows.append(output_row)

                    print(
                        f"{output_row['ProviderCountry']:20} | "
                        f"{str(output_row['Plan']):15} | "
                        f"{str(output_row['GB']):>10} | "
                        f"{str(output_row['Days']):>3} days | "
                        f"{output_row['Currency']} {output_row['Price']}"
                    )

                if TEST_MODE and allowed_country_count >= TEST_LIMIT:
                    print(f"\nTest mode reached limit of {TEST_LIMIT} allowed country/countries.")
                    break

                polite_sleep()

            except Exception as e:
                failed_countries += 1
                requests_done_in_window += 1
                print(f"Error scraping {slug} ({provider_country}): {e}")
                print("[error] cooling down before next country")
                time.sleep(random.uniform(5.0, 10.0))
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