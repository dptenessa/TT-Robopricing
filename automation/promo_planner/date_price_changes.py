from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .engine import preview_batch
from .store import Campaign, PromoStore
try:
    from automation.pricing_timeline import PricingTimeline, row_key_for_identity
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from pricing_timeline import PricingTimeline, row_key_for_identity


def schedule_path(root: Path) -> Path:
    return Path(root) / "outputs" / "promo_planner" / "date_price_changes.json"


def issue_choices_path(root: Path) -> Path:
    return Path(root) / "outputs" / "promo_planner" / "issue_choices"


def load_schedule(root: Path) -> list[dict]:
    db_path = Path(root) / "outputs" / "promo_planner" / "promo_schedule.db"
    PromoStore(db_path)  # imports the legacy JSON schedule once, if present
    timeline = PricingTimeline(db_path)
    timeline.migrate_legacy_date_overrides()
    return timeline.date_override_records()


def active_changes(root: Path, boundary: str, *, exclude_key_date: str | None = None) -> list[dict]:
    rows = [
        item for item in load_schedule(root)
        if item.get("key_date") != exclude_key_date
        and str(item.get("effective_from", "")) <= boundary
        and (not item.get("expires_at") or boundary < str(item["expires_at"]))
    ]
    return sorted(rows, key=lambda item: (str(item.get("effective_from", "")), int(item.get("campaign_id") or 0)))


def key_dates(
    campaigns: list[Campaign],
    *,
    store: PromoStore | None = None,
    today: date | None = None,
) -> list[str]:
    today = today or date.today()
    dates = set()
    for campaign in campaigns:
        started = store.has_execution(campaign.id, "START") if store else False
        ended = store.has_execution(campaign.id, "END") if store else False
        imported = campaign.status == "ACTIVE_IMPORTED"
        if campaign.start_date and not started and not imported:
            try:
                key_date = date.fromisoformat(campaign.start_date) - timedelta(days=1)
                if key_date >= today - timedelta(days=1):
                    dates.add(key_date.isoformat())
            except ValueError:
                pass
        if campaign.end_date and not ended:
            try:
                end = date.fromisoformat(campaign.end_date)
                if end >= today - timedelta(days=1):
                    dates.add(end.isoformat())
            except ValueError:
                pass
    if store:
        timeline = PricingTimeline(store.path)
        timeline.migrate_legacy_date_overrides()
        dates.update(timeline.key_dates(today=(today - timedelta(days=1)).isoformat()))
    return sorted(dates)


def _row_identity(row) -> tuple:
    def text(value):
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    def number(value):
        parsed = pd.to_numeric(value, errors="coerce")
        if pd.isna(parsed):
            return ""
        return round(float(parsed), 6)

    return (
        text(row.get("ISO", "")).upper(),
        text(row.get("Country", "")).casefold(),
        text(row.get("PricingUnitIdUsed", "")),
        text(row.get("Plan", "")),
        number(row.get("Days", "")),
        number(row.get("GB", "")),
    )


def _identity_json(row) -> dict:
    def text(value):
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    def number(value, default=0):
        parsed = pd.to_numeric(value, errors="coerce")
        return default if pd.isna(parsed) else float(parsed)

    return {
        "iso": text(row.get("ISO", "")).upper(),
        "country": text(row.get("Country", "")),
        "pricing_unit_id": text(row.get("PricingUnitIdUsed", "")),
        "plan": text(row.get("Plan", "")),
        "days": int(number(row.get("Days", 0))),
        "gb": number(row.get("GB", 0)),
    }


