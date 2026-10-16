import requests
import time
import csv
from pathlib import Path
import os
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE)

API_KEY = os.getenv("FREQUENT_DESTINATIONS_API_KEY")

# ============================================================
# CONFIGURATION
# ============================================================

URL = "https://aux-tables.amdocs-dbs.com/tables/806165ce-be35-4839-a57f-16264e3872ad"

# Small pause between requests when uploading everything.
DELAY_BETWEEN_REQUESTS = 0.15

# Default country used for the one-row test upload.
TEST_COUNTRY = "HR"


# ============================================================
# RECOMMENDED DESTINATIONS
# ============================================================

destinations = {
    "AD": "ES,FR,PT,IT,GB,DE",
    "AE": "IN,GB,SA,TH,TR,OM",
    "AF": "PK,IR,IN,AE,TR,SA",
    "AG": "USA,GB,CA,BB,FR,JM",
    "AI": "USA,GB,CA,FR,NL,PR",
    "AL": "IT,GR,DE,TR,GB,AT",
    "AM": "RU,GE,AE,TR,FR,DE",
    "AO": "PT,ZA,BR,AE,FR,ES",
    "AR": "BR,UY,CL,USA,ES,IT",
    "AT": "DE,IT,HR,ES,GR,CH",
    "AU": "NZ,ID,USA,GB,JP,TH",
    "AZ": "TR,RU,GE,AE,DE,IT",
    "BA": "HR,ME,RS,TR,AT,DE",
    "BB": "USA,GB,CA,PR,JM,LC",
    "BE": "FR,ES,NL,IT,DE,GB",
    "BG": "GR,TR,RO,DE,IT,ES",
    "BH": "SA,AE,GB,TR,EG,IN",
    "BM": "USA,GB,CA,BS,JM,FR",
    "BQ": "NL,USA,CO,FR,ES,MX",
    "BR": "AR,USA,PT,CL,UY,IT",
    "BS": "USA,CA,GB,JM,PR,MX",
    "BW": "ZA,KE,TZ,GB,AE,MU",
    "BY": "RU,TR,PL,GE,AE,EG",
    "CA": "USA,MX,GB,FR,JM,CU",
    "CD": "RW,TZ,ZA,BE,FR,KE",
    "CH": "FR,IT,DE,ES,AT,GB",
    "CL": "AR,BR,PE,USA,ES,MX",
    "CN": "JP,TH,KR,SG,USA,MY",
    "CO": "USA,MX,ES,PA,PR,EC",
    "CR": "USA,MX,PA,CO,ES,GT",
    "CU": "ES,MX,CA,RU,PR,USA",
    "CV": "PT,SN,FR,ES,BR,USA",
    "CY": "GR,GB,IT,FR,DE,AE",
    "CZ": "SK,AT,DE,IT,HR,GR",
    "DE": "ES,IT,AT,TR,FR,NL",
    "DK": "ES,DE,SE,IT,FR,NO",
    "DZ": "FR,TN,TR,ES,IT,AE",
    "EC": "USA,CO,PE,ES,MX,PA",
    "EE": "FI,LV,SE,ES,TR,DE",
    "EG": "SA,AE,TR,IT,DE,GB",
    "ES": "FR,PT,IT,GB,DE,MA",
    "ET": "KE,AE,USA,SA,GB,DE",
    "FI": "ES,SE,EE,GR,IT,DE",
    "FJ": "AU,NZ,USA,SG,JP,TH",
    "FO": "DK,IS,NO,GB,SE,DE",
    "FR": "ES,IT,GB,PT,BE,DE",
    "GB": "ES,FR,USA,IT,GR,PT",
    "GE": "TR,AE,AM,RU,IT,GR",
    "GG": "GB,FR,ES,PT,IT,JE",
    "GH": "GB,USA,NG,ZA,AE,DE",
    "GL": "DK,IS,USA,CA,NO,SE",
    "GM": "SN,GB,ES,FR,DE,NL",
    "GP": "FR,JM,USA,CA,PR,LC",
    "GR": "IT,CY,TR,GB,DE,FR",
    "GT": "USA,MX,SV,PA,CR,ES",
    "HK": "JP,TH,CN,TW,KR,SG",
    "HR": "BA,IT,AT,SI,DE,RS",
    "HU": "AT,HR,RO,IT,DE,GR",
    "ID": "MY,SG,TH,SA,JP,AU",
    "IE": "GB,ES,USA,FR,PT,IT",
    "IL": "GR,USA,IT,CY,AE,GB",
    "IN": "AE,TH,USA,SG,GB,SA",
    "IQ": "TR,IR,AE,SA,JO,GB",
    "IR": "TR,AE,IQ,AM,GE,DE",
    "IS": "ES,GB,DK,USA,DE,NO",
    "IT": "FR,ES,DE,GB,GR,CH",
    "JE": "GB,FR,ES,PT,IT,GG",
    "JM": "USA,CA,GB,BS,PR,MX",
    "JO": "SA,AE,TR,EG,USA,GB",
    "JP": "KR,TW,USA,TH,CN,SG",
    "KE": "RW,TZ,AE,GB,USA,ZA",
    "KG": "KZ,RU,UZ,TR,AE,CN",
    "KH": "TH,VN,CN,SG,MY,KR",
    "KN": "USA,GB,CA,AG,BB,FR",
    "KR": "JP,VN,TH,USA,TW,SG",
    "KW": "SA,AE,TR,GB,EG,USA",
    "KY": "USA,GB,CA,JM,MX,BS",
    "KZ": "RU,TR,UZ,KG,AE,GE",
    "LA": "TH,VN,CN,KR,JP,SG",
    "LB": "TR,AE,FR,CY,GR,EG",
    "LC": "USA,GB,CA,BB,GP,FR",
    "LI": "CH,AT,DE,IT,FR,ES",
    "LK": "IN,AE,SG,TH,GB,MY",
    "LR": "GH,SN,USA,NG,GB,SL",
    "LS": "ZA,BW,KE,TZ,MU,AE",
    "LT": "LV,PL,GB,DE,ES,IT",
    "LU": "FR,DE,BE,ES,PT,IT",
    "LV": "LT,EE,DE,GB,ES,IT",
    "LY": "TN,EG,TR,IT,AE,DE",
    "MA": "FR,ES,TR,IT,BE,DE",
    "MC": "FR,IT,CH,ES,GB,DE",
    "MD": "RO,IT,TR,DE,UA,GB",
    "ME": "RS,HR,BA,AL,IT,DE",
    "MK": "GR,RS,AL,TR,DE,IT",
    "ML": "SN,GH,FR,DZ,MA,TR",
    "MO": "HK,CN,JP,TH,TW,KR",
    "MT": "IT,GB,FR,DE,ES,GR",
    "MU": "FR,RE,ZA,GB,AE,IN",
    "MV": "IN,LK,AE,SG,TH,MY",
    "MX": "USA,ES,CA,CO,FR,GB",
    "MY": "TH,SG,ID,CN,JP,AU",
    "NG": "GB,USA,GH,AE,ZA,CA",
    "NI": "USA,CR,PA,SV,GT,MX",
    "NL": "DE,BE,FR,ES,GB,IT",
    "NO": "SE,DK,ES,GB,DE,IT",
    "NP": "IN,AE,TH,MY,USA,AU",
    "NZ": "AU,USA,FJ,GB,JP,ID",
    "OM": "AE,SA,IN,TR,GB,TH",
    "PA": "USA,CO,CR,MX,ES,PR",
    "PE": "USA,CL,CO,ES,MX,AR",
    "PF": "FR,USA,NZ,AU,JP,CL",
    "PG": "AU,SG,PH,ID,NZ,MY",
    "PH": "JP,SG,USA,TH,HK,KR",
    "PK": "AE,SA,TR,GB,TH,MY",
    "PL": "DE,CZ,IT,ES,GR,HR",
    "PR": "USA,JM,ES,MX,CO,FR",
    "PT": "ES,FR,GB,IT,DE,BR",
    "PW": "USA,JP,PH,SG,AU,KR",
    "PY": "BR,AR,USA,CL,UY,ES",
    "QA": "AE,SA,TR,GB,EG,OM",
    "RE": "FR,MU,ZA,IN,AE,TH",
    "RO": "BG,IT,GR,TR,DE,HU",
    "RS": "ME,BA,HR,GR,TR,DE",
    "RU": "TR,AE,TH,EG,GE,AM",
    "RW": "KE,TZ,CD,AE,ZA,MU",
    "SA": "AE,EG,TR,GB,BH,USA",
    "SD": "EG,SA,AE,ET,TR,KE",
    "SE": "ES,DK,NO,DE,GR,IT",
    "SG": "MY,ID,TH,JP,KR,AU",
    "SI": "HR,IT,AT,DE,GR,BA",
    "SK": "CZ,AT,HU,HR,IT,PL",
    "SL": "SN,LR,GH,GB,USA,GM",
    "SN": "FR,GM,MA,ES,GH,ML",
    "SR": "NL,FR,USA,BQ,BR,CO",
    "SV": "USA,GT,PA,MX,CR,NI",
    "TC": "USA,GB,CA,BS,JM,PR",
    "TD": "CD,FR,SD,NG,AE,EG",
    "TG": "GH,ML,SN,FR,NG,GM",
    "TH": "JP,SG,MY,KR,CN,VN",
    "TJ": "UZ,RU,KZ,TR,AE,KG",
    "TN": "FR,IT,TR,DE,DZ,ES",
    "TO": "NZ,AU,FJ,USA,SG,JP",
    "TR": "DE,GR,BG,GE,AE,IT",
    "TW": "JP,KR,TH,USA,SG,HK",
    "TZ": "KE,MU,ZA,AE,RW,GB",
    "UA": "PL,DE,TR,RO,CZ,IT",
    "USA": "MX,CA,GB,FR,IT,ES",
    "UY": "AR,BR,USA,ES,CL,PY",
    "UZ": "KZ,RU,TR,KG,AE,TJ",
    "VE": "CO,USA,ES,PR,BR,MX",
    "VG": "USA,GB,PR,LC,AG,JM",
    "VN": "TH,JP,KR,SG,CN,MY",
    "XK": "AL,DE,CH,TR,IT,ME",
    "ZA": "BW,KE,GB,USA,AE,TZ",
}


