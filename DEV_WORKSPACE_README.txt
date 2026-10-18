TT-Robopricing development worktree

This folder is the development copy. The sibling TT-Robopricing folder remains production on branch main.
This worktree uses branch refactor/temporal-pricing-store.

The copied price book, planner database, market data, scrape data, recommendations, and sales master are local development copies. The development profile marker is outputs/.development_workspace. Keep that marker in place: it makes path resolution prefer this worktree's local files and ignore inherited production sales or database paths.

Development sales data is in local_data/T-Travel_sales_master.csv. It is ignored by Git.
Generated data under outputs/, scrapes/, and automation/intelligence/*.db is also local runtime data.

The development copy was seeded from production on 2026-10-17. Refresh data only by copying from production into this folder; never copy development outputs back into production.
