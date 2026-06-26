# T Global Robopricing

Automated competitor scraping, market cleaning, USD/EUR pricing proposal generation, and manual price editing.

## Normal Weekly Flow

1. GitHub Actions runs `python automation/pricing_pipeline.py --scrape` every Monday.
2. Download the `weekly-proposal-pack` artifact from GitHub.
3. Open the `START_HERE` folder and double-click `1 Import weekly proposal pack.cmd`.

Choose the downloaded `weekly-proposal-pack.zip` when the file picker opens.

The importer prints which scrapers worked or failed, compares the incoming combined scrape with your current local combined scrape, and saves fresh change reports in `workable_data/diffs/`.

4. In `START_HERE`, double-click `2 Open pricing editor.cmd`.

5. Review proposals, adjust prices/promos, and export final USD/EUR prices.

The import helper updates scrape/proposal files and history, but does not touch manual editor exports or autosaves.

For normal use, the only files you need to launch manually are the two files in `START_HERE`. The other Python and workflow files are internal automation.

## Local Proposal Run

Reuse existing scraper current files:

```powershell
python automation/pricing_pipeline.py
```

Run fresh scraping locally:

```powershell
python automation/pricing_pipeline.py --scrape
```

Run fresh scraping and open the editor:

```powershell
python automation/pricing_pipeline.py --scrape --open-editor
```

## Important Folders

- `inputs/`: pricing units, promo catalog, wholesale PPG files, scraper reference files.
- `outputs/`: scraper current/previous CSVs.
- `workable_data/combined_scrapped_data_latest.csv`: combined competition snapshot.
- `workable_data/diffs/`: competitor price-change reports.
- `workable_data/market_prices_annotated.csv`: outlier-annotated market data.
- `workable_data/USD/` and `workable_data/EUR/`: generated model proposals.
- `workable_data/exports/USD/` and `workable_data/exports/EUR/`: final manual exports from the editor.