# ============================================================
# ISO -> AMDOCS DESTINATION MAPPING
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
MAPPING_FILE = SCRIPT_DIR / "0_destination_iso_codes_amdocs.csv"


def load_destination_mapping():
    """Load ISO -> destination mappings from the CSV beside this script."""
    if not MAPPING_FILE.exists():
        raise SystemExit(f"Mapping file not found: {MAPPING_FILE}")

    mapping = {}

    with MAPPING_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise SystemExit("Mapping CSV is empty or has no header.")

        # Accept the expected headers regardless of capitalization.
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        iso_field = fields.get("iso")
        destination_field = fields.get("destination")

        if not iso_field or not destination_field:
            raise SystemExit(
                "Mapping CSV must contain columns named 'destination' and 'ISO'."
            )

        for row in reader:
            iso = (row.get(iso_field) or "").strip().upper()
            destination = (row.get(destination_field) or "").strip()

            if iso and destination:
                mapping[iso] = destination

    if not mapping:
        raise SystemExit("No valid ISO -> destination mappings were found.")

    print(f"Loaded {len(mapping)} ISO -> destination mappings.")
    return mapping


def convert_recommended_destinations(recommended, mapping):
    """Convert a comma-separated ISO list to Amdocs destination names."""
    converted = []

    for iso in recommended.split(","):
        iso = iso.strip().upper()

        if iso not in mapping:
            raise ValueError(
                f"No destination mapping found for recommended ISO code: {iso}"
            )

        converted.append(mapping[iso])

    return ",".join(converted)


