import numpy as np
import pandas as pd
import re

from config import (
    K_NEIGHBORS,
    MIN_NEIGHBORS_REQUIRED,
    GB_WEIGHT,
    DAYS_WEIGHT,
    MAX_DISTANCE,
    ROW_RATIO_THRESHOLD,
    MIN_MATCHED_OFFERS,
    PROVIDER_RATIO_THRESHOLD,
    USE_LOG_PRICE
)
from pipeline_files import FILES, PipelineFiles


# ---------------------------------
# Helpers
# ---------------------------------
def robust_reference_price(prices):
    prices = np.asarray(prices, dtype=float)
    prices = prices[np.isfinite(prices) & (prices > 0)]

    if len(prices) == 0:
        return np.nan

    if USE_LOG_PRICE:
        return float(np.exp(np.median(np.log(prices))))

    return float(np.median(prices))


def keep_only_number(value):
    if pd.isna(value):
        return np.nan
    text = str(value).strip()
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return np.nan
    return float(m.group(1).replace(",", "."))


def compute_distance(row, candidates):
    """
    Weighted distance in log(GB), log(Days) space.
    """
    row_log_gb = np.log(float(row["GB"]))
    row_log_days = np.log(float(row["Days"]))

    cand_log_gb = np.log(candidates["GB"].astype(float).to_numpy())
    cand_log_days = np.log(candidates["Days"].astype(float).to_numpy())

    d_gb = cand_log_gb - row_log_gb
    d_days = cand_log_days - row_log_days

    dist = np.sqrt(GB_WEIGHT * d_gb**2 + DAYS_WEIGHT * d_days**2)
    return dist


def evaluate_iso_group(iso_df):
    """
    Evaluate one ISO3 market.
    Returns row-level market ratios.
    """
    iso_df = iso_df.copy().reset_index(drop=True)

    # Prepare result columns
    iso_df["NeighborCount"] = 0
    iso_df["LocalRefPrice"] = np.nan
    iso_df["MarketRatio"] = np.nan
    iso_df["RowFlag"] = False
    iso_df["RowReason"] = "no_comparison"

    for i, row in iso_df.iterrows():
        # Compare only against OTHER providers
        candidates = iso_df[iso_df["Provider"] != row["Provider"]].copy()

        if candidates.empty:
            continue

        distances = compute_distance(row, candidates)
        candidates = candidates.assign(Distance=distances)

        # Keep only reasonably comparable offers
        candidates = candidates[candidates["Distance"] <= MAX_DISTANCE].copy()

        if candidates.empty:
            continue

        candidates = candidates.sort_values("Distance", ascending=True)
        nearest = candidates.head(K_NEIGHBORS)

        if len(nearest) < MIN_NEIGHBORS_REQUIRED:
            continue

        ref_price = robust_reference_price(nearest["Price"])

        if not np.isfinite(ref_price) or ref_price <= 0:
            continue

        ratio = float(row["Price"]) / ref_price

        iso_df.at[i, "NeighborCount"] = int(len(nearest))
        iso_df.at[i, "LocalRefPrice"] = ref_price
        iso_df.at[i, "MarketRatio"] = ratio
        iso_df.at[i, "RowFlag"] = bool(ratio > ROW_RATIO_THRESHOLD)
        iso_df.at[i, "RowReason"] = "far_from_local_market" if ratio > ROW_RATIO_THRESHOLD else "ok"

    return iso_df


def clean_market_data_sparse(df):
    required_cols = {"ISO3", "Provider", "Days", "GB", "Price"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    work = df.copy()
    work = work.dropna(subset=["ISO3", "Provider", "Days", "GB", "Price"]).copy()

    # Force numeric
    work["Days"] = work["Days"].apply(keep_only_number)
    work["GB"] = work["GB"].apply(keep_only_number)
    work["Price"] = work["Price"].apply(keep_only_number)

    # Only valid positive commercial values
    work = work.dropna(subset=["Days", "GB", "Price"]).copy()
    work = work[(work["Days"] > 0) & (work["GB"] > 0) & (work["Price"] > 0)].copy()

    annotated_parts = []

    for iso3, iso_df in work.groupby("ISO3", sort=False):
        reviewed = evaluate_iso_group(iso_df)

        # Optional provider summary, but no removal
        provider_stats = (
            reviewed.dropna(subset=["MarketRatio"])
            .groupby("Provider", as_index=False)
            .agg(
                MatchedOffers=("MarketRatio", "count"),
                MedianRatio=("MarketRatio", "median"),
                MaxRatio=("MarketRatio", "max"),
                FlaggedRows=("RowFlag", "sum")
            )
        )

        if provider_stats.empty:
            reviewed["MatchedOffers"] = 0
            reviewed["MedianRatio"] = np.nan
            reviewed["MaxRatio"] = np.nan
            reviewed["FlaggedRows"] = 0
        else:
            reviewed = reviewed.merge(
                provider_stats,
                on="Provider",
                how="left"
            )
            reviewed["MatchedOffers"] = reviewed["MatchedOffers"].fillna(0).astype(int)
            reviewed["FlaggedRows"] = reviewed["FlaggedRows"].fillna(0).astype(int)

        # Keep everything; only annotate
        reviewed["ProviderStatus"] = "Kept"
        reviewed["UseForPricing"] = True

        annotated_parts.append(reviewed)

    df_annotated = pd.concat(annotated_parts, ignore_index=True) if annotated_parts else pd.DataFrame()

    # Audit log = only flagged rows, not removed providers
    df_audit = df_annotated[df_annotated["RowFlag"]].copy()

    return df_annotated, df_audit


def main(paths: PipelineFiles = FILES):
    raw_df = pd.read_csv(paths.combined_latest)

    # -------------------------
    # DEBUG: find dropped rows
    # -------------------------
    raw_df["_original_row"] = raw_df.index

    work = raw_df.copy()

    mask_required_missing = work[["ISO3", "Provider", "Days", "GB", "Price"]].isna().any(axis=1)

    tmp = work.copy()
    tmp["Days_num"] = tmp["Days"].apply(keep_only_number)
    tmp["GB_num"] = tmp["GB"].apply(keep_only_number)
    tmp["Price_num"] = tmp["Price"].apply(keep_only_number)

    mask_numeric_invalid = (
        tmp[["Days_num", "GB_num", "Price_num"]].isna().any(axis=1)
        | (tmp["Days_num"] <= 0)
        | (tmp["GB_num"] <= 0)
        | (tmp["Price_num"] <= 0)
    )

    dropped_rows = tmp[mask_required_missing | mask_numeric_invalid].copy()

    tmp["DropReason"] = np.select(
        [
            mask_required_missing,
            mask_numeric_invalid
        ],
        [
            "missing_required_field",
            "invalid_or_non_positive_numeric_value"
        ],
        default="kept"
    )

    dropped_rows = tmp[tmp["DropReason"] != "kept"].copy()

    paths.dropped_rows_debug.parent.mkdir(parents=True, exist_ok=True)
    dropped_rows.to_csv(paths.dropped_rows_debug, index=False)

    print(f"Dropped {len(dropped_rows)} rows before annotation.")

    # -------------------------
    # Your original pipeline
    # -------------------------
    df_annotated, df_audit = clean_market_data_sparse(raw_df)

    paths.market_annotated.parent.mkdir(parents=True, exist_ok=True)
    df_annotated.to_csv(paths.market_annotated, index=False)
    df_audit.to_csv(paths.market_outlier_audit, index=False)

    print(f"Done. Annotated {len(df_audit)} outlier rows. No providers were removed.")


if __name__ == "__main__":
    main()
