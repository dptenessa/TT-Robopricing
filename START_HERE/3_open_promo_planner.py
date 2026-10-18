from __future__ import annotations

from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "automation" / "fast pricing editor.py"


def main() -> int:
    """Open the integrated pricing app directly in its Campaign planner workspace."""
    return subprocess.call(
        [sys.executable, str(SCRIPT), "--campaigns"],
        cwd=PROJECT_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
