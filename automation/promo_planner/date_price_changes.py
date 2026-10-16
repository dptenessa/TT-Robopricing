from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .engine import preview_batch
from .store import Campaign, PromoStore


def schedule_path(root: Path) -> Path:
    return Path(root) / "outputs" / "promo_planner" / "date_price_changes.json"


def issue_choices_path(root: Path) -> Path:
    return Path(root) / "outputs" / "promo_planner" / "issue_choices"


def load_schedule(root: Path) -> list[dict]:
    path = schedule_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [item for item in data.get("changes", []) if isinstance(item, dict)]
    except (OSError, ValueError, TypeError):
        return []


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
        workbook,
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
    return projected, preview


def save_key_date_changes(
    root: Path,
    key_date: str,
    edited_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    campaigns: list[Campaign],
) -> tuple[int, int]:
    """Persist list-price edits as promo-owned JSON layers, never as current prices."""
    if edited_df.empty or baseline_df.empty:
        return 0, 0
    campaign_by_id = {int(c.id or 0): c for c in campaigns}
    baseline = {_row_identity(row): row for _, row in baseline_df.iterrows()}
    effective_from = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    changed_records = {}
    unlinked_rows = 0
    now = pd.Timestamp.now().isoformat(timespec="seconds")
    for _, row in edited_df.iterrows():
        identity = _row_identity(row)
        original = baseline.get(identity)
        if original is None:
            continue
        try:
            campaign_id = int(float(row.get("_PromoPlannerPromoOwner", 0) or 0))
        except (TypeError, ValueError):
            campaign_id = 0
        campaign = campaign_by_id.get(campaign_id)
        if campaign is None:
            has_price_change = any(
                pd.notna(pd.to_numeric(row.get(f"Price_{currency}"), errors="coerce"))
                and (
                    pd.isna(pd.to_numeric(original.get(f"Price_{currency}"), errors="coerce"))
                    or abs(
                        float(pd.to_numeric(row.get(f"Price_{currency}"), errors="coerce"))
                        - float(pd.to_numeric(original.get(f"Price_{currency}"), errors="coerce"))
                    ) > 1e-6
                )
                for currency in ("EUR", "USD")
            )
            unlinked_rows += int(has_price_change)
            continue
        prices = {}
        for currency in ("EUR", "USD"):
            column = f"Price_{currency}"
            new_value = pd.to_numeric(row.get(column), errors="coerce")
            old_value = pd.to_numeric(original.get(column), errors="coerce")
            if pd.notna(new_value) and (pd.isna(old_value) or abs(float(new_value) - float(old_value)) > 1e-6):
                prices[f"price_{currency.lower()}"] = round(float(new_value), 2)
        if not prices:
            continue
        end_date = date.fromisoformat(campaign.end_date) + timedelta(days=1)
        record = {
            "key_date": key_date,
            "effective_from": effective_from,
            "expires_at": end_date.isoformat(),
            "campaign_id": campaign_id,
            "campaign_name": campaign.name,
            "promo_start_date": campaign.start_date,
            "promo_end_date": campaign.end_date,
            "row": _identity_json(row),
            "updated_at": now,
            **prices,
        }
        changed_records[(campaign_id, identity)] = record

    path = schedule_path(root)
    existing = load_schedule(root)
    kept = [
        item for item in existing
        if not (
            item.get("key_date") == key_date
            and (int(item.get("campaign_id") or 0), _row_identity(item.get("row", {}))) not in changed_records
        )
    ]
    replaced = {
        (int(item.get("campaign_id") or 0), _row_identity(item.get("row", {})))
        for item in kept if item.get("key_date") == key_date
    }
    kept.extend(record for key, record in changed_records.items() if key not in replaced)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"version": 1, "changes": kept}, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return len(changed_records), unlinked_rows


def changes_for_key_date(root: Path, key_date: str) -> list[dict]:
    return [item for item in load_schedule(root) if item.get("key_date") == key_date]
