import os
import time
from pathlib import Path
import pandas as pd

from pricing_preparation import (
    prepare_market_data,
    prepare_ppg_data,
    build_market_with_ht_costs,
    get_eur_to_usd,
)
from pricing_model import (
    load_pricing_units,
    fit_global_competition_surface,
    build_ht_prices,
    check_cross_package_monotonicity,
)


from config import CHOSEN_STRATEGY
try:
    from config import BATCH_CURRENCIES
except Exception:
    BATCH_CURRENCIES = ("USD", "EUR")

from currency_support import CURRENCIES, DEFAULT_CURRENCY, normalize_currency
from pipeline_files import FILES, PipelineFiles


def canonicalize_country_from_iso(
    df: pd.DataFrame,
    iso_to_country: dict[str, str],
) -> pd.DataFrame:
    df = df.copy()
    df["ISO"] = df["ISO"].astype(str).str.strip().str.upper()
    df["Country"] = df["ISO"].map(iso_to_country).fillna(df.get("Country", pd.NA))
    return df


def build_iso_country_map(df: pd.DataFrame) -> dict[str, str]:
    work = df.copy()
    work["ISO"] = work["ISO"].astype(str).str.strip().str.upper()
    work["Country"] = work["Country"].astype(str).str.strip()

    work = work[
        work["ISO"].notna() &
        work["Country"].notna() &
        work["ISO"].ne("") &
        work["Country"].ne("") &
        work["Country"].ne("nan")
    ].copy()

    if work.empty:
        return {}

    work["_country_len"] = work["Country"].str.len()

    mapping = (
        work.sort_values(["ISO", "_country_len", "Country"], ascending=[True, False, True])
        .drop_duplicates(subset=["ISO"], keep="first")
        .set_index("ISO")["Country"]
        .to_dict()
    )

    return mapping


def configured_batch_currencies() -> list[str]:
    out: list[str] = []
    for currency in BATCH_CURRENCIES:
        normalized = normalize_currency(currency)
        if normalized in CURRENCIES and normalized not in out:
            out.append(normalized)
    return out or [DEFAULT_CURRENCY]


def bool_mask(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)
    return (
        series.fillna(False)
        .astype(str)
        .str.strip()
        .str.lower()
        .isin({"true", "1", "yes", "y"})
    )


