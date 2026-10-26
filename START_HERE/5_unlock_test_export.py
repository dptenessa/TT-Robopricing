"""Unlock price editing after a local test export. Changes only the editing lock."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import re
import sqlite3

ROOT=Path(__file__).resolve().parents[1]


def database_path(root):
    path=Path(root)/'outputs/promo_planner/promo_schedule.db'
    if not path.is_file():raise FileNotFoundError('Pricing database not found: '+str(path))
    return path


def locked_dates(root=ROOT, *, today=None):
    today=today or date.today()
    with closing(sqlite3.connect(database_path(root).resolve().as_uri()+'?mode=ro',uri=True)) as con:
        con.row_factory=sqlite3.Row
        result=[]
        for row in con.execute("SELECT id,effective_from,label,metadata_json FROM pricing_change_sets WHERE source='price_date' AND undone_at='' ORDER BY effective_from DESC,id DESC"):
            meta=json.loads(row['metadata_json'] or '{}')
            if meta.get('official') and row['effective_from']>=today.isoformat():
                result.append(dict(id=row['id'],date=row['effective_from'],label=row['label'],pack=meta.get('partner_pack',''),key_date=meta.get('key_date','')))
        return result


def unlock_price_date(root, event_id, *, today=None):
    """Clear only official=True on the selected date's active price-date markers.

    Prices, price-change records, campaign definitions/publication flags, export
    files and export history are untouched. Existing published base values stay
    in place, and subsequent editor changes use the normal dated-edit workflow.
    """
    today=today or date.today()
    with closing(sqlite3.connect(database_path(root),timeout=10)) as con:
        con.row_factory=sqlite3.Row
        try:
            con.execute('BEGIN IMMEDIATE')
            selected=con.execute("SELECT * FROM pricing_change_sets WHERE id=? AND source='price_date' AND undone_at=''",(int(event_id),)).fetchone()
            if selected is None:raise ValueError('This price date no longer exists.')
            if selected['effective_from']<today.isoformat():raise ValueError('This date is in the past. Choose today or a future date to continue editing.')
            selected_meta=json.loads(selected['metadata_json'] or '{}')
            key=selected_meta.get('key_date')
            if not key:raise ValueError('The selected price date has no timeline key.')
            count=0
            for row in con.execute("SELECT id,metadata_json FROM pricing_change_sets WHERE source='price_date' AND undone_at='' AND effective_from=?",(selected['effective_from'],)).fetchall():
                meta=json.loads(row['metadata_json'] or '{}')
                if meta.get('key_date')==key and meta.get('official'):
                    meta['official']=False
                    con.execute('UPDATE pricing_change_sets SET metadata_json=? WHERE id=?',(json.dumps(meta),row['id']))
                    count+=1
            con.commit()
            return dict(date=selected['effective_from'],count=count)
        except Exception:
            con.rollback()
            raise


def main():
    import tkinter as tk
    from tkinter import messagebox
    window=tk.Tk();window.title('Unlock price editing');window.geometry('820x290')
    tk.Label(window,text='Close Pricing Editor first. Select the date locked by your local test export.',padx=14,pady=14).pack(anchor='w')
    try:items=locked_dates()
    except Exception as exc:messagebox.showerror('Cannot read pricing dates',str(exc),parent=window);window.destroy();return
    if not items:messagebox.showinfo('No locked dates','No locked price dates for today or the future were found.',parent=window);window.destroy();return
    box=tk.Listbox(window,width=110,height=7,font=('Segoe UI',10));box.pack(fill='both',expand=True,padx=14)
    for item in items:
        filename=re.split(r'[/\\]',item['pack'])[-1]
        box.insert('end',f"{item['date']} | {item['label']} | {filename}")
    box.selection_set(0)
    def unlock():
        selection=box.curselection()
        if not selection:return
        try:
            result=unlock_price_date(ROOT,items[selection[0]]['id'])
            messagebox.showinfo('Unlocked',f"Price editing unlocked for {result['date']}.\n\nReopen Pricing Editor and select that timeline date.\nYour prices and campaigns are unchanged.",parent=window)
            window.destroy()
        except Exception as exc:messagebox.showerror('Could not unlock',str(exc),parent=window)
    tk.Button(window,text='Unlock selected date',command=unlock,padx=16,pady=8).pack(pady=14)
    window.mainloop()


if __name__=='__main__':main()
