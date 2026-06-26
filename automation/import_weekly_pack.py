from __future__ import annotations

import argparse
import shutil
import uuid
import zipfile
from fnmatch import fnmatch
from pathlib import Path

import pandas as pd
from combined_scrapped_data_diffs import (
    build_change_report,
    build_country_summary,
    build_provider_summary,
    print_pipe_table,
)


ALLOWED_PATTERNS = (
    "outputs/*_current.csv",
    "outputs/*_previous.csv",
    "workable_data/combined_scrapped_data_latest.csv",
    "workable_data/market_prices_annotated.csv",
    "workable_data/market_prices_outlier_audit_log.csv",
    "workable_data/ht_prices_latest.csv",
    "workable_data/ht_failed_countries_latest.csv",
    "workable_data/scrape_status_latest.csv",
    "workable_data/scrape_status_history/*.csv",
    "workable_data/logs/*.log",
    "workable_data/history/combined_scrapped_data_*.csv",
    "workable_data/history/ht_prices_*.csv",
    "workable_data/USD/*.csv",
    "workable_data/USD/history/*.csv",
    "workable_data/EUR/*.csv",
    "workable_data/EUR/history/*.csv",
)

DIAGNOSTIC_PATTERNS = (
    "workable_data/scrape_status_latest.csv",
    "workable_data/scrape_status_history/*.csv",
    "workable_data/logs/*.log",
)


PROTECTED_PREFIXES = (
    "workable_data/exports/",
    "workable_data/autosave/",
)


def clean_rel(path: Path) -> str:
    return path.as_posix().lstrip("./")


