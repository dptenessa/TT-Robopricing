"""Compact, date-effective pricing state backed by the shared planner SQLite DB.

The workbook is imported once as the migration baseline. Subsequent saves write
only changed editable fields; XLSX/CSV files remain compatible exports.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from contextlib import nullcontext
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable

import pandas as pd


EDITABLE_FIELDS = (
    "Price_EUR",
    "Price_USD",
    "PromoCode",
    "PromoType",
    "PromoValue",
    "PromoLabel",
    "AllowBelowCost",
    "_PromoPlannerPromoStack",
    "_PromoPlannerPromoOwner",
    "_PromoPlannerOriginalPrice_EUR",
    "_PromoPlannerOriginalPrice_USD",
    "_PromoPlannerPriceOwner",
    "PromoNet_EUR", "PromoNet_USD", "_PromoPlannerPAUnderlying", "_PromoPlannerPAUnresolved",
)

LAYER_PRIORITIES = {"base": 10, "campaign": 20, "override": 30, "import": 10}


SCHEMA = """
CREATE TABLE IF NOT EXISTS pricing_baseline_meta (
  id INTEGER PRIMARY KEY CHECK(id=1),
  imported_at TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_sha256 TEXT NOT NULL DEFAULT '',
  row_count INTEGER NOT NULL,
  columns_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pricing_baseline_rows (
  row_key TEXT PRIMARY KEY,
  row_order INTEGER NOT NULL,
  identity_json TEXT NOT NULL,
  row_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pricing_baseline_order ON pricing_baseline_rows(row_order);
CREATE TABLE IF NOT EXISTS pricing_change_sets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_from TEXT NOT NULL,
  expires_at TEXT NOT NULL DEFAULT '',
  layer TEXT NOT NULL DEFAULT 'base',
  priority INTEGER NOT NULL DEFAULT 10,
  source TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  undone_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pricing_change_sets_effective
  ON pricing_change_sets(effective_from,priority,id);
CREATE TABLE IF NOT EXISTS pricing_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  change_set_id INTEGER NOT NULL,
  row_key TEXT NOT NULL,
  field TEXT NOT NULL,
  old_value_json TEXT NOT NULL,
  new_value_json TEXT NOT NULL,
  FOREIGN KEY(change_set_id) REFERENCES pricing_change_sets(id),
  FOREIGN KEY(row_key) REFERENCES pricing_baseline_rows(row_key),
  UNIQUE(change_set_id,row_key,field)
);
CREATE INDEX IF NOT EXISTS idx_pricing_changes_row ON pricing_changes(row_key,field);
CREATE TABLE IF NOT EXISTS pricing_timeline_migrations (
  migration_key TEXT PRIMARY KEY,
  migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pricing_timeline_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _null(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _json_value(value: Any) -> Any:
    if _null(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return str(value)


def _text(value: Any) -> str:
    if _null(value):
        return ""
    return " ".join(str(value).strip().split())


def _number(value: Any) -> str:
    if _null(value):
        return ""
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    n = float(parsed)
    return str(int(n)) if n.is_integer() else format(n, ".10g")


def row_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        _text(row.get("ISO", "")).upper(),
        _text(row.get("Country", "")).casefold(),
        _text(row.get("PricingUnitIdUsed", "")),
        _text(row.get("Plan", "")),
        _number(row.get("Days", "")),
        _number(row.get("GB", "")),
    )


def _identity_payload(identity: tuple[str, ...]) -> str:
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def row_key_for_identity(row: dict[str, Any]) -> str:
    identity = _identity_payload(row_identity(row))
    return hashlib.sha256(f"{identity}\0{0}".encode("utf-8")).hexdigest()


def _row_records(frame: pd.DataFrame) -> list[tuple[str, str, dict[str, Any]]]:
    if frame.empty:
        return []
    rows = frame.to_dict(orient="records")
    seen: dict[str, int] = {}
    out = []
    for row in rows:
        identity = _identity_payload(row_identity(row))
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        row_key = hashlib.sha256(f"{identity}\0{occurrence}".encode("utf-8")).hexdigest()
        payload = {str(k): _json_value(v) for k, v in row.items()}
        out.append((row_key, identity, payload))
    return out


def _equal(field: str, left: Any, right: Any) -> bool:
    left, right = _json_value(left), _json_value(right)
    if field in {"Price_EUR", "Price_USD", "PromoValue", "_PromoPlannerOriginalPrice_EUR", "_PromoPlannerOriginalPrice_USD"}:
        if left is None or right is None:
            return left is right
        try:
            return abs(float(left) - float(right)) < 1e-7
        except (TypeError, ValueError):
            return str(left) == str(right)
    if field == "AllowBelowCost":
        left_bool = str(left).strip().lower() in {"true", "1", "yes"}
        right_bool = str(right).strip().lower() in {"true", "1", "yes"}
        return left_bool == right_bool
    return left == right


class PricingTimeline:
    """A baseline plus compact, ordered changesets in the shared SQLite file."""

    def __init__(self, database: str | Path):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def baseline_info(self) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM pricing_baseline_meta WHERE id=1").fetchone()
        return dict(row) if row else None

    def revision(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COALESCE(MAX(id),0) FROM pricing_change_sets").fetchone()
        return int(row[0]) if row else 0

    def initialize_baseline(
        self,
        frame: pd.DataFrame,
        *,
        source_path: str | Path = "",
        effective_from: str | None = None,
    ) -> bool:
        """Seed once. Refuse implicit re-seeding after timeline edits exist."""
        if frame.empty:
            raise ValueError("Cannot initialize the pricing timeline from an empty price book.")
        records = _row_records(frame)
        if len({item[0] for item in records}) != len(records):
            raise ValueError("The current price book contains duplicate SKU identities that need review before import.")
        source = Path(source_path) if source_path else None
        digest = hashlib.sha256(source.read_bytes()).hexdigest() if source and source.exists() else ""
        effective_from = effective_from or date.today().isoformat()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT 1 FROM pricing_baseline_meta WHERE id=1").fetchone()
            if existing:
                con.rollback()
                return False
            con.executemany(
                "INSERT INTO pricing_baseline_rows(row_key,row_order,identity_json,row_json) VALUES(?,?,?,?)",
                [
                    (key, order, identity, json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    for order, (key, identity, row) in enumerate(records)
                ],
            )
            con.execute(
                """INSERT INTO pricing_baseline_meta
                (id,imported_at,effective_from,source_path,source_sha256,row_count,columns_json)
                VALUES(1,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(timespec="seconds"), effective_from,
                    str(source or ""), digest, len(records),
                    json.dumps([str(c) for c in frame.columns], ensure_ascii=False),
                ),
            )
            con.execute(
                "INSERT OR REPLACE INTO pricing_timeline_state(key,value) VALUES('current_as_of',?)",
                (effective_from,),
            )
            con.commit()
        return True

    def current_as_of(self) -> str:
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM pricing_timeline_state WHERE key='current_as_of'"
            ).fetchone()
            if row:
                return str(row[0])
            row = con.execute("SELECT effective_from FROM pricing_baseline_meta WHERE id=1").fetchone()
            return str(row[0]) if row else date.today().isoformat()

    def set_current_as_of(self, effective_date: str) -> None:
        date.fromisoformat(effective_date)
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO pricing_timeline_state(key,value) VALUES('current_as_of',?)",
                (effective_date,),
            )

    def has_row(self, row_key: str) -> bool:
        with self._connect() as con:
            return con.execute("SELECT 1 FROM pricing_baseline_rows WHERE row_key=?", (row_key,)).fetchone() is not None

    @staticmethod
    def diff_summary(before: pd.DataFrame, after: pd.DataFrame) -> tuple[int, int]:
        """Return changed field and row counts, rejecting catalogue changes."""
        before_rows = _row_records(before)
        after_rows = _row_records(after)
        old_by_key = {key: row for key, _identity, row in before_rows}
        new_by_key = {key: row for key, _identity, row in after_rows}
        if old_by_key.keys() != new_by_key.keys():
            raise ValueError("The edited price book added or removed SKUs. Use the explicit catalogue migration workflow.")
        changed_rows = set()
        changed_fields = 0
        for key, old in old_by_key.items():
            new = new_by_key[key]
            for field in EDITABLE_FIELDS:
                if (field in old or field in new) and not _equal(field, old.get(field), new.get(field)):
                    changed_fields += 1
                    changed_rows.add(key)
        return changed_fields, len(changed_rows)

    def record_diff(
        self,
        before: pd.DataFrame,
        after: pd.DataFrame,
        *,
        effective_from: str | None = None,
        expires_at: str = "",
        layer: str = "base",
        source: str = "editor",
        label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int | None, int]:
        """Append only editable-field changes; an expiry reveals the lower layer."""
        before_rows = _row_records(before)
        after_rows = _row_records(after)
        old_by_key = {key: row for key, _identity, row in before_rows}
        new_by_key = {key: row for key, _identity, row in after_rows}
        if old_by_key.keys() != new_by_key.keys():
            raise ValueError("The edited price book added or removed SKUs. Use the explicit Excel import workflow for catalogue changes.")
        diffs = []
        for key, old in old_by_key.items():
            new = new_by_key[key]
            for field in EDITABLE_FIELDS:
                if field in old or field in new:
                    old_value, new_value = old.get(field), new.get(field)
                    if not _equal(field, old_value, new_value):
                        diffs.append((key, field, old_value, new_value))
        if not diffs:
            return None, 0
        effective_from = effective_from or self.current_as_of()
        layer = layer if layer in LAYER_PRIORITIES else "base"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            unknown = [key for key, _field, _old, _new in diffs if con.execute(
                "SELECT 1 FROM pricing_baseline_rows WHERE row_key=?", (key,)
            ).fetchone() is None]
            if unknown:
                con.rollback()
                raise ValueError("A changed SKU is not in the imported pricing baseline. Re-import through the review workflow.")
            cur = con.execute(
                """INSERT INTO pricing_change_sets
                (effective_from,expires_at,layer,priority,source,label,created_at,metadata_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    effective_from, expires_at or "", layer, LAYER_PRIORITIES[layer],
                    source, label, datetime.now().isoformat(timespec="seconds"),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            change_set_id = int(cur.lastrowid)
            con.executemany(
                """INSERT INTO pricing_changes
                (change_set_id,row_key,field,old_value_json,new_value_json)
                VALUES(?,?,?,?,?)""",
                [
                    (change_set_id, key, field, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False))
                    for key, field, old, new in diffs
                ],
            )
            con.commit()
        return change_set_id, len(diffs)

    def project(
        self,
        as_of: str | date | None = None,
        *,
        layers: Iterable[str] | None = None,
        exclude_key_date: str | None = None,
        include_drafts: bool | None = None,
    ) -> pd.DataFrame:
        include_drafts = (as_of is not None) if include_drafts is None else include_drafts
        as_of = as_of.isoformat() if isinstance(as_of, date) else str(as_of or self.current_as_of())
        allowed_layers = set(layers) if layers is not None else None
        with self._connect() as con:
            baseline = con.execute("SELECT row_key,row_order,row_json FROM pricing_baseline_rows ORDER BY row_order").fetchall()
            if not baseline:
                return pd.DataFrame()
            sets = con.execute(
                """SELECT id,effective_from,expires_at,layer,priority,metadata_json FROM pricing_change_sets
                WHERE undone_at='' AND effective_from<=?
                  AND (expires_at='' OR expires_at>?)
                ORDER BY effective_from,priority,id""",
                (as_of, as_of),
            ).fetchall()
            if exclude_key_date is not None:
                sets = [
                    row for row in sets
                    if json.loads(row["metadata_json"] or "{}").get("key_date") != exclude_key_date
                ]
            if allowed_layers is not None:
                sets = [row for row in sets if row["layer"] in allowed_layers]
            if not include_drafts:
                price_events = {
                    (m["metadata"].get("key_date"), m["metadata"].get("event_label"))
                    for m in self.event_markers() if m["source"] == "price_date"
                }
                sets = [row for row in sets if not (
                    ((metadata := json.loads(row["metadata_json"] or "{}")).get("is_base_price_change")
                     or (metadata.get("event_label") and (metadata.get("key_date"), metadata.get("event_label")) in price_events))
                    and not metadata.get("official")
                )]
            rows = {row["row_key"]: json.loads(row["row_json"]) for row in baseline}
            order = [row["row_key"] for row in baseline]
            for change_set in sets:
                changes = con.execute(
                    "SELECT row_key,field,new_value_json FROM pricing_changes WHERE change_set_id=? ORDER BY id",
                    (change_set["id"],),
                ).fetchall()
                for change in changes:
                    target = rows.get(change["row_key"])
                    if target is not None:
                        target[change["field"]] = json.loads(change["new_value_json"])
        frame = pd.DataFrame([rows[key] for key in order])
        columns = [str(c) for c in frame.columns]
        return frame.reindex(columns=columns)

    def list_change_sets(self, *, source: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as con:
            if source:
                rows = con.execute(
                    "SELECT s.*,count(c.id) AS field_changes FROM pricing_change_sets s LEFT JOIN pricing_changes c ON c.change_set_id=s.id WHERE s.source=? GROUP BY s.id ORDER BY s.effective_from,s.id",
                    (source,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT s.*,count(c.id) AS field_changes FROM pricing_change_sets s LEFT JOIN pricing_changes c ON c.change_set_id=s.id GROUP BY s.id ORDER BY s.effective_from,s.id"
                ).fetchall()
        return [dict(row) for row in rows]

    def undo_change_set(self, change_set_id: int) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE pricing_change_sets SET undone_at=? WHERE id=? AND undone_at=''",
                (datetime.now().isoformat(timespec="seconds"), int(change_set_id)),
            )

    def replace_key_date_overrides(self, key_date: str, groups: list[dict[str, Any]]) -> int:
        """Atomically replace editor overrides for one date, retaining their audit rows."""
        total = 0
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT id,metadata_json FROM pricing_change_sets WHERE source='editor_date' AND undone_at=''"
            ).fetchall()
            known_row_keys = {row[0] for row in con.execute("SELECT row_key FROM pricing_baseline_rows").fetchall()}
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"])
                except ValueError:
                    metadata = {}
                if metadata.get("key_date") == key_date:
                    con.execute(
                        "UPDATE pricing_change_sets SET undone_at=? WHERE id=?",
                        (datetime.now().isoformat(timespec="seconds"), row["id"]),
                    )
            for group in groups:
                fields = group.get("fields", [])
                if not fields:
                    continue
                if not {f[0] for f in fields}.issubset(known_row_keys):
                    raise ValueError("A dated override refers to a SKU outside the pricing baseline.")
                cur = con.execute(
                    """INSERT INTO pricing_change_sets
                    (effective_from,expires_at,layer,priority,source,label,created_at,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        group["effective_from"], group.get("expires_at", ""), "override",
                        LAYER_PRIORITIES["override"], "editor_date", str(group.get("label") or "Editor dated override"),
                        datetime.now().isoformat(timespec="seconds"),
                        json.dumps(group.get("metadata", {}), ensure_ascii=False),
                    ),
                )
                change_set_id = int(cur.lastrowid)
                con.executemany(
                    """INSERT INTO pricing_changes
                    (change_set_id,row_key,field,old_value_json,new_value_json)
                    VALUES(?,?,?,?,?)""",
                    [
                        (change_set_id, row_key, field, json.dumps(old), json.dumps(new))
                        for row_key, field, old, new in fields
                    ],
                )
                total += len(fields)
            con.commit()
        return total

    def migrate_legacy_date_overrides(self) -> None:
        """Move old campaign-keyed DB overrides into the general timeline once."""
        if not self.baseline_info():
            return
        marker = "legacy_key_date_price_changes_v1"
        with self._connect() as con:
            if con.execute("SELECT 1 FROM pricing_timeline_migrations WHERE migration_key=?", (marker,)).fetchone():
                return
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='key_date_price_changes'").fetchone():
                legacy = con.execute("SELECT * FROM key_date_price_changes ORDER BY effective_from,id").fetchall()
            else:
                legacy = []
            base_rows = con.execute("SELECT row_key,identity_json,row_json FROM pricing_baseline_rows").fetchall()
            by_identity = {row["identity_json"]: row for row in base_rows}
            grouped: dict[tuple, list] = {}
            for item in legacy:
                try:
                    identity_row = json.loads(item["row_json"])
                    identity = _identity_payload(row_identity(identity_row))
                    base = by_identity.get(identity)
                    if base is None:
                        continue
                    base_payload = json.loads(base["row_json"])
                    fields = []
                    for currency in ("eur", "usd"):
                        column = f"price_{currency}"
                        new_value = item[column]
                        if new_value is not None:
                            field = f"Price_{currency.upper()}"
                            fields.append((base["row_key"], field, base_payload.get(field), float(new_value)))
                    if not fields:
                        continue
                    key = (
                        item["key_date"], item["effective_from"], item["expires_at"],
                        int(item["campaign_id"] or 0), item["campaign_name"],
                        item["promo_start_date"], item["promo_end_date"],
                    )
                    grouped.setdefault(key, []).extend(fields)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            for key, fields in grouped.items():
                key_date, effective, expires, campaign_id, name, start, end = key
                cur = con.execute(
                    """INSERT INTO pricing_change_sets
                    (effective_from,expires_at,layer,priority,source,label,created_at,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        effective, expires or "", "override", LAYER_PRIORITIES["override"],
                        "legacy_key_date", "Migrated dated price override",
                        datetime.now().isoformat(timespec="seconds"),
                        json.dumps({"key_date": key_date, "campaign_id": campaign_id,
                                    "campaign_name": name, "promo_start_date": start,
                                    "promo_end_date": end}, ensure_ascii=False),
                    ),
                )
                con.executemany(
                    """INSERT INTO pricing_changes
                    (change_set_id,row_key,field,old_value_json,new_value_json)
                    VALUES(?,?,?,?,?)""",
                    [(int(cur.lastrowid), row_key, field, json.dumps(old), json.dumps(new)) for row_key, field, old, new in fields],
                )
            con.execute(
                "INSERT INTO pricing_timeline_migrations(migration_key,migrated_at) VALUES(?,?)",
                (marker, datetime.now().isoformat(timespec="seconds")),
            )
            con.commit()

    def date_override_records(self, boundary: str | None = None, *, key_date: str | None = None, exclude_key_date: str | None = None) -> list[dict[str, Any]]:
        self.migrate_legacy_date_overrides()
        price_events = {
            (m["metadata"].get("key_date"), m["metadata"].get("event_label"))
            for m in self.event_markers() if m["source"] == "price_date"
        }
        with self._connect() as con:
            if boundary is None:
                sets = con.execute(
                    """SELECT * FROM pricing_change_sets WHERE source IN ('editor_date','legacy_key_date','official_base')
                    AND undone_at='' ORDER BY effective_from,priority,id"""
                ).fetchall()
            else:
                sets = con.execute(
                    """SELECT * FROM pricing_change_sets WHERE source IN ('editor_date','legacy_key_date','official_base')
                    AND undone_at='' AND effective_from<=?
                    ORDER BY effective_from,priority,id""",
                    (boundary,),
                ).fetchall()
            grouped: dict[tuple[int, str], dict[str, Any]] = {}
            for change_set in sets:
                metadata = json.loads(change_set["metadata_json"] or "{}")
                event_date = str(metadata.get("key_date", ""))
                is_base = bool(metadata.get("is_base_price_change") or (
                    metadata.get("event_label") and (event_date, metadata.get("event_label")) in price_events
                ))
                if boundary and change_set["expires_at"] and change_set["expires_at"] <= boundary and not is_base:
                    continue
                if key_date is not None and event_date != key_date:
                    continue
                if exclude_key_date and event_date == exclude_key_date:
                    continue
                changes = con.execute(
                    """SELECT c.row_key,c.field,c.old_value_json,c.new_value_json,b.identity_json
                    FROM pricing_changes c JOIN pricing_baseline_rows b ON b.row_key=c.row_key
                    WHERE c.change_set_id=? ORDER BY c.id""",
                    (change_set["id"],),
                ).fetchall()
                for change in changes:
                    identity = json.loads(change["identity_json"])
                    if len(identity) != 6:
                        continue
                    row = {
                        "iso": identity[0], "country": identity[1], "pricing_unit_id": identity[2],
                        "plan": identity[3], "days": int(float(identity[4] or 0)),
                        "gb": float(identity[5]) if identity[5] else 0,
                    }
                    item_key = (int(change_set["id"]), change["row_key"])
                    item = grouped.setdefault(item_key, {
                        "key_date": event_date, "effective_from": change_set["effective_from"],
                        "expires_at": "" if is_base else change_set["expires_at"],
                        "campaign_id": 0 if is_base else int(metadata.get("campaign_id") or 0),
                        "campaign_name": str(metadata.get("campaign_name", "")),
                        "is_base_price_change": is_base,
                        "official": bool(metadata.get("official")),
                        "promo_start_date": str(metadata.get("promo_start_date", "")),
                        "promo_end_date": str(metadata.get("promo_end_date", "")),
                        "row": row,
                        "old_values": {},
                    })
                    field = change["field"]
                    old_value = json.loads(change["old_value_json"])
                    item["old_values"][field] = old_value
                    currency = field.replace("Price_", "").lower()
                    if currency in {"eur", "usd"}:
                        item[f"price_{currency}"] = json.loads(change["new_value_json"])
                    elif field in {"PromoCode", "PromoType", "PromoValue", "PromoLabel"}:
                        promo_field = {
                            "PromoCode": "promo_code", "PromoType": "promo_type",
                            "PromoValue": "promo_value", "PromoLabel": "promo_label",
                        }[field]
                        item[promo_field] = json.loads(change["new_value_json"])
            result = list(grouped.values())
        return result

    def schedule_price_change_date(self, key_date: str, label: str) -> None:
        if self.is_price_date_official(key_date):
            raise ValueError("This price date is already official. Add another date to change prices.")
        self._schedule_event_date(key_date, label, "price_date", "price_change_date")

    def schedule_promotion_date(self, key_date: str, label: str = "New promotion") -> None:
        self._schedule_event_date(key_date, label, "promotion_date", "promotion_start_date")

    def move_price_change_date(self, key_date: str, effective_date: str, *, today: date | None = None) -> str:
        """Move a future price event with its price edits, retaining audit history."""
        today = today or date.today()
        effective = date.fromisoformat(effective_date)
        old_effective = date.fromisoformat(key_date) + timedelta(days=1)
        markers = [m for m in self.event_markers(key_date) if m["source"] == "price_date"]
        if not markers:
            raise ValueError("Select a price-change event to move.")
        if effective < today or old_effective < today or (markers[0]["metadata"].get("official") and old_effective <= today):
            raise ValueError("Only a future price event can be moved. Add a new change for an effective official date.")
        new_key = (effective - timedelta(days=1)).isoformat()
        if new_key == key_date:
            return new_key
        if any(m["source"] == "price_date" for m in self.event_markers(new_key)):
            raise ValueError("A price change already exists on that date. Choose another date to avoid replacing it.")
        reference = self.project(effective_date, layers=("base", "import", "override"), exclude_key_date=key_date)
        reference_rows = {key: row for key, _identity, row in _row_records(reference)}
        marker = markers[0]
        old_label = marker["metadata"].get("event_label", marker["label"])
        label = f"Price update {effective:%d %b %Y}" if old_label == f"Price update {old_effective:%d %b %Y}" else old_label
        stamp = datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT * FROM pricing_change_sets WHERE source IN ('price_date','editor_date','legacy_key_date','official_base') AND undone_at='' ORDER BY id").fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                if metadata.get("key_date") != key_date:
                    continue
                if row["source"] != "price_date" and not (metadata.get("is_base_price_change") or metadata.get("event_label") == old_label):
                    continue
                was_official = bool(metadata.get("official"))
                metadata.update(key_date=new_key, event_label=label, moved_from=old_effective.isoformat(),
                                is_base_price_change=row["source"] != "price_date", official=False)
                if was_official:
                    metadata["previous_partner_pack"] = metadata.pop("partner_pack", "")
                    metadata.pop("official_at", None)
                source = "price_date" if row["source"] == "price_date" else "editor_date"
                layer = "base" if source == "price_date" else "override"
                cur = con.execute("INSERT INTO pricing_change_sets(effective_from,expires_at,layer,priority,source,label,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                                  (effective.isoformat(), "", layer, LAYER_PRIORITIES[layer], source, label, stamp, json.dumps(metadata)))
                changes = con.execute("SELECT row_key,field,old_value_json,new_value_json FROM pricing_changes WHERE change_set_id=? AND field IN ('Price_EUR','Price_USD')", (row["id"],)).fetchall()
                con.executemany("INSERT INTO pricing_changes(change_set_id,row_key,field,old_value_json,new_value_json) VALUES(?,?,?,?,?)", [
                    (cur.lastrowid, c["row_key"], c["field"], json.dumps(_json_value(reference_rows.get(c["row_key"], {}).get(c["field"], json.loads(c["old_value_json"])))), c["new_value_json"])
                    for c in changes
                ])
                con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (stamp, row["id"]))
            con.commit()
        return new_key

    def move_promotion_date(self, key_date: str, effective_date: str, *, today: date | None = None) -> str:
        today = today or date.today()
        effective = date.fromisoformat(effective_date)
        if effective < today or date.fromisoformat(key_date) + timedelta(days=1) < today:
            raise ValueError("Choose a current or future date for the unfinished promotion.")
        new_key = (effective - timedelta(days=1)).isoformat()
        if new_key == key_date:
            return new_key
        markers = [m for m in self.event_markers(key_date) if m["source"] == "promotion_date"]
        if not markers:
            raise ValueError("No unfinished promotion is selected.")
        if any(m["source"] == "promotion_date" for m in self.event_markers(new_key)):
            raise ValueError("Another unfinished promotion already uses that date.")
        marker = markers[0]
        metadata = dict(marker["metadata"], key_date=new_key, moved_from=marker["effective_from"])
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            stamp = datetime.now().isoformat(timespec="seconds")
            con.execute("INSERT INTO pricing_change_sets(effective_from,expires_at,layer,priority,source,label,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                        (effective.isoformat(), "", "base", LAYER_PRIORITIES["base"], "promotion_date", marker["label"], stamp, json.dumps(metadata)))
            con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (stamp, marker["id"]))
            con.commit()
        return new_key

    def event_markers(self, key_date: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM pricing_change_sets WHERE source IN ('price_date','promotion_date') AND undone_at='' ORDER BY id"
            ).fetchall()
        markers = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata_json"] or "{}")
            if key_date is None or item["metadata"].get("key_date") == key_date:
                markers.append(item)
        return markers

    def is_price_date_official(self, key_date: str) -> bool:
        return any(m["source"] == "price_date" and m["metadata"].get("official") for m in self.event_markers(key_date))

    def discard_promotion_date(self, key_date: str) -> None:
        with self._connect() as con:
            for row in con.execute("SELECT id,metadata_json FROM pricing_change_sets WHERE source='promotion_date' AND undone_at='' ").fetchall():
                if json.loads(row["metadata_json"] or "{}").get("key_date") == key_date:
                    con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), row["id"]))

    def cancel_price_change_date(self, key_date: str, *, today: date | None = None) -> None:
        today = today or date.today()
        effective = date.fromisoformat(key_date) + timedelta(days=1)
        if effective < today or (self.is_price_date_official(key_date) and effective <= today):
            raise ValueError("An effective official price change is kept in history. Add a new dated change instead.")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT id,source,metadata_json FROM pricing_change_sets WHERE source IN ('price_date','editor_date','official_base') AND undone_at='' ").fetchall()
            markers = [r for r in rows if r["source"] == "price_date" and json.loads(r["metadata_json"] or "{}").get("key_date") == key_date]
            if not markers:
                raise ValueError("No price-change event is selected.")
            label = json.loads(markers[0]["metadata_json"] or "{}").get("event_label", "")
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                if metadata.get("key_date") != key_date:
                    continue
                belongs = row["source"] in {"price_date", "official_base"} or metadata.get("is_base_price_change") or (label and metadata.get("event_label") == label)
                if belongs:
                    con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), row["id"]))
            con.commit()

    def mark_price_date_official(self, key_date: str, *, partner_pack: str = "", _connection=None) -> int:
        """Commit only this event's list-price changes as a permanent dated base layer."""
        markers = [m for m in self.event_markers(key_date) if m["source"] == "price_date"]
        if not markers:
            return 0  # A campaign-only export never bakes its discount into base prices.
        if self.is_price_date_official(key_date):
            return 0
        with (self._connect() if _connection is None else nullcontext(_connection)) as con:
            if _connection is None:
                con.execute("BEGIN IMMEDIATE")
            drafts = con.execute("SELECT id,metadata_json FROM pricing_change_sets WHERE source='editor_date' AND undone_at='' ORDER BY effective_from,priority,id").fetchall()
            fields = {}
            selected = []
            for draft in drafts:
                metadata = json.loads(draft["metadata_json"] or "{}")
                if metadata.get("key_date") != key_date:
                    continue
                if not metadata.get("is_base_price_change") and metadata.get("event_label") != markers[0]["metadata"].get("event_label"):
                    continue
                selected.append(draft["id"])
                for field in con.execute("SELECT row_key,field,old_value_json,new_value_json FROM pricing_changes WHERE change_set_id=? AND field IN ('Price_EUR','Price_USD')", (draft["id"],)):
                    fields[(field["row_key"],field["field"])] = tuple(field)
            marker = markers[0]
            metadata = dict(marker["metadata"], official=True, official_at=datetime.now().isoformat(timespec="seconds"), partner_pack=partner_pack, is_base_price_change=True)
            if fields:
                cur = con.execute("INSERT INTO pricing_change_sets(effective_from,expires_at,layer,priority,source,label,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)", (marker["effective_from"], "", "base", LAYER_PRIORITIES["base"], "official_base", marker["label"], datetime.now().isoformat(timespec="seconds"), json.dumps(metadata)))
                con.executemany("INSERT INTO pricing_changes(change_set_id,row_key,field,old_value_json,new_value_json) VALUES(?,?,?,?,?)", [(cur.lastrowid,*field) for field in fields.values()])
            for draft_id in selected:
                con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), draft_id))
            con.execute("UPDATE pricing_change_sets SET metadata_json=? WHERE id=?", (json.dumps(metadata), marker["id"]))
            if _connection is None:
                con.commit()
        return len(fields)

    def mark_price_changes_official_through(self, key_date: str, *, partner_pack: str = "") -> int:
        """Publish all base-price events included in this date's exported snapshot."""
        keys = sorted({
            marker["metadata"]["key_date"] for marker in self.event_markers()
            if marker["source"] == "price_date"
            and marker["metadata"].get("key_date", "") <= key_date
            and not marker["metadata"].get("official")
        })
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            count = sum(self.mark_price_date_official(key, partner_pack=partner_pack, _connection=con) for key in keys)
            con.commit()
        return count

    def _schedule_event_date(self, key_date: str, label: str, source: str, event_type: str) -> None:
        key = date.fromisoformat(str(key_date)).isoformat()
        name = " ".join(str(label).strip().split())
        if not name:
            raise ValueError("A name is required for the planned price change.")
        effective = (date.fromisoformat(key) + timedelta(days=1)).isoformat()
        metadata = {"key_date": key, "event_type": event_type, "event_label": name}
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT id,metadata_json FROM pricing_change_sets WHERE source=? AND undone_at=''", (source,)
            ).fetchall()
            marker_id = None
            for row in existing:
                try:
                    if json.loads(row["metadata_json"] or "{}").get("key_date") == key:
                        marker_id = int(row["id"])
                        break
                except (TypeError, ValueError):
                    continue
            if marker_id is not None:
                con.execute(
                    "UPDATE pricing_change_sets SET effective_from=?,label=?,metadata_json=? WHERE id=?",
                    (effective, name, json.dumps(metadata, ensure_ascii=False), marker_id),
                )
            else:
                con.execute(
                    """INSERT INTO pricing_change_sets
                    (effective_from,expires_at,layer,priority,source,label,created_at,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (effective, "", "base", LAYER_PRIORITIES["base"], source, name,
                     datetime.now().isoformat(timespec="seconds"), json.dumps(metadata, ensure_ascii=False)),
                )
            con.commit()

    def key_date_labels(self) -> dict[str, list[str]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT source,label,metadata_json FROM pricing_change_sets "
                "WHERE source IN ('price_date','promotion_date','editor_date','legacy_key_date','official_base') AND undone_at='' "
                "ORDER BY effective_from,id"
            ).fetchall()
        labels: dict[str, list[str]] = {}
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            key = str(metadata.get("key_date", ""))
            if not key:
                continue
            if row["source"] == "promotion_date":
                text = f"PROMOTION: {metadata.get('event_label') or row['label']}"
            elif row["source"] == "price_date":
                text = f"PRICE CHANGE: {metadata.get('event_label') or row['label'] or 'Planned price change'}"
            elif metadata.get("event_label"):
                text = f"PRICE CHANGE: {metadata['event_label']}"
            elif metadata.get("campaign_name"):
                text = f"PRICE OVERRIDE: {metadata['campaign_name']}"
            else:
                text = "PRICE CHANGE"
            labels.setdefault(key, [])
            if text not in labels[key]:
                labels[key].append(text)
        return labels
    def key_dates(self, *, today: str | None = None) -> list[str]:
        self.migrate_legacy_date_overrides()
        with self._connect() as con:
            sets = con.execute(
                "SELECT metadata_json FROM pricing_change_sets WHERE source IN ('editor_date','legacy_key_date','price_date','promotion_date','official_base') AND undone_at=''"
            ).fetchall()
        dates = set()
        for row in sets:
            try:
                key_date = str(json.loads(row["metadata_json"] or "{}").get("key_date", ""))
                if key_date and (today is None or key_date >= today):
                    dates.add(key_date)
            except (TypeError, ValueError):
                pass
        return sorted(dates)

