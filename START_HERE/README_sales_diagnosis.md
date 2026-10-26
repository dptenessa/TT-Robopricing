# Sales diagnosis in Market Insights

1. Extract the update into TT-Robopricing and replace the existing sales_evolution.py. Keep the two new files beside it in automation.
2. Run START_HERE/4_open_market_insights.py and regenerate the report in the pipeline panel. Open the newly generated report; refreshing an older HTML file does not regenerate it.
3. In tab 2, choose Buyer origin, the date the offer changed, and the number of post-change days to examine. Leave Destination on All destinations for the full market picture.

## What to read

- Total daily sales: all completed pass units, with the offer-change marker. Blue/pink bands mark the baseline and after periods. Hover or tap a point for exact units.
- Sales bridge: before units minus losses across products plus gains across products equals after units.
- Product lists and searchable table: identify which destination/package/duration products stopped selling, which grew, and whether gains compensated for losses. Sales that cannot be matched to a package/duration remain in an explicitly labelled category so totals reconcile.
- Offer evidence: published database prices and promo codes at the comparison endpoints, in EUR or USD. These are not transaction revenue. Pricing gaps, ambiguous matches and changes inside the comparison windows are flagged.

Comparisons use equal numbers of days with matching weekdays. Today is excluded; early comparisons use only the available full post-change days. The header states the exact dates. Make sure the sales export covers the end of the last usable day. Missing history is not treated as zero.

The suggested change date comes from recent published price changes where available, otherwise the latest Monday. Select the date of the offer change you are investigating.

A product sold zero units after a change can still appear as a major loss. A newly selling product is one with zero observed sales in the baseline, not proof of a new catalogue launch. Pricing changes coinciding with lower sales are evidence to investigate; this report does not establish causation or measure advertising traffic, stock/availability, checkout failures or statistical significance.

The update reads the pricing database without modifying it. The old report generator and development test scripts are not part of this package.
