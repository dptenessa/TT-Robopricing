import pdfplumber
import csv
import os
import re
import pycountry
from datetime import date
# from openpyxl import load_workbook
import pandas as pd
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
INPUT_PDF = "inputs/Charges.pdf"
OUTPUT_CSV = "outputs/vodafone_current.csv"
PDF_URL = "https://travel.vodafone.com/document/charges-guide"
WHITELIST_XLSX = "inputs/WS_PPG.csv"

FORCE_TEST = False

def get_iso_details(name):
    # Remove numbers/dots and normalize whitespace to fix issues like "15773.5 BURUNDI"
    clean_name = " ".join(re.sub(r'[\d\.]+', '', name).split()).upper()

    mapping = {
        "AFGANISTAN": "AF",
        "BOSNIA AND HERZEGOWINA": "BA",
        "BR VIRGIN ISLANDS": "VG",
        "TURKIYE": "TR",
        "TURKEY": "TR",
        "SIERRA LEON": "SL",
        "KOREA REPUBLIC OF": "KR",
        "CONGO THE DEMOCRATIC REPUBLIC": "CD",
        "TANZANIA UNITED REPUBLIC OF": "TZ",
        "USA": "US",
        "UK": "GB",
        "BURUNDI": "BI",
        "CAPE VERDE": "CV",
        "COTE D IVOIRE": "CI",
        "CUBA": "CU",
        "IRAN ISLAMIC REPUBLIC OF": "IR",
        "LAO PEOPLES DEMOCRATIC REPUBLIC": "LA",
        "LIBYAN ARAB JAMAHIRIYA": "LY",
        "MACAU": "MO",
        "MOLDOVA REPUBLIC OF": "MD",
        "NETHERLANDS ANTILLES": "BQ",
        "MACEDONIA THE FORMER YUGOSLAV REPUBLIC OF": "MK",
        "PALESTINIAN TERRITOR": "PS",
        "REPUBLIC OF CONGO": "CG",
        "SAO TOME AND PRINCIPE": "ST",
        "SLOVAKIA SLOVAK REPUBLIC": "SK"
    }
    
    iso2 = mapping.get(clean_name)
    if not iso2:
        try:
            results = pycountry.countries.search_fuzzy(clean_name.title())
            if results: iso2 = results[0].alpha_2
        except: pass

    if iso2:
        country_obj = pycountry.countries.get(alpha_2=iso2)
        return iso2, country_obj.alpha_3, country_obj.name
    return None, None, name.title()

import os
import pandas as pd

def load_allowed_iso3(filepath):
    """Loads allowed ISO3 codes from the whitelist CSV file."""
    
    if not os.path.exists(filepath):
        print(f"Warning: Whitelist file '{filepath}' not found. Returning empty whitelist.")
        return set()

    df = pd.read_csv(filepath)
    allowed = set()

    # Assumes second column contains the ISO3 codes
    for value in df.iloc[:, 1]:
        if pd.notna(value):
            allowed.add(str(value).strip().upper())

    return allowed

