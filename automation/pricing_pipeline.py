from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from pipeline_files import FILES, PipelineFiles


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs:02d}s"


def run_step(name: str, fn: Callable[[], object]) -> object:
    print()
    print("=" * 76)
    print(name)
    print("=" * 76)
    start = time.monotonic()
    result = fn()
    elapsed = time.monotonic() - start
    print(f"{name} finished in {format_duration(elapsed)}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the pricing workflow with the shared file layout."
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Run all competitor scrapers first. Without this, existing outputs/*_current.csv files are reused.",
    )
    parser.add_argument(
        "--resume-successful-today",
        action="store_true",
        help="When scraping, reuse scraper outputs that already succeeded today and rerun only the rest.",
    )
    parser.add_argument(
        "--skip-diff",
        action="store_true",
        help="Skip scraped-price change reports.",
    )
    parser.add_argument(
        "--skip-outliers",
        action="store_true",
        help="Skip market outlier annotation.",
    )
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="Skip USD/EUR model proposal generation.",
    )
    parser.add_argument(
        "--open-editor",
        action="store_true",
        help="Open the fast editor after proposals are generated.",
    )
    return parser


def run_pipeline(args: argparse.Namespace, paths: PipelineFiles = FILES) -> int:
    print(f"Pricing pipeline root: {paths.base_dir}")

    if args.scrape:
        from run_weekly_scrape import main as scrape_main

        scrape_code = run_step(
            "1. Scrape competitors and combine",
            lambda: scrape_main(resume_successful_today=args.resume_successful_today),
        )
        if scrape_code not in (0, None):
            print("Scraping failed after retry. Stopping before diff, outlier removal, and model proposal generation.")
            return int(scrape_code) if isinstance(scrape_code, int) else 1
    else:
        from combine_scrapped_data import combine_all_scraped_data

        run_step("1. Combine existing scraper outputs", lambda: combine_all_scraped_data(paths))

    if not args.skip_diff:
        from combined_scrapped_data_diffs import main as scrape_diff_main

        run_step("2. Compare latest scraped competition snapshot", lambda: scrape_diff_main(paths))

    if not args.skip_outliers:
        from outlier_removal import main as outlier_main

        run_step("3. Annotate market outliers", lambda: outlier_main(paths))

    if not args.skip_model:
        from pricing_batch import run_batch_pricing

        run_step("4. Generate USD/EUR model proposals", lambda: run_batch_pricing(paths))

    print()
    print("Pipeline outputs")
    print(f"- Combined competition latest: {paths.combined_latest}")
    print(f"- Annotated market latest: {paths.market_annotated}")
    print(f"- USD proposal latest: {paths.model_latest('USD')}")
    print(f"- EUR proposal latest: {paths.model_latest('EUR')}")
    print(f"- Manual export folder: {paths.editor_exports_dir}")

    if args.open_editor:
        from pricing_editor.main_window import run as run_editor

        run_step("5. Open fast editor", run_editor)
    else:
        print()
        print("Next: open the editor from START_HERE, or run `python \"automation/fast pricing editor.py\"`.")

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
