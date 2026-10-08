from __future__ import annotations

import os
import subprocess
from datetime import datetime
from .store import Campaign


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> str:
    if os.name != "nt":
        raise RuntimeError("Automatic Outlook sync is available on Windows only.")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cp = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=25,
        creationflags=creationflags,
    )
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "Outlook automation failed.").strip()
        raise RuntimeError(msg)
    return (cp.stdout or "").strip()


def _delete_entry(entry_id: str) -> None:
    if not entry_id:
        return
    script = f"""
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject Outlook.Application
$ns = $app.GetNamespace('MAPI')
try {{
    $item = $ns.GetItemFromID({_ps_quote(entry_id)})
    if ($null -ne $item) {{ $item.Delete() }}
}} catch {{
    # Missing/deleted item is harmless; only rethrow unexpected COM errors.
    if ($_.Exception.Message -notmatch 'cannot be found|not found|unknown entry') {{ throw }}
}}
"""
    _run_powershell(script)


def _create_event(c: Campaign, kind: str, event_date: str) -> str:
    title = f"T-Travel Promo {kind} - {c.name}"
    body = (
        f"Campaign: {c.name}\n"
        f"Promotion: {c.promo_name or c.promo_code}\n"
        f"Destinations: {', '.join(c.destinations)}\n"
        f"Plans: {', '.join(c.plans)}\n"
        f"Days: {', '.join(map(str, c.days))}\n\n"
        f"Open T-Travel Promo Planner, select {event_date}, preview the scheduled date batch and apply all changes for that date."
    )
    # Use invariant yyyy-MM-dd parsing to avoid locale ambiguity on Windows.
    script = f"""
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject Outlook.Application
$item = $app.CreateItem(1)
$item.Subject = {_ps_quote(title)}
$d = [datetime]::ParseExact({_ps_quote(event_date)}, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
$item.Start = $d.AddHours(9)
$item.Duration = 15
$item.AllDayEvent = $false
$item.ReminderSet = $true
$item.ReminderMinutesBeforeStart = 1440
$item.Body = {_ps_quote(body)}
$item.BusyStatus = 0
$item.Save()
Write-Output $item.EntryID
"""
    entry_id = _run_powershell(script).splitlines()[-1].strip()
    if not entry_id:
        raise RuntimeError("Outlook created the appointment but did not return an EntryID.")
    return entry_id


def sync_campaign_events(c: Campaign) -> tuple[str, str]:
    """Replace this campaign's Outlook appointments and return their EntryIDs.

    Planned campaigns get START and END reminders. Campaigns registered as already
    active never get a START reminder; they get only an END reminder when an end date
    exists. Open-ended active campaigns therefore create no Outlook reminder yet.
    """
    if c.outlook_start_id:
        _delete_entry(c.outlook_start_id)
    if c.outlook_end_id:
        _delete_entry(c.outlook_end_id)

    imported = c.status == "ACTIVE_IMPORTED"
    start_id = ""
    end_id = ""

    if not imported and c.start_date:
        start_id = _create_event(c, "START", c.start_date)
    if c.end_date:
        try:
            end_id = _create_event(c, "END", c.end_date)
        except Exception:
            if start_id:
                try:
                    _delete_entry(start_id)
                except Exception:
                    pass
            raise
    return start_id, end_id


def delete_campaign_events(c: Campaign) -> None:
    errors: list[str] = []
    for entry_id in (c.outlook_start_id, c.outlook_end_id):
        if not entry_id:
            continue
        try:
            _delete_entry(entry_id)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))