def run_batch_pricing_for_currency(
    *,
    paths: PipelineFiles,
    currency: str,
    market_file: Path,
    ppg_df: pd.DataFrame,
    pricing_units_df: pd.DataFrame,
    eur_to_usd: float,
) -> None:
    currency = normalize_currency(currency)
    ht_latest_csv = paths.model_latest(currency)
    ht_history_csv = paths.model_history(currency)
    failed_countries_latest_csv = paths.model_failed_countries(currency)

    for filepath in [
        ht_latest_csv,
        ht_history_csv,
        failed_countries_latest_csv,
    ]:
        filepath.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Pricing currency: {currency} ===")

    try:
        market_df_full = prepare_market_data(
            str(market_file),
            currency=currency,
            eur_to_usd=eur_to_usd,
        )
        iso_to_country = build_iso_country_map(market_df_full)
    except Exception as e:
        print(f"Error preparing {currency} market data: {e}")
        return

    if "UseForPricing" not in market_df_full.columns:
        print("Error: 'UseForPricing' column not found in annotated market data.")
        return

    market_df_pricing = market_df_full[bool_mask(market_df_full["UseForPricing"])].copy()

    if "ISO" not in ppg_df.columns:
        print("Error: 'ISO' column not found in prepared PPG data.")
        return

    try:
        global_coef, global_feature_means, n_global_rows = fit_global_competition_surface(market_df_pricing)
        print(f"Global {currency} competition surface fitted on {n_global_rows} rows.")
    except Exception as e:
        print(f"Error fitting {currency} global competition surface: {e}")
        return

    countries = (
        ppg_df["ISO"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .replace("", pd.NA)
        .dropna()
        .unique()
        .tolist()
    )

    print(f"Found {len(countries)} countries to process from PPG file.")

    all_country_dfs = []
    failed_countries = []
    all_monotonicity_issues = []

    for country in countries:

        try:
            df_with_costs = build_market_with_ht_costs(
                market_df_full,
                country,
                ppg_df,
                eur_to_usd,
                currency=currency,
            )

            if df_with_costs.empty:
                raise ValueError(f"build_market_with_ht_costs returned no rows for {country}.")

            df_priced = build_ht_prices(
                df=df_with_costs,
                market_df=market_df_pricing,
                pricing_units_df=pricing_units_df,
                global_coef=global_coef,
                global_feature_means=global_feature_means,
                cost_multiplier=1.25,
                strategy=CHOSEN_STRATEGY,
            )
            df_priced["Currency"] = currency
            df_priced[f"Price_{currency}"] = df_priced["Price"]

            issues = check_cross_package_monotonicity(df_priced)

            if issues.empty:
                print(f"  No cross-package monotonicity issues for {country}")
            else:
                print(f"  Cross-package monotonicity violations found for {country}")

                issues = issues.copy()
                issues["PriceDrop"] = issues["PrevPrice"] - issues["CurrPrice"]
                all_monotonicity_issues.append(issues)

                summary = (
                    issues.groupby(["PrevPlan", "CurrPlan"])
                    .agg(
                        Violations=("ISO3", "count"),
                        AvgDrop=("PriceDrop", "mean"),
                        MaxDrop=("PriceDrop", "max"),
                        MinDrop=("PriceDrop", "min"),
                    )
                    .reset_index()
                    .sort_values(["Violations", "MaxDrop"], ascending=[False, False])
                )

                print("\n  Summary by package transition:")
                print(summary.to_string(index=False))

                day_summary = (
                    issues.groupby("Days")
                    .agg(
                        Violations=("ISO3", "count"),
                        AvgDrop=("PriceDrop", "mean"),
                        MaxDrop=("PriceDrop", "max"),
                    )
                    .reset_index()
                    .sort_values("Days")
                )

                print("\n  Summary by duration (Days):")
                print(day_summary.to_string(index=False))

                worst = (
                    issues.sort_values("PriceDrop", ascending=False)
                    .head(5)
                    .copy()
                )

                print("\n  Top 5 worst violations:")
                print(
                    worst[
                        [
                            "ISO3",
                            "Country",
                            "Days",
                            "PrevPlan",
                            "PrevGB",
                            "PrevPrice",
                            "CurrPlan",
                            "CurrGB",
                            "CurrPrice",
                            "PriceDrop",
                        ]
                    ].to_string(index=False)
                )

            all_country_dfs.append(df_priced)

        except Exception as e:
            failed_countries.append({"ISO": country, "Currency": currency, "Error": str(e)})
            print(f"  Warning: Error processing {country}: {e}")

    if failed_countries:
        print("\nCountries skipped due to errors:")
        for row in failed_countries:
            print(f"  - {row['ISO']} ({currency}): {row['Error']}")
    if all_monotonicity_issues:
        all_issues_df = pd.concat(all_monotonicity_issues, ignore_index=True)

        global_summary = (
            all_issues_df.groupby(["ISO", "Country", "PrevPlan", "CurrPlan"])
            .agg(
                Violations=("ISO3", "count"),
                AvgDrop=("PriceDrop", "mean"),
                MaxDrop=("PriceDrop", "max"),
            )
            .reset_index()
            .sort_values(["Violations", "MaxDrop"], ascending=[False, False])
        )

        print("\nGlobal monotonicity summary:")
        print(global_summary.to_string(index=False))

    if not all_country_dfs:
        print(f"No {currency} data was generated. Exiting.")
        return

    final_df = pd.concat(all_country_dfs, ignore_index=True, sort=False)

    if "ISO" not in final_df.columns:
        raise ValueError("final_df is missing required column 'ISO'.")

    final_df["ISO"] = final_df["ISO"].astype(str).str.strip().str.upper()
    final_df = canonicalize_country_from_iso(final_df, iso_to_country)

    if {"PricingUnitIdUsed", "Plan", "Days"}.issubset(final_df.columns):
        final_df["SKU"] = (
            final_df["PricingUnitIdUsed"].astype(str).str.strip().str.upper() + "_" +
            final_df["Plan"].astype(str).str.strip().str.upper() + "_" +
            final_df["Days"].fillna(0).astype(int).astype(str)
        )

    ht_df = final_df.loc[final_df["Provider"].astype(str).str.strip().str.upper() == "HT"].copy()

    columns_to_keep = [
        "Provider",
        "ISO",
        "Country",
        "URL",
        "Plan",
        "GB",
        "Days",
        "Price",
        "Currency",
        f"Price_{currency}",
        "ISO3",
        "Type",
        "Cost",
        "Cost_Floor_Reference",
        "SurfaceModeUsed",
        "PricingSourceUsed",
        "PricingUnitIdUsed",
        "PricingRegionUsed",
        "PricingUnitCountriesUsed",
        "IsBelowCostFloor",
        "Cost_Floor_Gap",
        "sku_scope_key",
        "SKU",
        "OverridePrice",
        "PromoScopeKey",
        "PromoCode",
        "PromoType",
        "PromoValue",
        "PromoLabel",
        "PromoBasePrice",
        "FinalPriceAfterPromo",
        "CompetitorTargetPricePerGB",
        "CompetitorMinPricePerGB",
        "HTPricePerGB",
        "EUR_TO_USD",
    ]

    ht_df = ht_df[[c for c in columns_to_keep if c in ht_df.columns]]

    if ht_df.empty:
        print(f"No {currency} HT rows were generated. Exiting.")
        return

    failed_countries_df = pd.DataFrame(failed_countries)

    print(f"Saving {currency} HT-only operational outputs...")
    try:
        ht_df.to_csv(str(ht_latest_csv), index=False)
        ht_df.to_csv(str(ht_history_csv), index=False)

        if not failed_countries_df.empty:
            failed_countries_df.to_csv(str(failed_countries_latest_csv), index=False)
        elif os.path.exists(failed_countries_latest_csv):
            os.remove(failed_countries_latest_csv)

        print(f"Saved {currency} HT latest CSV: {ht_latest_csv}")
        print(f"Saved {currency} HT history CSV: {ht_history_csv}")

    except Exception as e:
        print(f"Warning: Error saving {currency} CSV outputs: {e}")
        return


def run_batch_pricing(paths: PipelineFiles = FILES):
    # Avoid .resolve() on OneDrive as it can return reparse point paths
    # (like \\?\C:\...) that many libraries fail to read, causing Errno 22.
    base_dir = paths.base_dir

    market_file = paths.market_annotated
    ppg_file = paths.ppg_xlsx
    pricing_units_file = paths.pricing_units_json

    if not os.path.exists(ppg_file):
        print(f"Error: Wholesale cost file '{ppg_file}' not found.")
        return

    if not os.path.exists(market_file):
        print(f"Error: Market data file '{market_file}' not found.")
        return

    if not os.path.exists(pricing_units_file):
        print(f"Error: Pricing units file '{pricing_units_file}' not found.")
        return

    def hydrate_onedrive_file(p: Path):
        """Forces OneDrive to finish downloading the file before we try to process it."""
        if p.exists() and p.is_file():
            try:
                with open(p, "rb") as f:
                    f.read(1)
            except Exception:
                pass

    print("Loading and preparing input files...")
    try:
        for f in [market_file, ppg_file, pricing_units_file]:
            hydrate_onedrive_file(f)

        ppg_df = prepare_ppg_data(str(ppg_file))
        pricing_units_df = load_pricing_units(str(pricing_units_file))
        eur_to_usd = get_eur_to_usd()
        print(f"EUR/USD = {eur_to_usd:.4f}")

    except Exception as e:
        error_msg = str(e)
        if "[Errno 22]" in error_msg:
            print(f"Error preparing input data: {error_msg}")
            print("TIP: Check if WS_PPG.xlsx is open in Excel. Please close it and try again.")
        else:
            print(f"Error preparing input data: {e}")
        return

    for currency in configured_batch_currencies():
        run_batch_pricing_for_currency(
            paths=paths,
            currency=currency,
            market_file=market_file,
            ppg_df=ppg_df,
            pricing_units_df=pricing_units_df,
            eur_to_usd=eur_to_usd,
        )


def main():
    start_time = time.perf_counter()
    run_batch_pricing()
    end_time = time.perf_counter()
    print(f"\nTotal execution time: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    main()  
