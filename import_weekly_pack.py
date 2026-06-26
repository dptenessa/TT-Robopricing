from __future__ import annotations

import argparse
import shutil
import uuid
import zipfile
from fnmatch import fnmatch
from pathlib import Path


ALLOWED_PATTERNS = (
    "outputs/*_current.csv",
    "outputs/*_previous.csv",
    "workable_data/combined_scrapped_data_latest.csv",
    "workable_data/market_prices_annotated.csv",
    "workable_data/market_prices_outlier_audit_log.csv",
    "workable_data/ht_prices_latest.csv",
    "workable_data/ht_failed_countries_latest.csv",
    "workable_data/history/combined_scrapped_data_*.csv",
    "workable_data/history/ht_prices_*.csv",
    "workable_data/diffs/*.csv",
    "workable_data/USD/*.csv",
    "workable_data/USD/history/*.csv",
    "workable_data/EUR/*.csv",
    "workable_data/EUR/history/*.csv",
)

PROTECTED_PREFIXES = (
    "workable_data/exports/",
    "workable_data/autosave/",
)


def clean_rel(path: Path) -> str:
    return path.as_posix().lstrip("./")


def is_allowed(rel_path: str) -> bool:
    rel_path = rel_path.replace("\\", "/")
    if any(rel_path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return False
    return any(fnmatch(rel_path, pattern) for pattern in ALLOWED_PATTERNS)


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


def copy_pack(src_root: Path, project_root: Path, dry_run: bool = False) -> tuple[int, int]:
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
            if not is_allowed(rel):
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

        copied, skipped = copy_pack(src_root, project_root, dry_run=dry_run)
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    print()
    action = "Would import" if dry_run else "Imported"
    print(f"{action} {copied} weekly scrape/proposal files.")
    if skipped:
        print(f"Skipped {skipped} files that are not part of the proposal pack.")
    print("Manual exports and autosaves were not touched.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a downloaded GitHub weekly-proposal-pack into this local project."
    )
    parser.add_argument("pack", help="Path to weekly-proposal-pack.zip, or an extracted pack folder.")
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent),
        help="Local project folder to update. Defaults to this script's folder.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be copied.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return import_pack(Path(args.pack), Path(args.project_root), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
