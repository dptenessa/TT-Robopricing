from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_project_root() -> Path:
    here = Path(__file__).resolve().parent
    root = here.parent
    app_file = root / "automation" / "promo_planner" / "app.py"
    if app_file.exists():
        return root
    raise FileNotFoundError(
        "Could not find automation/promo_planner/app.py. "
        "Keep this launcher inside the repository START_HERE folder."
    )


def main() -> None:
    root = find_project_root()
    log_dir = root / "outputs" / "promo_planner"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "launcher_error.log"

    # Start the planner as a fully independent process so this launcher exits
    # immediately and VS Code/PowerShell is free to launch the Pricing Editor.
    command = [sys.executable, "-m", "automation.promo_planner.app"]

    creationflags = 0
    popen_kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
    }

    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    # Keep stdout/stderr out of the VS Code terminal. If the planner fails at
    # startup, the traceback/error remains available in launcher_error.log.
    with log_file.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            **popen_kwargs,
        )


if __name__ == "__main__":
    main()
