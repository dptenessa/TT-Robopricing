from __future__ import annotations

from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "automation" / "fast pricing editor.py"


def main() -> int:
    return subprocess.call([sys.executable, str(SCRIPT)], cwd=PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
