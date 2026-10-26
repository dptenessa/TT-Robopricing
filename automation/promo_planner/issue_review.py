"""Presentation-only filtering/counts; export and floor enforcement stay in the engine."""
from dataclasses import replace
import math

from .engine import _state_net


def _text(value):
    text = str(value or '').strip()
    return '' if text.lower() in {'nan', 'none'} else text


def _key(iso, plan, days):
    return (_text(iso).upper(), _text(plan), int(float(days or 0)))


def _rows_by_point(preview):
    rows = {}
    for row in preview.projected_rows:
        if _text(row.get('PromoCode')):
            rows.setdefault(_key(row.get('ISO'), row.get('Plan'), row.get('Days')), []).append(row)
    return rows


def promotion_review(preview):
    """Keep manual/scheduled promos and their PA rows; hide ordinary price edits."""
    points = _rows_by_point(preview)
    return replace(preview, cost_floor_issues=[issue for issue in preview.cost_floor_issues
        if _key(issue.iso, issue.plan, issue.days) in points])


def _true(value):
    return str(value).strip().lower() in {'true', '1', '1.0', 'yes'}


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def review_summary(preview):
    """Count unresolved promo PRODUCTS (not currencies), plus unresolved conflicts."""
    preview = promotion_review(preview)
    rows = _rows_by_point(preview)
    unresolved = set()
    for issue in preview.cost_floor_issues:
        key = _key(issue.iso, issue.plan, issue.days)
        for row in rows.get(key, []):
            if _true(row.get('AllowBelowCost')) and not _true(row.get('_PromoPlannerPAUnresolved')):
                continue
            state = dict(list_eur=_number(row.get('Price_EUR')), list_usd=_number(row.get('Price_USD')),
                         promo_code=_text(row.get('PromoCode')), promo_type=_text(row.get('PromoType')),
                         promo_value=_number(row.get('PromoValue')) or 0,
                         net_eur=_number(row.get('PromoNet_EUR')), net_usd=_number(row.get('PromoNet_USD')))
            net = _state_net(state, issue.currency.upper())
            if net is None or net < issue.cost_floor - 1e-9:
                unresolved.add(key)
    # Engine items contain only unresolved collision messages; resolved choices
    # remain in collision_issues for inspection and must not inflate the badge.
    conflicts = len(preview.collisions)
    return dict(floor_products=len(unresolved), conflicts=conflicts, total=len(unresolved)+conflicts)
