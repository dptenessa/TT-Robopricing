#!/usr/bin/env python3
"""T-Travel pipeline launcher with a small PySide6 status window."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "events.db"

STEPS = [
    ("Event radar", "daily_event_radar.py"),
    ("Travel demand", "collect_travel_demand.py"),
    ("Opportunity scoring", "build_opportunities.py"),
    ("Campaign dashboard", "build_campaign_dashboard.py"),
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_log_table() -> None:
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                step TEXT PRIMARY KEY,
                started_at TEXT,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT
            )
            """
        )


def latest_runs() -> dict[str, dict[str, str]]:
    ensure_log_table()
    with sqlite3.connect(DATABASE) as db:
        rows = db.execute(
            "SELECT step, started_at, finished_at, status, message "
            "FROM pipeline_runs"
        ).fetchall()
    return {
        row[0]: {
            "started_at": row[1] or "",
            "finished_at": row[2] or "",
            "status": row[3] or "",
            "message": row[4] or "",
        }
        for row in rows
    }


def save_run(step: str, status: str, started: str, finished: str = "", message: str = "") -> None:
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            """
            INSERT INTO pipeline_runs(step, started_at, finished_at, status, message)
            VALUES(?,?,?,?,?)
            ON CONFLICT(step) DO UPDATE SET
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                status=excluded.status,
                message=excluded.message
            """,
            (step, started, finished, status, message[-2000:]),
        )


def status_for(step: str, runs: dict[str, dict[str, str]]) -> str:
    row = runs.get(step)
    if not row:
        return "Not run"
    if row["status"] == "failed":
        return "Failed"
    if row["status"] == "running":
        return "Running"
    return "Ready" if row["finished_at"].startswith(datetime.now().date().isoformat()) else "Needs run"


class Worker(QThread):
    line = Signal(str)
    done = Signal(bool)

    def __init__(self, selected: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.selected = selected

    def run(self) -> None:
        ensure_log_table()
        for name, filename in self.selected:
            started = now_utc()
            save_run(name, "running", started)
            self.line.emit(f"START  {name}")
            command = [sys.executable, str(ROOT / filename), "--database", str(DATABASE)]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                output, _ = process.communicate()
                if output:
                    for line in output.splitlines():
                        self.line.emit(line)
                if process.returncode:
                    save_run(name, "failed", started, now_utc(), f"Exit code {process.returncode}")
                    self.line.emit(f"FAILED {name}")
                    self.done.emit(False)
                    return
                save_run(name, "success", started, now_utc())
                self.line.emit(f"DONE   {name}")
            except Exception as exc:
                save_run(name, "failed", started, now_utc(), str(exc))
                self.line.emit(f"FAILED {name}: {exc}")
                self.done.emit(False)
                return
        self.done.emit(True)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("T-Travel pipeline")
        self.resize(900, 620)
        ensure_log_table()

        self.table = QTableWidget(len(STEPS), 3)
        self.table.setHorizontalHeaderLabels(["Step", "Script", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.run_button = QPushButton("Run required steps")
        self.all_button = QPushButton("Run full pipeline")
        self.refresh_button = QPushButton("Refresh status")
        self.run_button.clicked.connect(lambda: self.start(False))
        self.all_button.clicked.connect(lambda: self.start(True))
        self.refresh_button.clicked.connect(self.refresh)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Green = completed today. Yellow = needs to run. Red = failed."))
        layout.addWidget(self.table)
        layout.addWidget(self.run_button)
        layout.addWidget(self.all_button)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.log)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.worker = None
        self.refresh()

    def refresh(self) -> None:
        runs = latest_runs()
        for index, (name, filename) in enumerate(STEPS):
            self.table.setItem(index, 0, QTableWidgetItem(name))
            self.table.setItem(index, 1, QTableWidgetItem(filename))
            status = status_for(name, runs)
            item = QTableWidgetItem(status)
            self.table.setItem(index, 2, item)

    def start(self, full: bool) -> None:
        if self.worker and self.worker.isRunning():
            return
        runs = latest_runs()
        if full:
            selected = STEPS
        else:
            selected = []
            upstream_needed = False
            for name, filename in STEPS:
                current = status_for(name, runs)
                if current in {"Not run", "Needs run", "Failed"} or upstream_needed:
                    selected.append((name, filename))
                    upstream_needed = True
        if not selected:
            QMessageBox.information(self, "Pipeline", "Everything is up to date.")
            return
        self.run_button.setEnabled(False)
        self.all_button.setEnabled(False)
        self.log.appendPlainText("Running: " + ", ".join(name for name, _ in selected))
        self.worker = Worker(selected, self)
        self.worker.line.connect(self.log.appendPlainText)
        self.worker.done.connect(self.finished)
        self.worker.start()

    def finished(self, success: bool) -> None:
        self.run_button.setEnabled(True)
        self.all_button.setEnabled(True)
        self.refresh()
        self.log.appendPlainText("Pipeline completed." if success else "Pipeline stopped with an error.")


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", action="store_true", help="Run required steps without the GUI")
    parser.add_argument("--full", action="store_true", help="Run all steps")
    args = parser.parse_args()
    ensure_log_table()
    runs = latest_runs()
    selected = STEPS if args.full else [step for step in STEPS if status_for(step[0], runs) != "Ready"]
    if not selected:
        print("Everything is up to date.")
        return 0
    worker = Worker(selected)
    worker.line.connect(print)
    worker.start()
    worker.wait()
    return 0


if __name__ == "__main__":
    if "--cli" in sys.argv:
        raise SystemExit(cli())
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    raise SystemExit(app.exec())
