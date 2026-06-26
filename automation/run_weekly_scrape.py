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


def run_script(name: str, script: str, timeout_s: int) -> JobResult:
    script_path = SCRIPT_DIR / script
    if not script_path.exists():
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="missing",
            elapsed_s=0.0,
        )

    cmd = [PYTHON, str(script_path)]
    start = time.monotonic()

    try:
        completed = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            timeout=timeout_s,
            check=False,
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
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="timeout",
            elapsed_s=elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"[{name}] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return JobResult(
            name=name,
            script=script,
            exit_code=None,
            status="error",
            elapsed_s=elapsed,
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

        exit_info = "—" if result.exit_code is None else str(result.exit_code)
        print(
            f"  {result.name:<10} | {status_label:<7} | "
            f"exit={exit_info:<4} | {format_duration(result.elapsed_s):>8} | {result.script}"
        )

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

    combine_path = SCRIPT_DIR / COMBINE_SCRIPT
    combine_result: JobResult

    if not combine_path.exists():
        combine_result = JobResult(
            name="combine",
            script=COMBINE_SCRIPT,
            exit_code=None,
            status="missing",
            elapsed_s=0.0,
        )
    else:
        print(f"\nRunning {COMBINE_SCRIPT}...")
        combine_start = time.monotonic()
        try:
            completed = subprocess.run(
                [PYTHON, str(combine_path)],
                cwd=BASE_DIR,
                check=False,
            )
            combine_elapsed = time.monotonic() - combine_start
            exit_code = completed.returncode
            combine_result = JobResult(
                name="combine",
                script=COMBINE_SCRIPT,
                exit_code=exit_code,
                status="ok" if exit_code == 0 else "failed",
                elapsed_s=combine_elapsed,
            )
        except Exception as exc:
            combine_elapsed = time.monotonic() - combine_start
            print(
                f"[combine] unexpected error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            combine_result = JobResult(
                name="combine",
                script=COMBINE_SCRIPT,
                exit_code=None,
                status="error",
                elapsed_s=combine_elapsed,
            )

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
