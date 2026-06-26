# T Global Robopricing

Automated competitor scraping, market cleaning, USD/EUR pricing proposal generation, and manual price editing.

## Normal Weekly Flow

1. GitHub Actions runs `python pricing_pipeline.py --scrape` every Monday.
2. Download the `weekly-proposal-pack` artifact from GitHub.
3. Extract it into this local repo folder.
4. Open the editor:

```powershell
python "fast pricing editor.py"
```

5. Review proposals, adjust prices/promos, and export final USD/EUR prices.

## Local Proposal Run

Reuse existing scraper current files:

```powershell
python pricing_pipeline.py
```

Run fresh scraping locally:

```powershell
python pricing_pipeline.py --scrape
```

Run fresh scraping and open the editor:

```powershell
python pricing_pipeline.py --scrape --open-editor
```

## Important Folders

- `inputs/`: pricing units, promo catalog, wholesale PPG files, scraper reference files.
- `outputs/`: scraper current/previous CSVs.
- `workable_data/combined_scrapped_data_latest.csv`: combined competition snapshot.
- `workable_data/diffs/`: competitor price-change reports.
- `workable_data/market_prices_annotated.csv`: outlier-annotated market data.
- `workable_data/USD/` and `workable_data/EUR/`: generated model proposals.
- `workable_data/exports/USD/` and `workable_data/exports/EUR/`: final manual exports from the editor.
