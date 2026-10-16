from __future__ import annotations

import calendar
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .engine import PromoOption, preview_campaign, preview_batch, execute_batch, read_catalogue, restore_workbook_backup
from .paths import find_manual_prices, planner_output_dir, project_root
from .outlook_sync import delete_campaign_events, sync_campaign_events
from .management_calendar import generate_management_calendar
from .store import Campaign, PromoStore

MAGENTA = "#E20074"
MAGENTA_DARK = "#B0005A"
MAGENTA_SOFT = "#FCE7F2"
DARK = "#202124"
HEADER_BLACK = "#111111"
MID = "#666A73"
LIGHT = "#F4F5F7"
WHITE = "#FFFFFF"
BORDER = "#D9DCE2"
GREEN_SOFT = "#E9F7EF"
GREEN = "#147A45"
AMBER_SOFT = "#FFF4DA"
AMBER = "#8A5A00"
BLUE_SOFT = "#EAF2FF"
BLUE = "#315EA8"
GREY_SOFT = "#EFF1F4"


class CalendarPicker(tk.Toplevel):
    def __init__(self, parent, target_var: tk.StringVar):
        super().__init__(parent)
        self.target_var = target_var
        self.title("Choose date")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=WHITE)
        try:
            current = datetime.strptime(target_var.get(), "%Y-%m-%d").date()
        except Exception:
            current = date.today()
        self.year, self.month = current.year, current.month

        top = tk.Frame(self, bg=MAGENTA, padx=8, pady=8)
        top.pack(fill="x")
        tk.Button(top, text="‹", command=self._prev, bg=MAGENTA, fg=WHITE, bd=0, font=("Segoe UI", 14, "bold"), activebackground=MAGENTA_DARK, activeforeground=WHITE).pack(side="left")
        self.title_label = tk.Label(top, bg=MAGENTA, fg=WHITE, font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left", expand=True, fill="x")
        tk.Button(top, text="›", command=self._next, bg=MAGENTA, fg=WHITE, bd=0, font=("Segoe UI", 14, "bold"), activebackground=MAGENTA_DARK, activeforeground=WHITE).pack(side="right")

        self.body = tk.Frame(self, bg=WHITE, padx=10, pady=8)
        self.body.pack(fill="both", expand=True)
        self._render()
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + max(20, (parent.winfo_width() - self.winfo_width()) // 2)
            y = parent.winfo_rooty() + max(20, (parent.winfo_height() - self.winfo_height()) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _prev(self):
        self.month -= 1
        if self.month == 0:
            self.month = 12
            self.year -= 1
        self._render()

    def _next(self):
        self.month += 1
        if self.month == 13:
            self.month = 1
            self.year += 1
        self._render()

    def _render(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.title_label.configure(text=f"{calendar.month_name[self.month]} {self.year}")
        for i, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            tk.Label(self.body, text=name, bg=WHITE, fg=MID, font=("Segoe UI", 8, "bold"), width=4).grid(row=0, column=i, padx=1, pady=2)
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(self.year, self.month)
        today = date.today()
        for r, week in enumerate(weeks, 1):
            for c, daynum in enumerate(week):
                if not daynum:
                    continue
                chosen = date(self.year, self.month, daynum)
                is_today = chosen == today
                bg = MAGENTA_SOFT if is_today else WHITE
                fg = MAGENTA if is_today else DARK
                b = tk.Button(
                    self.body, text=str(daynum), width=4, bd=0, bg=bg, fg=fg,
                    activebackground=MAGENTA, activeforeground=WHITE,
                    font=("Segoe UI", 9, "bold" if is_today else "normal"),
                    command=lambda d=chosen: self._choose(d),
                )
                b.grid(row=r, column=c, padx=1, pady=1)
        foot = tk.Frame(self.body, bg=WHITE)
        foot.grid(row=len(weeks) + 1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        tk.Button(foot, text="Today", command=lambda: self._choose(today), bg=MAGENTA_SOFT, fg=MAGENTA, bd=0, padx=10, pady=4, font=("Segoe UI", 9, "bold")).pack()

    def _choose(self, chosen: date):
        self.target_var.set(chosen.isoformat())
        self.destroy()


class PromoPlanner(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("T-Travel Promo Planner")
        self.geometry("1320x820")
        self.minsize(1040, 690)
        self.configure(bg=LIGHT)
        self.root_dir = project_root()
        self.out_dir = planner_output_dir(self.root_dir)
        self.store = PromoStore(self.out_dir / "promo_schedule.db")
        self.workbook = find_manual_prices(self.root_dir)
        self.catalogue = read_catalogue(self.workbook)
        self.dest_map = {code: name for code, name in self.catalogue.destinations}
        self.selected_destinations: list[str] = []
        self.plan_vars: dict[str, tk.BooleanVar] = {}
        self.day_vars: dict[int, tk.BooleanVar] = {}
        self.editing_campaign_id: int | None = None
        self.promo_var = tk.StringVar()
        self._promo_by_display: dict[str, PromoOption] = {}
        self._build_style()
        self._build_ui()
        self._refresh_destinations()
        self._refresh_campaigns()

    def _build_style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=LIGHT)
        s.configure("Card.TFrame", background=WHITE)
        s.configure("Header.TFrame", background=HEADER_BLACK)
        s.configure("Title.TLabel", background=LIGHT, foreground=DARK, font=("Segoe UI", 20, "bold"))
        s.configure("HeaderTitle.TLabel", background=HEADER_BLACK, foreground=WHITE, font=("Segoe UI", 20, "bold"))
        s.configure("HeaderSub.TLabel", background=HEADER_BLACK, foreground="#D0D0D0", font=("Segoe UI", 9))
        s.configure("Sub.TLabel", background=LIGHT, foreground=MID, font=("Segoe UI", 9))
        s.configure("CardTitle.TLabel", background=WHITE, foreground=DARK, font=("Segoe UI", 11, "bold"))
        s.configure("CardText.TLabel", background=WHITE, foreground=MID, font=("Segoe UI", 9))
        s.configure("Status.TLabel", background=WHITE, foreground=MID, font=("Segoe UI", 8))
        s.configure("Accent.TButton", background=MAGENTA, foreground=WHITE, font=("Segoe UI", 9, "bold"), padding=(12, 8), borderwidth=0)
        s.map("Accent.TButton", background=[("active", MAGENTA_DARK), ("disabled", "#D7A7C1")], foreground=[("disabled", WHITE)])
        s.configure("Soft.TButton", background=MAGENTA_SOFT, foreground=MAGENTA, font=("Segoe UI", 9, "bold"), padding=(10, 7), borderwidth=0)
        s.map("Soft.TButton", background=[("active", "#F7D2E7")])
        s.configure("Grey.TButton", background=GREY_SOFT, foreground=DARK, font=("Segoe UI", 9, "bold"), padding=(10, 7), borderwidth=0)
        s.map("Grey.TButton", background=[("active", "#E1E4E8")])
        s.configure("Danger.TButton", foreground="#A11A2B", font=("Segoe UI", 9, "bold"))
        s.configure("TCheckbutton", background=WHITE, font=("Segoe UI", 9))
        s.configure("TNotebook", background=LIGHT, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI", 9, "bold"), background=WHITE, foreground=MID)
        s.map("TNotebook.Tab",
              background=[("selected", MAGENTA), ("!selected", WHITE)],
              foreground=[("selected", WHITE), ("!selected", MID)],
              padding=[("selected", (22, 11)), ("!selected", (14, 7))])
        s.configure("Treeview", rowheight=30, font=("Segoe UI", 9), bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 15))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="T-Travel Promo Planner", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(left, text=f"Planning future promotions · Live pricing book: {self.workbook.name}", style="HeaderSub.TLabel").pack(anchor="w", pady=(2, 0))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(12, 16))
        self.create_tab = ttk.Frame(self.nb, padding=10)
        self.dashboard_tab = ttk.Frame(self.nb, padding=10)
        self.timeline_tab = ttk.Frame(self.nb, padding=10)
        # Timeline first: this is the management / planning view and the default screen.
        self.nb.add(self.timeline_tab, text="Timeline")
        self.nb.add(self.dashboard_tab, text="Campaigns & execution")
        self.nb.add(self.create_tab, text="Create / edit promotion")
        self._build_create_tab()
        self._build_dashboard_tab()
        self._build_timeline_tab()
        self.nb.select(self.timeline_tab)

    def _card(self, parent, title, subtitle=""):
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        ttk.Label(f, text=title, style="CardTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(f, text=subtitle, style="CardText.TLabel", wraplength=540).pack(anchor="w", pady=(2, 10))
        return f

    def _scrollable_card(self, parent, title, subtitle=""):
        outer = ttk.Frame(parent, style="Card.TFrame")
        head = ttk.Frame(outer, style="Card.TFrame", padding=(14, 14, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(head, text=subtitle, style="CardText.TLabel", wraplength=480).pack(anchor="w", pady=(2, 0))
        holder = ttk.Frame(outer, style="Card.TFrame")
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=WHITE, bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, style="Card.TFrame", padding=(14, 6, 14, 14))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        return outer, inner

    def _build_create_tab(self):
        self.create_tab.columnconfigure(0, weight=3)
        self.create_tab.columnconfigure(1, weight=2)
        self.create_tab.rowconfigure(0, weight=1)

        dest = self._card(self.create_tab, "1. Destinations", "Search and add destinations. Common selections can be saved as reusable groups.")
        dest.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        form_outer, form = self._scrollable_card(self.create_tab, "2. Promotion", "Choose an existing promotion from the workbook catalogue, then select plans, durations and dates.")
        form_outer.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))

        tools = ttk.Frame(dest, style="Card.TFrame")
        tools.pack(fill="x", pady=(0, 7))
        self.search_var = tk.StringVar()
        ttk.Entry(tools, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        self.search_var.trace_add("write", lambda *_: self._refresh_destinations())
        ttk.Button(tools, text="Add selected →", command=self._add_selected, style="Soft.TButton").pack(side="left", padx=(7, 0))
        self.available = tk.Listbox(dest, selectmode="extended", activestyle="none", font=("Segoe UI", 10), bd=0, highlightthickness=1, highlightbackground=BORDER, selectbackground=MAGENTA, selectforeground=WHITE)
        self.available.pack(fill="both", expand=True)
        self.available.bind("<Double-Button-1>", lambda _e: self._add_selected())
        self.available.bind("<ButtonPress-1>", self._drag_start)
        self.available.bind("<ButtonRelease-1>", self._drag_release)

        grp = ttk.Frame(dest, style="Card.TFrame")
        grp.pack(fill="x", pady=(9, 0))
        self.group_var = tk.StringVar()
        self.group_combo = ttk.Combobox(grp, textvariable=self.group_var, state="readonly", width=23)
        self.group_combo.pack(side="left")
        ttk.Button(grp, text="Load group", command=self._load_group).pack(side="left", padx=5)
        ttk.Button(grp, text="Save selection", command=self._save_group).pack(side="left")

        ttk.Label(dest, text="Campaign destinations", style="CardTitle.TLabel").pack(anchor="w", pady=(12, 4))
        self.selected = tk.Listbox(dest, selectmode="extended", activestyle="none", font=("Segoe UI", 9), bd=0, highlightthickness=1, highlightbackground=BORDER, height=6, selectbackground=MAGENTA, selectforeground=WHITE)
        self.selected.pack(fill="x")
        self.selected.bind("<Delete>", lambda _e: self._remove_selected())
        ttk.Button(dest, text="Remove selected", command=self._remove_selected).pack(anchor="e", pady=(5, 0))

        self.name_var = tk.StringVar()
        self.start_var = tk.StringVar(value=date.today().isoformat())
        self.end_var = tk.StringVar(value=date.today().isoformat())
        self.allow_var = tk.BooleanVar(value=False)
        self._labeled_entry(form, "Campaign name", self.name_var)

        ttk.Label(form, text="Promotion from catalogue", style="CardText.TLabel").pack(anchor="w", pady=(10, 3))
        promo_row = ttk.Frame(form, style="Card.TFrame")
        promo_row.pack(fill="x")
        self.promo_combo = ttk.Combobox(promo_row, textvariable=self.promo_var, state="readonly")
        self.promo_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(promo_row, text="↻", width=3, command=self._reload_catalogue).pack(side="left", padx=(5, 0))
        self._load_promo_combo()

        datef = ttk.Frame(form, style="Card.TFrame")
        datef.pack(fill="x", pady=(12, 3))
        self._calendar_field(datef, "Start date", self.start_var, 0)
        self._calendar_field(datef, "End date", self.end_var, 1)

        ttk.Label(form, text="Plans", style="CardText.TLabel").pack(anchor="w", pady=(12, 3))
        pbox = ttk.Frame(form, style="Card.TFrame")
        pbox.pack(fill="x")
        for i, p in enumerate(self.catalogue.plans):
            v = tk.BooleanVar(value=True)
            self.plan_vars[p] = v
            ttk.Checkbutton(pbox, text=p, variable=v).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 12), pady=1)

        ttk.Label(form, text="Durations", style="CardText.TLabel").pack(anchor="w", pady=(12, 3))
        ttk.Label(form, text="Click to toggle. Shift-click another day to select the whole range between them.", style="Status.TLabel").pack(anchor="w", pady=(0, 5))
        dtoolbar = ttk.Frame(form, style="Card.TFrame")
        dtoolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(dtoolbar, text="All", command=lambda: self._select_all_days(True)).pack(side="left")
        ttk.Button(dtoolbar, text="Clear", command=lambda: self._select_all_days(False)).pack(side="left", padx=(5, 0))
        dbox = ttk.Frame(form, style="Card.TFrame")
        dbox.pack(fill="x")
        self.day_buttons: dict[int, tk.Button] = {}
        self._last_day_clicked: int | None = None
        for i, d in enumerate(self.catalogue.days):
            v = tk.BooleanVar(value=d in {7, 10})
            self.day_vars[d] = v
            b = tk.Button(dbox, text=str(d), width=3, bd=0, relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2")
            b.configure(command=lambda day=d: self._day_click(day, shift=False))
            b.bind("<Shift-Button-1>", lambda e, day=d: self._day_shift_click(e, day))
            b.grid(row=i // 10, column=i % 10, padx=2, pady=2, sticky="ew")
            self.day_buttons[d] = b
        self._refresh_day_buttons()

        ttk.Checkbutton(form, text="Allow prices below cost floor for this campaign", variable=self.allow_var).pack(anchor="w", pady=(12, 4))
        ttk.Label(form, text="Notes", style="CardText.TLabel").pack(anchor="w", pady=(6, 3))
        self.notes = tk.Text(form, height=3, font=("Segoe UI", 9), bd=1, relief="solid", highlightthickness=0)
        self.notes.pack(fill="x")

        actions = ttk.Frame(self.create_tab, style="Card.TFrame", padding=(12, 9))
        actions.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Label(actions, text="Saving schedules the campaign and creates Outlook reminders. Pricing changes only when you APPLY it.", style="CardText.TLabel").pack(side="left")
        ttk.Button(actions, text="Preview", command=self._preview_unsaved).pack(side="right", padx=(6, 0))
        self.save_button = ttk.Button(actions, text="Save campaign", command=self._save_campaign, style="Accent.TButton")
        self.save_button.pack(side="right", padx=(6, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel edit", command=self._cancel_edit)
        self.cancel_button.pack(side="right", padx=(6, 0))

    def _day_click(self, day: int, shift: bool = False):
        if shift and self._last_day_clicked is not None:
            lo, hi = sorted((self._last_day_clicked, day))
            for d, var in self.day_vars.items():
                if lo <= d <= hi:
                    var.set(True)
        else:
            self.day_vars[day].set(not self.day_vars[day].get())
        self._last_day_clicked = day
        self._refresh_day_buttons()

    def _day_shift_click(self, _event, day: int):
        self._day_click(day, shift=True)
        return "break"

    def _select_all_days(self, selected: bool):
        for var in self.day_vars.values():
            var.set(selected)
        self._last_day_clicked = None
        self._refresh_day_buttons()

    def _refresh_day_buttons(self):
        for d, b in getattr(self, "day_buttons", {}).items():
            selected = self.day_vars[d].get()
            b.configure(bg=MAGENTA if selected else GREY_SOFT, fg=WHITE if selected else DARK,
                        activebackground=MAGENTA_DARK if selected else MAGENTA_SOFT,
                        activeforeground=WHITE if selected else MAGENTA)

    def _sync_outlook(self, campaign_id: int) -> str:
        c = self.store.get_campaign(campaign_id)
        try:
            start_id, end_id = sync_campaign_events(c)
            self.store.set_outlook_ids(campaign_id, start_id, end_id)
            return "Outlook reminders synced for the day before START and END."
        except Exception as exc:
            return f"Campaign saved, but Outlook reminders could not be synced: {exc}"

    def _build_dashboard_tab(self):
        self.dashboard_tab.columnconfigure(0, weight=1)
        self.dashboard_tab.rowconfigure(2, weight=1)

        top = ttk.Frame(self.dashboard_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Campaigns & date execution", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="Refresh", command=self._refresh_campaigns).grid(row=0, column=1, padx=3)
        ttk.Button(top, text="Edit", command=self._edit_selected).grid(row=0, column=2, padx=3)
        ttk.Button(top, text="Duplicate", command=self._duplicate_selected).grid(row=0, column=3, padx=3)
        ttk.Button(top, text="Delete", command=self._delete_selected, style="Danger.TButton").grid(row=0, column=4, padx=3)

        execution = ttk.Frame(self.dashboard_tab, style="Card.TFrame", padding=(14, 12))
        execution.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        execution.columnconfigure(2, weight=1)
        ttk.Label(execution, text="Execute a planned date", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(execution, text="Each key date marks changes at midnight. Removals are applied before new starts.", style="CardText.TLabel").grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 8))

        keynav = ttk.Frame(execution, style="Card.TFrame")
        keynav.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(0, 9))
        keynav.columnconfigure(2, weight=1)
        ttk.Label(keynav, text="KEY DATES", style="CardText.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(keynav, text="‹ Previous", command=self._previous_key_date, style="Grey.TButton").grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.key_date_strip = ttk.Frame(keynav, style="Card.TFrame")
        self.key_date_strip.grid(row=0, column=2, sticky="ew")
        ttk.Button(keynav, text="Next ›", command=self._next_key_date, style="Grey.TButton").grid(row=0, column=3, sticky="e", padx=(8, 0))

        self.execution_date_var = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(execution, textvariable=self.execution_date_var, state="readonly", width=13).grid(row=3, column=0, sticky="w")
        ttk.Button(execution, text="📅", width=3, command=lambda: CalendarPicker(self, self.execution_date_var)).grid(row=3, column=1, padx=(4, 10))
        ttk.Button(execution, text="Show date", command=self._refresh_execution_date, style="Soft.TButton").grid(row=3, column=2, sticky="w")
        ttk.Button(execution, text="Preview all", command=self._preview_execution_date).grid(row=3, column=3, padx=(8, 4))
        ttk.Button(execution, text="APPLY ALL CHANGES", command=self._apply_execution_date, style="Accent.TButton").grid(row=3, column=4, padx=(4, 0))
        self.execution_summary = ttk.Label(execution, text="", style="CardText.TLabel", wraplength=1050)
        self.execution_summary.grid(row=4, column=0, columnspan=4, sticky="w", pady=(9, 0))
        self.undo_date_button = ttk.Button(execution, text="Undo execution for this date", command=self._undo_execution_date, style="Danger.TButton")
        self.undo_date_button.grid(row=4, column=4, sticky="e", pady=(9, 0))

        cols = ("id", "name", "start", "end", "promo", "scope", "state")
        self.tree = ttk.Treeview(self.dashboard_tab, columns=cols, show="headings", selectmode="browse")
        labels = {"id": "#", "name": "Campaign", "start": "Start", "end": "End", "promo": "Promotion", "scope": "Scope", "state": "State"}
        widths = {"id": 42, "name": 220, "start": 95, "end": 95, "promo": 210, "scope": 220, "state": 210}
        for c in cols:
            self.tree.heading(c, text=labels[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_campaign_detail())
        self.tree.tag_configure("planned", background=BLUE_SOFT, foreground=BLUE)
        self.tree.tag_configure("draft", background=AMBER_SOFT, foreground=AMBER)
        self.tree.tag_configure("running", background=GREEN_SOFT, foreground=GREEN)
        self.tree.tag_configure("completed", background=GREY_SOFT, foreground=MID)
        scr = ttk.Scrollbar(self.dashboard_tab, orient="vertical", command=self.tree.yview)
        scr.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scr.set)

        bottom = ttk.Frame(self.dashboard_tab, style="Card.TFrame", padding=(12, 10))
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.detail = tk.Text(bottom, height=5, font=("Segoe UI", 9), state="disabled", bd=0, bg=WHITE, wrap="word")
        self.detail.grid(row=0, column=0, sticky="ew")
        ttk.Label(bottom, text="Published and expired campaigns are read-only. If a date batch was applied by mistake, use Undo execution for this date.", style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self._refresh_execution_date()

    def _key_dates(self) -> list[str]:
        """Return midnight boundaries: end dates and the prior day for starts."""
        dates = set()
        for c in self.store.list_campaigns():
            if c.start_date:
                start = datetime.strptime(c.start_date, "%Y-%m-%d").date()
                dates.add((start - timedelta(days=1)).isoformat())
            if c.end_date:
                dates.add(c.end_date)
        return sorted(dates)

    def _key_date_counts(self, key_date: str) -> tuple[int, int]:
        campaigns = self.store.list_campaigns()
        next_day = (datetime.strptime(key_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        starting = sum(1 for c in campaigns if c.start_date == next_day)
        ending = sum(1 for c in campaigns if c.end_date == key_date)
        return starting, ending

    def _jump_to_execution_date(self, key_date: str, switch_tab: bool = False):
        if not hasattr(self, "execution_date_var"):
            return
        self.execution_date_var.set(key_date)
        self._refresh_execution_date()
        if switch_tab and hasattr(self, "nb"):
            self.nb.select(self.dashboard_tab)

    def _previous_key_date(self):
        dates = self._key_dates()
        if not dates:
            return
        current = self.execution_date_var.get()
        previous = [d for d in dates if d < current]
        target = previous[-1] if previous else dates[0]
        self._jump_to_execution_date(target)

    def _next_key_date(self):
        dates = self._key_dates()
        if not dates:
            return
        current = self.execution_date_var.get()
        following = [d for d in dates if d > current]
        target = following[0] if following else dates[-1]
        self._jump_to_execution_date(target)

    def _render_key_date_strip(self):
        if not hasattr(self, "key_date_strip"):
            return
        for child in self.key_date_strip.winfo_children():
            child.destroy()
        dates = self._key_dates()
        if not dates:
            ttk.Label(self.key_date_strip, text="No key dates yet", style="CardText.TLabel").pack(side="left")
            return
        selected = self.execution_date_var.get() if hasattr(self, "execution_date_var") else date.today().isoformat()
        # Show a compact window around the selected date. If it is not a key date,
        # anchor on the next upcoming key date (or the last one in history).
        idx = next((i for i, d in enumerate(dates) if d >= selected), len(dates) - 1)
        start = max(0, min(idx - 2, max(0, len(dates) - 6)))
        visible = dates[start:start + 6]
        for d in visible:
            try:
                boundary = datetime.strptime(d, "%Y-%m-%d").date()
                next_day = boundary + timedelta(days=1)
                label_date = f"{boundary.strftime('%d %b').upper()} 24:00\n→ {next_day.strftime('%d %b').upper()} 00:00"
            except Exception:
                label_date = d
            label = label_date
            style = "Accent.TButton" if d == selected else "Grey.TButton"
            ttk.Button(
                self.key_date_strip, text=label, style=style,
                command=lambda kd=d: self._jump_to_execution_date(kd),
            ).pack(side="left", padx=(0, 5))

    def _actions_for_execution_date(self, execution_date: str):
        actions = []
        next_day = (datetime.strptime(execution_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        for c in self.store.list_campaigns():
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            if c.end_date == execution_date and started and not ended:
                actions.append((c, "END"))
            if c.start_date == next_day and not started:
                actions.append((c, "START"))
        return actions

    def _refresh_execution_date(self):
        if not hasattr(self, "execution_date_var"):
            return
        d = self.execution_date_var.get()
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            self.execution_summary.configure(text="Invalid execution date.")
            return
        self._render_key_date_strip()
        actions = self._actions_for_execution_date(d)
        ending = [c.name for c, a in actions if a == "END"]
        starting = [c.name for c, a in actions if a == "START"]
        boundary = datetime.strptime(d, "%Y-%m-%d").date()
        next_day = boundary + timedelta(days=1)
        batch = self.store.get_active_batch_for_date(d)
        parts = []
        if ending:
            parts.append(f"ENDING {boundary.strftime('%d %b').upper()} AT 24:00: " + ", ".join(ending))
        if starting:
            parts.append(f"STARTING {next_day.strftime('%d %b').upper()} AT 00:00: " + ", ".join(starting))
        if not parts:
            parts.append("No unapplied campaign changes scheduled for this midnight boundary.")
        if batch:
            parts.append(f"This date was executed at {batch['executed_at']} (batch #{batch['id']}).")
        self.execution_summary.configure(text="  |  ".join(parts))
        if hasattr(self, "undo_date_button"):
            self.undo_date_button.configure(state="normal" if batch else "disabled")

    def _preview_execution_date(self):
        d = self.execution_date_var.get()
        actions = self._actions_for_execution_date(d)
        if not actions:
            return messagebox.showinfo("Date preview", f"No unapplied changes are scheduled for {d}.")
        try:
            p = preview_batch(self.workbook, actions, d)
        except Exception as exc:
            return messagebox.showerror("Date preview", str(exc))
        lines = [f"Changes scheduled for {d}", ""]
        for item in p.items:
            verb = "REMOVE" if item.action == "END" else "PUBLISH"
            lines.append(f"{verb}: {item.campaign_name} · {item.changed_rows} rows")
            if item.below_floor_rows:
                lines.append(f"  Cost-floor warnings: {item.below_floor_rows}")
            if item.collisions:
                lines.append(f"  Collisions: {len(item.collisions)}")
        lines.append("")
        lines.append(f"Total row changes: {p.total_changed_rows}")
        if p.collisions:
            lines.append("\nCOLLISIONS\n" + "\n".join(p.collisions[:20]))
        messagebox.showinfo("Date preview", "\n".join(lines))

    def _apply_execution_date(self):
        d = self.execution_date_var.get()
        if self.store.get_active_batch_for_date(d):
            return messagebox.showinfo("Already executed", f"An active execution batch already exists for {d}. Undo it first if it was wrong.")
        actions = self._actions_for_execution_date(d)
        if not actions:
            return messagebox.showinfo("Execute date", f"No unapplied changes are scheduled for {d}.")
        try:
            p = preview_batch(self.workbook, actions, d)
        except Exception as exc:
            return messagebox.showerror("Execute date", str(exc))
        if p.collisions:
            return messagebox.showerror("Execution blocked", "Collisions were found:\n\n" + "\n".join(p.collisions[:20]))
        ending = [c.name for c, a in actions if a == "END"]
        starting = [c.name for c, a in actions if a == "START"]
        summary = []
        if ending:
            summary.append("REMOVE:\n- " + "\n- ".join(ending))
        if starting:
            summary.append("PUBLISH:\n- " + "\n- ".join(starting))
        if not messagebox.askyesno(
            "Apply all scheduled changes",
            f"Apply ALL changes planned for {d}?\n\n" + "\n\n".join(summary) + f"\n\nExpected row changes: {p.total_changed_rows}\n\nOne backup will be created before the whole batch.",
        ):
            return
        try:
            backup, changed_map = execute_batch(self.workbook, actions, self.out_dir / "backups", d)
            details = [
                {"campaign_id": int(c.id), "campaign_name": c.name, "action": action, "changed_rows": changed_map.get((int(c.id), action), 0)}
                for c, action in actions
            ]
            batch_id = self.store.create_batch(d, self.workbook, backup, details)
            for c, action in actions:
                changed = changed_map.get((int(c.id), action), 0)
                self.store.record_execution(c.id, action, self.workbook, backup, changed, details=f"Date batch {d}", batch_id=batch_id)
        except Exception as exc:
            return messagebox.showerror("Execution blocked", str(exc))
        self._refresh_campaigns()
        self._refresh_execution_date()
        messagebox.showinfo("Date executed", f"All scheduled changes for {d} were applied.\n\nRows changed: {sum(changed_map.values())}\nBackup: {backup.name}\nBatch: #{batch_id}")

    def _undo_execution_date(self):
        d = self.execution_date_var.get()
        batch = self.store.get_active_batch_for_date(d)
        if not batch:
            return messagebox.showinfo("Undo execution", f"There is no active execution batch for {d}.")
        if self.store.has_later_active_batch(int(batch["id"])):
            return messagebox.showerror(
                "Undo blocked",
                "A later date batch has already been executed. Undo the most recent batch first so current prices are not overwritten with an older state.",
            )
        if not messagebox.askyesno(
            "Undo date execution",
            f"Restore manual prices to the exact state before batch #{batch['id']} for {d}?\n\nThis is intended for correcting an execution mistake.",
        ):
            return
        try:
            restore_workbook_backup(self.workbook, Path(batch["backup_path"]))
            self.store.undo_batch(int(batch["id"]))
        except Exception as exc:
            return messagebox.showerror("Undo failed", str(exc))
        self._refresh_campaigns()
        self._refresh_execution_date()
        messagebox.showinfo("Execution undone", f"Batch #{batch['id']} for {d} was undone and manual prices were restored from its backup.")

    def _build_timeline_tab(self):
        self.timeline_tab.columnconfigure(0, weight=1)
        self.timeline_tab.rowconfigure(1, weight=1)
        top = ttk.Frame(self.timeline_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top, text="Campaign timeline", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="Bars show planned campaign windows; the vertical magenta line is today.", style="Sub.TLabel").pack(side="left", padx=(14, 0), pady=(7, 0))
        ttk.Button(top, text="Refresh", command=self._render_timeline).pack(side="right")
        ttk.Button(top, text="Open management HTML", command=self._open_management_calendar, style="Accent.TButton").pack(side="right", padx=(0, 7))
        holder = ttk.Frame(self.timeline_tab, style="Card.TFrame")
        holder.grid(row=1, column=0, sticky="nsew")
        self.timeline_canvas = tk.Canvas(holder, bg=WHITE, highlightthickness=0)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=self.timeline_canvas.xview)
        ybar = ttk.Scrollbar(holder, orient="vertical", command=self.timeline_canvas.yview)
        self.timeline_canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.timeline_canvas.grid(row=0, column=0, sticky="nsew")
        xbar.grid(row=1, column=0, sticky="ew")
        ybar.grid(row=0, column=1, sticky="ns")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

    def _render_timeline(self):
        if not hasattr(self, "timeline_canvas"):
            return
        cv = self.timeline_canvas
        cv.delete("all")
        campaigns = self.store.list_campaigns()
        if not campaigns:
            cv.create_text(24, 24, anchor="nw", text="No campaigns yet.", fill=MID, font=("Segoe UI", 11))
            cv.configure(scrollregion=(0, 0, 700, 400))
            return
        parsed = [(c, datetime.strptime(c.start_date, "%Y-%m-%d").date(), datetime.strptime(c.end_date, "%Y-%m-%d").date()) for c in campaigns]
        today = date.today()
        first = min([d1 for _, d1, _ in parsed] + [today])
        last = max([d2 for _, _, d2 in parsed] + [today])
        from datetime import timedelta
        first -= timedelta(days=3)
        last += timedelta(days=3)
        day_w, left, top, row_h = 20, 210, 48, 34
        total_days = (last - first).days + 1
        width = left + total_days * day_w + 30
        height = top + len(parsed) * row_h + 40
        d = first
        for i in range(total_days):
            x = left + i * day_w
            if d.day == 1 or d.weekday() == 0:
                cv.create_line(x, 28, x, height, fill="#ECEEF2")
                label = d.strftime("%d %b") if d.day != 1 else d.strftime("%b %Y")
                cv.create_text(x + 2, 18, anchor="nw", text=label, fill=MID, font=("Segoe UI", 8))
            d += timedelta(days=1)
        # Key-date markers: clicking one jumps directly to that execution date.
        for key_date in self._key_dates():
            try:
                kd = datetime.strptime(key_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            boundary = kd + timedelta(days=1)
            if boundary < first or boundary > last:
                continue
            kx = left + (boundary - first).days * day_w
            tag = "keydate_" + key_date.replace("-", "_")
            cv.create_line(kx, 28, kx, height, fill="#F2B7D4", dash=(2, 4), tags=(tag,))
            cv.create_oval(kx - 4, 28, kx + 4, 36, fill=MAGENTA, outline="", tags=(tag,))
            starts, ends = self._key_date_counts(key_date)
            hint = []
            if starts:
                hint.append(f"+{starts}")
            if ends:
                hint.append(f"−{ends}")
            cv.create_text(kx + 6, 31, anchor="w", text="/".join(hint), fill=MAGENTA, font=("Segoe UI", 7, "bold"), tags=(tag,))
            cv.tag_bind(tag, "<Button-1>", lambda _e, d=key_date: self._jump_to_execution_date(d, switch_tab=True))
            cv.tag_bind(tag, "<Enter>", lambda _e: cv.configure(cursor="hand2"))
            cv.tag_bind(tag, "<Leave>", lambda _e: cv.configure(cursor=""))

        today_x = left + (today - first).days * day_w
        cv.create_line(today_x, 26, today_x, height, fill=MAGENTA, width=2)
        cv.create_text(today_x + 3, 30, anchor="nw", text="TODAY", fill=MAGENTA, font=("Segoe UI", 8, "bold"))
        for r, (c, d1, d2) in enumerate(parsed):
            y = top + r * row_h
            cv.create_text(12, y + 12, anchor="w", text=c.name[:28], fill=DARK, font=("Segoe UI", 9, "bold"))
            x1 = left + (d1 - first).days * day_w
            x2 = left + ((d2 - first).days + 1) * day_w
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            if ended:
                fill = "#A4A9B1"
            elif started and d2 < today:
                fill = "#D97706"
            elif started:
                fill = "#27885A"
            elif d2 < today:
                fill = "#8C6F9E"
            elif d1 <= today:
                fill = MAGENTA
            else:
                fill = "#6B8FDC"
            cv.create_rectangle(x1, y + 5, x2, y + 25, fill=fill, outline="")
            cv.create_text(x1 + 5, y + 15, anchor="w", text=(c.promo_name or c.promo_code)[:22], fill=WHITE, font=("Segoe UI", 8, "bold"))
        cv.configure(scrollregion=(0, 0, width, height))

    def _open_management_calendar(self):
        try:
            path = generate_management_calendar(
                self.store,
                self.out_dir / "promotion_calendar.html",
                open_after=True,
            )
        except Exception as exc:
            return messagebox.showerror("Management calendar", str(exc))
        messagebox.showinfo("Management calendar", f"Management timeline created:\n{path}")

    def _labeled_entry(self, parent, label, var):
        ttk.Label(parent, text=label, style="CardText.TLabel").pack(anchor="w", pady=(6, 3))
        ttk.Entry(parent, textvariable=var).pack(fill="x")

    def _calendar_field(self, parent, label, var, col):
        pad = (0, 4) if col == 0 else (4, 0)
        box = ttk.Frame(parent, style="Card.TFrame")
        box.grid(row=0, column=col, sticky="ew", padx=pad)
        parent.columnconfigure(col, weight=1)
        ttk.Label(box, text=label, style="CardText.TLabel").pack(anchor="w")
        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=(2, 0))
        ttk.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="📅", width=3, command=lambda v=var: CalendarPicker(self, v)).pack(side="left", padx=(4, 0))

    def _load_promo_combo(self, prefer_code: str = ""):
        self._promo_by_display = {p.display: p for p in self.catalogue.promos}
        values = list(self._promo_by_display)
        if hasattr(self, "promo_combo"):
            self.promo_combo["values"] = values
        target = ""
        if prefer_code:
            for display, promo in self._promo_by_display.items():
                if promo.code == prefer_code:
                    target = display
                    break
        if not target and values:
            target = values[0]
        self.promo_var.set(target)

    def _selected_promo(self) -> PromoOption:
        promo = self._promo_by_display.get(self.promo_var.get())
        if promo is None:
            raise ValueError("Choose a promotion from the workbook Promo catalogue.")
        return promo

    def _reload_catalogue(self):
        try:
            current_code = self._selected_promo().code if self.promo_var.get() else ""
        except Exception:
            current_code = ""
        try:
            self.catalogue = read_catalogue(self.workbook)
            self.dest_map = {code: name for code, name in self.catalogue.destinations}
            self._load_promo_combo(current_code)
        except Exception as exc:
            messagebox.showerror("Promo catalogue", str(exc))
            return
        messagebox.showinfo("Promo catalogue", f"Reloaded {len(self.catalogue.promos)} promotions from {self.workbook.name}.")

    def _refresh_destinations(self):
        q = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        if not hasattr(self, "available"):
            return
        self.available.delete(0, "end")
        for code, name in self.catalogue.destinations:
            text = f"{name}  [{code}]"
            if not q or q in text.lower():
                self.available.insert("end", text)
        self._refresh_groups()
        self._render_timeline()
        self._render_key_date_strip()

    def _code_from_display(self, text):
        if "[" in text and text.endswith("]"):
            return text.rsplit("[", 1)[1][:-1]
        return text.strip().upper()

    def _add_selected(self):
        for i in self.available.curselection():
            c = self._code_from_display(self.available.get(i))
            if c not in self.selected_destinations:
                self.selected_destinations.append(c)
        self._render_selected()

    def _render_selected(self):
        self.selected.delete(0, "end")
        for c in self.selected_destinations:
            self.selected.insert("end", f"{self.dest_map.get(c, c)}  [{c}]")

    def _remove_selected(self):
        remove = {self._code_from_display(self.selected.get(i)) for i in self.selected.curselection()}
        self.selected_destinations = [c for c in self.selected_destinations if c not in remove]
        self._render_selected()

    def _drag_start(self, _e):
        self._dragging = True

    def _drag_release(self, _e):
        if getattr(self, "_dragging", False):
            self._add_selected()
        self._dragging = False

    def _refresh_groups(self):
        if hasattr(self, "group_combo"):
            self.group_combo["values"] = list(self.store.list_groups().keys())

    def _save_group(self):
        if not self.selected_destinations:
            return messagebox.showwarning("Promo Planner", "Select destinations first.")
        name = simpledialog.askstring("Save group", "Group name:", parent=self)
        if name:
            self.store.save_group(name, self.selected_destinations)
            self._refresh_groups()

    def _load_group(self):
        vals = self.store.list_groups().get(self.group_var.get(), [])
        for c in vals:
            if c not in self.selected_destinations:
                self.selected_destinations.append(c)
        self._render_selected()

    def _validate_form(self):
        name = self.name_var.get().strip()
        if not name:
            raise ValueError("Campaign name is required.")
        for value, label in ((self.start_var.get(), "Start date"), (self.end_var.get(), "End date")):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{label} is invalid.")
        if self.end_var.get() < self.start_var.get():
            raise ValueError("End date cannot be before start date.")
        promo = self._selected_promo()
        plans = [p for p, v in self.plan_vars.items() if v.get()]
        days = [d for d, v in self.day_vars.items() if v.get()]
        if not self.selected_destinations:
            raise ValueError("Select at least one destination.")
        if not plans:
            raise ValueError("Select at least one plan.")
        if not days:
            raise ValueError("Select at least one duration.")
        return name, promo, plans, days

    def _campaign_from_form(self):
        name, promo, plans, days = self._validate_form()
        return Campaign(
            id=None,
            name=name,
            start_date=self.start_var.get(),
            end_date=self.end_var.get(),
            promo_type=promo.promo_type,
            promo_value=promo.value,
            promo_code=promo.code,
            promo_name=promo.label,
            destinations=list(self.selected_destinations),
            plans=plans,
            days=days,
            allow_below_cost=self.allow_var.get(),
            notes=self.notes.get("1.0", "end").strip(),
        )

    def _save_campaign(self):
        try:
            if self.editing_campaign_id is None:
                c = self._campaign_from_form()
                cid = self.store.save_campaign(c)
                msg = f"Campaign saved as #{cid}.\nNothing has been changed in pricing yet."
            else:
                old = self.store.get_campaign(self.editing_campaign_id)
                if self.store.has_execution(old.id, "START") or self.store.has_execution(old.id, "END"):
                    raise ValueError("Executed campaigns are read-only. Undo the execution batch first if it was applied by mistake.")
                c = self._campaign_from_form()
                c.id = old.id
                c.created_at = old.created_at
                c.status = old.status
                self.store.update_campaign(c)
                cid = c.id
                msg = f"Campaign #{c.id} updated."
                self._cancel_edit(clear_form=False)
        except Exception as exc:
            return messagebox.showerror("Cannot save campaign", str(exc))
        outlook_note = self._sync_outlook(cid)
        self._refresh_campaigns()
        self._refresh_execution_date()
        self.nb.select(self.dashboard_tab)
        if outlook_note:
            msg += "\n\n" + outlook_note
        messagebox.showinfo("Promo Planner", msg)

    def _preview_unsaved(self):
        try:
            c = self._campaign_from_form()
            p = preview_campaign(self.workbook, c, "START")
        except Exception as exc:
            return messagebox.showerror("Preview", str(exc))
        self._show_preview(p)

    def _refresh_campaigns(self):
        if not hasattr(self, "tree"):
            return
        for x in self.tree.get_children():
            self.tree.delete(x)
        today = date.today().isoformat()
        for c in self.store.list_campaigns():
            sdone = self.store.has_execution(c.id, "START")
            edone = self.store.has_execution(c.id, "END")
            if edone:
                state, tag = "Expired · removed from current prices", "completed"
            elif sdone and c.end_date < today:
                state, tag = "PUBLISHED · removal overdue", "draft"
            elif sdone:
                state, tag = "PUBLISHED to current prices", "running"
            elif c.end_date < today:
                state, tag = "Expired · never published", "completed"
            elif c.start_date <= today:
                state, tag = "START due · not yet published", "planned"
            else:
                state, tag = "Planned · not yet published", "planned"
            scope = f"{len(c.destinations)} dest · {len(c.plans)} plans · {len(c.days)} durations"
            promo = c.promo_name or f"{c.promo_value:g}{'%' if c.promo_type == 'percent' else ' off'}"
            self.tree.insert("", "end", iid=str(c.id), values=(c.id, c.name, c.start_date, c.end_date, promo, scope, state), tags=(tag,))
        self._refresh_groups()
        self._render_timeline()

    def _selected_campaign(self):
        sel = self.tree.selection()
        if not sel:
            raise ValueError("Select a campaign first.")
        return self.store.get_campaign(int(sel[0]))

    def _show_campaign_detail(self):
        try:
            c = self._selected_campaign()
        except Exception:
            return
        started = self.store.has_execution(c.id, "START")
        ended = self.store.has_execution(c.id, "END")
        state = "Expired / removed" if ended else ("Published to current prices" if started else "Planned / not yet published")
        txt = (
            f"{c.name}\n"
            f"Promotion: {c.promo_name or '-'} | catalogue code: {c.promo_code}\n"
            f"{c.start_date} → {c.end_date} | {state}\n"
            f"Destinations: {', '.join(c.destinations)}\n"
            f"Plans: {', '.join(c.plans)} | Days: {', '.join(map(str, c.days))}\n"
            f"Notes: {c.notes or '-'}"
        )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", txt)
        self.detail.configure(state="disabled")

    def _load_campaign_into_form(self, c):
        self.name_var.set(c.name)
        self.start_var.set(c.start_date)
        self.end_var.set(c.end_date)
        self.allow_var.set(c.allow_below_cost)
        self.selected_destinations = list(c.destinations)
        self._render_selected()
        for p, v in self.plan_vars.items():
            v.set(p in c.plans)
        for d, v in self.day_vars.items():
            v.set(d in c.days)
        self._refresh_day_buttons()
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", c.notes)
        # Match by stored promo definition first, then label; edits may come from v0.1/v0.2 campaigns.
        target = ""
        for display, promo in self._promo_by_display.items():
            if (promo.label == c.promo_name and c.promo_name) or (promo.promo_type == c.promo_type and abs(promo.value - c.promo_value) < 1e-9):
                target = display
                break
        if target:
            self.promo_var.set(target)

    def _edit_selected(self):
        try:
            c = self._selected_campaign()
        except Exception as exc:
            return messagebox.showwarning("Edit campaign", str(exc))
        if self.store.has_execution(c.id, "START") or self.store.has_execution(c.id, "END"):
            return messagebox.showinfo("Campaign read-only", "Once a campaign has been executed it is kept as audit history and cannot be edited. If publication was a mistake, undo that execution date first.")
        self.editing_campaign_id = c.id
        self._load_campaign_into_form(c)
        self.save_button.configure(text="Save changes")
        self.nb.select(self.create_tab)

    def _cancel_edit(self, clear_form=True):
        self.editing_campaign_id = None
        if hasattr(self, "save_button"):
            self.save_button.configure(text="Save campaign")
        if clear_form:
            self.name_var.set("")
            self.start_var.set(date.today().isoformat())
            self.end_var.set(date.today().isoformat())
            self.selected_destinations = []
            self._render_selected()
            self.notes.delete("1.0", "end")
            if self._promo_by_display:
                self.promo_var.set(next(iter(self._promo_by_display)))
            for d, v in self.day_vars.items():
                v.set(d in {7, 10})
            self._refresh_day_buttons()

    def _duplicate_selected(self):
        try:
            c = self._selected_campaign()
            cid = self.store.duplicate_campaign(c.id, c.promo_code)
        except Exception as exc:
            return messagebox.showerror("Duplicate campaign", str(exc))
        note = self._sync_outlook(cid)
        self._refresh_campaigns()
        messagebox.showinfo("Promo Planner", f"Created editable copy as campaign #{cid}." + (f"\n\n{note}" if note else ""))

    def _delete_selected(self):
        try:
            c = self._selected_campaign()
        except Exception as exc:
            return messagebox.showwarning("Delete campaign", str(exc))
        if self.store.has_execution(c.id, "START") or self.store.has_execution(c.id, "END"):
            return messagebox.showinfo("Campaign read-only", "Executed campaigns are kept for audit. Undo the relevant date execution first if it was applied by mistake.")
        if not messagebox.askyesno("Delete campaign", f"Delete planned campaign '{c.name}'?\n\nIts Outlook START/END reminders will also be deleted."):
            return
        outlook_warning = ""
        try:
            try:
                delete_campaign_events(c)
            except Exception as exc:
                outlook_warning = f"Outlook reminder cleanup failed: {exc}"
            self.store.delete_campaign(c.id)
        except Exception as exc:
            return messagebox.showerror("Delete campaign", str(exc))
        self._refresh_campaigns()
        self._refresh_execution_date()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        if outlook_warning:
            messagebox.showwarning("Outlook", outlook_warning)

    def _preview_selected(self, action):
        try:
            p = preview_campaign(self.workbook, self._selected_campaign(), action)
        except Exception as exc:
            return messagebox.showerror("Preview", str(exc))
        self._show_preview(p)

    def _show_preview(self, p):
        msg = f"{p.action}\n\nMatched rows: {p.matched_rows}\nWould change: {p.changed_rows}\nBelow cost floor: {p.below_floor_rows}\nCollisions: {len(p.collisions)}"
        if p.collisions:
            msg += "\n\nCOLLISIONS\n" + "\n".join(p.collisions[:12])
        if p.examples:
            msg += "\n\nExamples\n" + "\n".join(p.examples)
        messagebox.showinfo("Promotion preview", msg)




def main():
    try:
        PromoPlanner().mainloop()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("T-Travel Promo Planner", str(exc))
        root.destroy()
        raise


if __name__ == "__main__":
    main()
