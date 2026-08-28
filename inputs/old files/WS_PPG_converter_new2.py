import json
import re
import subprocess
from pathlib import Path

import pandas as pd
import pycountry


BASE_DIR = Path(__file__).resolve().parent

CRODIS_FILE = BASE_DIR / "T_Cost_Crodis.xlsx"
COVERAGE_FILE = BASE_DIR / "Connections_2026-05-26.csv"
OUTPUT_FILE = BASE_DIR / "WS_PPG_test3.csv"


ISO_EXCEPTIONS = {
    "ANT": {"ISO_Code_A3": "BES", "ISO_Code_A2": "BQ"},
    "ROM": {"ISO_Code_A3": "ROU", "ISO_Code_A2": "RO"},

    # Kosovo exceptions from CRODIS operator ISO in brackets:
    # IPKO (XKX), Monaco Telecom (XKX)
    "XK": {"ISO_Code_A3": "KXK", "ISO_Code_A2": "XK"},
    "XKX": {"ISO_Code_A3": "KXK", "ISO_Code_A2": "XK"},
    "KOS": {"ISO_Code_A3": "KXK", "ISO_Code_A2": "XK"},
}

# Exceptions detected from CRODIS_FILE operator names.
OPERATOR_EXCEPTIONS = {
    "JERSEY AIRTEL": {
        "ISO_Code_A3": "JEY",
        "ISO_Code_A2": "JE",
        "country": "Jersey",
    },
    "JERSEY TELECOMS": {
        "ISO_Code_A3": "JEY",
        "ISO_Code_A2": "JE",
        "country": "Jersey",
    },
    "SURE GUERNSEY": {
        "ISO_Code_A3": "GGY",
        "ISO_Code_A2": "GG",
        "country": "Channel Islands",
    },
    "MANX TELECOM": {
        "ISO_Code_A3": "IMN",
        "ISO_Code_A2": "IM",
    },
}

# Exceptions detected from COVERAGE_FILE TADIG code.
COVERAGE_TADIG_EXCEPTIONS = {
    "K0001": {"ISO_Code_A3": "KXK", "ISO_Code_A2": "XK"},
    "K00TK": {"ISO_Code_A3": "KXK", "ISO_Code_A2": "XK"},
}

# Exceptions detected from COVERAGE_FILE operator names.
COVERAGE_OPERATOR_EXCEPTIONS = {
    "SURE GUERNSEY": {
        "ISO_Code_A3": "GGY",
        "ISO_Code_A2": "GG",
        "country": "Channel Islands",
    },
    "JT": {
        "ISO_Code_A3": "JEY",
        "ISO_Code_A2": "JE",
        "country": "Jersey",
    },
    "JERSEY TELECOMS": {
        "ISO_Code_A3": "JEY",
        "ISO_Code_A2": "JE",
        "country": "Jersey",
    },
}


# Countries intentionally allowed even when missing from COVERAGE_FILE.
MISSING_COVERAGE_COUNTRIES = {
    "HRV": {"ISO_Code_A3": "HRV", "ISO_Code_A2": "HR"},
}


ISO3_TO_ISO2_EXCEPTIONS = {
    "KXK": "XK",
}


COUNTRY_EXCEPTIONS = {
    "Turkey_2": "Turkey",
}


I18N_LANGUAGE = "en"
I18N_NAME_SELECTION = "official"
I18N_PACKAGE_VERSION = "7.14.0"


def load_i18n_country_names():
    """Return canonical ISO2 -> country-name mapping from i18n-iso-countries.

    The Node.js package must be installed in this project (or otherwise
    resolvable by Node from BASE_DIR). Keeping this lookup here makes
    WS_PPG.csv the single upstream source of canonical partner country names.
    """
    js = f"""
const countries = require('i18n-iso-countries');
const packageInfo = require('i18n-iso-countries/package.json');
const names = countries.getNames('{I18N_LANGUAGE}', {{ select: '{I18N_NAME_SELECTION}' }});
process.stdout.write(JSON.stringify({{ version: packageInfo.version, names }}));
"""

    try:
        result = subprocess.run(
            ["node", "-e", js],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Node.js was not found. Install Node.js and ensure 'node' is available in PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            "Could not load the 'i18n-iso-countries' Node.js package. "
            f"Install it in the project, for example with: npm install i18n-iso-countries@{I18N_PACKAGE_VERSION}"
            + (f"\nNode error: {details}" if details else "")
        ) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "i18n-iso-countries returned invalid JSON."
        ) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("names"), dict):
        raise RuntimeError(
            "i18n-iso-countries did not return the expected ISO2-to-name mapping."
        )

    installed_version = str(payload.get("version", "")).strip()
    if installed_version != I18N_PACKAGE_VERSION:
        raise RuntimeError(
            "Unexpected i18n-iso-countries version. "
            f"Expected {I18N_PACKAGE_VERSION}, found {installed_version or 'unknown'}. "
            f"Install the pinned version with: npm install i18n-iso-countries@{I18N_PACKAGE_VERSION}"
        )

    names = payload["names"]
    return {
        str(iso2).strip().upper(): str(name).strip()
        for iso2, name in names.items()
        if str(iso2).strip() and str(name).strip()
    }



