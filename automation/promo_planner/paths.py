from __future__ import annotations
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def planner_output_dir(root: Path | None = None) -> Path:
    root = root or project_root()
    path = root / "outputs" / "promo_planner"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_manual_prices(root: Path | None = None) -> Path:
    """Use the production current workbook; never guess from other snapshots."""
    root = Path(root) if root is not None else project_root()
    path = root / "outputs" / "manual_prices" / "current" / "manual_prices_current.xlsx"
    if path.is_file():
        return path
    raise FileNotFoundError(
        f"Could not find the current production price book: {path}. "
        "Place manual_prices_current.xlsx in outputs/manual_prices/current before initializing the pricing database."
    )
