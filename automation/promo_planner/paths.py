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
    root = root or project_root()
    base = root / "outputs" / "manual_prices"
    preferred = [
        base / "manual_prices_current.xlsx",
        base / "manual_prices_current.xlsm",
    ]
    for path in preferred:
        if path.exists():
            return path
    candidates = sorted(
        list(base.rglob("manual_prices_current*.xlsx")) + list(base.rglob("manual_prices_current*.xlsm")),
        key=lambda p: ("autosave" in p.name.lower(), len(p.parts), p.name.lower()),
    )
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        "Could not find outputs/manual_prices/manual_prices_current.xlsx. "
        "Export/current-save your pricing book first."
    )