def clean_columns(df):
    df.columns = df.columns.astype(str).str.strip()
    return df


def get_country_name_from_iso2(iso2, i18n_country_names):
    if pd.isna(iso2):
        return None

    iso2 = str(iso2).strip().upper()

    # Kosovo is not part of ISO 3166-1, so keep the existing project-specific
    # exception if the library does not provide a value for XK.
    if iso2 == "XK":
        return i18n_country_names.get(iso2, "Kosovo")

    return i18n_country_names.get(iso2)


def find_column(df, *candidates, required=True):
    columns_by_normalized_name = {
        str(column).strip().lower(): column
        for column in df.columns
    }

    for candidate in candidates:
        normalized_candidate = str(candidate).strip().lower()

        if normalized_candidate in columns_by_normalized_name:
            return columns_by_normalized_name[normalized_candidate]

    if required:
        raise KeyError(f"Missing required column: {', '.join(candidates)}")

    return None


def extract_iso(operator):
    if pd.isna(operator):
        return None

    match = re.search(r"\(([A-Z]{2,3})\)", str(operator).upper())
    return match.group(1) if match else None


def get_operator_exception(operator):
    if pd.isna(operator):
        return None

    operator_upper = str(operator).upper()

    for key, value in OPERATOR_EXCEPTIONS.items():
        if key in operator_upper:
            return value

    return None


def get_coverage_operator_exception(operator):
    if pd.isna(operator):
        return None

    operator_upper = str(operator).strip().upper()

    if not operator_upper:
        return None

    # JT is intentionally exact because it is too short for safe substring matching.
    if operator_upper in COVERAGE_OPERATOR_EXCEPTIONS:
        return COVERAGE_OPERATOR_EXCEPTIONS[operator_upper]

    for key, value in COVERAGE_OPERATOR_EXCEPTIONS.items():
        if key == "JT":
            continue

        if key in operator_upper:
            return value

    return None


def iso3_to_iso2(iso3):
    if pd.isna(iso3):
        return None

    iso3 = str(iso3).strip().upper()

    if iso3 in ISO3_TO_ISO2_EXCEPTIONS:
        return ISO3_TO_ISO2_EXCEPTIONS[iso3]

    for exception in ISO_EXCEPTIONS.values():
        if iso3 == exception["ISO_Code_A3"]:
            return exception["ISO_Code_A2"]

    for exception in OPERATOR_EXCEPTIONS.values():
        if iso3 == exception["ISO_Code_A3"]:
            return exception["ISO_Code_A2"]

    if iso3 in MISSING_COVERAGE_COUNTRIES:
        return MISSING_COVERAGE_COUNTRIES[iso3]["ISO_Code_A2"]

    country = pycountry.countries.get(alpha_3=iso3)

    if country is None:
        return None

    return country.alpha_2


def get_coverage_iso3(row):
    tadig_code = row["TADIG code"]

    if tadig_code in COVERAGE_TADIG_EXCEPTIONS:
        return COVERAGE_TADIG_EXCEPTIONS[tadig_code]["ISO_Code_A3"]

    # Kosovo TADIG codes, e.g. K0001, K00TK
    if str(tadig_code).startswith("K00"):
        return "KXK"

    operator_exception = get_coverage_operator_exception(row["Operator"])

    if operator_exception is not None:
        return operator_exception["ISO_Code_A3"]

    if not tadig_code:
        return None

    return tadig_code[:3]


def get_coverage_iso2(row):
    tadig_code = row["TADIG code"]

    if tadig_code in COVERAGE_TADIG_EXCEPTIONS:
        return COVERAGE_TADIG_EXCEPTIONS[tadig_code]["ISO_Code_A2"]

    # Kosovo TADIG codes, e.g. K0001, K00TK
    if str(tadig_code).startswith("K00"):
        return "XK"

    operator_exception = get_coverage_operator_exception(row["Operator"])

    if operator_exception is not None:
        return operator_exception["ISO_Code_A2"]

    return iso3_to_iso2(row["Country (ISO3)"])


def get_coverage_country(row):
    operator_exception = get_coverage_operator_exception(row["Operator"])

    if operator_exception is not None and operator_exception.get("country"):
        return operator_exception["country"]

    return row["International name"]


def get_final_iso3(row):
    if row["operator_exception"] is not None:
        return row["operator_exception"]["ISO_Code_A3"]

    if row["raw_iso"] in ISO_EXCEPTIONS:
        return ISO_EXCEPTIONS[row["raw_iso"]]["ISO_Code_A3"]

    return row["raw_iso"]


def get_forced_iso2(row):
    if row["operator_exception"] is not None:
        return row["operator_exception"]["ISO_Code_A2"]

    if row["raw_iso"] in ISO_EXCEPTIONS:
        return ISO_EXCEPTIONS[row["raw_iso"]]["ISO_Code_A2"]

    if row["ISO_Code_A3"] in MISSING_COVERAGE_COUNTRIES:
        return MISSING_COVERAGE_COUNTRIES[row["ISO_Code_A3"]]["ISO_Code_A2"]

    return None


