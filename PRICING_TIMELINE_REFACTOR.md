# Shared pricing timeline

## Target design

The development database at `outputs/promo_planner/promo_schedule.db` is the shared source of truth for Pricing Editor and Promo Planner. SQLite stores one imported pricing baseline and compact dated field changes. The workbook is a compatibility import/export for Excel users and downstream tools; it is not the edit history.

Each change set has an effective date, a layer, a source, and an optional expiry. Projection orders changes chronologically, then applies campaign and override priority on the same date. When an override expires, the projection reveals the next active value underneath it. Undo marks a change set reversed and preserves its audit record.

## Implemented in the development worktree

- Use the SQLite timeline as the normal startup source for both interfaces. Read the current workbook only for a one-time migration when the database has no baseline.
- Record editor saves as changed editable fields instead of writing a full pricebook history snapshot on each save.
- Save key-date price and promo changes in the same timeline, with optional expiry tied to the promo end date.
- Preview campaign and dated changes from in-memory projections; date navigation no longer rewrites or reloads the full XLSX.
- Record Promo Planner applies as campaign-layer changesets and undo them in the database. Normal planner execution no longer copies and rewrites the full workbook or creates another full history snapshot.
- Make external workbook imports explicit: review the changed field and pricing-point counts, then accept the import as a dated change. Changes to the set of pricing points are stopped for a separate catalogue migration.
- Keep Excel import/export explicit. Imports show a field and pricing-point diff and require approval before changes enter the timeline. Exports materialize a workbook only when requested and create a dated CSV recovery point; the official partner-pack flow still uses the same workbook export path.
- Build Promo Planner catalogues, previews and calendar prices from the database projection rather than reopening the workbook.
- Keep runtime databases, workbooks, and other copied business data local to the dev worktree and excluded from Git.

## Compatibility and remaining work

- The Pricing Editor is the persistent workspace. One top timeline selector drives both the price view and the selected-date campaign controls. A right-side campaign workspace contains selected-date review, campaign browsing with affected-destination navigation, and a compact New promotion form. Applying or undoing a campaign handover refreshes the open price view from the same database.
- Legacy JSON and campaign-keyed date overrides are imported into SQLite once and left intact as rollback references.
- Existing full history files are not deleted. They remain available for historical comparison while the timeline projection becomes authoritative for new edits.
- Before a production rollout, compare an exported workbook with the source workbook by SKU identity, editable values, row count, and downstream partner-pack output. Production remains untouched until that migration is reviewed and explicitly promoted.

## Operating rules

- Run both interfaces from this development worktree while developing; the `outputs/.development_workspace` marker must remain in place.
- Do not copy development outputs back to production.
- Use the editor and planner for normal price and promo edits. Use Excel import only for intentional bulk changes, after reviewing the diff. Excel export is optional except when requested for a partner pack or downstream handoff.
- Use the timeline undo action for a mistaken planner execution; do not restore an old workbook snapshot over newer timeline edits.