def project_dataframe(
    current_df: pd.DataFrame,
    workbook: Path,
    store: PromoStore,
    key_date: str,
    root: Path,
    *,
    exclude_key_date_changes: bool = False,
) -> tuple[pd.DataFrame, object]:
    """Project the current workbook through the selected midnight boundary."""
    boundary = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    campaigns = store.list_campaigns()
    actions = []
    for campaign in campaigns:
        started = store.has_execution(campaign.id, "START")
        ended = store.has_execution(campaign.id, "END")
        imported = campaign.status == "ACTIVE_IMPORTED"
        starts_by_boundary = bool(
            not started and not imported and campaign.start_date
            and date.fromisoformat(campaign.start_date) <= date.fromisoformat(boundary)
        )
        if starts_by_boundary:
            actions.append((campaign, "START"))
        ends_by_boundary = bool(
            not ended and campaign.end_date
            and date.fromisoformat(campaign.end_date) < date.fromisoformat(boundary)
            and (started or imported or starts_by_boundary)
        )
        if ends_by_boundary:
            actions.append((campaign, "END"))

    cost_choices = {}
    conflict_choices = {}
    choices_dir = issue_choices_path(root)
    if choices_dir.exists():
        for path in sorted(choices_dir.glob("*.json")):
            if path.stem > key_date:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for record in data.get("cost_floor", []):
                if len(record) == 6:
                    cid, iso, plan, days, currency, choice = record
                    cost_choices[(int(cid), str(iso).upper(), str(plan), int(days), str(currency).upper())] = str(choice)
            for record in data.get("conflicts", []):
                if len(record) == 6:
                    cid, iso, plan, days, _current_code, choice = record
                    conflict_choices[(int(cid), str(iso).upper(), str(plan), int(days))] = str(choice)

    date_prices = active_changes(
        root,
        boundary,
        exclude_key_date=key_date if exclude_key_date_changes else None,
    )
    preview = preview_batch(
        current_df,
        actions,
        key_date,
        cost_floor_choices=cost_choices,
        conflict_choices=conflict_choices,
        date_price_changes=date_prices,
        campaigns=campaigns,
    )

    projected = current_df.copy().reset_index(drop=True)
    queues = defaultdict(deque)
    for row in preview.projected_rows:
        queues[_row_identity(row)].append(row)
    passthrough = (
        "PromoCode", "PromoType", "PromoValue", "PromoLabel", "Price_EUR", "Price_USD",
        "AllowBelowCost", "_PromoPlannerPromoStack", "_PromoPlannerPromoOwner",
        "_PromoPlannerOriginalPrice_EUR", "_PromoPlannerOriginalPrice_USD", "_PromoPlannerPriceOwner",
    )
    for index, row in projected.iterrows():
        identity = _row_identity(row)
        if not queues[identity]:
            continue
        simulated = queues[identity].popleft()
        for column in passthrough:
            if column in simulated:
                projected.at[index, column] = simulated[column]
    promo_overrides: dict[tuple, dict] = {}
    for change in date_prices:
        row_key = change.get("row", change)
        identity = _row_identity(row_key)
        fields = {
            column: change[source]
            for column, source in (
                ("PromoCode", "promo_code"), ("PromoType", "promo_type"),
                ("PromoValue", "promo_value"), ("PromoLabel", "promo_label"),
            )
            if source in change
        }
        if fields:
            promo_overrides.setdefault(identity, {}).update(fields)
    for index, row in projected.iterrows():
        for column, value in promo_overrides.get(_row_identity(row), {}).items():
            projected.at[index, column] = value
    return projected, preview


def remove_key_date_overrides(
    frame: pd.DataFrame,
    root: Path,
    key_date: str,
    boundary: str,
) -> pd.DataFrame:
    """Build the editable comparison base by reversing only this date's layer."""
    result = frame.copy()
    timeline = PricingTimeline(Path(root) / "outputs" / "promo_planner" / "promo_schedule.db")
    records = timeline.date_override_records(boundary=boundary, key_date=key_date)
    positions = defaultdict(deque)
    for index, row in result.iterrows():
        positions[_row_identity(row)].append(index)
    for change in records:
        indexes = positions.get(_row_identity(change.get("row", {})))
        if not indexes:
            continue
        index = indexes[0]
        for field, old_value in change.get("old_values", {}).items():
            if field in result.columns:
                result.at[index, field] = old_value
    return result


