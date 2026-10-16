"""Tkinter control panel for the T-Travel advisory pipeline."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser
from pathlib import Path

from intelligence_pipeline import Pipeline, STEPS, BY_KEY, VERSION, parsed_time


class Launcher:
    def __init__(self, root, pipeline):
        self.root=root; self.pipeline=pipeline
        self.queue=queue.Queue(); self.thread=None; self.running_key=None; self.failed=False
        root.title('T-Travel · Market Insights launcher · '+VERSION)
        root.geometry('1160x820');root.minsize(920,650);root.configure(bg='#f5f5f7')
        root.protocol('WM_DELETE_WINDOW',self.close)
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('TFrame',background='#f5f5f7')
        style.configure('TLabel',background='#f5f5f7',font=('Segoe UI',10))
        style.configure('TButton',font=('Segoe UI',10),padding=(12,9))
        style.configure('Accent.TButton',background='#e20074',foreground='white',borderwidth=0)
        style.map('Accent.TButton',background=[('active','#bb0060'),('disabled','#d1b9c6')])
        style.configure('Treeview',font=('Segoe UI',10),rowheight=42,background='white',fieldbackground='white')
        style.configure('Treeview.Heading',font=('Segoe UI',10,'bold'),padding=8)
        style.configure('TProgressbar',background='#e20074')
        header=tk.Frame(root,bg='#171717',padx=22,pady=17);header.pack(fill='x')
        tk.Label(header,text='T-Travel  /  Market Insights',bg='#171717',fg='white',font=('Segoe UI',20,'bold')).pack(anchor='w')
        tk.Label(header,text='Refresh evidence → score routes → compare products → build the report',bg='#171717',fg='#ddd',font=('Segoe UI',10)).pack(anchor='w',pady=(6,0))
        body=ttk.Frame(root,padding=18);body.pack(fill='both',expand=True)
        ttk.Label(body,text='Advisory only: no competitor scraping, price publication, promo execution or campaign launch.',wraplength=1080).pack(anchor='w',pady=(0,10))
        self.tree=ttk.Treeview(body,columns=('step','state','success','reason'),show='headings',height=5,selectmode='browse')
        for name,title,width in [('step','Process',245),('state','Status',110),('success','Last completed',155),('reason','Why / next action',490)]:
            self.tree.heading(name,text=title);self.tree.column(name,width=width,minwidth=80,stretch=name=='reason')
        self.tree.pack(fill='x')
        for tag,color in [('Ready','#eaf7ef'),('Ready*','#eff7ed'),('Needs run','#fff5df'),('Warning','#fff0dd'),('Blocked','#fdecec'),('Not tracked','#f4f4f7'),('Running','#ffe5f2')]: self.tree.tag_configure(tag,background=color)
        self.tree.bind('<<TreeviewSelect>>',self.selection_changed)
        self.detail=tk.StringVar(value='Select a process to see its script and latest log.')
        ttk.Label(body,textvariable=self.detail,wraplength=1060).pack(anchor='w',pady=(8,10))
        buttons=ttk.Frame(body);buttons.pack(fill='x')
        self.run_needed=ttk.Button(buttons,text='Run required steps',style='Accent.TButton',command=lambda:self.start('needed'));self.run_needed.pack(side='left',padx=(0,8))
        self.run_all=ttk.Button(buttons,text='Run all',command=lambda:self.start('full'));self.run_all.pack(side='left',padx=(0,8))
        self.run_one=ttk.Button(buttons,text='Run selected + prerequisites',command=lambda:self.start('selected'));self.run_one.pack(side='left',padx=(0,8))
        self.stop_button=ttk.Button(buttons,text='Stop after current step',command=self.stop,state='disabled');self.stop_button.pack(side='left')
        toolbar=ttk.Frame(body);toolbar.pack(fill='x',pady=(8,8))
        self.refresh_button=ttk.Button(toolbar,text='Refresh status',command=self.refresh);self.refresh_button.pack(side='left',padx=(0,8))
        ttk.Button(toolbar,text='Open Market Insights',command=self.open_report).pack(side='left',padx=(0,8))
        ttk.Button(toolbar,text='Open latest step log',command=self.open_log).pack(side='left',padx=(0,8))
        self.choose_sales=ttk.Button(toolbar,text='Choose sales CSV…',command=self.select_sales);self.choose_sales.pack(side='left')
        self.open_after=tk.BooleanVar(value=True)
        ttk.Checkbutton(toolbar,text='Open report after build',variable=self.open_after).pack(side='right')
        self.progress=ttk.Progressbar(body,mode='indeterminate');self.progress.pack(fill='x',pady=(0,6))
        self.message=tk.StringVar(value='Checking local inputs…')
        ttk.Label(body,textvariable=self.message,wraplength=1060).pack(anchor='w',pady=(0,8))
        self.inputs=tk.StringVar()
        ttk.Label(body,textvariable=self.inputs,wraplength=1060,font=('Segoe UI',9)).pack(anchor='w')
        ttk.Label(body,text='Live process output',font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(10,4))
        log_frame=ttk.Frame(body);log_frame.pack(fill='both',expand=True)
        self.log=tk.Text(log_frame,height=10,bg='#1d1d23',fg='#f3f3f3',font=('Consolas',9),wrap='word',state='disabled',relief='flat')
        scroll=ttk.Scrollbar(log_frame,command=self.log.yview);self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.log.pack(side='left',fill='both',expand=True)
        self.refresh();self.root.after(100,self.poll)

    def refresh(self):
        try:
            statuses=self.pipeline.statuses()
            selected=self.tree.selection()
            for item in self.tree.get_children(): self.tree.delete(item)
            for step in STEPS:
                state,why=statuses[step.key]
                record=self.pipeline.state.get(step.key,{})
                completed=record.get('last_completed') or (record.get('finished_at') if record.get('status') in ('success','warning') else record.get('last_success'))
                success=parsed_time(completed)
                if step.key==self.running_key: state,why='Running','Live output below; elapsed time is not a completion estimate'
                self.tree.insert('', 'end',iid=step.key,values=(step.title,state,success.strftime('%d %b %H:%M') if success else 'Not recorded',why),tags=(state,))
            if selected: self.tree.selection_set(selected[0])
            self.inputs.set(self.pipeline.input_summary())
            self.message.set('First use: existing data is preserved. Run once to establish tracked status.' if not self.pipeline.state else 'Ready* = completed with upstream source warnings. Warnings alone do not require a rerun; select that source step to retry it.')
            self.selection_changed()
        except Exception as exc: messagebox.showerror('Status unavailable',str(exc),parent=self.root)

    def selection_changed(self,event=None):
        selected=self.tree.selection()
        if selected:
            key=selected[0];entry=self.pipeline.state.get(key,{})
            self.detail.set(str(self.pipeline.script(key))+(' · last attempt: '+entry.get('status','not tracked')))

    def busy(self,value):
        for button in [self.run_needed,self.run_all,self.run_one,self.refresh_button,self.choose_sales]: button.configure(state='disabled' if value else 'normal')
        self.stop_button.configure(state='normal' if value else 'disabled')
        if value:self.progress.start(12)
        else:self.progress.stop()

    def append(self,line):
        self.log.configure(state='normal');self.log.insert('end',line+'\n')
        # Keep the UI responsive on large corridor runs; full output stays on disk.
        lines=int(self.log.index('end-1c').split('.')[0])
        if lines>2500:self.log.delete('1.0',f'{lines-2000}.0')
        self.log.see('end');self.log.configure(state='disabled')

    def start(self,mode):
        if self.thread and self.thread.is_alive():return
        selected=self.tree.selection()
        if mode=='selected' and not selected:
            messagebox.showinfo('Select a process','Select a row first.',parent=self.root);return
        try:keys=self.pipeline.plan(mode,selected[0] if selected else None)
        except Exception as exc:messagebox.showerror('Cannot plan run',str(exc),parent=self.root);return
        if not keys:self.message.set('No refresh required. Source warnings remain visible; select a warning step to retry it after fixing the source.');return
        self.failed=False;self.keys=keys;self.busy(True)
        self.append('Run order: '+' → '.join(BY_KEY[k].title for k in keys))
        def run():
            try:self.pipeline.execute(keys,lambda kind,line:self.queue.put((kind,line)))
            except Exception as exc:self.queue.put(('error',str(exc)))
            finally:self.queue.put(('finished',''))
        self.thread=threading.Thread(target=run,daemon=False);self.thread.start()

    def poll(self):
        for _ in range(250):
            try:kind,line=self.queue.get_nowait()
            except queue.Empty:break
            if kind=='step':
                self.running_key=line
                self.tree.set(line,'state','Running');self.tree.item(line,tags=('Running',))
                self.message.set('Running '+BY_KEY[line].title+'…')
            elif kind=='updated':
                entry=self.pipeline.state.get(line,{})
                label='Warning' if entry.get('status')=='warning' else 'Ready'
                self.tree.set(line,'state',label);self.tree.item(line,tags=(label,))
            elif kind=='error':self.failed=True;self.append('ERROR: '+line);self.message.set('Stopped on error. Open the step log for details.')
            elif kind=='stopped':self.failed=True;self.append(line)
            elif kind=='finished':
                self.running_key=None;self.busy(False);self.refresh()
                self.message.set('Stopped; inspect the latest step log.' if self.failed else 'Completed. Review source warnings and report evidence dates.')
                if not self.failed and 'report' in self.keys and self.open_after.get():self.open_report()
            else:self.append(line)
        self.root.after(100,self.poll)

    def stop(self):
        self.pipeline.stop.set();self.stop_button.configure(state='disabled')
        self.message.set('Will stop after the current script finishes, so SQLite writes are not interrupted.')

    def open_report(self):
        if self.pipeline.report.exists():webbrowser.open(self.pipeline.report.as_uri())
        else:messagebox.showinfo('No report yet','Run the required steps to build Market Insights.',parent=self.root)

    def open_log(self):
        selected=self.tree.selection()
        if not selected:return
        value=self.pipeline.state.get(selected[0],{}).get('log')
        if value and Path(value).exists():
            if os.name=='nt':os.startfile(value)
            else:webbrowser.open(Path(value).as_uri())
        else:messagebox.showinfo('No log yet','Run this step once to create its log.',parent=self.root)

    def select_sales(self):
        path=filedialog.askopenfilename(title='Select T-Travel sales master (read-only input)',initialdir=str(self.pipeline.sales.parent) if self.pipeline.sales.parent.exists() else str(self.pipeline.root),filetypes=[('CSV files','*.csv'),('All files','*.*')])
        if path:self.pipeline.save_settings(path);self.refresh()

    def close(self):
        if self.thread and self.thread.is_alive():
            self.message.set('A step is running. Use “Stop after current step”, then close when it finishes.');return
        self.root.destroy()


def launch(pipeline=None):
    root=tk.Tk()
    Launcher(root,pipeline or Pipeline())
    root.mainloop()


if __name__=='__main__':launch()
