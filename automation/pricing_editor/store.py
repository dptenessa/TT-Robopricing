from __future__ import annotations
import json
import csv
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


@dataclass
class Campaign:
    id: int | None
    name: str
    start_date: str
    end_date: str
    promo_type: str
    promo_value: float
    promo_code: str
    promo_name: str
    destinations: list[str]
    plans: list[str]
    days: list[int]
    allow_below_cost: bool = False
    notes: str = ""
    status: str = "PLANNED"
    created_at: str = ""
    # Legacy fields kept for backwards compatibility with v0.1-v0.4 databases.
    official_partner_pack: str = ""
    official_partner_pack_at: str = ""
    outlook_start_id: str = ""
    outlook_end_id: str = ""

    monotonic_destinations: list[str] = field(default_factory=list)
    monotonic_all_promos: bool = False

    @property
    def display_start_date(self) -> str:
        """Stable visual anchor; an unknown actual start remains unknown."""
        start = self.start_date
        if not start:
            try:
                start = date.fromisoformat(self.created_at[:10]).isoformat()
            except (ValueError, TypeError):
                start = self.end_date
        return min(start, self.end_date) if self.end_date else start

    @property
    def is_locked(self) -> bool:
        # v0.5 locks from publication state in app/store logic, not partner-pack state.
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  promo_type TEXT NOT NULL,
  promo_value REAL NOT NULL,
  promo_code TEXT NOT NULL,
  promo_name TEXT NOT NULL DEFAULT '',
  destinations_json TEXT NOT NULL,
  plans_json TEXT NOT NULL,
  days_json TEXT NOT NULL,
  allow_below_cost INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'PLANNED',
  created_at TEXT NOT NULL,
  official_partner_pack TEXT NOT NULL DEFAULT '',
  official_partner_pack_at TEXT NOT NULL DEFAULT '',
  outlook_start_id TEXT NOT NULL DEFAULT '',
  outlook_end_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  executed_at TEXT NOT NULL,
  workbook_path TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  affected_rows INTEGER NOT NULL,
  details TEXT NOT NULL DEFAULT '',
  batch_id INTEGER,
  FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);
CREATE INDEX IF NOT EXISTS idx_exec_campaign_action ON executions(campaign_id, action);
CREATE INDEX IF NOT EXISTS idx_exec_batch ON executions(batch_id);
CREATE TABLE IF NOT EXISTS execution_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_date TEXT NOT NULL,
  executed_at TEXT NOT NULL,
  workbook_path TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '[]',
  undone_at TEXT NOT NULL DEFAULT '',
  timeline_change_set_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_batch_date ON execution_batches(execution_date);
CREATE TABLE IF NOT EXISTS groups (
  name TEXT PRIMARY KEY,
  destinations_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS key_date_price_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_date TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  expires_at TEXT NOT NULL DEFAULT '',
  campaign_id INTEGER NOT NULL DEFAULT 0,
  campaign_name TEXT NOT NULL DEFAULT '',
  promo_start_date TEXT NOT NULL DEFAULT '',
  promo_end_date TEXT NOT NULL DEFAULT '',
  identity_key TEXT NOT NULL,
  row_json TEXT NOT NULL,
  price_eur REAL,
  price_usd REAL,
  updated_at TEXT NOT NULL DEFAULT '',
  UNIQUE(key_date, campaign_id, identity_key)
);
CREATE INDEX IF NOT EXISTS idx_key_date_price_changes_effective
  ON key_date_price_changes(effective_from, expires_at);
CREATE TABLE IF NOT EXISTS campaign_definition_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL,
  changed_at TEXT NOT NULL,
  old_json TEXT NOT NULL,
  new_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaign_definition_history_campaign ON campaign_definition_history(campaign_id,id);
