from __future__ import annotations

from pricing_pipeline import main as pipeline_main


def main() -> int:
    return pipeline_main(["--scrape"])


if __name__ == "__main__":
    raise SystemExit(main())
