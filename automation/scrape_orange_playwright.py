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

try:
    from country_identity import allowed_iso3 as ppg_allowed_iso3, resolve_country
except ImportError:
    from automation.country_identity import allowed_iso3 as ppg_allowed_iso3, resolve_country
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright #, TimeoutError as PWTimeoutError

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)


OUTPUT_CURRENT_CSV = "scrapes/orange_current.csv"
OUTPUT_PREVIOUS_CSV = "scrapes/orange_previous.csv"
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


def load_allowed_iso3(path: str) -> set[str]:
    """Load the PPG ISO3 whitelist through the shared country identity helper."""
    return {str(code).strip().upper() for code in ppg_allowed_iso3(path) if code}


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
    # Accept both singular and plural forms, e.g. "1 Day" and "7 Days".
    m = re.search(r"\b(\d+)\s*Days?\b", text, flags=re.I)
    return int(m.group(1)) if m else None


def iso2_to_iso3(iso2: str | None) -> str | None:
    identity = resolve_country(iso2, WHITELIST_XLSX)
    return identity["iso3"] if identity else None


def iso2_to_country_name(iso2: str | None) -> str | None:
    identity = resolve_country(iso2, WHITELIST_XLSX)
    return identity["country"] if identity else None


def country_name_to_iso2(name: str | None) -> str | None:
    identity = resolve_country(name, WHITELIST_XLSX)
    return identity["iso2"] if identity else None


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
    """Extract visible offers and associate each with its nearest duration.

    The lookup is restricted to the nearest visible offer section/container,
    preventing unrelated page text from being interpreted as a duration.
    """
    wait_for_visible_offers_stable(page)
    cards = page.locator("esim-offer-card:visible")
    visible_count = cards.count()
    rows: list[dict] = []
    skipped = 0

    for i in range(visible_count):
        card = cards.nth(i)
        raw = clean(card.inner_text())
        card_days = extract_days_from_text(raw)

        preceding_days = card.evaluate(
            r"""
            (card) => {
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                           style.visibility !== 'hidden' &&
                           style.opacity !== '0' &&
                           rect.width > 0 && rect.height > 0;
                };

                const durationRe = /^(\d+)\s*Days?$/i;

                // Prefer a nearby semantic/container ancestor. Stop before
                // broad layout roots so the scan cannot drift into header/footer.
                let container = card.parentElement;
                while (container && container !== document.body) {
                    const visibleCards = container.querySelectorAll('esim-offer-card');
                    if (visibleCards.length > 1) break;
                    container = container.parentElement;
                }
                if (!container || container === document.body) {
                    container = card.closest('main, section, article, form') || card.parentElement;
                }
                if (!container) return null;

                const walker = document.createTreeWalker(
                    container,
                    NodeFilter.SHOW_ELEMENT,
                    {
                        acceptNode: (node) => {
                            if (!isVisible(node)) return NodeFilter.FILTER_REJECT;
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );

                let lastDays = null;
                let node = walker.currentNode;
                while (node) {
                    if (node === card) break;

                    // Only leaf-like text nodes are considered. This avoids a
                    // parent div whose innerText contains several headings/cards.
                    const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                    const hasVisibleElementChild = Array.from(node.children || []).some(isVisible);
                    if (!hasVisibleElementChild) {
                        const match = text.match(durationRe);
                        if (match) lastDays = Number(match[1]);
                    }
                    node = walker.nextNode();
                }
                return lastDays;
            }
            """
        )

        final_days = card_days if card_days is not None else preceding_days
        gb = extract_gb(card)
        price, currency = extract_price(card)
        offer = extract_special_offer(card)

        if not gb or price is None or final_days is None:
            skipped += 1
            print(
                f"[extract] skipped visible card: plan={plan_name!r}, "
                f"gb={gb!r}, price={price!r}, days={final_days!r}, raw={raw!r}"
            )
            continue

        rows.append({
            "plan": plan_name + "-" + str(gb),
            "gb": gb,
            "days": int(final_days),
            "price": price,
            "currency": currency or detect_currency_from_page(page) or CURRENCY,
            "special_offer": offer,
            "offer_ponder": extract_offer_ponder(offer),
            "raw": raw,
        })

    if skipped:
        print(
            f"[validation] {plan_name}: extracted {len(rows)} of "
            f"{visible_count} visible cards; {skipped} card(s) were incomplete"
        )
    elif visible_count:
        print(f"[validation] {plan_name}: extracted all {visible_count} visible cards")
    else:
        print(f"[validation] {plan_name}: no visible offer cards found")

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
        plan_order = 0 if x["plan"].startswith("Data+Calls+SMS") else 1
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