def get_latest_csv_date(filepath):
    """Reads the last PriceDate from the existing CSV file."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return None
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            last_row = None
            for last_row in reader:
                pass
            return last_row.get("PriceDate") if last_row else None
    except Exception as e:
        print(f"Warning: Could not read existing CSV for date comparison: {e}")
        return None

def extract_vodafone_data(pdf_path, allowed_iso3=None):
    all_rows = []
    current_country = "Unknown"
    country_buffer = []
    price_date_str = date.today().isoformat()
    
    # Pre-check for PDF signature to avoid crashing on HTML/empty files
    try:
        with open(pdf_path, 'rb') as f:
            if not f.read(5).startswith(b'%PDF-'):
                print(f"Error: '{pdf_path}' is not a valid PDF file (invalid header). It might be an HTML error page.")
                return []
    except Exception as e:
        print(f"Error reading file '{pdf_path}': {e}")
        return []

    print(f"Opening {pdf_path}...")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Extract Modified Date from PDF metadata
            mod_date = pdf.metadata.get('ModDate') or pdf.metadata.get('CreationDate')
            if mod_date and isinstance(mod_date, str):
                # Standard PDF date format is usually D:YYYYMMDDHHmmSS...
                date_match = re.search(r'(\d{4})(\d{2})(\d{2})', mod_date)
                if date_match:
                    price_date_str = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

            # Loop strictly from Page 10 (Index 9) to Page 27 (Index 26)
            # This prevents even looking at Page 29
            for i in range(9, len(pdf.pages)):
                    
                page = pdf.pages[i]
                text = page.extract_text()
                if not text: continue
                stop_after_this_page = False

                topup_match = re.search(
                    r"Regional\s+Top\s*up",
                    text,
                    re.IGNORECASE
                )

                if topup_match:
                    print(f"DEBUG: Top Up section starts on Page {i+1}. Truncating page and stopping after valid rows.")
                    text = text[:topup_match.start()]
                    stop_after_this_page = True

                lines = text.split('\n')
                for line in lines:

                    parts = line.strip().split()
                    
                    # Stop scraping if "Top up" is found in the first cell/start of the row
                    if line.strip().upper().startswith("TOP UP"):
                        print(f"DEBUG: Found 'Top up' trigger on Page {i+1}. Stopping extraction.")
                        return all_rows

                    # Skip noise/headers
                    if not parts or any(x in line.upper() for x in ["PAGE", "C2 GENERAL", "EURO", "POUND", "DOLLAR", "BUNDLE", "SIZE"]):
                        continue

                    # Check for data (digit or "UNL" for GB size)
                    has_data_trigger = any(p.isdigit() or p.upper() == "UNL" for p in parts)

                    if not has_data_trigger:
                        # Buffer multi-word names (like Bosnia and...)
                        if len(parts) < 5:
                            country_buffer.append(" ".join(parts))
                        continue
                    
                    # Process data row
                    data_idx = -1
                    name_on_this_line = []
                    for idx, word in enumerate(parts):
                        if word.isdigit() or word.upper() == "UNL":
                            data_idx = idx
                            break
                        name_on_this_line.append(word)
                    
                    # Combine buffer with current line name
                    if name_on_this_line or country_buffer:
                        full_name = " ".join(country_buffer + name_on_this_line).strip()
                        if full_name and "Plans" not in full_name:
                            # Clean artifacts like "18560.0 60030.0" from the name
                            cleaned_name = " ".join(re.sub(r'[\d\.]+', '', full_name).split())
                            if cleaned_name:
                                current_country = cleaned_name
                        country_buffer = [] 
                    
                    if data_idx == -1: continue
                    data_row = parts[data_idx:]

                    # Extract prices (0:EUR, 1:GBP, 2:USD)
                    decimals = [p for p in data_row if "." in p]
                    
                    if len(data_row) >= 2 and len(decimals) >= 3:
                        gb = data_row[0]
                        days = data_row[1]
                        eur_val = decimals[0]
                        usd_val = decimals[2] 
                        
                        # Exclude regions that might get fuzzy-matched to countries (e.g. Africa -> South Africa)
                        if current_country.strip().upper() == "AFRICA":
                            continue

                        iso2, iso3, country_official = get_iso_details(current_country)

                        # Whitelist Check
                        if allowed_iso3:
                            if iso3 and iso3 not in allowed_iso3:
                                print(f"Skipping {current_country} ({iso3}) - not in whitelist")
                                continue
                            # Skip regions if we have a strict whitelist and no mapping
                            if not iso3:
                                print(f"Skipping {current_country} (Unknown ISO) - whitelist active")
                                continue

                        plan_type = "Unlimited" if gb.upper() in ["100", "UNL"] else "Standard"

                        all_rows.append({
                            "Provider": "vodafone",
                            "ProviderCountry": current_country,
                            "ISO": iso2,
                            "Country": country_official,
                            "URL":"",
                            "Plan": plan_type,
                            "GB": gb,
                            "Days": days,
                            "Price": usd_val,
                            "Currency": "USD",
                            "SpecialOffer": "N",
                            "OfferPonder": 1.0,
                            "PriceDate": price_date_str,
                            "ISO3": iso3,
                            "variant_id": "",
                            "name": f"{gb} GB {days} Days ({current_country})",
                            "eur_price": eur_val,
                            "usd_price": usd_val
                        })
                if stop_after_this_page:
                    print(f"DEBUG: Stopping extraction after Page {i+1}.")
                    return all_rows

    except Exception as e:
        print(f"Critical error parsing PDF: {e}")
        return []

    return all_rows

def download_pdf_with_playwright(url, output_path):
    print(f"Downloading PDF using Playwright from {url}...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                accept_downloads=True
            )
            page = context.new_page()
            
            # Handle potential file download event
            download_obj = None
            def on_download(download):
                nonlocal download_obj
                download_obj = download
            page.on("download", on_download)

            response = None
            try:
                response = page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000) # Wait for redirects or download start
            except Exception as e:
                print(f"Playwright navigation noticed: {e}")

            # Handle OneTrust Cookie Banner to unblock clicks
            try:
                accept_btn = page.locator("#onetrust-accept-btn-handler")
                if accept_btn.count() > 0 and accept_btn.is_visible():
                    print("Dismissing OneTrust cookie banner...")
                    accept_btn.click()
                    page.wait_for_timeout(2000)
            except Exception as e:
                print(f"Cookie banner handling issue: {e}")

            # If no download triggered automatically, look for a download button/link
            if not download_obj:
                print("No automatic download detected. Searching for download button...")
                try:
                    # 1. Look for direct .pdf links
                    pdf_links = page.locator("a[href$='.pdf']")
                    if pdf_links.count() > 0:
                        print("Found .pdf link, clicking...")
                        pdf_links.first.click(force=True)
                        page.wait_for_timeout(5000)
                    else:
                        # 2. Look for "Download" text or aria-labels
                        btn = page.locator("button, a").filter(has_text=re.compile(r"download", re.IGNORECASE)).first
                        if not btn.is_visible():
                            btn = page.locator("[title*='ownload' i], [aria-label*='ownload' i]").first
                        
                        if btn.is_visible():
                            print("Found potential download button, clicking...")
                            btn.click(force=True)
                            page.wait_for_timeout(5000)
                except Exception as e:
                    print(f"Error attempting to click download button: {e}")

            if download_obj:
                download_obj.save_as(output_path)
                print(f"Successfully saved PDF via Playwright download to {output_path}")
                browser.close()
                return True

            if response:
                content = response.body()
                if content.startswith(b"%PDF"):
                    with open(output_path, "wb") as f:
                        f.write(content)
                    print(f"Successfully saved PDF via Playwright to {output_path}")
                    browser.close()
                    return True
                else:
                    print(f"Playwright received non-PDF content. Preview: {content[:100]!r}")
            browser.close()
    except Exception as e:
        print(f"Playwright download failed: {e}")
    return False

if __name__ == "__main__":
    # Always attempt to download the latest PDF using Playwright
    download_success = download_pdf_with_playwright(PDF_URL, INPUT_PDF)
    if not download_success:
        print(f"Error: Failed to download the latest PDF from {PDF_URL}. Please check the URL or your network connection.")
        print(f"Attempting to use local file '{INPUT_PDF}' if it exists and is valid.")

    # Check if a valid PDF file exists after the download attempt
    if not os.path.exists(INPUT_PDF) or os.path.getsize(INPUT_PDF) == 0 or not open(INPUT_PDF, 'rb').read(5).startswith(b'%PDF-'):
        print(f"Critical Error: '{INPUT_PDF}' is missing, empty, or not a valid PDF after download attempt. Cannot proceed.")
        exit(1)

    allowed_iso3 = load_allowed_iso3(WHITELIST_XLSX)
    print(f"Loaded {len(allowed_iso3)} allowed ISO3 codes from {WHITELIST_XLSX}")

    # Check if PDF metadata date matches the latest date in the CSV
    pdf_date = date.today().isoformat()
    try:
        with pdfplumber.open(INPUT_PDF) as pdf:
            mod_date = pdf.metadata.get('ModDate') or pdf.metadata.get('CreationDate')
            if mod_date and isinstance(mod_date, str):
                date_match = re.search(r'(\d{4})(\d{2})(\d{2})', mod_date)
                if date_match:
                    pdf_date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
    except:
        pass

    latest_csv_date = get_latest_csv_date(OUTPUT_CSV)
    if latest_csv_date == pdf_date and not FORCE_TEST:
        print(f"INFO: PDF Date ({pdf_date}) matches existing CSV. No update needed.")
        exit(0)

    results = extract_vodafone_data(INPUT_PDF, allowed_iso3)

    #DEBUG
    print(f"Extracted rows: {len(results)}")

    if results:
        print("First row:")
        print(results[0])

        print("Last 10 rows:")
        for row in results[-10:]:
            print(row["ProviderCountry"], row["GB"], row["Days"], row["Price"])
    #DEBUG

    if results:
        os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        print(f"DONE: Saved {len(results)} rows with date {pdf_date}. Extraction stopped at Top Up section.")
    else:
        print("No data extracted from the PDF.")