from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
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
  undone_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batch_date ON execution_batches(execution_date);
CREATE TABLE IF NOT EXISTS groups (
  name TEXT PRIMARY KEY,
  destinations_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class PromoStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            self._migrate(con)

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

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

        ecols = {r["name"] for r in con.execute("PRAGMA table_info(executions)").fetchall()}
        if "batch_id" not in ecols:
            con.execute("ALTER TABLE executions ADD COLUMN batch_id INTEGER")
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
            return int(cur.lastrowid)

    def update_campaign(self, c: Campaign) -> None:
        if c.id is None:
            raise ValueError("Campaign has no ID.")
        if self.has_execution(c.id, "START") and not self.has_execution(c.id, "END"):
            raise ValueError("Published campaigns cannot be edited. Undo the publication batch first if it was applied by mistake.")
        if self.has_execution(c.id, "END"):
            raise ValueError("Expired campaigns are read-only. Duplicate it to create a new campaign.")
        with self._connect() as con:
            if con.execute("SELECT 1 FROM campaigns WHERE id=?", (c.id,)).fetchone() is None:
                raise KeyError(c.id)
            con.execute(
                """UPDATE campaigns SET
                name=?,start_date=?,end_date=?,promo_type=?,promo_value=?,promo_code=?,promo_name=?,
                destinations_json=?,plans_json=?,days_json=?,allow_below_cost=?,notes=?,status=?
                WHERE id=?""",
                (c.name,c.start_date,c.end_date,c.promo_type,c.promo_value,c.promo_code,c.promo_name,
                 json.dumps(c.destinations),json.dumps(c.plans),json.dumps(c.days),int(c.allow_below_cost),
                 c.notes,c.status,c.id),
            )

    def delete_campaign(self, campaign_id: int) -> None:
        if self.has_execution(campaign_id, "START") or self.has_execution(campaign_id, "END"):
            raise ValueError("Executed campaigns are kept for audit. Undo the relevant execution batch if it was applied by mistake.")
        with self._connect() as con:
            con.execute("DELETE FROM executions WHERE campaign_id=?", (campaign_id,))
            con.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))

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
    ) -> int:
        """Commit a date batch and all its campaign audit rows atomically."""
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO execution_batches
                (execution_date,executed_at,workbook_path,backup_path,details_json,undone_at)
                VALUES (?,?,?,?,?,'')""",
                (execution_date, datetime.now().isoformat(timespec="seconds"), str(workbook), str(backup), json.dumps(details, ensure_ascii=False)),
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
