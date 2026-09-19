from __future__ import annotations

from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMPORTER = PROJECT_ROOT / "automation" / "import_weekly_pack.py"


def choose_pack() -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        downloads = Path.home() / "Downloads"
        filename = filedialog.askopenfilename(
            title="Select weekly-proposal-pack.zip",
            initialdir=str(downloads if downloads.exists() else Path.home()),
            filetypes=[("Weekly proposal pack", "*.zip"), ("All files", "*.*")],
        )
        root.destroy()
        return filename or None
    except Exception as exc:
        print(f"Could not open file picker: {exc}")
        return None


def main() -> int:
    pack = choose_pack()
    if not pack:
        print("Import cancelled.")
        return 0
    return subprocess.call(
        [sys.executable, str(IMPORTER), pack, "--project-root", str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