CREATE TABLE IF NOT EXISTS store_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class PromoStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            self._migrate(con)
            self._migrate_legacy_key_date_changes(con)
            self._migrate_pack_publications(con)

    def _migrate_pack_publications(self, con):
        """Recover publication marks from packs created before campaign locking existed."""
        marker = "official_campaign_publications_v1"
        if con.execute("SELECT 1 FROM store_metadata WHERE key=?", (marker,)).fetchone():
            return
        history = self.path.parent.parent / "manual_prices" / "history"
        campaigns = {int(row["id"]): row for row in con.execute("SELECT * FROM campaigns")}
        if campaigns:
            for metadata_path in sorted(history.glob("export_*.json")):
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if metadata.get("official") is not True or not metadata.get("partner_pack_created"):
                        continue
                    token = metadata_path.stem[len("export_"):]
                    effective = datetime.strptime(token[:8], "%Y%m%d").date().isoformat()
                    snapshot = history / f"manual_prices_{token}.csv"
                    published = set()
                    with snapshot.open(encoding="utf-8-sig", newline="") as stream:
                        for row in csv.DictReader(stream):
                            try:
                                owner = int(float(row.get("_PromoPlannerPromoOwner") or 0))
                            except (TypeError, ValueError):
                                continue
                            campaign = campaigns.get(owner)
                            if campaign is None or campaign["start_date"] > effective or campaign["end_date"] < effective:
                                continue
                            if row.get("PromoCode") in {campaign["promo_code"], "PA"}:
                                published.add(owner)
                    con.executemany("UPDATE campaigns SET official_partner_pack=?,official_partner_pack_at=? WHERE id=?",
                                    [(metadata.get("partner_zip_path") or metadata.get("partner_zip_name") or "Official pack",
                                      metadata.get("exported_at") or effective, owner) for owner in published])
                except (OSError, ValueError, TypeError):
                    continue
        con.execute("INSERT INTO store_metadata(key,value) VALUES(?,?)", (marker, "1"))

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            with con:
                yield con

    def _migrate_legacy_key_date_changes(self, con: sqlite3.Connection) -> None:
        """Import the old JSON schedule once, leaving the source file untouched."""
        marker = "legacy_key_date_price_changes_imported"
        if con.execute("SELECT 1 FROM store_metadata WHERE key=?", (marker,)).fetchone():
            return
        source = self.path.with_name("date_price_changes.json")
        try:
            payload = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
        except (OSError, ValueError, TypeError):
            payload = {}
        changes = payload.get("changes", []) if isinstance(payload, dict) else []
        for item in changes:
            if not isinstance(item, dict):
                continue
            row = item.get("row", {})
            if not isinstance(row, dict):
                row = {}
            try:
                campaign_id = int(item.get("campaign_id") or 0)
            except (TypeError, ValueError):
                campaign_id = 0
            identity_key = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            con.execute(
                """INSERT OR IGNORE INTO key_date_price_changes
                (key_date,effective_from,expires_at,campaign_id,campaign_name,
                 promo_start_date,promo_end_date,identity_key,row_json,price_eur,price_usd,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(item.get("key_date", "")), str(item.get("effective_from", "")),
                    str(item.get("expires_at", "") or ""), campaign_id,
                    str(item.get("campaign_name", "") or ""),
                    str(item.get("promo_start_date", "") or ""),
                    str(item.get("promo_end_date", "") or ""), identity_key,
                    json.dumps(row, ensure_ascii=False), item.get("price_eur"),
                    item.get("price_usd"), str(item.get("updated_at", "") or ""),
                ),
            )
        con.execute("INSERT INTO store_metadata(key,value) VALUES(?,?)", (marker, "1"))

    def list_key_date_price_changes(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM key_date_price_changes ORDER BY effective_from,id"
            ).fetchall()
        return [
            {
                "key_date": row["key_date"],
                "effective_from": row["effective_from"],
                "expires_at": row["expires_at"] or "",
                "campaign_id": row["campaign_id"],
                "campaign_name": row["campaign_name"],
                "promo_start_date": row["promo_start_date"],
                "promo_end_date": row["promo_end_date"],
                "row": json.loads(row["row_json"]),
                "updated_at": row["updated_at"],
                **({"price_eur": row["price_eur"]} if row["price_eur"] is not None else {}),
                **({"price_usd": row["price_usd"]} if row["price_usd"] is not None else {}),
            }
            for row in rows
        ]

    def replace_key_date_price_changes(self, key_date: str, changes: list[dict]) -> None:
        """Atomically replace one date's price overrides, preserving every other date."""
        with self._connect() as con:
            con.execute("DELETE FROM key_date_price_changes WHERE key_date=?", (key_date,))
            for item in changes:
                row = item.get("row", {})
                campaign_id = int(item.get("campaign_id") or 0)
                identity_key = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                con.execute(
                    """INSERT INTO key_date_price_changes
                    (key_date,effective_from,expires_at,campaign_id,campaign_name,
                     promo_start_date,promo_end_date,identity_key,row_json,price_eur,price_usd,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key_date, str(item.get("effective_from", "")), str(item.get("expires_at", "") or ""),
                        campaign_id, str(item.get("campaign_name", "") or ""),
                        str(item.get("promo_start_date", "") or ""), str(item.get("promo_end_date", "") or ""),
                        identity_key, json.dumps(row, ensure_ascii=False), item.get("price_eur"),
                        item.get("price_usd"), str(item.get("updated_at", "") or ""),
                    ),
                )

    def key_date_price_changes_for_date(self, key_date: str) -> list[dict]:
        return [item for item in self.list_key_date_price_changes() if item["key_date"] == key_date]

    def _migrate(self, con: sqlite3.Connection) -> None:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(campaigns)").fetchall()}
        for name, ddl in (
            ("promo_name", "TEXT NOT NULL DEFAULT ''"),
            ("official_partner_pack", "TEXT NOT NULL DEFAULT ''"),
            ("official_partner_pack_at", "TEXT NOT NULL DEFAULT ''"),
            ("outlook_start_id", "TEXT NOT NULL DEFAULT ''"),
            ("outlook_end_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in cols:
                con.execute(f"ALTER TABLE campaigns ADD COLUMN {name} {ddl}")

        table_sql_row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='campaigns'").fetchone()
        table_sql = (table_sql_row[0] if table_sql_row else "") or ""
        if "promo_code TEXT NOT NULL UNIQUE" in table_sql or "UNIQUE (promo_code)" in table_sql or "UNIQUE(promo_code)" in table_sql:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("""CREATE TABLE campaigns_v05 (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              promo_type TEXT NOT NULL, promo_value REAL NOT NULL, promo_code TEXT NOT NULL,
              promo_name TEXT NOT NULL DEFAULT '', destinations_json TEXT NOT NULL,
              plans_json TEXT NOT NULL, days_json TEXT NOT NULL,
              allow_below_cost INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'PLANNED', created_at TEXT NOT NULL,
              official_partner_pack TEXT NOT NULL DEFAULT '', official_partner_pack_at TEXT NOT NULL DEFAULT '',
              outlook_start_id TEXT NOT NULL DEFAULT '', outlook_end_id TEXT NOT NULL DEFAULT ''
            )""")
            con.execute("""INSERT INTO campaigns_v05
              (id,name,start_date,end_date,promo_type,promo_value,promo_code,promo_name,destinations_json,plans_json,days_json,
               allow_below_cost,notes,status,created_at,official_partner_pack,official_partner_pack_at,outlook_start_id,outlook_end_id)
              SELECT id,name,start_date,end_date,promo_type,promo_value,promo_code,
                     COALESCE(promo_name,''),destinations_json,plans_json,days_json,allow_below_cost,notes,status,created_at,
                     COALESCE(official_partner_pack,''),COALESCE(official_partner_pack_at,''),
                     COALESCE(outlook_start_id,''),COALESCE(outlook_end_id,'') FROM campaigns""")
            con.execute("DROP TABLE campaigns")
            con.execute("ALTER TABLE campaigns_v05 RENAME TO campaigns")
            con.execute("PRAGMA foreign_keys=ON")

        cols = {r["name"] for r in con.execute("PRAGMA table_info(campaigns)")}
        if "monotonic_destinations_json" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN monotonic_destinations_json TEXT NOT NULL DEFAULT '[]'")

        if "monotonic_all_promos" not in cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN monotonic_all_promos INTEGER NOT NULL DEFAULT 0")

        ecols = {r["name"] for r in con.execute("PRAGMA table_info(executions)").fetchall()}
        if "batch_id" not in ecols:
            con.execute("ALTER TABLE executions ADD COLUMN batch_id INTEGER")
        bcols = {r["name"] for r in con.execute("PRAGMA table_info(execution_batches)").fetchall()}
        if "timeline_change_set_id" not in bcols:
            con.execute("ALTER TABLE execution_batches ADD COLUMN timeline_change_set_id INTEGER")
        con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_exec_batch ON executions(batch_id);
        CREATE TABLE IF NOT EXISTS execution_batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          execution_date TEXT NOT NULL,
          executed_at TEXT NOT NULL,
          workbook_path TEXT NOT NULL,
          backup_path TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '[]',
          undone_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_batch_date ON execution_batches(execution_date);
        """)

    def save_campaign(self, c: Campaign) -> int:
        if c.status != "ACTIVE_IMPORTED":
            if not c.start_date:
                raise ValueError("A promotion start date is required.")
            date.fromisoformat(c.start_date)
        if not c.end_date or (c.start_date and c.end_date < c.start_date):
            raise ValueError("Choose an end date on or after the promotion start.")
        date.fromisoformat(c.end_date)
        created = c.created_at or datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO campaigns
                (name,start_date,end_date,promo_type,promo_value,promo_code,promo_name,destinations_json,plans_json,days_json,
                 allow_below_cost,notes,status,created_at,official_partner_pack,official_partner_pack_at,outlook_start_id,outlook_end_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c.name,c.start_date,c.end_date,c.promo_type,c.promo_value,c.promo_code,c.promo_name,
                 json.dumps(c.destinations),json.dumps(c.plans),json.dumps(c.days),int(c.allow_below_cost),
                 c.notes,c.status,created,c.official_partner_pack,c.official_partner_pack_at,c.outlook_start_id,c.outlook_end_id),
            )
            con.execute("UPDATE campaigns SET monotonic_destinations_json=?, monotonic_all_promos=? WHERE id=?",
                        (json.dumps(sorted(set(c.monotonic_destinations) & set(c.destinations))), int(c.monotonic_all_promos), cur.lastrowid))
            return int(cur.lastrowid)

    def update_campaign(self, c: Campaign) -> None:
        if c.id is None:
            raise ValueError("Campaign has no ID.")
        original = self.get_campaign(c.id)
        if original.official_partner_pack or original.official_partner_pack_at:
            raise ValueError("This campaign is published and read-only. Duplicate it to plan a new offer.")
        if c.start_date != original.start_date:
            raise ValueError("The start date is fixed by Timeline. Cancel the future promotion and create it on a new date instead.")
        if not c.end_date or (c.start_date and c.end_date < c.start_date):
            raise ValueError("Choose an end date on or after the promotion start.")
        if self.has_execution(c.id, "END"):
            raise ValueError("Expired campaigns are read-only. Duplicate it to create a new campaign.")
        with self._connect() as con:
            if con.execute("SELECT 1 FROM campaigns WHERE id=?", (c.id,)).fetchone() is None:
                raise KeyError(c.id)
            con.execute(
                "INSERT INTO campaign_definition_history(campaign_id,changed_at,old_json,new_json) VALUES(?,?,?,?)",
                (c.id, datetime.now().isoformat(timespec="seconds"), json.dumps(asdict(original)), json.dumps(asdict(c))),
            )
            con.execute(
                """UPDATE campaigns SET
                name=?,start_date=?,end_date=?,promo_type=?,promo_value=?,promo_code=?,promo_name=?,
                destinations_json=?,plans_json=?,days_json=?,allow_below_cost=?,notes=?,status=?
                WHERE id=?""",
                (c.name,c.start_date,c.end_date,c.promo_type,c.promo_value,c.promo_code,c.promo_name,
                 json.dumps(c.destinations),json.dumps(c.plans),json.dumps(c.days),int(c.allow_below_cost),
                 c.notes,c.status,c.id),
            )

            con.execute("UPDATE campaigns SET monotonic_destinations_json=?, monotonic_all_promos=? WHERE id=?",
                        (json.dumps(sorted(set(c.monotonic_destinations) & set(c.destinations))), int(c.monotonic_all_promos), c.id))

    def mark_campaigns_official(self, effective_date: str, partner_pack: str) -> None:
        """Record which campaign definitions were included in a successful pack."""
        date.fromisoformat(effective_date)
        with self._connect() as con:
            con.execute("""UPDATE campaigns SET official_partner_pack=?,official_partner_pack_at=?
                           WHERE (start_date='' OR start_date<=?) AND end_date>=?""",
                        (partner_pack, datetime.now().isoformat(timespec="seconds"), effective_date, effective_date))

    def original_campaign_definition(self, campaign_id: int) -> Campaign:
        with self._connect() as con:
            row = con.execute("SELECT old_json FROM campaign_definition_history WHERE campaign_id=? ORDER BY id LIMIT 1", (campaign_id,)).fetchone()
        return Campaign(**json.loads(row[0])) if row else self.get_campaign(campaign_id)

    def move_campaign_start(self, campaign_id: int, start_date: str, *, keep_duration: bool = True, today: date | None = None) -> Campaign:
        """The Timeline's explicit start-date move; ordinary form saves cannot move it."""
        today = today or date.today()
        campaign = self.get_campaign(campaign_id)
        new_start = date.fromisoformat(start_date)
        if campaign.status == "ACTIVE_IMPORTED" or not campaign.start_date:
            raise ValueError("The start of an imported active promotion cannot be moved.")
        old_start = date.fromisoformat(campaign.start_date)
        if old_start < today or new_start < today or self.has_execution(campaign_id, "START") or self.has_execution(campaign_id, "END"):
            raise ValueError("Only a promotion that has not started can be moved.")
        delta = new_start - old_start
        new_end = date.fromisoformat(campaign.end_date) + delta if keep_duration else date.fromisoformat(campaign.end_date)
        if new_end < new_start:
            raise ValueError("The new start is after the existing end. Keep the duration or choose an earlier start.")
        moved = replace(campaign, start_date=new_start.isoformat(), end_date=new_end.isoformat(),
                        official_partner_pack="", official_partner_pack_at="")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            stamp = datetime.now().isoformat(timespec="seconds")
            con.execute("INSERT INTO campaign_definition_history(campaign_id,changed_at,old_json,new_json) VALUES(?,?,?,?)",
                        (campaign_id, stamp, json.dumps(asdict(campaign)), json.dumps(asdict(moved))))
            con.execute("UPDATE campaigns SET start_date=?,end_date=?,official_partner_pack='',official_partner_pack_at='' WHERE id=?", (moved.start_date, moved.end_date, campaign_id))
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pricing_change_sets'").fetchone():
                rows = con.execute("SELECT * FROM pricing_change_sets WHERE source IN ('editor_date','legacy_key_date') AND undone_at='' ORDER BY id").fetchall()
                for row in rows:
                    metadata = json.loads(row["metadata_json"] or "{}")
                    if int(metadata.get("campaign_id") or 0) != campaign_id or metadata.get("is_base_price_change"):
                        continue
                    old_key = metadata.get("key_date")
                    if old_key:
                        metadata["key_date"] = (date.fromisoformat(old_key) + delta).isoformat()
                    metadata.update(promo_start_date=moved.start_date, promo_end_date=moved.end_date,
                                    moved_from=row["effective_from"])
                    expiry = (new_end + timedelta(days=1)).isoformat() if row["expires_at"] else ""
                    cur = con.execute("INSERT INTO pricing_change_sets(effective_from,expires_at,layer,priority,source,label,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                                      ((date.fromisoformat(row["effective_from"]) + delta).isoformat(), expiry, row["layer"], row["priority"], row["source"], row["label"], stamp, json.dumps(metadata)))
                    con.execute("INSERT INTO pricing_changes(change_set_id,row_key,field,old_value_json,new_value_json) SELECT ?,row_key,field,old_value_json,new_value_json FROM pricing_changes WHERE change_set_id=?", (cur.lastrowid,row["id"]))
                    con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (stamp,row["id"]))
            con.commit()
        return moved

    def delete_campaign(self, campaign_id: int) -> None:
        campaign = self.get_campaign(campaign_id)
        if campaign.status == "ACTIVE_IMPORTED" or not campaign.start_date or campaign.start_date < date.today().isoformat():
            raise ValueError("Only a future promotion can be cancelled. Change the end date of an active promotion.")
        if self.has_execution(campaign_id, "START") or self.has_execution(campaign_id, "END"):
            raise ValueError("Executed campaigns are kept for audit. Undo the relevant execution batch if it was applied by mistake.")
        with self._connect() as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pricing_change_sets'").fetchone():
                for row in con.execute("SELECT id,metadata_json FROM pricing_change_sets WHERE source IN ('editor_date','legacy_key_date') AND undone_at='' ").fetchall():
                    metadata = json.loads(row["metadata_json"] or "{}")
                    if int(metadata.get("campaign_id") or 0) == campaign_id:
                        con.execute("UPDATE pricing_change_sets SET undone_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), row["id"]))
            con.execute("DELETE FROM executions WHERE campaign_id=?", (campaign_id,))
            con.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))

    def rename_campaign(self, campaign_id: int, name: str) -> None:
        name = str(name).strip()
        if not name:
            raise ValueError("Enter a campaign name.")
        original = self.get_campaign(campaign_id)
        from .timeline_labels import campaign_presentation
        if campaign_presentation(original, self)["readonly"]:
            raise ValueError("Published or finished campaigns are read-only.")
        self.update_campaign(replace(original, name=name))

    def duplicate_campaign(self, campaign_id: int, new_code: str) -> int:
        c = self.get_campaign(campaign_id)
        clone = replace(
            c, id=None, name=f"{c.name} - Copy", promo_code=new_code, status="PLANNED", created_at="",
            official_partner_pack="", official_partner_pack_at="", outlook_start_id="", outlook_end_id="",
        )
        return self.save_campaign(clone)

    def set_outlook_ids(self, campaign_id: int, start_id: str, end_id: str) -> None:
        with self._connect() as con:
            con.execute("UPDATE campaigns SET outlook_start_id=?, outlook_end_id=? WHERE id=?", (start_id or "", end_id or "", campaign_id))

    def list_campaigns(self) -> list[Campaign]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM campaigns ORDER BY start_date, id").fetchall()
        return [self._row_to_campaign(r) for r in rows]

    def get_campaign(self, campaign_id: int) -> Campaign:
        with self._connect() as con:
            row = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise KeyError(campaign_id)
        return self._row_to_campaign(row)

    def _row_to_campaign(self, r) -> Campaign:
        keys = set(r.keys())
        return Campaign(
            id=int(r["id"]), name=r["name"], start_date=r["start_date"], end_date=r["end_date"],
            promo_type=r["promo_type"], promo_value=float(r["promo_value"]), promo_code=r["promo_code"],
            promo_name=(r["promo_name"] if "promo_name" in keys else ""), destinations=json.loads(r["destinations_json"]), plans=json.loads(r["plans_json"]),
            days=[int(x) for x in json.loads(r["days_json"])], allow_below_cost=bool(r["allow_below_cost"]),
            notes=r["notes"], status=r["status"], created_at=r["created_at"],
            official_partner_pack=r["official_partner_pack"] if "official_partner_pack" in keys else "",
            official_partner_pack_at=r["official_partner_pack_at"] if "official_partner_pack_at" in keys else "",
            outlook_start_id=r["outlook_start_id"] if "outlook_start_id" in keys else "",
            outlook_end_id=r["outlook_end_id"] if "outlook_end_id" in keys else "",
            monotonic_all_promos=bool(r["monotonic_all_promos"]) if "monotonic_all_promos" in keys else False,
            monotonic_destinations=json.loads(r["monotonic_destinations_json"]) if "monotonic_destinations_json" in keys else [],
        )

    def has_execution(self, campaign_id: int, action: str) -> bool:
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM executions WHERE campaign_id=? AND action=? ORDER BY id DESC LIMIT 1",
                (campaign_id, action.upper()),
            ).fetchone() is not None

    def record_execution(self, campaign_id: int, action: str, workbook: Path, backup: Path, affected: int, details: str = "", batch_id: int | None = None):
        with self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO executions
                (campaign_id,action,executed_at,workbook_path,backup_path,affected_rows,details,batch_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                (campaign_id, action.upper(), datetime.now().isoformat(timespec="seconds"), str(workbook), str(backup), affected, details, batch_id),
            )

    def create_batch(self, execution_date: str, workbook: Path, backup: Path, details: list[dict]) -> int:
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO execution_batches(execution_date,executed_at,workbook_path,backup_path,details_json,undone_at)
                   VALUES (?,?,?,?,?,'')""",
                (execution_date, datetime.now().isoformat(timespec="seconds"), str(workbook), str(backup), json.dumps(details, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def create_batch_with_executions(
        self,
        execution_date: str,
        workbook: Path,
        backup: Path,
        details: list[dict],
        executions: list[tuple[int, str, int, str]],
        timeline_change_set_id: int | None = None,
    ) -> int:
        """Commit a date batch and all its campaign audit rows atomically."""
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO execution_batches
                (execution_date,executed_at,workbook_path,backup_path,details_json,undone_at,timeline_change_set_id)
                VALUES (?,?,?,?,?,'',?)""",
                (execution_date, datetime.now().isoformat(timespec="seconds"), str(workbook), str(backup), json.dumps(details, ensure_ascii=False), timeline_change_set_id),
            )
            batch_id = int(cur.lastrowid)
            executed_at = datetime.now().isoformat(timespec="seconds")
            con.executemany(
                """INSERT OR REPLACE INTO executions
                (campaign_id,action,executed_at,workbook_path,backup_path,affected_rows,details,batch_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (campaign_id, action.upper(), executed_at, str(workbook), str(backup), affected, detail, batch_id)
                    for campaign_id, action, affected, detail in executions
                ],
            )
            return batch_id

    def get_active_batch_for_date(self, execution_date: str):
        with self._connect() as con:
            return con.execute(
                "SELECT * FROM execution_batches WHERE execution_date=? AND COALESCE(undone_at,'')='' ORDER BY id DESC LIMIT 1",
                (execution_date,),
            ).fetchone()

    def has_later_active_batch(self, batch_id: int) -> bool:
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM execution_batches WHERE id>? AND COALESCE(undone_at,'')='' LIMIT 1", (batch_id,)
            ).fetchone() is not None

    def undo_batch(self, batch_id: int) -> None:
        with self._connect() as con:
            batch = con.execute(
                "SELECT timeline_change_set_id FROM execution_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch and batch["timeline_change_set_id"]:
                con.execute(
                    "UPDATE pricing_change_sets SET undone_at=? WHERE id=? AND undone_at=''",
                    (datetime.now().isoformat(timespec="seconds"), int(batch["timeline_change_set_id"])),
                )
            con.execute("DELETE FROM executions WHERE batch_id=?", (batch_id,))
            con.execute("UPDATE execution_batches SET undone_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), batch_id))

    def save_group(self, name: str, destinations: Iterable[str]):
        vals = sorted(set(destinations))
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO groups(name,destinations_json,updated_at) VALUES (?,?,?)",
                (name.strip(), json.dumps(vals), datetime.now().isoformat(timespec="seconds")),
            )

    def list_groups(self) -> dict[str, list[str]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM groups ORDER BY name").fetchall()
        return {r["name"]: json.loads(r["destinations_json"]) for r in rows}