# ============================================================
# BASIC VALIDATION
# ============================================================

def validate_data():
    problems = []

    for origin, value in destinations.items():
        dests = value.split(",")

        if len(dests) != 6:
            problems.append(
                f"{origin}: expected 6 destinations, found {len(dests)}"
            )

        if origin in dests:
            problems.append(
                f"{origin}: origin country appears in its own destinations"
            )

        if len(set(dests)) != len(dests):
            problems.append(
                f"{origin}: duplicate destination"
            )

    if problems:
        print("\nDATA VALIDATION FAILED:")
        for problem in problems:
            print(" -", problem)
        raise SystemExit("\nNothing has been uploaded.")

    print(f"Validation OK: {len(destinations)} origin countries.")



def api_headers(include_json=False):
    headers = {
        "x-api-key": API_KEY,
    }
    if include_json:
        headers["content-type"] = "application/json"
    return headers


# ============================================================
# API OPERATIONS
# ============================================================

def upload_country(session, iso, recommended, mapping):
    try:
        recommended_destinations = convert_recommended_destinations(
            recommended,
            mapping,
        )
    except ValueError as exc:
        print(f"ERROR {iso.lower()} -> {exc}")
        return False

    payload = {
        "location": iso.lower(),
        "recommendeddestinations": recommended_destinations,
    }

    try:
        response = session.post(
            URL,
            headers=api_headers(include_json=True),
            json=payload,
            timeout=30,
        )

        if response.ok:
            print(
                f"OK   {iso.lower()} -> {recommended_destinations} "
                f"[HTTP {response.status_code}]"
            )
            return True

        print(
            f"FAIL {iso.lower()} -> {recommended_destinations} "
            f"[HTTP {response.status_code}]"
        )
        print(f"     Response: {response.text[:500]}")
        return False

    except requests.RequestException as exc:
        print(f"ERROR {iso.lower()} -> {exc}")
        return False


