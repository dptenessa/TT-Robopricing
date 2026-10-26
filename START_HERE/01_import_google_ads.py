"""Run with VS Code Play; choose freshly downloaded Keyword Planner CSVs."""
from datetime import date
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKETS = {'ES':'Spain', 'DE':'Germany', 'US':'United States', 'FR':'France', 'IT':'Italy'}


def origin_for_file(path):
    match = re.fullmatch(r'google_ads_(ES|DE|US|FR|IT)(?: \(\d+\))?\.csv', Path(path).name, re.IGNORECASE)
    if not match:
        raise ValueError('Name each download google_ads_ES.csv, google_ads_DE.csv, google_ads_US.csv, google_ads_FR.csv or google_ads_IT.csv.')
    return match[1].upper()


def import_downloads(files, *, root=ROOT, observed=None, runner=subprocess.run):
    root = Path(root)
    day = date.fromisoformat(str(observed)) if observed else date.today()
    chosen = [(Path(path), origin_for_file(path)) for path in files]
    if len({origin for _,origin in chosen}) != len(chosen):
        raise ValueError('Select only one downloaded file per country.')
    if any(not path.is_file() for path,_ in chosen):
        raise ValueError('A selected download no longer exists.')
    importer = root/'automation/intelligence/import_google_ads_export.py'
    database = root/'automation/intelligence/events.db'
    mapping = root/'START_HERE/google_ads_keywords/keyword_destination_map.csv'
    for path in (importer, database, mapping):
        if not path.is_file():
            raise ValueError(f'Required project file is missing: {path}')
    archive = root/'automation/intelligence/inputs/google_ads_manual'/day.isoformat()
    archive.mkdir(parents=True, exist_ok=True)
    completed = []
    for path, origin in chosen:
        stored = archive/f'google_ads_{origin}.csv'
        if path.resolve() != stored.resolve():
            shutil.copy2(path, stored)
        result = runner([sys.executable, str(importer), '--origin', origin, '--export', str(stored),
                         '--database', str(database), '--keyword-map', str(mapping), '--date', day.isoformat()],
                        cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.stdout:
            print(result.stdout, end='')
        if result.returncode:
            detail = result.stderr or result.stdout or 'See the terminal output.'
            raise RuntimeError(f'Import failed for {MARKETS[origin]}. Already imported: {", ".join(completed) or "none"}.\n\n{detail}')
        completed.append(origin)
    return completed


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    window = tk.Tk()
    window.withdraw()
    try:
        files = filedialog.askopenfilenames(title='Choose fresh Google Ads CSV downloads (one file per country)',
                                          initialdir=str(Path.home()/'Downloads'),
                                          filetypes=[('Google Ads CSV','*.csv')])
        if not files:
            return 0
        completed = import_downloads(files)
        messagebox.showinfo('Google Ads imported', 'Imported: '+', '.join(MARKETS[code] for code in completed)+
                            '.\n\nNow refresh Sales and route opportunities, then Market Insights.')
        return 0
    except Exception as exc:
        print(exc, file=sys.stderr)
        messagebox.showerror('Google Ads import', str(exc))
        return 1
    finally:
        window.destroy()


if __name__ == '__main__':
    raise SystemExit(main())
