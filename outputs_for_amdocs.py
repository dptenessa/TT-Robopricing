import glob
import json
import os
from pathlib import Path

import pandas as pd
from currency_support import CURRENCIES, DEFAULT_CURRENCY
from pipeline_files import FILES, PipelineFiles


HT_PATTERN = "HT_prices_*.csv"
PROMO_PATTERN = "promos_*.json"

HT_KEY_COLS = ["ISO3", "PricingUnitIdUsed", "Plan", "Days", "GB"]
HT_COMPARE_COLS = [
    "Price",
    "PromoCode",
]


def load_two_latest_files(history_dir: str, pattern: str) -> tuple[str, str]:
    files = sorted(glob.glob(os.path.join(history_dir, pattern)))
    if len(files) < 2:
        raise RuntimeError(f"Need at least 2 files matching {pattern} to compare.")
    return files[-2], files[-1]


def normalize_ht_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in HT_KEY_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    for col in HT_COMPARE_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    text_cols = ["ISO3", "PricingUnitIdUsed", "Package", "PromoCode"]
    text_cols = ["ISO3", "PricingUnitIdUsed", "Plan", "PromoCode"]
    for col in text_cols:
        df[col] = df[col].fillna("").astype(str).str.strip()

    df["ISO3"] = df["ISO3"].str.upper()
    df["PricingUnitIdUsed"] = df["PricingUnitIdUsed"].str.upper()
    df["Plan"] = df["Plan"].str.upper()

    num_cols = ["Days", "GB", "Price"]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def build_ht_change_report(previous_df: pd.DataFrame, current_df: pd.DataFrame) -> pd.DataFrame:
    prev = normalize_ht_df(previous_df)
    curr = normalize_ht_df(current_df)

    prev_small = prev[HT_KEY_COLS + HT_COMPARE_COLS].drop_duplicates(subset=HT_KEY_COLS, keep="first")
    curr_small = curr[HT_KEY_COLS + HT_COMPARE_COLS].drop_duplicates(subset=HT_KEY_COLS, keep="first")

    merged = prev_small.merge(
        curr_small,
        on=HT_KEY_COLS,
        how="outer",
        suffixes=("_old", "_new"),
        indicator=True,
    )

    for col in ["Price_old", "Price_new"]:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

    merged["ChangeType"] = ""
    merged.loc[merged["_merge"] == "left_only", "ChangeType"] = "deleted"
    merged.loc[merged["_merge"] == "right_only", "ChangeType"] = "new"

    both_mask = merged["_merge"] == "both"

    price_changed = (
        merged["Price_old"].round(6).fillna(-999999)
        != merged["Price_new"].round(6).fillna(-999999)
    )

    promo_changed = (
        merged["PromoCode_old"].fillna("").astype(str).str.strip()
        != merged["PromoCode_new"].fillna("").astype(str).str.strip()
    )

    merged.loc[both_mask & (price_changed | promo_changed), "ChangeType"] = "changed"

    merged["ChangeReason"] = ""
    merged.loc[both_mask & price_changed & promo_changed, "ChangeReason"] = "price+promo"
    merged.loc[both_mask & price_changed & ~promo_changed, "ChangeReason"] = "price"
    merged.loc[both_mask & ~price_changed & promo_changed, "ChangeReason"] = "promo"

    changes = merged[merged["ChangeType"] != ""].copy()

    ordered_cols = [
        "ISO3",
        "PricingUnitIdUsed",
        "Plan",
        "Days",
        "GB",
        "ChangeType",
        "ChangeReason",
        "Price_old",
        "Price_new",
        "PromoCode_old",
        "PromoCode_new",
    ]

    changes = changes[ordered_cols].sort_values(
        by=["ISO3", "PricingUnitIdUsed", "Plan", "Days", "GB"],
        ascending=[True, True, True, True, True],
    )

    return changes


