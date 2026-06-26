#!/usr/bin/env python3
"""
Launch all competitor scrapers in parallel, then combine outputs.

Usage:
    python run_weekly_scrape.py
"""

from __future__ import annotations

import csv
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pipeline_files import FILES

BASE_DIR = FILES.base_dir
SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable

SCRAPERS: list[tuple[str, str, int]] = [
    ("vodafone", "scrape_vodafone_pdf_improved.py", 30 * 60),
    ("holafly", "scrape_holafly.py", 30 * 60),
    ("saily", "scrape_saily.py", 30 * 60),
    ("airalo", "scrape_airalo_with_playwright.py", 60 * 60),
    ("orange", "scrape_orange_playwright.py", 60 * 60),
]

COMBINE_SCRIPT = "combine_scrapped_data.py"


@dataclass
class JobResult:
    name: str
    script: str
    exit_code: int | None
    status: str
    elapsed_s: float
    log_path: str = ""


def relative_log_path(path: Path) -> str:
    try:
        return path.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return str(path)


def build_log_path(name: str) -> Path:
    log_dir = FILES.work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"{stamp}_{name}.log"


def run_script(name: str, script: str, timeout_s: int) -> JobResult:
    script_path = SCRIPT_DIR / script
    log_path = build_log_path(name)

    if not script_path.exists():
        log_path.write_text(f"Missing script: {script_path}\n", encoding="utf-8")
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="missing",
            elapsed_s=0.0,
            log_path=relative_log_path(log_path),
        )

    cmd = [PYTHON, str(script_path)]
    start = time.monotonic()

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            print(f"Command: {' '.join(cmd)}", file=log_file)
            print(f"Working directory: {BASE_DIR}", file=log_file)
            print("-" * 72, file=log_file)
            log_file.flush()
            completed = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                timeout=timeout_s,
                check=False,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

        elapsed = time.monotonic() - start
        exit_code = completed.returncode
        status = "ok" if exit_code == 0 else "failed"
        return JobResult(
            name=name,
            script=script,
            exit_code=exit_code,
            status=status,
            elapsed_s=elapsed,
            log_path=relative_log_path(log_path),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            print(f"\nTIMEOUT after {format_duration(elapsed)}", file=log_file)
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="timeout",
            elapsed_s=elapsed,
            log_path=relative_log_path(log_path),
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            print(f"\nUnexpected error: {type(exc).__name__}: {exc}", file=log_file)
        print(f"[{name}] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="error",
            elapsed_s=elapsed,
            log_path=relative_log_path(log_path),
        )


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs:02d}s"


def print_summary(title: str, results: list[JobResult]) -> None:
    width = 72
    print()
    print("=" * width)
    print(title.center(width))
    print("=" * width)

    for result in results:
        if result.status == "ok":
            status_label = "SUCCESS"
        elif result.status == "timeout":
            status_label = "TIMEOUT"
        elif result.status == "missing":
            status_label = "MISSING"
        elif result.status == "error":
            status_label = "ERROR"
        else:
            status_label = "FAILED"

        exit_info = "-" if result.exit_code is None else str(result.exit_code)
        print(
            f"  {result.name:<10} | {status_label:<7} | "
            f"exit={exit_info:<4} | {format_duration(result.elapsed_s):>8} | {result.script}"
        )
        if result.log_path:
            print(f"  {'':<10} | {'log':<7} | {result.log_path}")

    print("=" * width)


def write_status_report(
    scraper_results: list[JobResult],
    combine_result: JobResult,
    overall_elapsed: float,
) -> None:
    report_time = datetime.now()
    rows: list[dict[str, str]] = []

    for category, results in [
        ("scraper", scraper_results),
        ("combine", [combine_result]),
    ]:
        for result in results:
            rows.append(
                {
                    "RunTimestamp": report_time.isoformat(timespec="seconds"),
                    "Category": category,
                    "Name": result.name,
                    "Status": result.status,
                    "ExitCode": "" if result.exit_code is None else str(result.exit_code),
                    "DurationSeconds": f"{result.elapsed_s:.1f}",
                    "Duration": format_duration(result.elapsed_s),
                    "Script": result.script,
                    "LogFile": result.log_path,
                    "OverallDuration": format_duration(overall_elapsed),
                }
            )

    report_dir = FILES.work_dir
    history_dir = report_dir / "scrape_status_history"
    latest_path = report_dir / "scrape_status_latest.csv"
    history_path = history_dir / f"scrape_status_{report_time.strftime('%Y-%m-%d_%H%M%S')}.csv"

    report_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "RunTimestamp",
        "Category",
        "Name",
        "Status",
        "ExitCode",
        "DurationSeconds",
        "Duration",
        "Script",
        "LogFile",
        "OverallDuration",
    ]

    for path in [latest_path, history_path]:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"Saved scrape status report: {latest_path}")


def main() -> int:
    print(f"Starting weekly scrape from: {BASE_DIR}")
    print(f"Python: {PYTHON}")
    print(f"Running {len(SCRAPERS)} scrapers in parallel...\n")

    overall_start = time.monotonic()
    results: list[JobResult] = []

    with ThreadPoolExecutor(max_workers=len(SCRAPERS)) as executor:
        futures = {
            executor.submit(run_script, name, script, timeout_s): name
            for name, script, timeout_s in SCRAPERS
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[{result.name}] finished with status={result.status}")

    results.sort(key=lambda r: r.name)
    print_summary("SCRAPER SUMMARY", results)

    print(f"\nRunning {COMBINE_SCRIPT}...")
    combine_result = run_script("combine", COMBINE_SCRIPT, 10 * 60)
    print_summary("COMBINE SUMMARY", [combine_result])

    overall_elapsed = time.monotonic() - overall_start
    scraper_failures = [r for r in results if r.status != "ok"]
    combine_failed = combine_result.status != "ok"

    print(f"\nTotal elapsed: {format_duration(overall_elapsed)}")

    if scraper_failures:
        failed_names = ", ".join(r.name for r in scraper_failures)
        print(f"Scrapers with issues: {failed_names}")

    if combine_failed:
        print("Combine step did not complete successfully.")

    write_status_report(results, combine_result, overall_elapsed)

    if scraper_failures or combine_failed:
        return 1

    print("Weekly scrape completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
