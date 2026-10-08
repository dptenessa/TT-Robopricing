from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from .store import Campaign


def _ics_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def export_campaign_ics(c: Campaign, out_dir: Path, open_after: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{c.promo_code}.ics"
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    desc = _ics_escape(
        f"Promo: {c.promo_type} {c.promo_value}\nDestinations: {', '.join(c.destinations)}\n"
        f"Plans: {', '.join(c.plans)}\nDays: {', '.join(map(str, c.days))}\nOpen T-Travel Promo Planner and execute the due action."
    )
    def ev(kind: str, date: str, uid_suffix: str):
        d = date.replace("-", "")
        return "\r\n".join([
            "BEGIN:VEVENT", f"UID:{c.promo_code}-{uid_suffix}@ttravel.local", f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d}", f"SUMMARY:T-Travel Promo {kind} - {_ics_escape(c.name)}",
            f"DESCRIPTION:{desc}", "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY",
            f"DESCRIPTION:T-Travel Promo {kind} - {_ics_escape(c.name)}", "END:VALARM", "END:VEVENT"
        ])
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//T-Travel//Promo Planner//EN", "CALSCALE:GREGORIAN",
                           ev("START", c.start_date, "start"), ev("END", c.end_date, "end"), "END:VCALENDAR", ""])
    path.write_text(body, encoding="utf-8")
    if open_after and os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    return path
