from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import shutil

from PySide6.QtCore import QDate, Qt, Signal, QRect
from PySide6.QtGui import QColor, QBrush, QFont, QPen, QDesktopServices, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QDialogButtonBox,
    QVBoxLayout,
    QWidget,
)

from .engine import PromoOption, preview_campaign, preview_batch, execute_batch, read_catalogue, restore_workbook_backup
from .management_calendar import generate_management_calendar
from .outlook_sync import delete_campaign_events, sync_campaign_events
from .paths import find_manual_prices, planner_output_dir, project_root
from .store import Campaign, PromoStore

MAGENTA = "#E20074"
MAGENTA_DARK = "#B0005A"
MAGENTA_SOFT = "#FCE7F2"
HEADER_BLACK = "#111111"
DARK = "#202124"
MID = "#676B73"
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


class CleanCheckBox(QCheckBox):
    """Checkbox with a small visible box only; no row-level background/border."""

    BOX = 11

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        box_top = max(0, (self.height() - self.BOX) // 2)
        box = QRect(1, box_top, self.BOX, self.BOX)

        if self.isChecked():
            # Checked state: show only the clean magenta tick, with no surrounding frame.
            path = QPainterPath()
            path.moveTo(box.left() + 2, box.top() + 7)
            path.lineTo(box.left() + 6, box.bottom() - 2)
            path.lineTo(box.right() - 1, box.top() + 2)
            painter.setPen(QPen(QColor("#A00052"), 1.35, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        else:
            # Unticked state: a small, discoverable empty grey box.
            painter.setPen(QPen(QColor("#BFC3C9"), 0.65))
            painter.setBrush(QColor(WHITE))
            painter.drawRoundedRect(box, 1.8, 1.8)

        text_rect = QRect(box.right() + 7, 0, max(0, self.width() - box.right() - 8), self.height())
        painter.setPen(QColor(DARK if self.isEnabled() else MID))
        painter.setFont(self.font())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), self.fontMetrics().horizontalAdvance(self.text()) + 28))
        hint.setHeight(max(hint.height(), 22))
        return hint


class DestinationDropList(QListWidget):
    codesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    def dragEnterEvent(self, event):
        src = event.source()
        if isinstance(src, QListWidget):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        src = event.source()
        if isinstance(src, QListWidget):
            codes = []
            for item in src.selectedItems():
                code = item.data(Qt.UserRole)
                if code:
                    codes.append(str(code))
            if codes:
                self.codesDropped.emit(codes)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class DayButton(QPushButton):
    dayClicked = Signal(int, bool)

    def __init__(self, day: int, parent=None):
        super().__init__(str(day), parent)
        self.day = day
        self._shift_on_press = False
        self.setCheckable(True)
        self.setObjectName("dayChip")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.toggled.connect(self._apply_visual_state)
        self.clicked.connect(self._emit_click)
        self._apply_visual_state(False)

    def _apply_visual_state(self, checked: bool):
        if checked:
            self.setStyleSheet(
                f"QPushButton {{ background:{MAGENTA}; color:white; border:1px solid {MAGENTA}; "
                "border-radius:6px; padding:0; font-size:8.5pt; font-weight:700; }}"
                f"QPushButton:hover {{ background:{MAGENTA_DARK}; border-color:{MAGENTA_DARK}; }}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{ background:white; color:{DARK}; border:1px solid {BORDER}; "
                "border-radius:6px; padding:0; font-size:8.5pt; font-weight:650; }}"
                f"QPushButton:hover {{ background:{MAGENTA_SOFT}; color:{DARK}; border-color:#E9A8C8; }}"
            )

    def mousePressEvent(self, event):
        self._shift_on_press = bool(event.modifiers() & Qt.ShiftModifier)
        super().mousePressEvent(event)

    def _emit_click(self):
        self.dayClicked.emit(self.day, self._shift_on_press)
        self._shift_on_press = False


