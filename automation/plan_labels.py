from __future__ import annotations

from typing import Any


PLAN_LABELS: dict[str, str] = {
    "basic": "S",
    "medium": "M",
    "moderate": "M",
    "large": "L",
    "unlimited": "Unlimited",
}

PARTNER_PLAN_LABELS: dict[str, str] = {
    "basic": "Lite",
    "medium": "Standard",
    "moderate": "Standard",
    "large": "Max",
    "unlimited": "Unlimited",
}

PARTNER_PLAN_PACKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Lite", ("Basic",)),
    ("Standard", ("Medium", "Moderate")),
    ("Max", ("Large",)),
    ("Unlimited", ("Unlimited",)),
)


def display_plan_label(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "nan":
        return ""
    return PLAN_LABELS.get(text.lower(), text)


def partner_display_plan_label(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "nan":
        return ""
    return PARTNER_PLAN_LABELS.get(text.lower(), text)