def is_allowed(rel_path: str, patterns: tuple[str, ...] = ALLOWED_PATTERNS) -> bool:
    rel_path = rel_path.replace("\\", "/")
    if any(rel_path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return False
    return any(fnmatch(rel_path, pattern) for pattern in patterns)


def find_pack_root(extracted_or_folder: Path) -> Path:
    candidates = [extracted_or_folder]
    candidates.extend(p for p in extracted_or_folder.rglob("*") if p.is_dir())

    with_both: list[Path] = []
    with_any: list[Path] = []

    for candidate in candidates:
        has_outputs = (candidate / "outputs").is_dir()
        has_workable = (candidate / "workable_data").is_dir()
        if has_outputs and has_workable:
            with_both.append(candidate)
        elif has_outputs or has_workable:
            with_any.append(candidate)

    if with_both:
        return min(with_both, key=lambda p: len(p.parts))
    if with_any:
        return min(with_any, key=lambda p: len(p.parts))

    raise FileNotFoundError(
        "Could not find outputs/ or workable_data/ inside the weekly pack."
    )


def safe_label(label: str) -> str:
    cleaned = []
    for char in label:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "snapshot"


def latest_combined_history_label(root: Path, fallback: str) -> str:
    history_dir = root / "workable_data" / "history"
    files = sorted(history_dir.glob("combined_scrapped_data_*.csv"))
    if not files:
        return fallback
    return files[-1].stem.replace("combined_scrapped_data_", "")


def print_scrape_status(src_root: Path) -> bool:
    status_path = src_root / "workable_data" / "scrape_status_latest.csv"

    print()
    print("Scrape Status")

    if not status_path.exists():
        print("No scrape status file was included in this weekly pack.")
        return True

    status_df = pd.read_csv(status_path).fillna("")

    if status_df.empty:
        print("The scrape status file is empty.")
        return False

    display_cols = [
        col for col in ["Category", "Name", "Status", "Attempt", "Duration", "Script", "LogFile"]
        if col in status_df.columns
    ]
    print_pipe_table(status_df[display_cols], "SCRAPERS AND COMBINE")

    if "Status" not in status_df.columns:
        return True

    issues = status_df[status_df["Status"].astype(str).str.lower() != "ok"]
    if issues.empty:
        print("All scrapers and the combine step finished OK.")
        return True

    names = ", ".join(issues["Name"].astype(str).tolist())
    print(f"Needs attention: {names}")
    return False


def compare_incoming_to_local(
    src_root: Path,
    project_root: Path,
    dry_run: bool = False,
) -> None:
    local_latest = project_root / "workable_data" / "combined_scrapped_data_latest.csv"
    incoming_latest = src_root / "workable_data" / "combined_scrapped_data_latest.csv"

    print()
    print("Changes Versus Local Latest")

    if not incoming_latest.exists():
        print("No incoming combined scrape file was included in this weekly pack.")
        return

    if not local_latest.exists():
        print("No local combined scrape exists yet, so this is treated as the first import.")
        return

    previous_label = latest_combined_history_label(project_root, "local_previous")
    current_label = latest_combined_history_label(src_root, "incoming")
    if previous_label == current_label:
        current_label = f"{current_label}_incoming"

    previous_df = pd.read_csv(local_latest)
    current_df = pd.read_csv(incoming_latest)

    changes = build_change_report(previous_df, current_df)
    country_summary = build_country_summary(changes)
    provider_summary = build_provider_summary(changes)

    print(f"Total rows with changes: {len(changes)}")
    print_pipe_table(provider_summary, "PROVIDER SUMMARY")
    print_pipe_table(country_summary, "COUNTRY SUMMARY")

    if dry_run:
        print("Dry run: change reports were not saved.")
        return

    output_dir = project_root / "workable_data" / "diffs"
    output_dir.mkdir(parents=True, exist_ok=True)

    prev = safe_label(previous_label)
    curr = safe_label(current_label)
    diff_file = output_dir / f"diff_{prev}_vs_{curr}.csv"
    summary_file = output_dir / f"summary_{prev}_vs_{curr}.csv"
    provider_summary_file = output_dir / f"provider_summary_{prev}_vs_{curr}.csv"

    changes.to_csv(diff_file, index=False)
    country_summary.to_csv(summary_file, index=False)
    provider_summary.to_csv(provider_summary_file, index=False)

    print()
    print("Saved local change reports:")
    print(f"- {provider_summary_file}")
    print(f"- {summary_file}")
    print(f"- {diff_file}")


def copy_pack(
    src_root: Path,
    project_root: Path,
    dry_run: bool = False,
    patterns: tuple[str, ...] = ALLOWED_PATTERNS,
) -> tuple[int, int]:
    copied = 0
    skipped = 0

    for top_level in ("outputs", "workable_data"):
        folder = src_root / top_level
        if not folder.exists():
            continue

        for source in folder.rglob("*"):
            if not source.is_file():
                continue

            rel = clean_rel(source.relative_to(src_root))
            if not is_allowed(rel, patterns=patterns):
                skipped += 1
                continue

            target = project_root / rel
            copied += 1
            if dry_run:
                print(f"Would copy: {rel}")
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            print(f"Copied: {rel}")

    return copied, skipped


def import_pack(pack_path: Path, project_root: Path, dry_run: bool = False) -> int:
    pack_path = pack_path.expanduser().resolve()
    project_root = project_root.expanduser().resolve()

    if not pack_path.exists():
        print(f"Weekly pack not found: {pack_path}")
        return 1

    if not project_root.exists():
        print(f"Project folder not found: {project_root}")
        return 1

    tmp_dir: Path | None = None

    try:
        if pack_path.is_file():
            if pack_path.suffix.lower() != ".zip":
                print("Please provide the downloaded weekly-proposal-pack .zip file.")
                return 1
            tmp_dir = project_root / f"_weekly_pack_import_{uuid.uuid4().hex}"
            tmp_dir.mkdir(parents=True, exist_ok=False)
            with zipfile.ZipFile(pack_path) as zf:
                zf.extractall(tmp_dir)
            src_root = find_pack_root(tmp_dir)
        else:
            src_root = find_pack_root(pack_path)

        scrape_ok = print_scrape_status(src_root)
        if not scrape_ok:
            print()
            print("Scrape failed or combine was skipped. Importing diagnostics only; local prices and proposals were not changed.")
            copied, skipped = copy_pack(
                src_root,
                project_root,
                dry_run=dry_run,
                patterns=DIAGNOSTIC_PATTERNS,
            )
            import_blocked = True
        else:
            compare_incoming_to_local(src_root, project_root, dry_run=dry_run)
            copied, skipped = copy_pack(src_root, project_root, dry_run=dry_run)
            import_blocked = False
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    action = "Would import" if dry_run else "Imported"
    print(f"{action} {copied} weekly scrape/proposal files.")
    if skipped:
        print(f"Skipped {skipped} files that are not part of the proposal pack.")
    print("Manual exports and autosaves were not touched.")
    if locals().get("import_blocked", False):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a downloaded GitHub weekly-proposal-pack into this local project."
    )
    parser.add_argument("pack", help="Path to weekly-proposal-pack.zip, or an extracted pack folder.")
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Local project folder to update. Defaults to this script's folder.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return import_pack(Path(args.pack), Path(args.project_root), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