def build_ht_country_summary(changes: pd.DataFrame) -> pd.DataFrame:
    if changes.empty:
        return pd.DataFrame(
            columns=["ISO3", "TotalChanges", "New", "Deleted", "Changed", "Price", "Promo", "Price+Promo"]
        )

    summary = changes.groupby("ISO3").size().rename("TotalChanges").reset_index()

    type_counts = (
        changes.pivot_table(
            index="ISO3",
            columns="ChangeType",
            values="Plan",
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
                    values="Plan",
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


def load_promo_json(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_promo_list(data: dict | list) -> pd.DataFrame:
    if isinstance(data, list):
        promos = data
    elif isinstance(data, dict):
        promos = data.get("promos", data.get("changes", []))
    else:
        promos = []
    df = pd.DataFrame(promos)

    expected_cols = [
        "PromoScopeKey",
        "PromoCode",
    ]

    for col in expected_cols:
        if col not in df.columns:
            df[col] = pd.NA

    if df.empty:
        return pd.DataFrame(columns=expected_cols)

    df["PromoScopeKey"] = df["PromoScopeKey"].fillna("").astype(str).str.strip()
    df["PromoCode"] = df["PromoCode"].fillna("").astype(str).str.strip()

    df = df[expected_cols].drop_duplicates(subset=["PromoScopeKey"], keep="first")
    return df


def build_promo_change_report(previous_data: dict, current_data: dict) -> dict:
    prev = normalize_promo_list(previous_data)
    curr = normalize_promo_list(current_data)

    merged = prev.merge(
        curr,
        on="PromoScopeKey",
        how="outer",
        suffixes=("_old", "_new"),
        indicator=True,
    )

    merged["ChangeType"] = ""
    merged.loc[merged["_merge"] == "left_only", "ChangeType"] = "deleted"
    merged.loc[merged["_merge"] == "right_only", "ChangeType"] = "new"

    both_mask = merged["_merge"] == "both"
    promo_changed = (
        merged["PromoCode_old"].fillna("").astype(str).str.strip()
        != merged["PromoCode_new"].fillna("").astype(str).str.strip()
    )
    merged.loc[both_mask & promo_changed, "ChangeType"] = "changed"

    changes = merged[merged["ChangeType"] != ""].copy().sort_values(
        by=["PromoScopeKey"],
        ascending=[True],
    )

    change_rows = []
    for _, row in changes.iterrows():
        change_rows.append({
            "PromoScopeKey": row["PromoScopeKey"],
            "ChangeType": row["ChangeType"],
            "old": {
                "PromoCode": row.get("PromoCode_old"),
            },
            "new": {
                "PromoCode": row.get("PromoCode_new"),
            },
        })

    summary = {
        "total_changes": int(len(change_rows)),
        "new": int((changes["ChangeType"] == "new").sum()) if not changes.empty else 0,
        "deleted": int((changes["ChangeType"] == "deleted").sum()) if not changes.empty else 0,
        "changed": int((changes["ChangeType"] == "changed").sum()) if not changes.empty else 0,
    }

    return {
        "summary": summary,
        "changes": change_rows,
    }


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


def history_contexts(paths: PipelineFiles = FILES) -> list[tuple[str, Path]]:
    contexts: list[tuple[str, Path]] = []
    for currency in CURRENCIES:
        history_dir = paths.editor_history_dir(paths.editor_exports_dir, currency)
        if len(list(history_dir.glob(HT_PATTERN))) >= 2:
            contexts.append((currency, history_dir))

    if contexts:
        return contexts

    return [(DEFAULT_CURRENCY, paths.editor_exports_dir / "history")]


def run_context(currency: str, history_dir: Path, paths: PipelineFiles = FILES) -> None:
    ht_previous_file, ht_current_file = load_two_latest_files(str(history_dir), HT_PATTERN)
    promo_previous_file, promo_current_file = load_two_latest_files(str(history_dir), PROMO_PATTERN)

    ht_previous_df = pd.read_csv(ht_previous_file)
    ht_current_df = pd.read_csv(ht_current_file)

    ht_changes = build_ht_change_report(ht_previous_df, ht_current_df)
    ht_summary = build_ht_country_summary(ht_changes)

    promo_previous_data = load_promo_json(promo_previous_file)
    promo_current_data = load_promo_json(promo_current_file)
    promo_report = build_promo_change_report(promo_previous_data, promo_current_data)

    ht_prev_name = Path(ht_previous_file).stem.replace("HT_prices_", "")
    ht_curr_name = Path(ht_current_file).stem.replace("HT_prices_", "")
    promo_prev_name = Path(promo_previous_file).stem.replace("promos_", "")
    promo_curr_name = Path(promo_current_file).stem.replace("promos_", "")

    output_dir = paths.amdocs_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    suffix = f"_{currency}" if currency else ""
    ht_diff_file = os.path.join(
        output_dir,
        f"HT_prices_diff{suffix}_{ht_prev_name}_vs_{ht_curr_name}.csv",
    )
    promo_diff_file = os.path.join(
        output_dir,
        f"promos_diff{suffix}_{promo_prev_name}_vs_{promo_curr_name}.json",
    )

    ht_changes.to_csv(ht_diff_file, index=False)

    promo_payload = {
        "previous_file": promo_previous_file,
        "current_file": promo_current_file,
        "summary": promo_report["summary"],
        "changes": promo_report["changes"],
    }
    with open(promo_diff_file, "w", encoding="utf-8") as f:
        json.dump(promo_payload, f, indent=2, ensure_ascii=False)

    print(f"\nCurrency: {currency}")
    print(f"Previous HT export: {ht_previous_file}")
    print(f"Current HT export: {ht_current_file}")
    print(f"HT diff file: {ht_diff_file}")
    print(f"Total HT changed rows: {len(ht_changes)}")

    print_pipe_table(ht_summary, "HT COUNTRY SUMMARY")

    print(f"\nPrevious promos export: {promo_previous_file}")
    print(f"Current promos export: {promo_current_file}")
    print(f"Promos diff file: {promo_diff_file}")
    print("PROMO SUMMARY")
    print(json.dumps(promo_report["summary"], indent=2))


def main(paths: PipelineFiles = FILES):
    for currency, history_dir in history_contexts(paths):
        run_context(currency, history_dir, paths)


if __name__ == "__main__":
    main()