def get_forced_country(row):
    if row["operator_exception"] is not None:
        return row["operator_exception"].get("country")

    if row["ISO_Code_A3"] in MISSING_COVERAGE_COUNTRIES:
        return MISSING_COVERAGE_COUNTRIES[row["ISO_Code_A3"]].get("country")

    return None


def main():
    i18n_country_names = load_i18n_country_names()

    crodis = pd.read_excel(CRODIS_FILE, sheet_name="Crodis")

    coverage = pd.read_csv(
        COVERAGE_FILE,
        sep=None,
        engine="python",
        encoding="utf-8-sig",
    )

    crodis = clean_columns(crodis)
    coverage = clean_columns(coverage)

    tadig_column = find_column(coverage, "TADIG code", "TADIG Code")
    coverage_country_column = find_column(coverage, "Country")
    coverage_operator_column = find_column(coverage, "Operator", required=False)

    coverage_columns = [tadig_column, coverage_country_column]

    if coverage_operator_column is not None:
        coverage_columns.append(coverage_operator_column)

    crodis = crodis[["country", "operator", "dIOT PSD"]].copy()
    coverage = coverage[coverage_columns].copy()

    coverage_rename_columns = {
        tadig_column: "TADIG code",
        coverage_country_column: "International name",
    }

    if coverage_operator_column is not None:
        coverage_rename_columns[coverage_operator_column] = "Operator"

    coverage = coverage.rename(columns=coverage_rename_columns)

    if "Operator" not in coverage.columns:
        coverage["Operator"] = ""

    crodis["country"] = crodis["country"].replace(COUNTRY_EXCEPTIONS)

    coverage["TADIG code"] = (
        coverage["TADIG code"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )

    coverage["Operator"] = (
        coverage["Operator"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    coverage["International name"] = (
        coverage["International name"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    coverage["Country (ISO3)"] = coverage.apply(get_coverage_iso3, axis=1)
    coverage["Country (ISO2)"] = coverage.apply(get_coverage_iso2, axis=1)
    coverage["International name"] = coverage.apply(get_coverage_country, axis=1)

    coverage = coverage[
        coverage["Country (ISO3)"].notna()
        & coverage["Country (ISO2)"].notna()
    ].copy()

    coverage = coverage.drop_duplicates(subset=["Country (ISO3)"])

    crodis["raw_iso"] = crodis["operator"].apply(extract_iso)
    crodis["operator_exception"] = crodis["operator"].apply(get_operator_exception)
    crodis["ISO_Code_A3"] = crodis.apply(get_final_iso3, axis=1)

    crodis["Min"] = pd.to_numeric(crodis["dIOT PSD"], errors="coerce") * 1024

    # Exclude blank, invalid, and zero values from minimum calculation.
    crodis = crodis[crodis["Min"].notna() & (crodis["Min"] > 0)].copy()

    merged = crodis.merge(
        coverage,
        left_on="ISO_Code_A3",
        right_on="Country (ISO3)",
        how="left",
    )

    # Keep:
    # 1. normal rows only if they exist in Connections table
    # 2. ANT and ROM even if BES/ROU do not exist in Connections table
    # 3. countries explicitly allowed even when missing from Connections table, e.g. HRV
    #
    # IMN is still not kept unless it exists in Connections table.
    merged = merged[
        merged["Country (ISO3)"].notna()
        | merged["raw_iso"].isin(ISO_EXCEPTIONS.keys())
        | merged["ISO_Code_A3"].isin(MISSING_COVERAGE_COUNTRIES.keys())
    ].copy()

    merged["forced_iso2"] = merged.apply(get_forced_iso2, axis=1)

    merged["ISO_Code_A2"] = merged["forced_iso2"].combine_first(
        merged["Country (ISO2)"]
    )

    merged["forced_country"] = merged.apply(get_forced_country, axis=1)
    merged["country"] = merged["forced_country"].combine_first(merged["country"])

    # Final technical country name is generated from ISO2 using
    # i18n-iso-countries. This makes WS_PPG.csv the upstream source used later
    # for both partner CSV Destination and JSON destination values.
    merged["mapped_country"] = merged["ISO_Code_A2"].apply(
        lambda iso2: get_country_name_from_iso2(iso2, i18n_country_names)
    )

    missing_name_iso2 = sorted(
        {
            str(value).strip().upper()
            for value in merged.loc[merged["mapped_country"].isna(), "ISO_Code_A2"]
            if pd.notna(value) and str(value).strip()
        }
    )
    if missing_name_iso2:
        raise ValueError(
            "Missing canonical country names in i18n-iso-countries for ISO2: "
            + ", ".join(missing_name_iso2)
        )

    merged["country"] = merged["mapped_country"]

    result = (
        merged
        .groupby(["ISO_Code_A2", "ISO_Code_A3", "country"], as_index=False)
        .agg({"Min": "min"})
        .rename(columns={"Min": "Min of Min"})
    )

    result = result[["ISO_Code_A2", "ISO_Code_A3", "country", "Min of Min"]]

    result.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    print(f"Script finished, total rows exported: {len(result)}")


if __name__ == "__main__":
    main()