class PromoPlanner(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("T-Travel Promo Planner")
        self.resize(1320, 820)
        self.setMinimumSize(1060, 700)

        self.root_dir = project_root()
        self.out_dir = planner_output_dir(self.root_dir)
        self.store = PromoStore(self.out_dir / "promo_schedule.db")
        self.workbook = find_manual_prices(self.root_dir)
        self.catalogue = read_catalogue(self.workbook)
        self.dest_map = {code: name for code, name in self.catalogue.destinations}

        self.selected_destinations: list[str] = []
        self.editing_campaign_id: int | None = None
        self.day_buttons: dict[int, DayButton] = {}
        self.plan_checks: dict[str, QCheckBox] = {}
        self._promo_by_display: dict[str, PromoOption] = {}
        self._last_day_clicked: int | None = None
        self._last_execution_key_date: str | None = None

        self._apply_style()
        self._build_ui()
        self._load_promo_combo()
        self._refresh_destinations()
        self._refresh_groups()
        self._refresh_campaigns()

    # ---------- UI shell ----------
    def _apply_style(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {LIGHT}; color: {DARK}; font-family: 'Segoe UI'; font-size: 9pt; }}
            QFrame#header {{ background: {HEADER_BLACK}; }}
            QLabel#headerTitle {{ color: white; font-size: 20pt; font-weight: 700; background: {HEADER_BLACK}; }}
            QLabel#headerSub {{ color: #CFCFCF; background: {HEADER_BLACK}; }}
            QFrame.card {{ background: white; border: 1px solid {BORDER}; border-radius: 8px; }}
            QLabel.cardTitle {{ background: white; font-size: 11pt; font-weight: 700; }}
            QLabel.muted {{ color: {MID}; background: transparent; }}
            QPushButton {{ background: {GREY_SOFT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 7px 11px; font-weight: 600; }}
            QPushButton:hover {{ background: #E3E6EA; }}
            QPushButton#primary {{ background: {MAGENTA}; color: white; border-color: {MAGENTA}; font-weight: 700; }}
            QPushButton#primary:hover {{ background: {MAGENTA_DARK}; }}
            QPushButton#primary:disabled {{ background: #D5D8DD; color: #6B7078; border-color: #C4C8CE; }}
            QPushButton#soft {{ background: {MAGENTA_SOFT}; color: {MAGENTA_DARK}; border-color: #F4C4DD; }}
            QPushButton#danger {{ color: #A11A2B; }}
            QPushButton#dayChip {{
                background: white; color: {DARK}; border: 1px solid {BORDER};
                border-radius: 6px; padding: 0; font-size: 8.5pt; font-weight: 650;
            }}
            QPushButton#dayChip:hover {{ background: {MAGENTA_SOFT}; border-color: #E9A8C8; }}
            QPushButton#dayChip:checked {{ background: {MAGENTA}; color: white; border-color: {MAGENTA}; font-weight: 750; }}
            QFrame#durationPanel {{ background: white; border: 1px solid {BORDER}; border-radius: 8px; }}
            QTabWidget::pane {{ border: 0; top: -1px; }}
            QTabBar::tab {{ background: #E5E7EA; color: {MID}; border: 0; padding: 8px 16px; margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px; font-weight: 650; }}
            QTabBar::tab:selected {{ background: {MAGENTA}; color: white; padding: 12px 24px; font-weight: 750; }}
            QLineEdit, QComboBox, QDateEdit, QTextEdit, QListWidget, QTreeWidget {{ background: white; border: 1px solid #B9BEC7; border-radius: 5px; padding: 4px; }}
            QComboBox#requiredInput, QDateEdit#requiredInput {{ border: 1px solid #9EA4AE; background: white; }}
            QComboBox#requiredInput:focus, QDateEdit#requiredInput:focus {{ border: 2px solid {MAGENTA}; }}
            QCheckBox {{ background: transparent; color: #202124; border: 0; padding: 0; spacing: 0; }}
            QListWidget::item:selected, QTreeWidget::item:selected {{ background: {MAGENTA}; color: white; }}
            QHeaderView::section {{ background: #ECEEF1; color: {DARK}; border: 0; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; padding: 7px; font-weight: 700; }}
            QScrollArea {{ border: 0; background: transparent; }}
        """)

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame(objectName="header")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 14, 24, 14)
        title = QLabel("T-Travel Promo Planner", objectName="headerTitle")
        sub = QLabel(f"Planning future promotions · Live pricing book: {self.workbook.name}", objectName="headerSub")
        hl.addWidget(title)
        hl.addWidget(sub)
        outer.addWidget(header)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(18, 12, 18, 16)
        self.tabs = QTabWidget()
        bl.addWidget(self.tabs)
        outer.addWidget(body, 1)

        self.timeline_tab = QWidget()
        self.execution_tab = QWidget()
        self.create_tab = QWidget()
        self.tabs.addTab(self.timeline_tab, "Timeline")
        self.tabs.addTab(self.execution_tab, "Campaigns")
        self.tabs.addTab(self.create_tab, "Create / edit promotion")
        self.tabs.setCurrentWidget(self.timeline_tab)

        self._build_timeline_tab()
        self._build_execution_tab()
        self._build_create_tab()
        self.setCentralWidget(root)

    def _card(self, title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setProperty("class", "card")
        frame.setStyleSheet(f"QFrame {{ background:white; border:1px solid {BORDER}; border-radius:8px; }} QLabel {{ border:0; background:white; }}")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(7)
        t = QLabel(title)
        t.setProperty("class", "cardTitle")
        t.setStyleSheet("font-size:11pt;font-weight:700;border:0;")
        lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setWordWrap(True)
            s.setStyleSheet(f"color:{MID};border:0;")
            lay.addWidget(s)
        return frame, lay

    # ---------- Create/edit ----------
    def _build_create_tab(self):
        root = QVBoxLayout(self.create_tab)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # Destinations card
        dest_card, dl = self._card("1. Destinations", "Click a destination and it is added immediately to the campaign. The panel below is the exact campaign scope. Save common selections as groups.")
        search_row = QHBoxLayout()
        self.search_edit = QComboBox()
        self.search_edit.setEditable(True)
        self.search_edit.lineEdit().setPlaceholderText("Search destination...")
        self.search_edit.lineEdit().textChanged.connect(self._refresh_destinations)
        search_row.addWidget(self.search_edit, 1)
        dl.addLayout(search_row)

        self.available_list = QListWidget()
        self.available_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.available_list.setDragEnabled(True)
        self.available_list.setDragDropMode(QAbstractItemView.DragOnly)
        self.available_list.itemClicked.connect(self._pick_destination)
        dl.addWidget(self.available_list, 1)

        group_row = QHBoxLayout()
        self.group_combo = QComboBox()
        load_group = QPushButton("Load group")
        save_group = QPushButton("Save selection")
        load_group.clicked.connect(self._load_group)
        save_group.clicked.connect(self._save_group)
        group_row.addWidget(self.group_combo, 1)
        group_row.addWidget(load_group)
        group_row.addWidget(save_group)
        dl.addLayout(group_row)

        selected_header = QHBoxLayout()
        selected_title = QLabel("Selected destinations")
        selected_title.setStyleSheet("font-weight: 700;")
        self.selected_count_label = QLabel("0 selected")
        self.selected_count_label.setObjectName("muted")
        selected_header.addWidget(selected_title)
        selected_header.addStretch(1)
        selected_header.addWidget(self.selected_count_label)
        dl.addLayout(selected_header)

        self.selected_list = DestinationDropList()
        self.selected_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.selected_list.setMaximumHeight(190)
        self.selected_list.codesDropped.connect(self._add_destination_codes)
        dl.addWidget(self.selected_list)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected)
        dl.addWidget(remove_btn, 0, Qt.AlignRight)
        splitter.addWidget(dest_card)

        # Promotion form, scrollable
        form_card, fl = self._card("2. Promotion", "Use the existing workbook promotion catalogue so execution remains fully compatible with current pricing.")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background:white;")
        form = QVBoxLayout(inner)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(9)

        name_label = QLabel("Campaign name *")
        name_label.setStyleSheet("background:transparent;font-weight:600;")
        form.addWidget(name_label)
        self.name_edit = QComboBox()
        self.name_edit.setObjectName("requiredInput")
        self.name_edit.setEditable(True)
        self.name_edit.lineEdit().setPlaceholderText("e.g. Black Friday Asia")
        form.addWidget(self.name_edit)

        promo_row = QHBoxLayout()
        promo_row.setSpacing(10)
        promo_label = QLabel("Promotion from catalogue *")
        promo_label.setStyleSheet("background:transparent;font-weight:600;")
        promo_row.addWidget(promo_label)
        self.promo_combo = QComboBox()
        self.promo_combo.setObjectName("requiredInput")
        self.promo_combo.setMinimumWidth(260)
        reload_promo = QPushButton("↻")
        reload_promo.setFixedWidth(38)
        reload_promo.clicked.connect(self._reload_catalogue)
        promo_row.addWidget(self.promo_combo, 1)
        promo_row.addWidget(reload_promo)
        form.addLayout(promo_row)

        self.already_active = CleanCheckBox("Already active in current prices — do not publish, only schedule removal")
        self.already_active.setObjectName("optionCheck")
        self.already_active.toggled.connect(self._toggle_existing_active)
        form.addWidget(self.already_active)

        dates = QHBoxLayout()
        self.start_date = self._date_control("Start date")
        self.end_date = self._date_control("End date")
        dates.addLayout(self.start_date[0])
        dates.addLayout(self.end_date[0])
        form.addLayout(dates)

        plans_label = QLabel("Plans")
        plans_label.setStyleSheet("background:transparent;font-weight:600;")
        form.addWidget(plans_label)
        plan_box = QFrame()
        plan_box.setStyleSheet(f"QFrame {{ background:white; border:1px solid {BORDER}; border-radius:7px; }}")
        plan_grid = QGridLayout(plan_box)
        plan_grid.setContentsMargins(10, 8, 10, 8)
        plan_grid.setHorizontalSpacing(28)
        plan_grid.setVerticalSpacing(6)
        for i, plan in enumerate(self.catalogue.plans):
            cb = CleanCheckBox(plan)
            cb.setObjectName("planCheck")
            cb.setChecked(True)
            self.plan_checks[plan] = cb
            plan_grid.addWidget(cb, i // 2, i % 2)
        form.addWidget(plan_box)

        duration_panel = QFrame()
        duration_panel.setObjectName("durationPanel")
        duration_box = QVBoxLayout(duration_panel)
        duration_box.setContentsMargins(10, 9, 10, 10)
        duration_box.setSpacing(6)

        duration_head = QHBoxLayout()
        duration_title = QLabel("Durations")
        duration_title.setStyleSheet("font-weight:700;background:transparent;")
        duration_head.addWidget(duration_title)
        duration_head.addStretch(1)
        all_btn = QPushButton("All 1–30")
        clear_btn = QPushButton("Clear")
        all_btn.setMaximumHeight(28)
        clear_btn.setMaximumHeight(28)
        all_btn.clicked.connect(lambda: self._select_all_days(True))
        clear_btn.clicked.connect(lambda: self._select_all_days(False))
        duration_head.addWidget(all_btn)
        duration_head.addWidget(clear_btn)
        duration_box.addLayout(duration_head)

        hint = QLabel("Click individual days · Shift-click a second day to fill the whole range")
        hint.setStyleSheet(f"color:{MID};background:transparent;font-size:8pt;")
        duration_box.addWidget(hint)

        day_grid = QGridLayout()
        day_grid.setHorizontalSpacing(4)
        day_grid.setVerticalSpacing(4)
        day_grid.setContentsMargins(0, 2, 0, 2)
        days = list(self.catalogue.days)
        columns = 15
        for i, day in enumerate(days):
            b = DayButton(day)
            b.setChecked(day in {7, 10})
            b.dayClicked.connect(self._day_click)
            self.day_buttons[day] = b
            day_grid.addWidget(b, i // columns, i % columns, alignment=Qt.AlignLeft)
        for col in range(columns):
            day_grid.setColumnStretch(col, 0)
        day_grid.setColumnStretch(columns, 1)
        duration_box.addLayout(day_grid)

        self.day_summary = QLabel("")
        self.day_summary.setStyleSheet(f"color:{MAGENTA_DARK};background:transparent;font-weight:700;font-size:8pt;")
        duration_box.addWidget(self.day_summary)
        form.addWidget(duration_panel)
        self._refresh_day_summary()

        self.allow_below = CleanCheckBox("Allow prices below cost floor for this campaign")
        self.allow_below.setObjectName("optionCheck")
        form.addWidget(self.allow_below)
        notes_label = QLabel("Notes")
        notes_label.setStyleSheet("background:transparent;font-weight:600;")
        form.addWidget(notes_label)
        self.notes_edit = QTextEdit()
        self.notes_edit.setMaximumHeight(90)
        form.addWidget(self.notes_edit)
        form.addStretch(1)
        scroll.setWidget(inner)
        fl.addWidget(scroll, 1)
        splitter.addWidget(form_card)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)

        action_bar = QFrame()
        action_bar.setStyleSheet(f"background:white;border:1px solid {BORDER};border-radius:8px;")
        al = QHBoxLayout(action_bar)
        al.setContentsMargins(12, 9, 12, 9)
        info = QLabel("Saving schedules the campaign and Outlook reminders. Current prices change only when you execute a key date.")
        info.setStyleSheet(f"color:{MID};border:0;")
        al.addWidget(info, 1)
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview_unsaved)
        self.cancel_edit_btn = QPushButton("Cancel edit")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit)
        self.save_btn = QPushButton("Save campaign")
        self.save_btn.setObjectName("primary")
        self.save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {MAGENTA}; color: white; border: 1px solid {MAGENTA};
                border-radius: 7px; padding: 8px 14px; font-weight: 700;
            }}
            QPushButton:hover:enabled {{ background: {MAGENTA_DARK}; border-color: {MAGENTA_DARK}; }}
            QPushButton:disabled {{
                background: #D7D9DD; color: #666B73; border: 1px solid #C7CBD1;
            }}
        """)
        self.save_btn.clicked.connect(self._save_campaign)
        al.addWidget(preview)
        al.addWidget(self.cancel_edit_btn)
        al.addWidget(self.save_btn)
        root.addWidget(action_bar)

    def _date_control(self, label: str):
        lay = QVBoxLayout()
        lay.setSpacing(3)
        lbl = QLabel(f"{label} *")
        lbl.setStyleSheet("background:transparent;font-weight:600;")
        lay.addWidget(lbl)
        ctrl = QDateEdit(QDate.currentDate())
        ctrl.setObjectName("requiredInput")
        ctrl.setCalendarPopup(True)
        ctrl.setDisplayFormat("dd MMM yyyy")
        lay.addWidget(ctrl)
        return lay, ctrl

    def _day_click(self, day: int, shift: bool):
        # Normal click toggles a single chip. Shift-click fills the range from
        # the previous anchor to the clicked day, including both endpoints.
        if shift and self._last_day_clicked is not None:
            lo, hi = sorted((self._last_day_clicked, day))
            for d, b in self.day_buttons.items():
                if lo <= d <= hi:
                    b.setChecked(True)
        self._last_day_clicked = day
        self._refresh_day_summary()

    def _select_all_days(self, selected: bool):
        for b in self.day_buttons.values():
            b.setChecked(selected)
        self._last_day_clicked = None
        self._refresh_day_summary()

    def _refresh_day_summary(self):
        if not hasattr(self, "day_summary"):
            return
        selected = [d for d, b in self.day_buttons.items() if b.isChecked()]
        if not selected:
            self.day_summary.setText("No durations selected")
            return
        # Compress consecutive days for a quick visual confirmation.
        groups = []
        start = prev = selected[0]
        for d in selected[1:]:
            if d == prev + 1:
                prev = d
                continue
            groups.append(str(start) if start == prev else f"{start}–{prev}")
            start = prev = d
        groups.append(str(start) if start == prev else f"{start}–{prev}")
        self.day_summary.setText(f"Selected: {', '.join(groups)}  ·  {len(selected)} duration{'s' if len(selected) != 1 else ''}")

    # ---------- Campaign list ----------
    def _build_execution_tab(self):
        root = QVBoxLayout(self.execution_tab)
        root.setContentsMargins(0, 0, 0, 0)
        title_row = QHBoxLayout()
        title = QLabel("Campaigns")
        title.setStyleSheet("font-size:20pt;font-weight:700;")
        title_row.addWidget(title)
        subtitle = QLabel("Review, edit and duplicate campaign definitions. Date execution is managed from Timeline.")
        subtitle.setStyleSheet(f"color:{MID};")
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        for text, fn in (("Refresh", self._refresh_campaigns), ("Edit", self._edit_selected), ("Duplicate", self._duplicate_selected)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            title_row.addWidget(b)
        d = QPushButton("Delete")
        d.setObjectName("danger")
        d.clicked.connect(self._delete_selected)
        title_row.addWidget(d)
        root.addLayout(title_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["#", "Campaign", "Start", "End", "Promotion", "Scope", "State"])
        widths = [48, 225, 100, 100, 210, 220, 240]
        for i, w in enumerate(widths):
            self.tree.setColumnWidth(i, w)
        self.tree.setAlternatingRowColors(False)
        self.tree.itemSelectionChanged.connect(self._show_campaign_detail)
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self._edit_selected())
        root.addWidget(self.tree, 1)

        detail_card, dl = self._card("Selected campaign")
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(125)
        dl.addWidget(self.detail)
        note = QLabel("Planned campaigns can be edited freely. Published campaigns keep their commercial scope fixed, but their end date can still be shortened or prolonged until removal is executed.")
        note.setStyleSheet(f"color:{MID};")
        note.setWordWrap(True)
        dl.addWidget(note)
        root.addWidget(detail_card)

    # ---------- Timeline ----------
    def _build_timeline_tab(self):
        root = QVBoxLayout(self.timeline_tab)
        root.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        title = QLabel("Campaign timeline")
        title.setStyleSheet("font-size:20pt;font-weight:700;")
        top.addWidget(title)
        desc = QLabel("Plan, review and execute the dates where promotions start or end")
        desc.setStyleSheet(f"color:{MID};")
        top.addWidget(desc)
        top.addStretch(1)
        html = QPushButton("Open management HTML")
        html.setObjectName("primary")
        html.clicked.connect(self._open_management_calendar)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_campaigns)
        top.addWidget(html)
        top.addWidget(refresh)
        root.addLayout(top)

        # The execution date is intentionally hidden: users navigate only among real key dates.
        self.execution_date = QDateEdit(QDate.currentDate(), self.timeline_tab)
        self.execution_date.hide()
        self.execution_date.dateChanged.connect(self._on_execution_date_changed)

        # Main Timeline composition: promotion plan takes most of the width,
        # while the selected-date execution panel stays fixed on the right.
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        key_card, kl = self._card("Key dates", "Only dates where at least one campaign starts or ends are shown. Select a date to review that day's planned changes.")
        key_nav = QHBoxLayout()
        self.prev_key_btn = QPushButton("‹ Previous")
        self.prev_key_btn.clicked.connect(self._previous_key_date)
        self.key_date_row = QHBoxLayout()
        self.key_date_row.setSpacing(8)
        self.next_key_btn = QPushButton("Next ›")
        self.next_key_btn.clicked.connect(self._next_key_date)
        key_nav.addWidget(self.prev_key_btn)
        key_nav.addLayout(self.key_date_row, 1)
        key_nav.addWidget(self.next_key_btn)
        kl.addLayout(key_nav)
        left_layout.addWidget(key_card)

        card, cl = self._card("Promotion plan", "Campaign bars are placed against their real start/end dates. Scroll horizontally to move through time; the magenta line marks today.")
        self.timeline_scene = QGraphicsScene(self)
        self.timeline_view = QGraphicsView(self.timeline_scene)
        self.timeline_view.setRenderHints(self.timeline_view.renderHints())
        self.timeline_view.setMinimumHeight(430)
        self.timeline_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.timeline_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.timeline_view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        cl.addWidget(self.timeline_view, 1)
        left_layout.addWidget(card, 1)
        split.addWidget(left)

        exec_card, el = self._card("Selected key date", "END actions are applied before START actions when both happen on the same date.")
        exec_card.setMinimumWidth(285)
        exec_card.setMaximumWidth(430)
        self.execution_date_title = QLabel("No key date selected")
        self.execution_date_title.setStyleSheet("font-size:14pt;font-weight:750;")
        el.addWidget(self.execution_date_title)

        summary_scroll = QScrollArea()
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setFrameShape(QFrame.NoFrame)
        summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        summary_inner = QWidget()
        summary_inner.setStyleSheet("background:white;")
        summary_layout = QVBoxLayout(summary_inner)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        self.execution_summary = QLabel("")
        self.execution_summary.setWordWrap(True)
        self.execution_summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.execution_summary.setStyleSheet(f"color:{MID};background:white;")
        summary_layout.addWidget(self.execution_summary)
        summary_layout.addStretch(1)
        summary_scroll.setWidget(summary_inner)
        el.addWidget(summary_scroll, 1)

        self.include_next_day_starts = CleanCheckBox("Include next-day starts in this batch")
        self.include_next_day_starts.setToolTip(
            "Keep the campaigns' real start date, but publish starts scheduled for the following day "
            "together with this date's changes so one partner price pack can cover the transition."
        )
        self.include_next_day_starts.toggled.connect(lambda _checked: self._refresh_execution_date())
        self.include_next_day_starts.setVisible(False)
        el.addWidget(self.include_next_day_starts)

        self.preview_date_btn = QPushButton("Preview changes")
        self.preview_date_btn.clicked.connect(self._preview_execution_date)
        self.apply_date_btn = QPushButton("APPLY CHANGES")
        self.apply_date_btn.setObjectName("primary")
        self.apply_date_btn.clicked.connect(self._apply_execution_date)
        self.undo_btn = QPushButton("Undo changes")
        self.undo_btn.setObjectName("danger")
        self.undo_btn.clicked.connect(self._undo_execution_date)
        el.addWidget(self.preview_date_btn)
        el.addWidget(self.apply_date_btn)
        el.addWidget(self.undo_btn)
        split.addWidget(exec_card)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([930, 310])

    def _render_timeline(self):
        self.timeline_scene.clear()
        campaigns = self.store.list_campaigns()
        if not campaigns:
            self.timeline_scene.addText("No campaigns yet.")
            return

        today = date.today()
        parsed = []
        for c in campaigns:
            imported = c.status == "ACTIVE_IMPORTED"
            d1 = self._parse_iso(c.start_date) if c.start_date else today
            d2 = self._parse_iso(c.end_date) if c.end_date else (today + timedelta(days=90))
            if imported and d1 > today:
                d1 = today
            parsed.append((c, d1, d2))
        first = min([s for _, s, _ in parsed] + [today]) - timedelta(days=3)
        last = max([e for _, _, e in parsed] + [today]) + timedelta(days=3)
        day_w = 18
        left = 220
        top = 54
        row_h = 36
        total_days = (last - first).days + 1
        width = left + total_days * day_w + 40
        height = top + len(parsed) * row_h + 45
        self.timeline_scene.setSceneRect(0, 0, width, height)

        d = first
        for i in range(total_days):
            x = left + i * day_w
            if d.day == 1 or d.weekday() == 0:
                self.timeline_scene.addLine(x, 28, x, height, QPen(QColor("#ECEEF2")))
                txt = d.strftime("%b %Y") if d.day == 1 else d.strftime("%d %b")
                ti = self.timeline_scene.addText(txt, QFont("Segoe UI", 7))
                ti.setDefaultTextColor(QColor(MID))
                ti.setPos(x + 2, 8)
            d += timedelta(days=1)

        for kd in self._key_dates():
            dd = self._parse_iso(kd)
            if first <= dd <= last:
                x = left + (dd - first).days * day_w
                self.timeline_scene.addLine(x, 28, x, height, QPen(QColor("#F2B7D4"), 1, Qt.DashLine))
                self.timeline_scene.addEllipse(x - 4, 27, 8, 8, QPen(Qt.NoPen), QBrush(QColor(MAGENTA)))

        tx = left + (today - first).days * day_w
        self.timeline_scene.addLine(tx, 25, tx, height, QPen(QColor(MAGENTA), 2))
        tt = self.timeline_scene.addText("TODAY", QFont("Segoe UI", 7, QFont.Bold))
        tt.setDefaultTextColor(QColor(MAGENTA))
        tt.setPos(tx + 4, 28)

        for r, (c, d1, d2) in enumerate(parsed):
            y = top + r * row_h
            label = self.timeline_scene.addText(c.name[:34], QFont("Segoe UI", 8, QFont.Bold))
            label.setDefaultTextColor(QColor(DARK))
            label.setPos(8, y + 4)
            x1 = left + (d1 - first).days * day_w
            x2 = left + ((d2 - first).days + 1) * day_w
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            imported = c.status == "ACTIVE_IMPORTED"
            if ended:
                color = "#A4A9B1"
            elif imported and c.end_date and d2 < today:
                color = "#D97706"
            elif imported:
                color = GREEN
            elif started and d2 < today:
                color = "#D97706"
            elif started:
                color = GREEN
            elif d2 < today:
                color = "#8C6F9E"
            elif d1 <= today:
                color = MAGENTA
            else:
                color = "#6B8FDC"
            self.timeline_scene.addRect(x1, y + 6, max(18, x2 - x1), 22, QPen(Qt.NoPen), QBrush(QColor(color)))
            txt = self.timeline_scene.addText((c.promo_name or c.promo_code)[:24], QFont("Segoe UI", 7, QFont.Bold))
            txt.setDefaultTextColor(QColor(WHITE))
            txt.setPos(x1 + 4, y + 7)

    # ---------- Data / actions ----------
    def _reload_catalogue(self):
        try:
            old = self._selected_promo().code if self.promo_combo.count() else ""
        except Exception:
            old = ""
        try:
            self.catalogue = read_catalogue(self.workbook)
            self.dest_map = {code: name for code, name in self.catalogue.destinations}
            self._load_promo_combo(old)
            self._refresh_destinations()
        except Exception as exc:
            self._error("Reload catalogue", str(exc))

    def _load_promo_combo(self, prefer_code: str = ""):
        self._promo_by_display = {p.display: p for p in self.catalogue.promos}
        self.promo_combo.blockSignals(True)
        self.promo_combo.clear()
        self.promo_combo.addItems(list(self._promo_by_display))
        if prefer_code:
            for i, display in enumerate(self._promo_by_display):
                if self._promo_by_display[display].code == prefer_code:
                    self.promo_combo.setCurrentIndex(i)
                    break
        self.promo_combo.blockSignals(False)

    def _selected_promo(self) -> PromoOption:
        promo = self._promo_by_display.get(self.promo_combo.currentText())
        if promo is None:
            raise ValueError("Select a promotion from the catalogue.")
        return promo

    @staticmethod
    def _is_country_or_mini_region(code: str) -> bool:
        """Return True for ISO2 countries and compact mini-region pricing units."""
        clean = str(code or "").strip().upper()
        if len(clean) == 2 and clean.isalpha():
            return True
        parts = clean.split("-")
        return len(parts) >= 2 and all(len(part) == 2 and part.isalpha() for part in parts)

    def _refresh_destinations(self, *_args):
        query = self.search_edit.currentText().strip().lower() if hasattr(self, "search_edit") else ""
        if not hasattr(self, "available_list"):
            return

        self.available_list.clear()
        countries_and_mini = []
        regions_and_global = []

        for code, name in self.catalogue.destinations:
            display = f"{name} ({code})"
            if query and query not in display.lower():
                continue
            target = countries_and_mini if self._is_country_or_mini_region(code) else regions_and_global
            target.append((name, code, display))

        countries_and_mini.sort(key=lambda x: (x[0].lower(), x[1]))
        regions_and_global.sort(key=lambda x: (x[0].lower(), x[1]))

        def add_destination(code: str, display: str):
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, code)
            self.available_list.addItem(item)

        for _name, code, display in countries_and_mini:
            add_destination(code, display)

        if countries_and_mini and regions_and_global:
            divider = QListWidgetItem("────────────────────────────────")
            divider.setFlags(Qt.NoItemFlags)
            divider.setForeground(QColor("#b8b8b8"))
            divider.setTextAlignment(Qt.AlignCenter)
            divider.setSizeHint(divider.sizeHint())
            self.available_list.addItem(divider)

        for _name, code, display in regions_and_global:
            add_destination(code, display)

    def _add_destination_codes(self, codes: list[str]):
        changed = False
        for code in codes:
            if code not in self.selected_destinations and code in self.dest_map:
                self.selected_destinations.append(code)
                changed = True
        if changed:
            self._render_selected()

    def _pick_destination(self, item: QListWidgetItem):
        code = item.data(Qt.UserRole)
        if code:
            self._add_destination_codes([str(code)])

    def _add_selected(self):
        # Kept for compatibility with older signal wiring / saved UI state.
        codes = [str(i.data(Qt.UserRole)) for i in self.available_list.selectedItems() if i.data(Qt.UserRole)]
        self._add_destination_codes(codes)

    def _render_selected(self):
        self.selected_list.clear()
        for code in self.selected_destinations:
            item = QListWidgetItem(f"{self.dest_map.get(code, code)} ({code})")
            item.setData(Qt.UserRole, code)
            self.selected_list.addItem(item)
        if hasattr(self, "selected_count_label"):
            n = len(self.selected_destinations)
            self.selected_count_label.setText(f"{n} selected")

    def _remove_selected(self):
        remove = {str(i.data(Qt.UserRole)) for i in self.selected_list.selectedItems()}
        self.selected_destinations = [c for c in self.selected_destinations if c not in remove]
        self._render_selected()

    def _refresh_groups(self):
        if not hasattr(self, "group_combo"):
            return
        current = self.group_combo.currentText()
        groups = self.store.list_groups()
        self.group_combo.clear()
        self.group_combo.addItems(sorted(groups))
        idx = self.group_combo.findText(current)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)

    def _save_group(self):
        if not self.selected_destinations:
            return self._info("Save group", "Select at least one destination first.")
        name, ok = QInputDialog.getText(self, "Save destination group", "Group name:")
        if ok and name.strip():
            self.store.save_group(name.strip(), self.selected_destinations)
            self._refresh_groups()

    def _load_group(self):
        name = self.group_combo.currentText().strip()
        if not name:
            return
        groups = self.store.list_groups()
        self.selected_destinations = [c for c in groups.get(name, []) if c in self.dest_map]
        self._render_selected()

    def _toggle_existing_active(self, checked: bool):
        # Existing live campaigns are registered without a START action, but still
        # always require an end date so they can be terminated through a key date.
        self.start_date[1].setEnabled(not checked)
        self.end_date[1].setEnabled(True)
        self.save_btn.setText("Register active campaign" if checked else ("Save changes" if self.editing_campaign_id is not None else "Save campaign"))

    def _validate_form(self):
        name = self.name_edit.currentText().strip()
        if not name:
            raise ValueError("Campaign name is required.")
        promo = self._selected_promo()
        existing_active = self.already_active.isChecked()
        start = "" if existing_active else self._qdate_iso(self.start_date[1].date())
        end = self._qdate_iso(self.end_date[1].date())
        if not end:
            raise ValueError("End date is required for every campaign.")
        if start and end < start:
            raise ValueError("End date must be on or after start date.")
        if not self.selected_destinations:
            raise ValueError("Select at least one destination.")
        plans = [p for p, cb in self.plan_checks.items() if cb.isChecked()]
        days = [d for d, b in self.day_buttons.items() if b.isChecked()]
        if not plans:
            raise ValueError("Select at least one plan.")
        if not days:
            raise ValueError("Select at least one duration.")
        return name, promo, start, end, plans, days, existing_active

    def _campaign_from_form(self) -> Campaign:
        name, promo, start, end, plans, days, existing_active = self._validate_form()
        return Campaign(
            id=None,
            name=name,
            start_date=start,
            end_date=end,
            promo_type=promo.promo_type,
            promo_value=promo.value,
            promo_code=promo.code,
            promo_name=promo.label,
            destinations=list(self.selected_destinations),
            plans=plans,
            days=days,
            allow_below_cost=self.allow_below.isChecked(),
            notes=self.notes_edit.toPlainText().strip(),
            status="ACTIVE_IMPORTED" if existing_active else "PLANNED",
        )

    def _save_campaign(self):
        try:
            if self.editing_campaign_id is None:
                c = self._campaign_from_form()
                cid = self.store.save_campaign(c)
                if c.status == "ACTIVE_IMPORTED":
                    msg = f"Campaign saved as #{cid} as already active.\nCurrent prices were NOT changed; only its future removal is scheduled."
                else:
                    msg = f"Campaign saved as #{cid}.\nNothing has been changed in current prices yet."
            else:
                old = self.store.get_campaign(self.editing_campaign_id)
                if self.store.has_execution(old.id, "END"):
                    raise ValueError("This campaign has already been removed from current prices and is read-only.")
                c = self._campaign_from_form()
                c.id = old.id
                c.created_at = old.created_at
                if self.store.has_execution(old.id, "START") and old.status != "ACTIVE_IMPORTED":
                    # Once a normal campaign has been published, its commercial scope is fixed.
                    # Only the end date (and notes) may be adjusted before removal executes.
                    changed = []
                    checks = [
                        (c.name, old.name, "campaign name"),
                        (c.start_date, old.start_date, "start date"),
                        (c.promo_code, old.promo_code, "promotion"),
                        (c.destinations, old.destinations, "destinations"),
                        (c.plans, old.plans, "plans"),
                        (c.days, old.days, "durations"),
                        (c.allow_below_cost, old.allow_below_cost, "cost-floor override"),
                    ]
                    for new_value, old_value, label in checks:
                        if new_value != old_value:
                            changed.append(label)
                    if changed:
                        raise ValueError(
                            "This campaign is already published. Only its end date (and notes) can be changed. "
                            "Changed: " + ", ".join(changed)
                        )
                self.store.update_campaign(c)
                cid = int(c.id)
                msg = f"Campaign #{cid} updated."
                self._cancel_edit(clear_form=False)
        except Exception as exc:
            return self._error("Cannot save campaign", str(exc))
        outlook_note = self._sync_outlook(cid)
        self._refresh_campaigns()
        self._refresh_execution_date()
        self.tabs.setCurrentWidget(self.execution_tab)
        if outlook_note:
            msg += "\n\n" + outlook_note
        self._info("Promo Planner", msg)

    def _sync_outlook(self, campaign_id: int) -> str:
        c = self.store.get_campaign(campaign_id)
        try:
            start_id, end_id = sync_campaign_events(c)
            self.store.set_outlook_ids(campaign_id, start_id, end_id)
            if c.status == "ACTIVE_IMPORTED":
                return "Outlook END reminder synced for the day before removal."
            return "Outlook reminders synced for the day before START and END."
        except Exception as exc:
            return f"Campaign saved, but Outlook reminders could not be synced: {exc}"

    def _preview_unsaved(self):
        try:
            c = self._campaign_from_form()
            if c.status == "ACTIVE_IMPORTED":
                p = preview_campaign(self.workbook, c, "END")
            else:
                p = preview_campaign(self.workbook, c, "START")
        except Exception as exc:
            return self._error("Preview", str(exc))
        self._show_preview(p)

    def _refresh_campaigns(self):
        if not hasattr(self, "tree"):
            return
        self.tree.clear()
        today = date.today().isoformat()
        for c in self.store.list_campaigns():
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            imported = c.status == "ACTIVE_IMPORTED"
            if ended:
                state, bg, fg = "Expired · removed from current prices", GREY_SOFT, MID
            elif imported and c.end_date < today:
                state, bg, fg = "PUBLISHED · imported manually · removal overdue", AMBER_SOFT, AMBER
            elif imported:
                state, bg, fg = "PUBLISHED · imported manually", GREEN_SOFT, GREEN
            elif started and c.end_date < today:
                state, bg, fg = "PUBLISHED · removal overdue", AMBER_SOFT, AMBER
            elif started:
                state, bg, fg = "PUBLISHED to current prices", GREEN_SOFT, GREEN
            elif c.end_date < today:
                state, bg, fg = "Expired · never published", GREY_SOFT, MID
            elif c.start_date <= today:
                state, bg, fg = "START due · not yet published", BLUE_SOFT, BLUE
            else:
                state, bg, fg = "Planned · not yet published", BLUE_SOFT, BLUE
            scope = f"{len(c.destinations)} dest · {len(c.plans)} plans · {len(c.days)} durations"
            promo = c.promo_name or c.promo_code
            start_display = c.start_date or "Already live"
            end_display = c.end_date
            item = QTreeWidgetItem([str(c.id), c.name, start_display, end_display, promo, scope, state])
            item.setData(0, Qt.UserRole, int(c.id))
            for col in range(7):
                item.setBackground(col, QBrush(QColor(bg)))
                item.setForeground(col, QBrush(QColor(fg)))
            self.tree.addTopLevelItem(item)
        self._refresh_groups()
        self._render_timeline()
        self._refresh_execution_date()

    def _selected_campaign(self) -> Campaign:
        items = self.tree.selectedItems()
        if not items:
            raise ValueError("Select a campaign first.")
        cid = int(items[0].data(0, Qt.UserRole))
        return self.store.get_campaign(cid)

    def _show_campaign_detail(self):
        try:
            c = self._selected_campaign()
        except Exception:
            self.detail.clear()
            return
        started = self.store.has_execution(c.id, "START")
        ended = self.store.has_execution(c.id, "END")
        imported = c.status == "ACTIVE_IMPORTED"
        state = "Expired / removed" if ended else ("Published · imported manually" if imported else ("Published to current prices" if started else "Planned / not yet published"))
        start_display = c.start_date or "Already live / start unknown"
        end_display = c.end_date
        self.detail.setPlainText(
            f"{c.name}\n"
            f"Promotion: {c.promo_name or '-'} | catalogue code: {c.promo_code}\n"
            f"{start_display} → {end_display} | {state}\n"
            f"Destinations: {', '.join(c.destinations)}\n"
            f"Plans: {', '.join(c.plans)} | Days: {', '.join(map(str, c.days))}\n"
            f"Notes: {c.notes or '-'}"
        )

    def _edit_selected(self):
        try:
            c = self._selected_campaign()
        except Exception as exc:
            return self._warn("Edit campaign", str(exc))
        if self.store.has_execution(c.id, "END"):
            return self._info("Campaign read-only", "This campaign has already been removed from current prices and is kept as audit history.")
        self.editing_campaign_id = c.id
        self._load_campaign_into_form(c)
        published_normal = self.store.has_execution(c.id, "START") and c.status != "ACTIVE_IMPORTED"
        self._set_end_only_edit_mode(published_normal)
        self.save_btn.setText("Save end date" if published_normal else "Save changes")
        self.tabs.setCurrentWidget(self.create_tab)

    def _set_end_only_edit_mode(self, enabled: bool):
        """For a published normal campaign, keep scope immutable but allow end-date changes."""
        self.name_edit.setEnabled(not enabled)
        self.promo_combo.setEnabled(not enabled)
        self.already_active.setEnabled(not enabled)
        self.start_date[1].setEnabled(not enabled and not self.already_active.isChecked())
        self.end_date[1].setEnabled(True)
        for cb in self.plan_checks.values():
            cb.setEnabled(not enabled)
        for btn in self.day_buttons.values():
            btn.setEnabled(not enabled)
        self.allow_below.setEnabled(not enabled)
        if hasattr(self, "available_list"):
            self.available_list.setEnabled(not enabled)
        if hasattr(self, "selected_list"):
            self.selected_list.setEnabled(not enabled)
        if hasattr(self, "search_edit"):
            self.search_edit.setEnabled(not enabled)
        if hasattr(self, "group_combo"):
            self.group_combo.setEnabled(not enabled)

    def _load_campaign_into_form(self, c: Campaign):
        self.name_edit.setEditText(c.name)
        imported = c.status == "ACTIVE_IMPORTED"
        self.already_active.setChecked(imported)
        if c.start_date:
            self.start_date[1].setDate(QDate.fromString(c.start_date, "yyyy-MM-dd"))
        else:
            self.start_date[1].setDate(QDate.currentDate())
        if c.end_date:
            self.end_date[1].setDate(QDate.fromString(c.end_date, "yyyy-MM-dd"))
        else:
            self.end_date[1].setDate(QDate.currentDate())
        self.allow_below.setChecked(c.allow_below_cost)
        self.selected_destinations = list(c.destinations)
        self._render_selected()
        for plan, cb in self.plan_checks.items():
            cb.setChecked(plan in c.plans)
        for day, b in self.day_buttons.items():
            b.setChecked(day in c.days)
        self._last_day_clicked = None
        self._refresh_day_summary()
        self.notes_edit.setPlainText(c.notes)
        for i, display in enumerate(self._promo_by_display):
            promo = self._promo_by_display[display]
            if promo.code == c.promo_code or ((promo.label == c.promo_name and c.promo_name) or (promo.promo_type == c.promo_type and abs(promo.value - c.promo_value) < 1e-9)):
                self.promo_combo.setCurrentIndex(i)
                break

    def _cancel_edit(self, clear_form: bool = True):
        self.editing_campaign_id = None
        self._set_end_only_edit_mode(False)
        self.save_btn.setText("Save campaign")
        if not clear_form:
            return
        self.name_edit.setEditText("")
        self.already_active.setChecked(False)
        self.start_date[1].setDate(QDate.currentDate())
        self.end_date[1].setDate(QDate.currentDate())
        self.selected_destinations = []
        self._render_selected()
        self.notes_edit.clear()
        self.allow_below.setChecked(False)
        if self.promo_combo.count():
            self.promo_combo.setCurrentIndex(0)
        for cb in self.plan_checks.values():
            cb.setChecked(True)
        for day, b in self.day_buttons.items():
            b.setChecked(day in {7, 10})
        self._last_day_clicked = None
        self._refresh_day_summary()

    def _duplicate_selected(self):
        try:
            c = self._selected_campaign()
            cid = self.store.duplicate_campaign(c.id, c.promo_code)
        except Exception as exc:
            return self._error("Duplicate campaign", str(exc))
        note = self._sync_outlook(cid)
        self._refresh_campaigns()
        self._info("Promo Planner", f"Created editable copy as campaign #{cid}." + (f"\n\n{note}" if note else ""))

    def _delete_selected(self):
        try:
            c = self._selected_campaign()
        except Exception as exc:
            return self._warn("Delete campaign", str(exc))
        if self.store.has_execution(c.id, "START") or self.store.has_execution(c.id, "END"):
            return self._info("Campaign read-only", "Executed campaigns are kept for audit. Undo the relevant date execution first if it was applied by mistake.")
        if not self._confirm("Delete campaign", f"Delete campaign '{c.name}'?\n\nIts Outlook reminders will also be deleted."):
            return
        outlook_warning = ""
        try:
            try:
                delete_campaign_events(c)
            except Exception as exc:
                outlook_warning = f"Outlook reminder cleanup failed: {exc}"
            self.store.delete_campaign(c.id)
        except Exception as exc:
            return self._error("Delete campaign", str(exc))
        self._refresh_campaigns()
        self.detail.clear()
        if outlook_warning:
            self._warn("Outlook", outlook_warning)

    def _save_manual_price_history_snapshot(self) -> Path:
        """Save the current workbook into the Pricing Editor history after a planner write."""
        workbook = Path(self.workbook)
        # Canonical layout is outputs/manual_prices/current/manual_prices_current.xlsx.
        # Keep a safe fallback for repositories where the workbook sits one level higher.
        if workbook.parent.name.lower() == "current":
            history_dir = workbook.parent.parent / "history"
        else:
            history_dir = workbook.parent / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot = history_dir / f"manual_prices_{ts}{workbook.suffix}"
        counter = 1
        while snapshot.exists():
            snapshot = history_dir / f"manual_prices_{ts}_{counter}{workbook.suffix}"
            counter += 1
        shutil.copy2(workbook, snapshot)
        return snapshot

    # ---------- Key dates / batch execution ----------
    def _key_dates(self) -> list[str]:
        dates = set()
        for c in self.store.list_campaigns():
            if c.start_date:
                dates.add(c.start_date)
            if c.end_date:
                dates.add(c.end_date)
        return sorted(dates)

    def _key_date_counts(self, key_date: str) -> tuple[int, int]:
        campaigns = self.store.list_campaigns()
        return (
            sum(1 for c in campaigns if c.start_date == key_date),
            sum(1 for c in campaigns if c.end_date == key_date),
        )

    def _jump_to_execution_date(self, key_date: str, switch_tab: bool = False):
        qd = QDate.fromString(key_date, "yyyy-MM-dd")
        if qd.isValid():
            self.execution_date.setDate(qd)
            self._refresh_execution_date()
        if switch_tab:
            self.tabs.setCurrentWidget(self.timeline_tab)

    def _previous_key_date(self):
        dates = self._key_dates()
        if not dates:
            return
        current = self._qdate_iso(self.execution_date.date())
        prev = [d for d in dates if d < current]
        self._jump_to_execution_date(prev[-1] if prev else dates[0])

    def _next_key_date(self):
        dates = self._key_dates()
        if not dates:
            return
        current = self._qdate_iso(self.execution_date.date())
        following = [d for d in dates if d > current]
        self._jump_to_execution_date(following[0] if following else dates[-1])

    def _render_key_date_strip(self):
        self._clear_layout(self.key_date_row)
        dates = self._key_dates()
        if not dates:
            msg = QLabel("No key dates yet")
            msg.setStyleSheet(f"color:{MID};")
            self.key_date_row.addStretch(1)
            self.key_date_row.addWidget(msg)
            self.key_date_row.addStretch(1)
            self.prev_key_btn.setEnabled(False)
            self.next_key_btn.setEnabled(False)
            return

        selected = self._qdate_iso(self.execution_date.date())
        if selected not in dates:
            today_iso = date.today().isoformat()
            future = [d for d in dates if d >= today_iso]
            selected = future[0] if future else dates[-1]
            self.execution_date.blockSignals(True)
            self.execution_date.setDate(QDate.fromString(selected, "yyyy-MM-dd"))
            self.execution_date.blockSignals(False)

        idx = dates.index(selected)
        start = max(0, min(idx - 2, max(0, len(dates) - 6)))
        visible = dates[start:start + 6]
        self.prev_key_btn.setEnabled(idx > 0)
        self.next_key_btn.setEnabled(idx < len(dates) - 1)

        self.key_date_row.addStretch(1)
        for pos, kd in enumerate(visible):
            if pos:
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setFixedWidth(26)
                line.setStyleSheet(f"border:0;border-top:1px solid {BORDER};background:transparent;")
                self.key_date_row.addWidget(line, 0, Qt.AlignVCenter)

            starts, ends = self._key_date_counts(kd)
            dd = self._parse_iso(kd)
            parts = [dd.strftime("%d"), dd.strftime("%b").upper()]
            counts = []
            if starts:
                counts.append(f"+{starts}")
            if ends:
                counts.append(f"−{ends}")
            if counts:
                parts.append(" / ".join(counts))
            b = QPushButton("  ".join(parts))
            b.setFixedSize(92, 42)
            b.setCursor(Qt.PointingHandCursor)
            if kd == selected:
                b.setStyleSheet(
                    f"QPushButton {{ background:{MAGENTA}; color:white; border:1px solid {MAGENTA}; "
                    "border-radius:9px; padding:3px 6px; font-size:7.5pt; font-weight:700; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background:white; color:{DARK}; border:1px solid #B9BEC7; "
                    "border-radius:9px; padding:3px 6px; font-size:7.5pt; font-weight:600; }} "
                    f"QPushButton:hover {{ border-color:{MAGENTA}; color:{MAGENTA_DARK}; }}"
                )
            b.setToolTip(f"{dd.strftime('%d %B %Y')} · {starts} starting · {ends} ending")
            b.clicked.connect(lambda _checked=False, d=kd: self._jump_to_execution_date(d))
            self.key_date_row.addWidget(b)
        self.key_date_row.addStretch(1)

    def _on_execution_date_changed(self, _date: QDate):
        # A next-day inclusion is an operational choice for one selected key date only.
        # Never carry it silently when the user navigates to another date.
        if hasattr(self, "include_next_day_starts"):
            self.include_next_day_starts.blockSignals(True)
            self.include_next_day_starts.setChecked(True)
            self.include_next_day_starts.blockSignals(False)
        self._refresh_execution_date()

    def _next_day_start_actions(self, execution_date: str):
        next_day = (self._parse_iso(execution_date) + timedelta(days=1)).isoformat()
        actions = []
        for c in self.store.list_campaigns():
            imported = c.status == "ACTIVE_IMPORTED"
            started = self.store.has_execution(c.id, "START")
            if (not imported) and c.start_date == next_day and not started:
                actions.append((c, "START"))
        return actions

    def _actions_for_execution_date(self, execution_date: str, include_next_day_starts: bool = False):
        actions = []
        for c in self.store.list_campaigns():
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            imported = c.status == "ACTIVE_IMPORTED"
            if c.end_date and c.end_date == execution_date and (started or imported) and not ended:
                actions.append((c, "END"))
            if (not imported) and c.start_date and c.start_date == execution_date and not started:
                actions.append((c, "START"))
        if include_next_day_starts:
            actions.extend(self._next_day_start_actions(execution_date))
        return actions

    def _refresh_execution_date(self):
        if not hasattr(self, "execution_date"):
            return
        self._render_key_date_strip()
        d = self._qdate_iso(self.execution_date.date())
        if hasattr(self, "execution_date_title"):
            try:
                pretty = self._parse_iso(d).strftime("%d %B %Y")
            except Exception:
                pretty = d
            self.execution_date_title.setText(pretty)

        if self._last_execution_key_date != d:
            self._last_execution_key_date = d
            if hasattr(self, "include_next_day_starts"):
                self.include_next_day_starts.blockSignals(True)
                self.include_next_day_starts.setChecked(True)
                self.include_next_day_starts.blockSignals(False)

        today_actions = self._actions_for_execution_date(d, include_next_day_starts=False)
        next_day_actions = self._next_day_start_actions(d)
        include_next = bool(
            hasattr(self, "include_next_day_starts")
            and self.include_next_day_starts.isChecked()
            and next_day_actions
        )
        actions = self._actions_for_execution_date(d, include_next_day_starts=include_next)

        next_day = self._parse_iso(d) + timedelta(days=1)
        if hasattr(self, "include_next_day_starts"):
            self.include_next_day_starts.setVisible(bool(next_day_actions))
            self.include_next_day_starts.setEnabled(bool(next_day_actions) and not bool(self.store.get_active_batch_for_date(d)))
            self.include_next_day_starts.setText(
                f"Include {len(next_day_actions)} start{'s' if len(next_day_actions) != 1 else ''} scheduled for "
                f"{next_day.strftime('%d %b')} in this batch"
            )

        ending = [c.name for c, a in today_actions if a == "END"]
        starting_today = [c.name for c, a in today_actions if a == "START"]
        starting_next = [c.name for c, _a in next_day_actions] if include_next else []
        batch = self.store.get_active_batch_for_date(d)
        parts = []
        if ending:
            parts.append("ENDING TODAY\n• " + "\n• ".join(ending))
        if starting_today:
            parts.append("STARTING TODAY\n• " + "\n• ".join(starting_today))
        if starting_next:
            parts.append(
                f"STARTING {next_day.strftime('%d %b').upper()} · INCLUDED IN THIS BATCH\n• "
                + "\n• ".join(starting_next)
            )
        elif next_day_actions:
            parts.append(
                f"OPTIONAL NEXT-DAY STARTS · {next_day.strftime('%d %b').upper()}\n"
                f"{len(next_day_actions)} campaign{'s' if len(next_day_actions) != 1 else ''} can be included using the option below."
            )
        if not today_actions and not next_day_actions:
            parts.append("No unapplied campaign changes are scheduled for this key date or the following day's start.")
        elif not today_actions and next_day_actions and not include_next:
            parts.insert(0, "No unapplied changes are scheduled for this date itself.")
        if batch:
            parts.append(f"EXECUTED\nBatch #{batch['id']} · {batch['executed_at']}")
        self.execution_summary.setText("\n\n".join(parts))

        has_actions = bool(actions)
        self.preview_date_btn.setEnabled(has_actions)
        self.apply_date_btn.setEnabled(has_actions and not bool(batch))
        self.apply_date_btn.setText(f"APPLY CHANGES FOR {self._parse_iso(d).strftime('%d %b').upper()}")
        self.undo_btn.setVisible(bool(batch))
        self.undo_btn.setEnabled(bool(batch))
        self.undo_btn.setText(f"Undo changes for {self._parse_iso(d).strftime('%d %b')}")

    def _earliest_outstanding_key_date(self) -> str | None:
        for kd in self._key_dates():
            if self._actions_for_execution_date(kd, include_next_day_starts=False):
                return kd
        return None

    def _confirm_sequence_override(self, selected_date: str) -> bool:
        earliest = self._earliest_outstanding_key_date()
        if not earliest or earliest >= selected_date:
            return True

        msg = QMessageBox(self)
        msg.setWindowTitle("Earlier key date still pending")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(f"Changes planned for {earliest} have not yet been executed.")
        msg.setInformativeText(
            f"Executing {selected_date} first can make current prices diverge from the planned sequence.\n\n"
            "Next-day START actions included from the selected date are allowed and are not treated as skipped dates."
        )
        go_btn = msg.addButton(f"Go to {earliest}", QMessageBox.AcceptRole)
        execute_btn = msg.addButton("Execute anyway", QMessageBox.DestructiveRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is go_btn:
            self._jump_to_execution_date(earliest)
            return False
        return clicked is execute_btn

    @staticmethod
    def _fmt_price(value) -> str:
        return "—" if value is None else f"{float(value):.2f}"

    def _batch_preview_dialog(self, preview, *, approval: bool = False) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Approve pricing changes" if approval else "Pricing change preview")
        dlg.resize(1380, 680)
        layout = QVBoxLayout(dlg)

        title = QLabel(
            ("Review every affected pricing row before writing to current prices." if approval else
             "Preview of every affected pricing row. Nothing will be written.")
        )
        title.setStyleSheet("font-size:11pt;font-weight:700;")
        layout.addWidget(title)
        sub = QLabel(
            f"Execution date: {preview.execution_date}   ·   Rows affected: {len(preview.price_changes)}   ·   "
            f"Campaign actions: {len(preview.items)}"
        )
        sub.setStyleSheet(f"color:{MID};")
        layout.addWidget(sub)

        columns = [
            "Destination", "Plan", "Days", "Change",
            "Promo before", "Promo after",
            "List EUR before", "List EUR after", "Net EUR before", "Net EUR after",
            "List USD before", "List USD after", "Net USD before", "Net USD after",
        ]
        table = QTableWidget(len(preview.price_changes), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        for r, ch in enumerate(preview.price_changes):
            vals = [
                f"{ch.country} ({ch.iso})" if ch.country and ch.country != ch.iso else ch.iso,
                ch.plan, str(ch.days), ch.change,
                ch.promo_before, ch.promo_after,
                self._fmt_price(ch.list_eur_before), self._fmt_price(ch.list_eur_after),
                self._fmt_price(ch.net_eur_before), self._fmt_price(ch.net_eur_after),
                self._fmt_price(ch.list_usd_before), self._fmt_price(ch.list_usd_after),
                self._fmt_price(ch.net_usd_before), self._fmt_price(ch.net_usd_after),
            ]
            for col, value in enumerate(vals):
                item = QTableWidgetItem(value)
                if col in (5, 7, 9, 11, 13):
                    item.setBackground(QBrush(QColor(MAGENTA_SOFT)))
                table.setItem(r, col, item)
        table.resizeColumnsToContents()
        table.setColumnWidth(0, min(max(table.columnWidth(0), 150), 230))
        table.setColumnWidth(3, min(max(table.columnWidth(3), 190), 320))
        layout.addWidget(table, 1)

        if preview.cost_floor_issues:
            warn = QLabel(
                f"Cost-floor control: {len(preview.cost_floor_issues)} currency price(s) were detected below floor in the initial simulation. "
                "Any approved list-price adjustment is already reflected in the AFTER columns above."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet(f"background:{AMBER_SOFT};color:{AMBER};border:1px solid #E7C46B;border-radius:6px;padding:7px;")
            layout.addWidget(warn)

        buttons = QDialogButtonBox()
        if approval:
            approve_btn = buttons.addButton("Approve & execute", QDialogButtonBox.AcceptRole)
            approve_btn.setObjectName("primary")
            cancel_btn = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
            approve_btn.clicked.connect(dlg.accept)
            cancel_btn.clicked.connect(dlg.reject)
        else:
            close_btn = buttons.addButton("Close", QDialogButtonBox.RejectRole)
            close_btn.clicked.connect(dlg.reject)
        layout.addWidget(buttons)
        return dlg.exec() == QDialog.Accepted

    def _preview_execution_date(self):
        d = self._qdate_iso(self.execution_date.date())
        include_next = bool(getattr(self, "include_next_day_starts", None) and self.include_next_day_starts.isChecked())
        actions = self._actions_for_execution_date(d, include_next_day_starts=include_next)
        if not actions:
            return self._info("Date preview", f"No unapplied changes are scheduled for {d}.")
        try:
            p = preview_batch(self.workbook, actions, d)
        except Exception as exc:
            return self._error("Date preview", str(exc))
        if p.collisions:
            return self._error("Preview blocked", "Collisions were found:\n\n" + "\n".join(p.collisions[:20]))
        self._batch_preview_dialog(p, approval=False)

    def _cost_floor_resolution_dialog(self, preview, actions) -> str:
        """Return 'adjust', 'keep', or 'cancel' for below-cost START rows."""
        issues = list(getattr(preview, "cost_floor_issues", []) or [])
        if not issues:
            return "none"

        campaigns = {int(c.id): c for c, action in actions if action == "START" and c.id is not None}
        all_can_override = all(bool(campaigns.get(int(i.campaign_id)) and campaigns[int(i.campaign_id)].allow_below_cost) for i in issues)

        dialog = QDialog(self)
        dialog.setWindowTitle("Cost-floor control")
        dialog.resize(820, 520)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"{len(issues)} currency price(s) would be below cost floor after the planned promo.")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #202124;")
        layout.addWidget(title)

        note = QLabel(
            "You can raise only the affected LIST prices so the resulting promo price is at least "
            "cost floor + 10%. Prices are rounded UP to the next valid 0.05 step."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #676B73; margin-bottom: 6px;")
        layout.addWidget(note)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setLineWrapMode(QTextEdit.NoWrap)
        rows = [
            "Campaign | Destination | Plan | Days | Cur | List now | Promo net | Floor | Target +10% | New list",
            "-" * 116,
        ]
        for i in issues:
            rows.append(
                f"{i.campaign_name} | {i.iso} | {i.plan} | {i.days} | {i.currency} | "
                f"{i.current_list_price:.2f} | {i.promo_net_price:.2f} | {i.cost_floor:.2f} | "
                f"{i.target_net_price:.2f} | {i.required_list_price:.2f}"
            )
        details.setPlainText("\n".join(rows))
        details.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 11px;")
        layout.addWidget(details, 1)

        result = {"choice": "cancel"}
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        buttons.addStretch(1)

        if all_can_override:
            keep = QPushButton("Keep list prices · use below-cost override")
            def keep_clicked():
                result["choice"] = "keep"
                dialog.accept()
            keep.clicked.connect(keep_clicked)
            buttons.addWidget(keep)

        adjust = QPushButton("Raise list prices to floor + 10% and continue")
        adjust.setStyleSheet(
            f"QPushButton {{ background: {MAGENTA}; color: white; border: none; border-radius: 5px; "
            "padding: 8px 14px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: {MAGENTA_DARK}; }}"
        )
        def adjust_clicked():
            result["choice"] = "adjust"
            dialog.accept()
        adjust.clicked.connect(adjust_clicked)
        buttons.addWidget(adjust)
        layout.addLayout(buttons)

        if dialog.exec() != QDialog.Accepted:
            return "cancel"
        return result["choice"]

    def _apply_execution_date(self):
        d = self._qdate_iso(self.execution_date.date())
        if self.store.get_active_batch_for_date(d):
            return self._info("Already executed", f"An active execution batch already exists for {d}. Undo it first if it was wrong.")
        include_next = bool(getattr(self, "include_next_day_starts", None) and self.include_next_day_starts.isChecked())
        actions = self._actions_for_execution_date(d, include_next_day_starts=include_next)
        if not actions:
            return self._info("Execute date", f"No unapplied changes are scheduled for {d}.")
        if not self._confirm_sequence_override(d):
            return
        try:
            p = preview_batch(self.workbook, actions, d)
        except Exception as exc:
            return self._error("Execute date", str(exc))
        if p.collisions:
            return self._error("Execution blocked", "Collisions were found:\n\n" + "\n".join(p.collisions[:20]))

        floor_choice = self._cost_floor_resolution_dialog(p, actions) if p.cost_floor_issues else "none"
        if floor_choice == "cancel":
            return
        adjust_floor_prices = floor_choice == "adjust"

        # Re-run the simulation with the chosen floor resolution so the AFTER
        # columns are exactly what will be written if the user approves.
        try:
            final_preview = preview_batch(
                self.workbook, actions, d,
                adjust_below_floor_to_margin=adjust_floor_prices,
            )
        except Exception as exc:
            return self._error("Execution preview", str(exc))
        if not self._batch_preview_dialog(final_preview, approval=True):
            return

        try:
            backup, changed_map, adjusted_cells = execute_batch(
                self.workbook, actions, self.out_dir / "backups", d,
                adjust_below_floor_to_margin=adjust_floor_prices,
            )
            details = [
                {
                    "campaign_id": int(c.id),
                    "campaign_name": c.name,
                    "action": action,
                    "scheduled_date": c.end_date if action == "END" else c.start_date,
                    "changed_rows": changed_map.get((int(c.id), action), 0),
                }
                for c, action in actions
            ]
            batch_id = self.store.create_batch(d, self.workbook, backup, details)
            for c, action in actions:
                changed = changed_map.get((int(c.id), action), 0)
                scheduled_date = c.end_date if action == "END" else c.start_date
                detail_text = f"Date batch {d}"
                if scheduled_date and scheduled_date != d:
                    detail_text += f"; campaign action scheduled for {scheduled_date}"
                self.store.record_execution(c.id, action, self.workbook, backup, changed, details=detail_text, batch_id=batch_id)
            history_snapshot = self._save_manual_price_history_snapshot()
        except Exception as exc:
            return self._error("Execution blocked", str(exc))
        self._refresh_campaigns()
        extra = f"\nList-price cells adjusted for cost floor +10%: {adjusted_cells}" if adjusted_cells else ""
        self._info("Date executed", f"All scheduled changes for {d} were applied.\n\nRows changed: {sum(changed_map.values())}{extra}\nBackup: {backup.name}\nHistory: {history_snapshot.name}\nBatch: #{batch_id}")

    def _undo_execution_date(self):
        d = self._qdate_iso(self.execution_date.date())
        batch = self.store.get_active_batch_for_date(d)
        if not batch:
            return self._info("Undo execution", f"There is no active execution batch for {d}.")
        if self.store.has_later_active_batch(int(batch["id"])):
            return self._error("Undo blocked", "A later date batch has already been executed. Undo the most recent batch first so current prices are not overwritten with an older state.")
        if not self._confirm("Undo date execution", f"Restore manual prices to the exact state before batch #{batch['id']} for {d}?\n\nThis is intended only for correcting an execution mistake."):
            return
        try:
            restore_workbook_backup(self.workbook, Path(batch["backup_path"]))
            self.store.undo_batch(int(batch["id"]))
            history_snapshot = self._save_manual_price_history_snapshot()
        except Exception as exc:
            return self._error("Undo failed", str(exc))
        self._refresh_campaigns()
        self._info("Execution undone", f"Batch #{batch['id']} for {d} was undone and manual prices were restored from its backup.\n\nHistory: {history_snapshot.name}")

    def _preview_selected(self, action: str):
        try:
            p = preview_campaign(self.workbook, self._selected_campaign(), action)
        except Exception as exc:
            return self._error("Preview", str(exc))
        self._show_preview(p)

    def _show_preview(self, p):
        msg = f"{p.action}\n\nMatched rows: {p.matched_rows}\nWould change: {p.changed_rows}\nBelow cost floor: {p.below_floor_rows}\nCollisions: {len(p.collisions)}"
        if p.collisions:
            msg += "\n\nCOLLISIONS\n" + "\n".join(p.collisions[:12])
        if p.examples:
            msg += "\n\nExamples\n" + "\n".join(p.examples)
        self._info("Promotion preview", msg)

    def _open_management_calendar(self):
        try:
            path = generate_management_calendar(self.store, self.out_dir / "promotion_calendar.html", open_after=True)
        except Exception as exc:
            return self._error("Management calendar", str(exc))
        self._info("Management calendar", f"Management timeline created:\n{path}")

    # ---------- helpers ----------
    @staticmethod
    def _qdate_iso(qd: QDate) -> str:
        return qd.toString("yyyy-MM-dd")

    @staticmethod
    def _parse_iso(text: str) -> date:
        return datetime.strptime(text, "%Y-%m-%d").date()

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child is not None:
                PromoPlanner._clear_layout(child)

    def _info(self, title: str, message: str):
        QMessageBox.information(self, title, message)

    def _warn(self, title: str, message: str):
        QMessageBox.warning(self, title, message)

    def _error(self, title: str, message: str):
        QMessageBox.critical(self, title, message)

    def _confirm(self, title: str, message: str) -> bool:
        return QMessageBox.question(self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


def main():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    try:
        window = PromoPlanner()
        window.show()
        app.exec()
    except Exception as exc:
        QMessageBox.critical(None, "T-Travel Promo Planner", str(exc))
        raise


if __name__ == "__main__":
    main()
