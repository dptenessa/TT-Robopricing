"""Campaign transitions derived from dated, overlapping campaign definitions."""

from datetime import date, timedelta


def today_campaign_transition(campaigns, today: date | None = None) -> str:
    today = today or date.today()
    transition = campaign_transition(campaigns, today.isoformat())
    if " → " not in transition:
        at_start_of_day = campaign_transition(campaigns, (today - timedelta(days=1)).isoformat())
        if " → " in at_start_of_day:
            return at_start_of_day
    return transition


def campaign_transition(campaigns, key_date: str, *, destination: str = "") -> str:
    before_day = date.fromisoformat(key_date).isoformat()
    after_day = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    campaigns = [c for c in campaigns if not destination or destination in c.destinations]
    events = [c for c in campaigns if c.start_date == after_day or c.end_date == before_day]
    # Compare the product scopes touched by this boundary. Unrelated campaigns
    # elsewhere must not be presented as replacing each other.
    scopes = {
        (iso, plan, day)
        for c in (events or campaigns)
        for iso in c.destinations if not destination or iso == destination
        for plan in c.plans for day in c.days
    }

    def visible(day):
        active = [c for c in campaigns if (not c.start_date or c.start_date <= day) and c.end_date >= day]
        names = set()
        remaining = scopes.copy()
        for campaign in sorted(active, key=lambda c: (c.start_date, int(c.id or 0)), reverse=True):
            destinations, plans, days = set(campaign.destinations), set(campaign.plans), set(campaign.days)
            covered = {scope for scope in remaining if scope[0] in destinations and scope[1] in plans and scope[2] in days}
            if covered:
                names.add(campaign.name)
                remaining.difference_update(covered)
            if not remaining:
                break
        return " + ".join(sorted(names)) or "Nothing"

    before, after = visible(before_day), visible(after_day)
    return after if before == after else f"{before} → {after}"


def campaign_presentation(campaign, store, *, today=None):
    """Shared publication labels and font policy for campaign lists and timelines."""
    today = today or date.today()
    published = bool(campaign.official_partner_pack_at or campaign.official_partner_pack
                     or campaign.status == 'ACTIVE_IMPORTED' or store.has_execution(campaign.id, 'START'))
    finished = bool(store.has_execution(campaign.id, 'END') or campaign.end_date < today.isoformat())
    readonly = published or finished
    label = ('Published · finished · read only' if published and finished else
             'Published · read only' if published else
             'Finished · read only' if finished else 'Planned · editable')
    return dict(label=label, published=published, readonly=readonly)


def campaign_context_ids(campaigns, key_date=None, *, today=None):
    """Prefer boundary starts/ends, otherwise highlight visible active offers."""
    today = today or date.today()
    effective = (date.fromisoformat(key_date) + timedelta(days=1)) if key_date else today
    before = (effective - timedelta(days=1)).isoformat()
    after = effective.isoformat()
    starting = {c.id for c in campaigns if c.start_date == after}
    if starting:
        return starting
    ending = {c.id for c in campaigns if c.end_date == before}
    if ending:
        return ending
    result, covered = set(), set()
    for c in sorted(campaigns, key=lambda c: (c.start_date, int(c.id or 0)), reverse=True):
        if (c.start_date and c.start_date > after) or c.end_date < after:
            continue
        scope = {(iso, plan, day) for iso in c.destinations for plan in c.plans for day in c.days}
        if scope - covered:
            result.add(c.id)
        covered.update(scope)
    return result


def key_date_read_only(campaigns, store, timeline, key_date, *, today=None):
    today = today or date.today()
    effective = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
    if effective < today.isoformat():
        return True
    for marker in timeline.event_markers(key_date):
        if marker['source'] == 'promotion_date' or (marker['source'] == 'price_date' and not marker['metadata'].get('official')):
            return False
    return not any((c.start_date == effective or c.end_date == key_date)
                   and not campaign_presentation(c, store, today=today)['readonly'] for c in campaigns)