def save_key_date_changes(
    root: Path,
    key_date: str,
    edited_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    campaigns: list[Campaign],
    event_label: str = "",
) -> tuple[int, int]:
    """Persist price and promo edits as dated SQLite layers, never as snapshots."""
    if edited_df.empty or baseline_df.empty:
        return 0, 0
    campaign_by_id = {int(c.id or 0): c for c in campaigns}
    baseline = {_row_identity(row): row for _, row in baseline_df.iterrows()}
    effective_from = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    groups: dict[tuple, dict] = {}
    for _, row in edited_df.iterrows():
        identity = _row_identity(row)
        original = baseline.get(identity)
        if original is None:
            continue
        try:
            campaign_id = int(float(row.get("_PromoPlannerPromoOwner", 0) or 0))
        except (TypeError, ValueError):
            campaign_id = 0
        if not campaign_id:
            try:
                campaign_id = int(float(original.get("_PromoPlannerPromoOwner", 0) or 0))
            except (TypeError, ValueError):
                campaign_id = 0
        campaign = campaign_by_id.get(campaign_id)
        if campaign is None:
            # Imported books may have a promo code but no planner owner ID.
            # Recover the matching active campaign so its override expires
            # with the campaign and reveals the campaign's normal end state.
            promo_code = str(row.get("PromoCode", "") or original.get("PromoCode", "")).strip()
            iso = str(row.get("ISO", "")).strip().upper()
            plan = str(row.get("Plan", "")).strip()
            try:
                days = int(float(row.get("Days", 0) or 0))
            except (TypeError, ValueError):
                days = 0
            matches = [
                candidate for candidate in campaigns
                if candidate.promo_code == promo_code
                and candidate.start_date <= effective_from <= candidate.end_date
                and (not candidate.destinations or iso in {str(x).upper() for x in candidate.destinations})
                and (not candidate.plans or plan in candidate.plans)
                and (not candidate.days or days in candidate.days)
            ]
            if matches:
                campaign = max(matches, key=lambda candidate: (candidate.start_date, int(candidate.id or 0)))
        expiry = (date.fromisoformat(campaign.end_date) + timedelta(days=1)).isoformat() if campaign else ""
        changes = []
        for field in ("Price_EUR", "Price_USD", "PromoCode", "PromoType", "PromoValue", "PromoLabel"):
            new_value = row.get(field, None)
            old_value = original.get(field, None)
            if field in {"Price_EUR", "Price_USD", "PromoValue"}:
                new_value = pd.to_numeric(new_value, errors="coerce")
                old_value = pd.to_numeric(old_value, errors="coerce")
                if pd.isna(new_value) and pd.isna(old_value):
                    continue
                if pd.notna(new_value) and pd.notna(old_value) and abs(float(new_value) - float(old_value)) < 1e-6:
                    continue
                if field.startswith("Price_") and pd.isna(new_value):
                    continue
                new_value = round(float(new_value), 2) if pd.notna(new_value) else None
                old_value = round(float(old_value), 2) if pd.notna(old_value) else None
            else:
                new_value = "" if pd.isna(new_value) else str(new_value).strip()
                old_value = "" if pd.isna(old_value) else str(old_value).strip()
                if new_value == old_value:
                    continue
            changes.append((row_key_for_identity(_identity_json(row)), field, old_value, new_value))
        if not changes:
            continue
        group_key = (int(campaign.id or 0) if campaign else 0, expiry)
        group = groups.setdefault(group_key, {
            "effective_from": effective_from,
            "expires_at": expiry,
            "metadata": {
                "key_date": key_date,
                "campaign_id": int(campaign.id or 0) if campaign else 0,
                "campaign_name": campaign.name if campaign else "",
                "event_label": event_label.strip(),
                "promo_start_date": campaign.start_date if campaign else "",
                "promo_end_date": campaign.end_date if campaign else "",
            },
            "label": event_label.strip() or (f"Promo price override: {campaign.name}" if campaign else "Price change"),
            "fields": [],
        })
        group["fields"].extend(changes)
    store = PromoStore(Path(root) / "outputs" / "promo_planner" / "promo_schedule.db")
    timeline = PricingTimeline(store.path)
    timeline.migrate_legacy_date_overrides()
    changed_fields = timeline.replace_key_date_overrides(key_date, list(groups.values()))
    return len({field[0] for group in groups.values() for field in group["fields"]}), 0


def changes_for_key_date(root: Path, key_date: str) -> list[dict]:
    db_path = Path(root) / "outputs" / "promo_planner" / "promo_schedule.db"
    store = PromoStore(db_path)
    timeline = PricingTimeline(store.path)
    timeline.migrate_legacy_date_overrides()
    return timeline.date_override_records(key_date=key_date)