def fetch_all_records(session):
    """Fetch all records currently stored in the auxiliary table."""
    try:
        response = session.get(URL, headers=api_headers(), timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch table records: {exc}") from exc

    if not response.ok:
        raise RuntimeError(
            f"Failed to fetch table records "
            f"[HTTP {response.status_code}]: {response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            "GET succeeded, but the response was not valid JSON."
        ) from exc


def _find_records_list(data):
    """Find the list of record objects in common API response shapes."""
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Unexpected GET response type: {type(data).__name__}"
        )

    # Common top-level wrapper names.
    for key in ("records", "items", "data", "results", "content"):
        value = data.get(key)
        if isinstance(value, list):
            return value

        # Some APIs nest the records one level deeper.
        if isinstance(value, dict):
            for nested_key in ("records", "items", "data", "results", "content"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested

    # Fallback: if there is exactly one list value, assume that is the record list.
    list_values = [value for value in data.values() if isinstance(value, list)]
    if len(list_values) == 1:
        return list_values[0]

    raise RuntimeError(
        "Could not identify the record list in the GET response. "
        f"Top-level keys: {', '.join(map(str, data.keys()))}"
    )


def extract_record_ids(data):
    """Extract record IDs from the GET response."""
    records = _find_records_list(data)
    record_ids = []

    possible_id_fields = (
        "id",
        "record_id",
        "recordId",
        "recordid",
        "_id",
        "uuid",
    )

    for record in records:
        if not isinstance(record, dict):
            continue

        record_id = None

        for key in possible_id_fields:
            value = record.get(key)
            if value not in (None, ""):
                record_id = str(value)
                break

        # Fallback: look for a key containing both "record" and "id".
        if record_id is None:
            for key, value in record.items():
                normalized = key.lower().replace("_", "").replace("-", "")
                if "record" in normalized and "id" in normalized:
                    if value not in (None, ""):
                        record_id = str(value)
                        break

        if record_id:
            record_ids.append(record_id)

    if records and not record_ids:
        sample = records[0]
        raise RuntimeError(
            "Records were returned, but the record ID field could not be found. "
            f"First record: {str(sample)[:500]}"
        )

    return record_ids


def delete_record(session, record_id):
    delete_url = f"{URL}/{record_id}"

    try:
        response = session.delete(delete_url, headers=api_headers(), timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR deleting {record_id} -> {exc}")
        return False

    if response.ok:
        print(f"OK   deleted {record_id} [HTTP {response.status_code}]")
        return True

    print(f"FAIL deleting {record_id} [HTTP {response.status_code}]")
    print(f"     Response: {response.text[:500]}")
    return False


def delete_all_records(session):
    print("\nFetching existing records...")

    try:
        data = fetch_all_records(session)
        record_ids = extract_record_ids(data)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return

    if not record_ids:
        print("The table is already empty.")
        return

    print(f"Found {len(record_ids)} records.")

    confirmation = input(
        f"Delete ALL {len(record_ids)} records? Type DELETE to confirm: "
    ).strip()

    if confirmation != "DELETE":
        print("Deletion cancelled.")
        return

    successful = 0
    failed = []

    for record_id in record_ids:
        if delete_record(session, record_id):
            successful += 1
        else:
            failed.append(record_id)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    print("\n" + "=" * 60)
    print("DELETE COMPLETE")
    print("=" * 60)
    print(f"Successful: {successful}")
    print(f"Failed:     {len(failed)}")

    if failed:
        print("\nFAILED RECORD IDs:")
        print(", ".join(failed))
    else:
        print("\nAll records deleted successfully.")


def upload_test_record(session, mapping):
    print("\n*** TEST UPLOAD: ONE ROW ONLY ***")

    country = input(
        f"Country code to test [{TEST_COUNTRY}]: "
    ).strip().upper()

    if not country:
        country = TEST_COUNTRY

    if country not in destinations:
        print(
            f"Unknown country code: {country}. "
            "Nothing has been uploaded."
        )
        return

    recommended = destinations[country]

    print(
        f"Only one row will be uploaded: {country} -> {recommended}\n"
    )

    success = upload_country(
        session,
        country,
        recommended,
        mapping,
    )

    if success:
        print("\nTEST SUCCESSFUL.")
        print("Only one row was uploaded.")
    else:
        print("\nTEST FAILED.")
        print("No other rows were attempted.")


def upload_all_records(session, mapping):
    print("\n*** FULL UPLOAD ***")
    print(f"About to upload {len(destinations)} countries.\n")

    confirmation = input(
        f"Add all {len(destinations)} rows to the table? Type ADD to confirm: "
    ).strip()

    if confirmation != "ADD":
        print("Upload cancelled.")
        return

    successful = []
    failed = []

    for iso, recommended in destinations.items():
        if upload_country(session, iso, recommended, mapping):
            successful.append(iso)
        else:
            failed.append(iso)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    print("\n" + "=" * 60)
    print("UPLOAD COMPLETE")
    print("=" * 60)
    print(f"Successful: {len(successful)}")
    print(f"Failed:     {len(failed)}")

    if failed:
        print("\nFAILED COUNTRIES:")
        print(", ".join(failed))
    else:
        print("\nAll countries uploaded successfully.")


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY:
        raise SystemExit(
            "FREQUENT_DESTINATIONS_API_KEY was not found in the .env file."
        )

    mapping = load_destination_mapping()
    validate_data()

    session = requests.Session()

    print("\nChoose action:")
    print("1 = Test upload ONE row")
    print("2 = Add/upload ALL rows")
    print("3 = Delete ALL rows")
    print("0 = Exit")

    choice = input("\nEnter 1, 2, 3, or 0: ").strip()

    if choice == "1":
        upload_test_record(session, mapping)
    elif choice == "2":
        upload_all_records(session, mapping)
    elif choice == "3":
        delete_all_records(session)
    elif choice == "0":
        print("Nothing changed.")
    else:
        print("Invalid option. Nothing changed.")


if __name__ == "__main__":
    main()
