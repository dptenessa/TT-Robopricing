# PySide6 Pricing Curve Editor

This is a simplified desktop refactor of the Dash pricing editor.

## What changed
- One **baseline/model** curve
- One **working** curve
- Drag points directly on a canvas
- Optional neighbor sculpting while dragging
- Load a prior session to continue editing
- Load a promo catalog and assign promos from a list
- Accept the current working curve as the new baseline

## Files
- `app.py` — launcher
- `pricing_editor/state.py` — data loading, editor state, promo/session/export logic
- `pricing_editor/canvas.py` — direct-manipulation curve canvas
- `pricing_editor/main_window.py` — desktop UI shell

## Install
```bash
pip install PySide6 pandas numpy openpyxl
```

## Run
```bash
python app.py
```

## Suggested workflow
1. Run `python pricing_pipeline.py` to prepare the latest proposal from existing scraper outputs.
2. Or run `python pricing_pipeline.py --scrape --open-editor` for the weekly scrape-to-editor flow.
3. Review the USD/EUR proposal against the last export/session loaded from `workable_data/exports`.
4. Drag points to sculpt the working curve.
5. Select a point and apply/remove shared promotions.
6. Save. The editor writes USD and EUR last-export files and timestamped history.

## File layout
- Model proposals: `workable_data/USD/ht_prices_latest.csv` and `workable_data/EUR/ht_prices_latest.csv`
- Last manual exports: `workable_data/exports/USD/HT_prices_last_export.csv` and `workable_data/exports/EUR/HT_prices_last_export.csv`
- Shared promo exports: `workable_data/exports/USD/promos_last_export.json` and `workable_data/exports/EUR/promos_last_export.json`
- Compatibility USD export: `workable_data/exports/HT_prices_last_export.csv`

## Notes
- This first version is intentionally simpler than the Dash app.
- It keeps the “baseline vs working curve” concept and removes most proposal-state branching.
- The canvas is optimized for direct point editing rather than Plotly trace clicking.


## Latest changes
- Competitors shown as points only
- HT Basic, Moderate, Unlimited shown together
- Added legend for HT packages, baseline, and competitors
- Added drag mode selector for sculpt vs single-point move

- Selected-point metadata panel now includes pricing unit/source/region/countries
- Promo options can be clicked directly on the chart
- Added rotate-left / rotate-right / rotate-both drag modes
- HT promo points now use orange fill instead of triangle markers
- Competitors use distinct shapes; unlimited points get green contour
- Dot darkness now reflects GB amount again

- Performance pass: lighter visuals, restored faster interaction
- Country scope info restored at top left
- Market prices loading/display path fixed
- Baseline/model promos now read from loaded HT/model file
- Styling simplified: grey lines/markers, green only for unlimited, red fill gradient by GB

- Removed save-session UI to simplify the app
- Cached country/competitor data and made drag refresh lighter
- Competitors now use direct country cache/fallback path for visibility

- phase 6: preload-once runtime model
- removed previous-session loading
- removed 'accept working as baseline'
- added 'use baseline as working curve'
- export is now the only place that rebuilds dataframe output

- phase 7: pricing-unit scope edits/promos
- added competitor names back to legend
- added hover info in status bar for HT and competitors
- added wheel zoom and double-click reset zoom

- phase 8: replaced rotate with photoshop-like range brush
- set brush start / end on selected points, then drag inside range to bend smoothly
- kept move selected point and local sculpt as additional tools

- phase 9: added hover tooltips, curvature-at-point tool, rotate left/right/both, collapsible left panel, fixed legend mapping

- phase 11: simpler hover text, airalo square, whole-curve inflate/deflate tool, stronger panel collapse behavior
