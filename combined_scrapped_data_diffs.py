import glob
import os
from pathlib import Path

import pandas as pd
from pipeline_files import FILES, PipelineFiles


SNAPSHOT_PATTERN = "combined_scrapped_data_*.csv"

KEY_COLS = ["Provider", "ISO3", "Plan", "Days", "GB"]
PRICE_COMPARE_COLS = ["Price", "Currency", "usd_price", "eur_price"]
COMPARE_COLS = PRICE_COMPARE_COLS + ["SpecialOffer"]


def load_two_latest_snapshots(history_dir: str, pattern: str) -> tuple[str, str]:
    files = sorted(glob.glob(os.path.join(history_dir, pattern)))
    if len(files) < 2:
        raise RuntimeError("Need at least 2 snapshot files to compare.")
    return files[-2], files[-1]


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in KEY_COLS:
        if col not in df.columns:
            df[col] = ""

    for col in COMPARE_COLS:
        if col not in df.columns:
            df[col] = ""

    df["Provider"] = df["Provider"].astype(str).str.strip()
    df["ISO3"] = df["ISO3"].astype(str).str.strip().str.upper()
    df["Plan"] = df["Plan"].fillna("").astype(str).str.strip()
    df["Days"] = pd.to_numeric(df["Days"], errors="coerce")
    df["GB"] = pd.to_numeric(df["GB"], errors="coerce")
    for col in ["Price", "usd_price", "eur_price"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Currency"] = df["Currency"].fillna("").astype(str).str.strip().str.upper()
    df["SpecialOffer"] = df["SpecialOffer"].fillna("").astype(str).str.strip()

    return df


def build_change_report(previous_df: pd.DataFrame, current_df: pd.DataFrame) -> pd.DataFrame:
    prev = normalize_df(previous_df)
    curr = normalize_df(current_df)

    prev_small = prev[KEY_COLS + COMPARE_COLS].drop_duplicates(subset=KEY_COLS, keep="first")
    curr_small = curr[KEY_COLS + COMPARE_COLS].drop_duplicates(subset=KEY_COLS, keep="first")

    merged = prev_small.merge(
        curr_small,
        on=KEY_COLS,
        how="outer",
        suffixes=("_old", "_new"),
        indicator=True,
    )

    merged["ChangeType"] = ""
    merged.loc[merged["_merge"] == "left_only", "ChangeType"] = "deleted"
    merged.loc[merged["_merge"] == "right_only", "ChangeType"] = "new"

    both_mask = merged["_merge"] == "both"
    price_changed = False
    for col in PRICE_COMPARE_COLS:
        old_col = f"{col}_old"
        new_col = f"{col}_new"
        if old_col not in merged.columns or new_col not in merged.columns:
            continue
        if col in {"Price", "usd_price", "eur_price"}:
            changed = (
                pd.to_numeric(merged[old_col], errors="coerce").round(6).fillna(-999999)
                != pd.to_numeric(merged[new_col], errors="coerce").round(6).fillna(-999999)
            )
        else:
            changed = (
                merged[old_col].fillna("").astype(str).str.strip()
                != merged[new_col].fillna("").astype(str).str.strip()
            )
        price_changed = changed if isinstance(price_changed, bool) else (price_changed | changed)
    promo_changed = merged["SpecialOffer_old"] != merged["SpecialOffer_new"]

    merged.loc[both_mask & (price_changed | promo_changed), "ChangeType"] = "changed"

    merged["ChangeReason"] = ""
    merged.loc[both_mask & price_changed & promo_changed, "ChangeReason"] = "price+promo"
    merged.loc[both_mask & price_changed & ~promo_changed, "ChangeReason"] = "price"
    merged.loc[both_mask & ~price_changed & promo_changed, "ChangeReason"] = "promo"

    changes = merged[merged["ChangeType"] != ""].copy()

    changes = changes[
        [
            "ISO3",
            "Provider",
            "Plan",
            "Days",
            "GB",
            "ChangeType",
            "ChangeReason",
            "Price_old",
            "Price_new",
            "Currency_old",
            "Currency_new",
            "usd_price_old",
            "usd_price_new",
            "eur_price_old",
            "eur_price_new",
            "SpecialOffer_old",
            "SpecialOffer_new",
        ]
    ].sort_values(
        by=["ISO3", "Provider", "Plan", "Days", "GB"],
        ascending=[True, True, True, True, True],
    )

    return changes


def build_provider_summary(changes: pd.DataFrame) -> pd.DataFrame:
    if changes.empty:
        return pd.DataFrame(
            columns=[
                "Provider",
                "TotalChanges",
                "New",
                "Deleted",
                "Changed",
                "Price",
                "Promo",
                "Price+Promo",
            ]
        )

    summary = changes.groupby("Provider").size().rename("TotalChanges").reset_index()

    type_counts = (
        changes.pivot_table(
            index="Provider",
            columns="ChangeType",
            values="ISO3",
            aggfunc="count",
            fill_value=0,
        )
        .reset_index()
    )

    reason_only = changes[changes["ChangeType"] == "changed"].copy()

    if reason_only.empty:
        reason_counts = pd.DataFrame(columns=["Provider", "price", "promo", "price+promo"])
    else:
        reason_counts = (
            reason_only.pivot_table(
                index="Provider",
                columns="ChangeReason",
                values="ISO3",
                aggfunc="count",
                fill_value=0,
            )
            .reset_index()
        )

    out = summary.merge(type_counts, on="Provider", how="left").merge(
        reason_counts, on="Provider", how="left"
    )

    for col in ["new", "deleted", "changed", "price", "promo", "price+promo"]:
        if col not in out.columns:
            out[col] = 0

    out = out.rename(
        columns={
            "new": "New",
            "deleted": "Deleted",
            "changed": "Changed",
            "price": "Price",
            "promo": "Promo",
            "price+promo": "Price+Promo",
        }
    )

    out = out[
        ["Provider", "TotalChanges", "New", "Deleted", "Changed", "Price", "Promo", "Price+Promo"]
    ].sort_values(by=["TotalChanges", "Provider"], ascending=[False, True])

    return out


def build_country_summary(changes: pd.DataFrame) -> pd.DataFrame:
    if changes.empty:
        return pd.DataFrame(
            columns=["ISO3", "TotalChanges", "New", "Deleted", "Changed", "Price", "Promo", "Price+Promo"]
        )

    summary = changes.groupby("ISO3").size().rename("TotalChanges").reset_index()

    type_counts = (
        changes.pivot_table(
            index="ISO3",
            columns="ChangeType",
            values="Provider",
            aggfunc="count",
            fill_value=0,
        )
        .reset_index()
    )

    reason_only = changes[changes["ChangeType"] == "changed"].copy()
    if reason_only.empty:
        reason_counts = pd.DataFrame(columns=["ISO3", "price", "promo", "price+promo"])
    else:
        reason_counts = (
            reason_only.pivot_table(
                index="ISO3",
                columns="ChangeReason",
                values="Provider",
                aggfunc="count",
                fill_value=0,
            )
            .reset_index()
        )

    out = summary.merge(type_counts, on="ISO3", how="left").merge(reason_counts, on="ISO3", how="left")

    for col in ["new", "deleted", "changed", "price", "promo", "price+promo"]:
        if col not in out.columns:
            out[col] = 0

    out = out.rename(
        columns={
            "new": "New",
            "deleted": "Deleted",
            "changed": "Changed",
            "price": "Price",
            "promo": "Promo",
            "price+promo": "Price+Promo",
        }
    )

    out = out[
        ["ISO3", "TotalChanges", "New", "Deleted", "Changed", "Price", "Promo", "Price+Promo"]
    ].sort_values(by=["TotalChanges", "ISO3"], ascending=[False, True])

    return out


def print_pipe_table(df: pd.DataFrame, title: str) -> None:
    print(f"\n{title}")

    if df.empty:
        print("(no rows)")
        return

    display_df = df.fillna("").copy()

    for col in display_df.columns:
        display_df[col] = display_df[col].astype(str)

    widths = {
        col: max(len(col), display_df[col].map(len).max())
        for col in display_df.columns
    }

    header = " | ".join(col.ljust(widths[col]) for col in display_df.columns)
    sep = "-+-".join("-" * widths[col] for col in display_df.columns)

    print(header)
    print(sep)

    for _, row in display_df.iterrows():
        print(" | ".join(row[col].ljust(widths[col]) for col in display_df.columns))


def main(paths: PipelineFiles = FILES):
    # 1. Get all available snapshot files
    history_dir = paths.combined_history_dir
    output_dir = paths.scraped_diffs_dir
    files = sorted(history_dir.glob(SNAPSHOT_PATTERN))

    # 2. Guard clause: Check if we have enough data to compare
    if len(files) < 2:
        print(f"--- Comparison Skipped ---")
        print(f"Found {len(files)} snapshot(s) in {history_dir}.")
        print("Need at least two files to generate a difference report.")
        return

    # 3. Identify the two most recent files
    previous_file, current_file = files[-2], files[-1]
    
    # 4. Extract clean timestamps/names from filenames for the output files
    # Using Path().stem.split('_')[-1] is often safer if your dates are at the end
    prev_name = Path(previous_file).stem.replace("combined_scrapped_data_", "")
    curr_name = Path(current_file).stem.replace("combined_scrapped_data_", "")

    # 5. Process the data
    print(f"Comparing snapshots:\n  Old: {prev_name}\n  New: {curr_name}")
    
    previous_df = pd.read_csv(previous_file)
    current_df = pd.read_csv(current_file)

    changes = build_change_report(previous_df, current_df)
    summary = build_country_summary(changes)
    provider_summary = build_provider_summary(changes)

    # 6. Setup Output directory and file paths
    output_dir.mkdir(parents=True, exist_ok=True)

    diff_file = output_dir / f"diff_{prev_name}_vs_{curr_name}.csv"
    summary_file = output_dir / f"summary_{prev_name}_vs_{curr_name}.csv"
    provider_summary_file = output_dir / f"provider_summary_{prev_name}_vs_{curr_name}.csv"

    # 7. Save and Print Results
    changes.to_csv(diff_file, index=False)
    summary.to_csv(summary_file, index=False)
    provider_summary.to_csv(provider_summary_file, index=False)

    print(f"\nSuccess!")
    print(f"Saved diff to: {diff_file}")
    print(f"Total rows with changes: {len(changes)}")
    print(f"Saved provider summary to: {provider_summary_file}")

    print_pipe_table(summary, "COUNTRY SUMMARY")
    print_pipe_table(provider_summary, "PROVIDER SUMMARY")

    


if __name__ == "__main__":
    main()
