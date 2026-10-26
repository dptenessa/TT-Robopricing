"""Press Play in VS Code: compare an exported Excel with a history snapshot."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'automation')]


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from automation.pricing_comparison import save_comparison
    window = tk.Tk()
    window.withdraw()
    try:
        exports = ROOT / 'outputs' / 'manual_prices'
        current = filedialog.askopenfilename(parent=window, title='1. Select the NEW Excel exported from the Pricing Editor',
            initialdir=str(exports), filetypes=[('Pricing Editor Excel', '*.xlsx')])
        if not current:
            return 0
        previous = filedialog.askopenfilename(parent=window, title='2. Select the PREVIOUS history snapshot (or an older editor Excel)',
            initialdir=str(exports / 'history'), filetypes=[('Pricing snapshots', '*.csv *.xlsx'), ('History CSV', '*.csv'), ('Excel', '*.xlsx')])
        if not previous:
            return 0
        output = filedialog.asksaveasfilename(parent=window, title='3. Save the comparison workbook',
            initialdir=str(exports), initialfile=f'comparison_{Path(current).stem}.xlsx',
            defaultextension='.xlsx', filetypes=[('Excel workbook', '*.xlsx')])
        if not output:
            return 0
        summary = save_comparison(previous, current, output)
        print(summary.to_string(index=False))
        print(f'Comparison saved: {output}')
        messagebox.showinfo('Comparison ready', f'Saved:\n{output}\n\nSheets: Summary, All price changes, Partner changes, Notes.\nNo prices or official history were changed.', parent=window)
        return 0
    except Exception as exc:
        messagebox.showerror('Compare pricing', str(exc), parent=window)
        print(f'Comparison failed: {exc}')
        return 1
    finally:
        window.destroy()


if __name__ == '__main__':
    raise SystemExit(main())
