from __future__ import annotations

import json
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:11434"


def _json_request(path: str, payload: dict | None = None, timeout: float = 2.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def available_models() -> list[str]:
    try:
        data = _json_request("/api/tags", timeout=1.5)
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def choose_default_model(models: list[str]) -> str:
    if not models:
        return ""
    # Prefer the user's stronger local model, then practical instruction models.
    preferred = ["gemma4:e2b", "gemma4", "qwen2.5:7b", "qwen2:7b", "gemma3:4b"]
    low = {m.lower(): m for m in models}
    for p in preferred:
        if p.lower() in low:
            return low[p.lower()]
    # Also accept tags such as gemma4:e2b-latest.
    for p in preferred:
        for m in models:
            if p.lower() in m.lower():
                return m
    return models[0]


def parse_campaign(
    text: str,
    model: str,
    destination_codes: list[str],
    plans: list[str],
    days: list[int],
    promos: list[dict],
) -> dict[str, Any]:
    prompt = f"""You convert a T-Travel promotion description into strict JSON for a commercial planner.
Return JSON only. Do not invent missing values.

Allowed destination ISO codes:
{destination_codes}

Allowed plans:
{plans}

Allowed durations in days:
{days}

Official promo catalogue (you MUST choose only from this list; never invent a promo):
{json.dumps(promos, ensure_ascii=False)}

Return exactly these keys:
{{
  "name": string or "",
  "start_date": "YYYY-MM-DD" or "",
  "end_date": "YYYY-MM-DD" or "",
  "destinations": [ISO codes],
  "plans": [allowed plan names],
  "days": [allowed integers],
  "promo_code": exact code from the official catalogue or "",
  "promo_label": exact label from the official catalogue or "",
  "allow_below_cost": false,
  "notes": string or "",
  "missing": [short descriptions of required information that cannot be inferred safely]
}}

Rules:
- Match country names in the user's wording to the corresponding allowed ISO code.
- Do not select destinations, plans or days not explicitly implied.
- Select a promo only if the wording clearly maps to one official catalogue entry.
- If multiple catalogue promos could fit, leave promo_code and promo_label blank and put "promotion" in missing.
- Dates must be explicit ISO dates when the user gives enough information; otherwise mark them missing.
- Never set allow_below_cost true unless the user explicitly says it is allowed.

User description:
{text}
"""
    data = _json_request(
        "/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=90,
    )
    response = data.get("response", "{}")
    if isinstance(response, dict):
        return response
    return json.loads(response)
