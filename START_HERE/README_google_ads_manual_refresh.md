# Google Ads: simple manual refresh

Keep the updated `automation/intelligence/import_google_ads_export.py` in place. No restore is needed.

1. **Open Google Ads → Tools → Planning → Keyword Planner → Get search volume and forecasts.**
2. **Choose ONE origin country**: Spain, Germany, United States, France or Italy. Use Google Search and the last 12 complete months. Keep the Google Ads interface in English so the download has compatible column names.
3. **Paste the matching keyword list** from `START_HERE/google_ads_keywords/` (see table below). Get the results.
4. **Download the historical metrics CSV, including monthly search volumes.** Save it anywhere, including Downloads, and rename it as shown below. Repeat for the other countries you want to refresh.
5. **Open `START_HERE/5_import_google_ads.py` in VS Code and press Play.** Select the CSV downloads you just made. You can select one file or several together. It imports them into BI and saves dated copies automatically.
6. **Open the Market Insights launcher. Refresh Sales and route opportunities, then Market Insights.**

| Google location | Paste all lines from | Rename the downloaded results to |
|---|---|---|
| Spain | `keywords_ES.txt` — Spanish | `google_ads_ES.csv` |
| Germany | `keywords_DE.txt` — German | `google_ads_DE.csv` |
| United States | `keywords_US.txt` — English | `google_ads_US.csv` |
| France | `keywords_FR.txt` — French | `google_ads_FR.csv` |
| Italy | `keywords_IT.txt` — Italian | `google_ads_IT.csv` |

**Example:** for Germany, set the Google location to Germany and paste `keywords_DE.txt`. The countries named in the keywords are the travel destinations.

The Google interface stays in English; the keyword text is in the local language. The country location is the source market, not the destination.

Choose historical monthly searches, rather than a forecast of clicks or costs. On 22 October 2026, a full-year example is October 2025–September 2026, if those months are available. Use the same historical window for the markets refreshed together.

The importer should show a positive imported row count and 12 monthly columns. If Google shows search-volume ranges instead of numbers, or the import reports an error, keep the file and check that message before continuing.

This is the manual download procedure. It does not need Google Ads API access. Only select fresh downloads: selecting an old file would label that data as collected today.

Official Google guide: https://support.google.com/google-ads/answer/7337243?hl=en
