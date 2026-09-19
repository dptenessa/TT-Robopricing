from __future__ import annotations

import argparse
import shutil
import uuid
import zipfile
from fnmatch import fnmatch
from pathlib import Path

import pandas as pd
def print_pipe_table(df: pd.DataFrame, title: str) -> None:
    print()
    print(title)
    if df.empty:
        print("No rows.")
        return
    print(df.to_string(index=False))


try:
    from market_history import print_history_summary, update_market_history
except ImportError:
    from automation.market_history import print_history_summary, update_market_history

try:
    from pipeline_files import PipelineFiles
except ImportError:
    from automation.pipeline_files import PipelineFiles


ALLOWED_PATTERNS = (
    "scrapes/*_current.csv",
    "scrapes/*_previous.csv",
    "outputs/combined_scrapes/combined_scrape_latest.csv",
    "outputs/combined_scrapes/history/combined_scrape_*.csv",
    "outputs/market_analysis/market_prices_annotated_latest.csv",
    "outputs/market_analysis/outlier_audit_latest.csv",
)

DIAGNOSTIC_PATTERNS = (
    "outputs/diagnostics/scrape_status_latest.csv",
)


PROTECTED_PREFIXES = (
    "outputs/manual_prices/",
    "outputs/partner_packs/",
)


NEW_OUTPUT_MARKERS = (
    "combined_scrapes",
    "market_analysis",
    "diagnostics",
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
        has_scrapes = (candidate / "scrapes").is_dir()
        has_new_outputs = any((candidate / "outputs" / marker).exists() for marker in NEW_OUTPUT_MARKERS)
        if has_scrapes and has_new_outputs:
            with_both.append(candidate)
            continue

        if has_scrapes or has_new_outputs:
            with_any.append(candidate)

    if with_both:
        return min(with_both, key=lambda p: len(p.parts))
    if with_any:
        return min(with_any, key=lambda p: len(p.parts))

    raise FileNotFoundError(
        "Could not find scrapes/ plus outputs/ inside the weekly pack."
    )


def copy_pack(
    src_root: Path,
    project_root: Path,
    dry_run: bool = False,
    patterns: tuple[str, ...] = ALLOWED_PATTERNS,
) -> tuple[int, int]:
    copied = 0
    skipped = 0

    for top_level in ("scrapes", "outputs"):
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
            print("Scrape failed or combine was skipped. Importing diagnostics only; local prices and market data were not changed.")
            copied, skipped = copy_pack(
                src_root,
                project_root,
                dry_run=dry_run,
                patterns=DIAGNOSTIC_PATTERNS,
            )
            import_blocked = True
        else:
            copied, skipped = copy_pack(src_root, project_root, dry_run=dry_run)
            import_blocked = False

            if not dry_run:
                try:
                    local_paths = PipelineFiles(base_dir=project_root)
                    history_stats = update_market_history(local_paths)
                    print_history_summary(history_stats)
                except Exception as exc:
                    # Market history is analytical acceleration, not a reason to
                    # reject an otherwise valid weekly market-data import.
                    print()
                    print(f"Warning: market-history DB update failed: {exc}")
                    print("The weekly market data was imported successfully; the DB can be rebuilt later.")
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    action = "Would import" if dry_run else "Imported"
    print(f"{action} {copied} weekly market-data files.")
    if skipped:
        print(f"Skipped {skipped} files that are not part of the market-data pack.")
    print("Manual exports and autosaves were not touched.")
    if locals().get("import_blocked", False):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a downloaded GitHub weekly market-data pack into this local project."
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
