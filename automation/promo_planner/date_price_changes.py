from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import replace
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
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return ""
        return "" if pd.isna(parsed) else round(parsed, 6)

    return (
        text(row.get("ISO", row.get("iso", ""))).upper(),
        text(row.get("Country", row.get("country", ""))).casefold(),
        text(row.get("PricingUnitIdUsed", row.get("pricing_unit_id", ""))),
        text(row.get("Plan", row.get("plan", ""))),
        number(row.get("Days", row.get("days", ""))),
        number(row.get("GB", row.get("gb", ""))),
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


def _published_price_adjustments(frame, campaigns, boundary):
    """Retain approved temporary list raises while replaying unchanged offers."""
    by_id = {int(c.id or 0): c for c in campaigns}
    adjustments = []
    for row in frame.to_dict(orient="records"):
        try:
            stack = json.loads(str(row.get("_PromoPlannerPromoStack") or "[]"))
            stack = stack if isinstance(stack, list) else []
        except (TypeError, ValueError):
            stack = []
        layers = stack + [{
            "promo_owner": row.get("_PromoPlannerPromoOwner"), "price_owner": row.get("_PromoPlannerPriceOwner"),
            "promo_code": row.get("PromoCode"), "promo_type": row.get("PromoType"), "promo_value": row.get("PromoValue"),
            "list_eur": row.get("Price_EUR"), "list_usd": row.get("Price_USD"),
            "original_list_eur": row.get("_PromoPlannerOriginalPrice_EUR"),
            "original_list_usd": row.get("_PromoPlannerOriginalPrice_USD"),
        }]
        if row.get("_PromoPlannerPAUnderlying"):
            try:
                under = json.loads(str(row["_PromoPlannerPAUnderlying"]))
                layers = under.get("promo_stack", []) + [under]
            except (TypeError, ValueError):
                pass
        iso, plan, days = str(row.get("ISO", "")).upper(), str(row.get("Plan", "")), int(float(row.get("Days") or 0))
        for layer in layers:
            try:
                campaign = by_id.get(int(float(layer.get("promo_owner") or 0)))
            except (TypeError, ValueError):
                campaign = None
            if campaign is None or campaign.end_date < boundary or (campaign.start_date and campaign.start_date > boundary):
                continue
            if iso not in campaign.destinations or plan not in campaign.plans or days not in campaign.days:
                continue
            if layer.get("price_owner") != campaign.promo_code or layer.get("promo_code") != campaign.promo_code:
                continue
            if layer.get("promo_type") != campaign.promo_type or float(layer.get("promo_value") or 0) != campaign.promo_value:
                continue
            change = {"campaign_id": campaign.id, "apply_on_start": True,
                      "row": {"iso": iso, "plan": plan, "days": days, "pricing_unit_id": str(row.get("PricingUnitIdUsed", ""))}}
            for currency in ("eur", "usd"):
                original, price = layer.get(f"original_list_{currency}"), layer.get(f"list_{currency}")
                if original is not None and pd.notna(original) and price is not None and pd.notna(price):
                    change[f"price_{currency}"] = float(price)
            if "price_eur" in change or "price_usd" in change:
                adjustments.append(change)
    return adjustments


def _date_review_rows(campaigns, date_prices, key_date, *, include_adjustments=False):
    """Pricing points affected at this midnight, rather than all replayed offers."""
    boundary = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    points = set()
    for campaign in campaigns:
        starts = campaign.status != "ACTIVE_IMPORTED" and campaign.start_date == boundary
        ends = campaign.end_date == key_date
        if not (starts or ends):
            continue
        durations = set(campaign.days)
        # Ending/starting an anchor also changes shorter PA prices.
        for iso in campaign.destinations:
            days = (set(range(1, (30 if campaign.monotonic_all_promos else max(durations, default=0)) + 1))
                    if include_adjustments and iso in campaign.monotonic_destinations else durations)
            points.update((str(iso).upper(), plan, int(day))
                          for plan in campaign.plans for day in days)
    if include_adjustments:
        # Another offer starting/ending can change a broad campaign's anchors.
        for campaign in campaigns:
            if not (campaign.monotonic_all_promos and campaign.end_date >= boundary
                    and (not campaign.start_date or campaign.start_date <= boundary)):
                continue
            for event in campaigns:
                if not (event.start_date == boundary or event.end_date == key_date):
                    continue
                points.update((str(iso).upper(), plan, day)
                              for iso in set(event.destinations) & set(campaign.monotonic_destinations)
                              for plan in set(event.plans) & set(campaign.plans)
                              for day in range(1, max(event.days, default=0) + 1))
    for change in date_prices:
        if change.get("key_date") != key_date and change.get("effective_from") != boundary:
            continue
        row = change.get("row", change)
        iso, plan, day = str(row.get("iso", "")).upper(), str(row.get("plan", "")), int(row.get("days") or 0)
        points.add((iso, plan, day))
        for campaign in campaigns:
            if (include_adjustments and iso in campaign.monotonic_destinations and plan in campaign.plans
                    and (campaign.monotonic_all_promos or day <= max(campaign.days, default=0)) and campaign.end_date >= boundary and (not campaign.start_date or campaign.start_date <= boundary)):
                # Either an anchor edit or a shorter underlying-price edit can change PA.
                points.update((iso, plan, duration) for duration in range(1, day + 1))
    return points


def project_dataframe(
    current_df: pd.DataFrame,
    workbook: Path,
    store: PromoStore,
    key_date: str,
    root: Path,
    *,
    exclude_key_date_changes: bool = False,
    replay_executed: bool = False,
    include_drafts: bool = True,
    campaigns_override: list[Campaign] | None = None,
) -> tuple[pd.DataFrame, object]:
    """Project the current workbook through the selected midnight boundary."""
    boundary = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    saved_campaigns = store.list_campaigns()
    campaigns = saved_campaigns if campaigns_override is None else campaigns_override
    if not include_drafts:
        # Today's reference view must not activate a saved-but-unpublished
        # promotion merely because its planned start date has passed.
        campaigns = [c for c in campaigns if c.status == "ACTIVE_IMPORTED"
                     or c.official_partner_pack or store.has_execution(c.id, "START")]
    saved_ids = {c.id for c in saved_campaigns}
    actions = []
    has_published = False
    baseline_day = PricingTimeline(store.path).baseline_info()["effective_from"] if replay_executed else ""
    for campaign in campaigns:
        started = store.has_execution(campaign.id, "START")
        has_published = has_published or started
        ended = store.has_execution(campaign.id, "END")
        imported = campaign.status == "ACTIVE_IMPORTED"
        if replay_executed and imported and baseline_day <= boundary:
            # The migration baseline already contains imported promotions.
            # Remove their original scope, then replay the current definition
            # so amendments add/remove countries and change the offer correctly.
            original = store.original_campaign_definition(campaign.id) if campaign.id in saved_ids else campaign
            cleanup_day = (date.fromisoformat(baseline_day) - timedelta(days=1)).isoformat()
            actions.append((replace(original, end_date=cleanup_day), "END"))
            actions.append((replace(campaign, start_date=baseline_day), "START"))
        starts_by_boundary = bool(
            (replay_executed or not started) and not imported and campaign.start_date
            and date.fromisoformat(campaign.start_date) <= date.fromisoformat(boundary)
        )
        if starts_by_boundary:
            actions.append((campaign, "START"))
        ends_by_boundary = bool(
            (replay_executed or not ended) and campaign.end_date
            and date.fromisoformat(campaign.end_date) < date.fromisoformat(boundary)
            and (started or imported or starts_by_boundary)
        )
        if ends_by_boundary:
            actions.append((campaign, "END"))

    cost_choices = {}
    cost_choice_dates = {}
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
                    choice_key = (int(cid), str(iso).upper(), str(plan), int(days), str(currency).upper())
                    cost_choices[choice_key] = str(choice)
                    cost_choice_dates[choice_key] = path.stem
            for record in data.get("conflicts", []):
                if len(record) == 6:
                    cid, iso, plan, days, _current_code, choice = record
                    conflict_choices[(int(cid), str(iso).upper(), str(plan), int(days))] = str(choice)

    date_prices = active_changes(
        root,
        boundary,
        exclude_key_date=key_date if exclude_key_date_changes else None,
    )
    # A newly edited manual offer cannot inherit a prior offer's approval.
    for change in date_prices:
        if not any(field in change for field in ("promo_code", "promo_type", "promo_value", "promo_net_eur", "promo_net_usd")):
            continue
        row = change.get("row", {})
        for currency in ("EUR", "USD"):
            choice_key = (0, str(row.get("iso", "")).upper(), str(row.get("plan", "")), int(row.get("days") or 0), currency)
            if cost_choice_dates.get(choice_key, "") < change.get("key_date", ""):
                cost_choices.pop(choice_key, None)
    if not include_drafts:
        date_prices = [row for row in date_prices if not row.get("is_base_price_change") or row.get("official")]
    if replay_executed and has_published:
        published = PricingTimeline(store.path).project(boundary, include_drafts=include_drafts)
        date_prices = _published_price_adjustments(published, campaigns, boundary) + date_prices
    preview = preview_batch(
        current_df,
        actions,
        key_date,
        cost_floor_choices=cost_choices,
        conflict_choices=conflict_choices,
        date_price_changes=date_prices,
        campaigns=campaigns,
        review_rows=_date_review_rows(campaigns, date_prices, key_date),
        adjustment_review_rows=_date_review_rows(campaigns, date_prices, key_date, include_adjustments=True),
    )

    projected = current_df.copy().reset_index(drop=True)
    passthrough = (
        "PromoCode", "PromoType", "PromoValue", "PromoLabel", "Price_EUR", "Price_USD",
        "AllowBelowCost", "_PromoPlannerPromoStack", "_PromoPlannerPromoOwner",
        "PromoNet_EUR", "PromoNet_USD", "_PromoPlannerPAUnderlying", "_PromoPlannerPAUnresolved", "_ManualPALinks",
        "_PromoPlannerOriginalPrice_EUR", "_PromoPlannerOriginalPrice_USD", "_PromoPlannerPriceOwner",
    )
    # preview_batch preserves input row order, including duplicate identities.
    # Assign complete columns instead of rebuilding row identities and writing
    # every cell through pandas .at on every date switch.
    simulated = pd.DataFrame.from_records(preview.projected_rows)
    if len(simulated) != len(projected):
        raise ValueError("The campaign projection changed the price-book row count.")
    for column in passthrough:
        if column in simulated:
            projected[column] = simulated[column].to_numpy(copy=False)
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
    if not records:
        return result
    positions = defaultdict(deque)
    identity_columns = [c for c in ("ISO", "Country", "PricingUnitIdUsed", "Plan", "Days", "GB") if c in result]
    for index, row in zip(result.index, result[identity_columns].to_dict(orient="records")):
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
    *,
    displayed_df: pd.DataFrame | None = None,
    normalize_price=None,
) -> tuple[int, int]:
    """Persist price and promo edits as dated SQLite layers, never as snapshots."""
    if edited_df.empty or baseline_df.empty:
        return 0, 0
    store = PromoStore(Path(root) / "outputs" / "promo_planner" / "promo_schedule.db")
    timeline = PricingTimeline(store.path)
    if timeline.is_price_date_official(key_date):
        raise ValueError("This price date is official. Add another date for further changes.")
    base_price_event = any(m["source"] == "price_date" for m in timeline.event_markers(key_date))
    if base_price_event and displayed_df is not None:
        # Keep raw dated base edits separate from automatic, temporary list
        # raises made by the promotion cost-floor preview. Only user changes
        # relative to what was displayed become new permanent base values.
        candidate = baseline_df.astype(object).copy()
        positions = {_row_identity(row): index for index, row in zip(candidate.index, candidate.to_dict(orient="records"))}
        for change in timeline.date_override_records(key_date=key_date):
            if not change.get("is_base_price_change"):
                continue
            index = positions.get(_row_identity(change.get("row", {})))
            if index is None:
                continue
            for currency in ("EUR", "USD"):
                value = change.get(f"price_{currency.lower()}")
                if value is not None:
                    candidate.at[index, f"Price_{currency}"] = value
            for field, source in (("PromoCode", "promo_code"), ("PromoType", "promo_type"),
                                  ("PromoValue", "promo_value"), ("PromoLabel", "promo_label"),
                                  ("PromoNet_EUR", "promo_net_eur"), ("PromoNet_USD", "promo_net_usd"), ("_ManualPALinks", "manual_pa_links")):
                if source in change:
                    candidate.at[index, field] = change[source]
        displayed = {_row_identity(row): row for row in displayed_df.to_dict(orient="records")}
        normalize = normalize_price or float
        for row in edited_df.to_dict(orient="records"):
            identity = _row_identity(row)
            original_display = displayed.get(identity)
            index = positions.get(identity)
            if index is None or original_display is None:
                continue
            for field in ("Price_EUR", "Price_USD"):
                value = pd.to_numeric(row.get(field), errors="coerce")
                previous = pd.to_numeric(original_display.get(field), errors="coerce")
                if pd.notna(value) and (pd.isna(previous) or abs(float(value) - normalize(float(previous))) > 1e-6):
                    candidate.at[index, field] = float(value)
            promo_fields = ("PromoCode", "PromoType", "PromoValue", "PromoLabel", "PromoNet_EUR", "PromoNet_USD", "_ManualPALinks")
            def same(field):
                a, b = row.get(field), original_display.get(field)
                if field in {"PromoValue", "PromoNet_EUR", "PromoNet_USD"}:
                    a, b = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
                    if field == "PromoValue" and not str(row.get("PromoCode") or "").strip() and not str(original_display.get("PromoCode") or "").strip():
                        return True
                    return (pd.isna(a) and pd.isna(b)) or (pd.notna(a) and pd.notna(b) and abs(float(a)-float(b)) < 1e-9)
                return ("" if pd.isna(a) else str(a).strip()) == ("" if pd.isna(b) else str(b).strip())
            if any(not same(field) for field in promo_fields):
                for field in promo_fields:
                    value = row.get(field)
                    if field in {"PromoValue", "PromoNet_EUR", "PromoNet_USD"}:
                        value = pd.to_numeric(value, errors="coerce")
                        value = float(value) if pd.notna(value) else None
                    candidate.at[index, field] = value
                candidate.at[index, "_PromoPlannerPAUnderlying"] = ""
                candidate.at[index, "_PromoPlannerPAUnresolved"] = False
                candidate.at[index, "_PromoPlannerPromoOwner"] = ""
        edited_df = candidate
    campaign_by_id = {int(c.id or 0): c for c in campaigns}
    baseline = {_row_identity(row): row for row in baseline_df.to_dict(orient="records")}
    effective_from = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    groups: dict[tuple, dict] = {}
    for row in edited_df.to_dict(orient="records"):
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
        if base_price_event:
            campaign = None
        expiry = (date.fromisoformat(campaign.end_date) + timedelta(days=1)).isoformat() if campaign else ""
        changes = []
        for field in ("Price_EUR", "Price_USD", "PromoCode", "PromoType", "PromoValue", "PromoLabel", "PromoNet_EUR", "PromoNet_USD", "_ManualPALinks"):
            new_value = row.get(field, None)
            old_value = original.get(field, None)
            if field in {"Price_EUR", "Price_USD", "PromoValue", "PromoNet_EUR", "PromoNet_USD"}:
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
            changes.append((row_key_for_identity(row), field, old_value, new_value))
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
                "is_base_price_change": base_price_event,
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
