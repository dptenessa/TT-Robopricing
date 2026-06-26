from datetime import date

import pandas as pd
from pipeline_files import FILES, PipelineFiles


def canonicalize_country_names_by_iso3(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ISO3"] = df["ISO3"].astype(str).str.strip().str.upper()
    df["Country"] = df["Country"].astype(str).str.strip()

    iso3_to_country = (
        df.groupby("ISO3")["Country"]
        .agg(lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0])
        .to_dict()
    )

    df["Country"] = df["ISO3"].map(iso3_to_country)

    return df


def combine_all_scraped_data(paths: PipelineFiles = FILES):
    csv_files = sorted(paths.scraper_outputs_dir.glob("*_current.csv"))

    if not csv_files:
        print("No CSV files found.")
        return

    li = [pd.read_csv(f) for f in csv_files]
    df = pd.concat(li, axis=0, ignore_index=True)

    # Normalize ISO3 and make Country consistent per ISO3
    df["ISO3"] = df["ISO3"].astype(str).str.strip().str.upper()
    df = canonicalize_country_names_by_iso3(df)

    # Convert Days to numeric
    df["Days"] = pd.to_numeric(df["Days"], errors="coerce")

    # Normalize GB text once
    df["GB_raw"] = df["GB"].astype(str).str.strip()
    df["GB_norm"] = df["GB_raw"].str.lower()

    # Use normalized GB for dedupe
    # df_unique = df.drop_duplicates(
    #     subset=["Provider", "ISO3", "Days", "GB_norm"],
    #     keep="first",
    # ).copy()
    df_unique = df.copy()

    # Detect unlimited (case-insensitive via normalization)
    mask_unlimited = df_unique["GB_norm"].str.contains(r"unl", na=False)

    # Convert through text first so pandas string columns accept the unlimited conversion.
    df_unique["GB"] = df_unique["GB"].astype("object")
    df_unique.loc[mask_unlimited, "GB"] = (
        df_unique.loc[mask_unlimited, "Days"].fillna(0) * 3
    ).astype(str)
    df_unique["GB"] = pd.to_numeric(df_unique["GB"], errors="coerce")

    # Cleanup
    df_unique = df_unique.drop(columns=["GB_raw", "GB_norm"])

    # Outputs
    today_str = date.today().isoformat()

    latest_path = paths.combined_latest
    history_path = paths.combined_history(date.today())

    latest_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    df_unique.to_csv(latest_path, index=False)
    df_unique.to_csv(history_path, index=False)

    print(f"Combined {len(csv_files)} current files.")
    print(f"Total unique price points: {len(df_unique)}")
    print(f"Latest file: {latest_path}")
    print(f"History file: {history_path}")


if __name__ == "__main__":
    combine_all_scraped_data()
