from __future__ import annotations
from html import escape

from datetime import date, datetime, timedelta
import csv
import json
from pathlib import Path
import subprocess
import sys
import pandas as pd

from PySide6.QtCore import QDate, Qt, Signal, Slot, QRect, QThread
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
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
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

from .engine import PromoOption, preview_campaign, preview_batch, read_catalogue_frame
from .calendar_export import generate_promotion_calendar
from .management_calendar import generate_management_calendar
from .date_price_changes import active_changes, changes_for_key_date, project_dataframe
from .outlook_sync import delete_campaign_events, sync_campaign_events
from .paths import find_manual_prices, planner_output_dir, project_root
from .store import Campaign, PromoStore
from .timeline_labels import campaign_transition, today_campaign_transition, campaign_presentation, campaign_context_ids, key_date_read_only
try:
    from automation.pricing_book import read_pricing_workbook
    from automation.pricing_timeline import PricingTimeline
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from pricing_book import read_pricing_workbook
    from pricing_timeline import PricingTimeline

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


class BatchApplyWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, timeline, execution_date, actions, floor_choices, conflict_choices, campaigns, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.execution_date = execution_date
        self.actions = list(actions)
        self.floor_choices = dict(floor_choices)
        self.conflict_choices = dict(conflict_choices)
        self.campaigns = list(campaigns)

    def run(self):
        try:
            self.progress.emit("Preparing the campaign timeline change…", 1, 2)
            before = self.timeline.project(self.execution_date, include_drafts=False)
            campaign_preview = preview_batch(
                before,
                self.actions,
                self.execution_date,
                cost_floor_choices=self.floor_choices,
                conflict_choices=self.conflict_choices,
                campaigns=self.campaigns,
                date_price_changes=[
                    row for row in self.timeline.date_override_records(
                        boundary=(date.fromisoformat(self.execution_date) + timedelta(days=1)).isoformat()
                    ) if row.get("official") and row.get("is_base_price_change")
                ],
            )
            self.succeeded.emit((before, campaign_preview))
        except Exception as exc:
            self.failed.emit(str(exc))


class ApplyProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.can_close = False

    def reject(self):
        if self.can_close:
            super().reject()

    def closeEvent(self, event):
        if self.can_close:
            event.accept()
        else:
            event.ignore()


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
                f"border-radius:6px; padding:0; font-size:8.5pt; font-weight:700; }}\n"
                f"QPushButton:hover {{ background:{MAGENTA_DARK}; border-color:{MAGENTA_DARK}; }}"
            )
        else:
            self.setStyleSheet(
                f"QPushButton {{ background:white; color:{DARK}; border:1px solid {BORDER}; "
                f"border-radius:6px; padding:0; font-size:8.5pt; font-weight:650; }}\n"
                f"QPushButton:hover {{ background:{MAGENTA_SOFT}; color:{DARK}; border-color:#E9A8C8; }}"
            )

    def mousePressEvent(self, event):
        self._shift_on_press = bool(event.modifiers() & Qt.ShiftModifier)
        super().mousePressEvent(event)

    def _emit_click(self):
        self.dayClicked.emit(self.day, self._shift_on_press)
        self._shift_on_press = False


class PromoPlanner(QMainWindow):
    campaignsChanged = Signal()

    def __init__(self, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self.embedded = embedded
        self.on_timeline_changed = None
        self.on_destination_selected = None
        self.before_timeline_update = None
        self._shared_key_date: str | None = None
        self._timeline_start_date: str | None = None
        self._draft_timeline_key_date: str | None = None
        self.setWindowTitle(("DEV ? " if (project_root() / "outputs" / ".development_workspace").exists() else "") + "T-Travel Promo Planner")
        if self.embedded:
            self.resize(360, 760)
            self.setMinimumSize(330, 480)
        else:
            self.resize(1320, 820)
            self.setMinimumSize(1060, 700)

        self.root_dir = project_root()
        self.out_dir = planner_output_dir(self.root_dir)
        self.store = PromoStore(self.out_dir / "promo_schedule.db")
        self.timeline = PricingTimeline(self.store.path)
        self.timeline.set_current_as_of(date.today().isoformat())
        if not self.timeline.baseline_info():
            # One-time migration only. Routine planner launches use SQLite and
            # do not open the compatibility workbook.
            migration_source = find_manual_prices(self.root_dir)
            baseline = read_pricing_workbook(migration_source)
            self.timeline.initialize_baseline(
                baseline,
                source_path=migration_source,
                effective_from=date.today().isoformat(),
            )
        self._refresh_catalogue_from_timeline()
        self.dest_map = {code: name for code, name in self.catalogue.destinations}

        self.selected_destinations: list[str] = []
        self.monotonic_destinations: set[str] = set()
        self._campaign_read_only = False
        self.editing_campaign_id: int | None = None
        self.day_buttons: dict[int, DayButton] = {}
        self.plan_checks: dict[str, QCheckBox] = {}
        self._promo_by_display: dict[str, PromoOption] = {}
        self._last_day_clicked: int | None = None
        self._last_execution_key_date: str | None = None
        self._pending_cost_floor_choices = {}
        self._pending_conflict_choices = {}
        self._pending_conflict_contexts = {}
        self._apply_worker = None
        self._apply_progress_dialog = None
        self._apply_progress_label = None
        self._apply_progress_bar = None
        self._active_apply_date = None
        self._apply_actions = None

        self._apply_style()
        self._build_ui()
        if self.embedded:
            self.setStyleSheet(self.styleSheet() + """
                QWidget { font-size: 8pt; }
                QPushButton { padding: 4px 7px; }
                QLineEdit, QComboBox, QDateEdit, QTextEdit { padding: 3px; min-height: 20px; }
                QCheckBox { spacing: 3px; }
            """)
            self.tabs.setStyleSheet(
                "QTabBar::tab { padding: 4px 7px; margin-right: 2px; font-size: 8pt; }"
                "QTabBar::tab:selected { padding: 5px 8px; }"
            )
        self._load_promo_combo()
        self._refresh_destinations()
        self._refresh_groups()
        self._refresh_campaigns()

    def _refresh_catalogue_from_timeline(self):
        frame = self.timeline.project()
        promo_path = self.root_dir / "inputs" / "promos.json"
        promo_options = None
        if promo_path.exists():
            try:
                promo_options = []
                for item in json.loads(promo_path.read_text(encoding="utf-8")):
                    promo_type = str(item.get("promo_type", "")).strip().lower()
                    value = pd.to_numeric(item.get("promo_value"), errors="coerce")
                    code = str(item.get("promo_code", "")).strip()
                    if not code or pd.isna(value):
                        continue
                    if promo_type in {"percentage", "pct", "%"}:
                        promo_type = "percent"
                    if promo_type in {"fixed", "amount", "value"}:
                        promo_type = "absolute"
                    if promo_type in {"percent", "absolute"}:
                        promo_options.append(PromoOption(
                            code, promo_type, float(value), str(item.get("label") or code)
                        ))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(f"Could not load the promo catalogue from {promo_path}: {exc}") from exc
        self.catalogue = read_catalogue_frame(frame, promo_options)

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
        sub = QLabel("Planning future promotions · Live prices: shared database timeline", objectName="headerSub")
        hl.addWidget(title)
        hl.addWidget(sub)
        outer.addWidget(header)
        header.setVisible(not self.embedded)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(8 if self.embedded else 18, 8 if self.embedded else 12, 8 if self.embedded else 18, 8 if self.embedded else 16)
        self.tabs = QTabWidget()
        bl.addWidget(self.tabs)
        outer.addWidget(body, 1)

        self.timeline_tab = QWidget()
        self.execution_tab = QWidget()
        self.create_tab = QWidget()
        self.tabs.addTab(self.timeline_tab, "Selected date" if self.embedded else "Timeline")
        self.tabs.addTab(self.execution_tab, "Campaigns")
        self.tabs.addTab(self.create_tab, "Edit promotion" if self.embedded else "Create / edit promotion")
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
        outer = QVBoxLayout(self.create_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        self.campaign_heading = QLabel("New promotion")
        self.campaign_heading.setTextFormat(Qt.PlainText)
        self.campaign_heading.setWordWrap(True)
        self.campaign_heading.setStyleSheet(f"font-size:14pt;font-weight:700;color:{MAGENTA_DARK};padding:6px 8px;")
        outer.addWidget(self.campaign_heading)
        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page = QWidget()
        root = QVBoxLayout(page)
        page_scroll.setWidget(page)
        outer.addWidget(page_scroll)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Search and selected destinations sit side by side; the selected
        # campaign scope gets twice the width of the country search list.
        dest_card, dl = self._card("1. Destinations", "Click a destination to add it to the campaign scope.")
        destination_columns = QHBoxLayout()
        destination_columns.setSpacing(8)

        available_card, available_layout = self._card("Find", "Search or click a destination.")
        self.search_edit = QComboBox()
        self.search_edit.setEditable(True)
        self.search_edit.setMinimumWidth(0)
        self.search_edit.lineEdit().setPlaceholderText("Search destination...")
        self.search_edit.lineEdit().textChanged.connect(self._refresh_destinations)
        available_layout.addWidget(self.search_edit)

        self.available_list = QListWidget()
        self.available_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.available_list.setFixedHeight(140)
        self.available_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.available_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.available_list.setDragEnabled(True)
        self.available_list.setDragDropMode(QAbstractItemView.DragOnly)
        self.available_list.itemClicked.connect(self._pick_destination)
        available_layout.addWidget(self.available_list, 1)
        destination_columns.addWidget(available_card, 1)

        selected_card, selected_layout = self._card("Selected destinations", "This is the exact campaign scope.")
        selected_header = QHBoxLayout()
        selected_title = QLabel("Countries in this campaign")
        selected_title.setStyleSheet("font-weight: 700;")
        self.selected_count_label = QLabel("0 selected")
        self.selected_count_label.setObjectName("muted")
        selected_header.addWidget(selected_title)
        selected_header.addStretch(1)
        selected_header.addWidget(self.selected_count_label)
        selected_layout.addLayout(selected_header)

        self.selected_list = DestinationDropList()
        self.selected_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.selected_list.setFixedHeight(140)
        self.selected_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.selected_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.selected_list.codesDropped.connect(self._add_destination_codes)
        self.selected_list.currentItemChanged.connect(self._preview_destination_curve)
        selected_layout.addWidget(self.selected_list, 1)
        self.selected_list.itemChanged.connect(self._monotonic_destination_changed)
        monotonicity_row = QHBoxLayout()
        legend = QLabel("Monotonicity · tick beside each country")
        legend.setToolTip("Temporarily reduce shorter-duration net prices when they exceed a longer promotional price. Existing offers return afterward.")
        legend.setWordWrap(True)
        monotonicity_row.addWidget(legend, 1)
        self.monotonic_all_check = QCheckBox("ALL")
        self.monotonic_all_check.setToolTip("Enable or remove monotonicity for all countries in this campaign.")
        self.monotonic_all_check.clicked.connect(self._set_monotonic_all)
        monotonicity_row.addWidget(self.monotonic_all_check)
        selected_layout.insertLayout(selected_layout.indexOf(self.selected_list), monotonicity_row)

        self.monotonic_other_promos_check = QCheckBox("Also align other promotional prices in affected curves")
        self.monotonic_other_promos_check.setToolTip(
            "For countries ticked for monotonicity, also use other promotional durations as anchors. "
            "All resulting PA adjustments last only for this campaign.")
        selected_layout.insertWidget(selected_layout.indexOf(self.selected_list), self.monotonic_other_promos_check)

        group_row = QHBoxLayout()
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(0)
        load_group = QPushButton("Load group")
        save_group = QPushButton("Save selection")
        load_group.clicked.connect(self._load_group)
        save_group.clicked.connect(self._save_group)
        selected_layout.addWidget(self.group_combo)
        group_row.addWidget(load_group)
        group_row.addWidget(save_group)
        selected_layout.addLayout(group_row)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected)
        self.scope_mutation_buttons = (load_group, save_group, remove_btn)
        selected_layout.addWidget(remove_btn, 0, Qt.AlignRight)
        destination_columns.addWidget(selected_card, 2)
        dl.addLayout(destination_columns)
        dest_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        root.addWidget(dest_card)

        # Promotion form, scrollable
        form_card, fl = self._card("2. Promotion", "Saving schedules this promotion. Prices change when its key date is applied.")
        form_card.setMinimumHeight(300)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        self.name_edit.currentTextChanged.connect(self._update_campaign_heading)
        self.name_edit.lineEdit().setPlaceholderText("e.g. Black Friday Asia")
        form.addWidget(self.name_edit)

        selection_row = QHBoxLayout()
        selection_row.setSpacing(8)
        promo_card, promo_layout = self._card("Promotion")
        selection_row.addWidget(promo_card, 1)
        promo_label = QLabel("Promotion from catalogue *")
        promo_label.setStyleSheet("background:transparent;font-weight:600;")
        promo_layout.addWidget(promo_label)
        promo_row = QHBoxLayout()
        promo_row.setSpacing(4)
        self.promo_combo = QComboBox()
        self.promo_combo.setObjectName("requiredInput")
        self.promo_combo.setMinimumWidth(0)
        reload_promo = QPushButton("↻")
        reload_promo.setFixedWidth(38)
        reload_promo.clicked.connect(self._reload_catalogue)
        promo_row.addWidget(self.promo_combo, 1)
        promo_row.addWidget(reload_promo)
        promo_layout.addLayout(promo_row)

        self.already_active = CleanCheckBox("Already active in current prices — do not publish, only schedule removal")
        self.already_active.setObjectName("optionCheck")
        self.already_active.setVisible(not self.embedded)
        self.already_active.toggled.connect(self._toggle_existing_active)

        dates = QHBoxLayout()
        self.start_date = self._date_control("Start date · set in Timeline" if self.embedded else "Start date")
        self.start_date[1].setEnabled(not self.embedded)
        self.end_date = self._date_control("End date")
        self.start_date[1].dateChanged.connect(self._constrain_campaign_dates)
        self.end_date[1].dateChanged.connect(self._constrain_campaign_dates)
        dates.addLayout(self.start_date[0])
        dates.addLayout(self.end_date[0])

        plan_card, plan_layout = self._card("Plans")
        plan_box = QFrame()
        plan_box.setStyleSheet(f"QFrame {{ background:white; border:1px solid {BORDER}; border-radius:7px; }}")
        plan_grid = QGridLayout(plan_box)
        plan_grid.setContentsMargins(10, 8, 10, 8)
        plan_grid.setHorizontalSpacing(8 if self.embedded else 28)
        plan_grid.setVerticalSpacing(4 if self.embedded else 6)
        plan_columns = 2
        for i, plan in enumerate(self.catalogue.plans):
            cb = CleanCheckBox(plan)
            cb.setObjectName("planCheck")
            cb.setChecked(True)
            self.plan_checks[plan] = cb
            plan_grid.addWidget(cb, i // plan_columns, i % plan_columns)
        plan_layout.addWidget(plan_box)
        selection_row.addWidget(plan_card, 1)
        form.addLayout(selection_row)
        form.addWidget(self.already_active)
        form.addLayout(dates)

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
            b.setChecked(False)
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

        notes_label = QLabel("Notes")
        notes_label.setStyleSheet("background:transparent;font-weight:600;")
        form.addWidget(notes_label)
        self.notes_edit = QTextEdit()
        self.notes_edit.setMinimumHeight(90)
        form.addWidget(self.notes_edit, 1)
        scroll.setWidget(inner)
        fl.addWidget(scroll, 1)
        root.addWidget(form_card, 1)

        action_bar = QFrame()
        action_bar.setStyleSheet(f"background:white;border:1px solid {BORDER};border-radius:8px;")
        al = QVBoxLayout(action_bar) if self.embedded else QHBoxLayout(action_bar)
        al.setContentsMargins(12, 9, 12, 9)
        info = QLabel("Saving schedules the campaign and Outlook reminders. Current prices change only when you execute a key date.")
        info.setStyleSheet(f"color:{MID};border:0;")
        info.setWordWrap(self.embedded)
        if self.embedded:
            info.setMaximumHeight(34)
        self.edit_state_label = info
        al.addWidget(info, 1)
        preview = QPushButton("Preview")
        preview.clicked.connect(lambda _checked=False: self._preview_unsaved())
        self.preview_form_btn = preview
        self.cancel_edit_btn = QPushButton("Cancel edit")
        self.cancel_edit_btn.clicked.connect(self._cancel_edit_clicked)
        self.save_btn = QPushButton("Save campaign")
        self.save_btn.setEnabled(not self.embedded)
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
        if self.embedded:
            actions = QHBoxLayout()
            actions.setSpacing(4)
            actions.addWidget(preview)
            actions.addWidget(self.cancel_edit_btn)
            actions.addWidget(self.save_btn)
            al.addLayout(actions)
        else:
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
        ctrl.setDisplayFormat("dd MMM yy" if self.embedded else "dd MMM yyyy")
        if self.embedded:
            ctrl.setMinimumWidth(0)
            ctrl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        note = QLabel("Edit the offer, destinations and end date here. To move the start date of a planned promotion, use Move start date… in the Timeline. Completed promotions remain read-only.")
        note.setStyleSheet(f"color:{MID};")
        note.setWordWrap(True)
        dl.addWidget(note)
        root.addWidget(detail_card)

    # ---------- Timeline ----------
    def _build_timeline_tab(self):
        if self.embedded:
            root = QVBoxLayout(self.timeline_tab)
            root.setContentsMargins(6, 5, 6, 5)
            root.setSpacing(5)
            self.execution_date = QDateEdit(QDate.currentDate(), self.timeline_tab)
            self.execution_date.hide()
            self.execution_date.dateChanged.connect(self._on_execution_date_changed)
            self.execution_date_title = QLabel("Current manual prices")
            self.execution_date_title.setWordWrap(True)
            self.execution_date_title.setStyleSheet("font-size:10pt;font-weight:700;")
            root.addWidget(self.execution_date_title)
            self.execution_summary = QLabel("")
            self.execution_summary.setWordWrap(True)
            self.execution_summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.execution_summary.setStyleSheet(f"color:{MID};")
            self.execution_summary.setTextFormat(Qt.RichText)
            self.execution_summary.setMaximumHeight(72)
            root.addWidget(self.execution_summary)
            self.midnight_changes_note = QLabel(
                "Preview before/after prices and resolve conflicts here or from the top Timeline."
            )
            self.midnight_changes_note.setWordWrap(True)
            self.midnight_changes_note.setMaximumHeight(32)
            self.midnight_changes_note.setStyleSheet(f"color:{MID};font-size:8pt;")
            root.addWidget(self.midnight_changes_note)
            self.preview_date_btn = QPushButton("Preview & resolve")
            self.preview_date_btn.clicked.connect(self._preview_execution_date)
            self.apply_date_btn = QPushButton("Apply campaign handover")
            self.apply_date_btn.setObjectName("primary")
            self.apply_date_btn.clicked.connect(self._apply_execution_date)
            self.undo_btn = QPushButton("Undo applied changes")
            self.undo_btn.setObjectName("danger")
            self.undo_btn.setVisible(False)
            self.undo_btn.clicked.connect(self._undo_execution_date)
            action_row = QHBoxLayout()
            action_row.setSpacing(5)
            action_row.addWidget(self.preview_date_btn)
            action_row.addWidget(self.apply_date_btn)
            action_row.addWidget(self.undo_btn)
            root.addLayout(action_row)
            return

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
        export = QPushButton("Export promotion calendar")
        export.setObjectName("soft")
        export.clicked.connect(self._export_promotion_calendar)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_campaigns)
        top.addWidget(export)
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

        exec_card, el = self._card("Midnight changes", "Review the state before and after midnight on the selected key date.")
        exec_card.setMinimumWidth(285)
        exec_card.setMaximumWidth(430)
        self.execution_date_title = QLabel("No key date selected")
        self.execution_date_title.setWordWrap(True)
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

        self.midnight_changes_note = QLabel("Ending campaigns are removed before starting campaigns are published.")
        self.midnight_changes_note.setToolTip("All changes on this key date are applied together at midnight.")
        el.addWidget(self.midnight_changes_note)

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
        if self.embedded:
            return
        self.timeline_scene.clear()
        campaigns = self.store.list_campaigns()
        if not campaigns:
            self.timeline_scene.addText("No campaigns yet.")
            return

        today = date.today()
        parsed = []
        for c in campaigns:
            imported = c.status == "ACTIVE_IMPORTED"
            d1 = self._parse_iso(c.display_start_date)
            d2 = self._parse_iso(c.end_date) if c.end_date else (today + timedelta(days=90))
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
            boundary = dd + timedelta(days=1)
            if first <= boundary <= last:
                x = left + (boundary - first).days * day_w
                self.timeline_scene.addLine(x, 28, x, height, QPen(QColor("#F2B7D4"), 1, Qt.DashLine))
                self.timeline_scene.addEllipse(x - 4, 27, 8, 8, QPen(Qt.NoPen), QBrush(QColor(MAGENTA)))

        tx = left + (today - first).days * day_w
        self.timeline_scene.addLine(tx, 25, tx, height, QPen(QColor(MAGENTA), 2))
        tt = self.timeline_scene.addText("TODAY", QFont("Segoe UI", 7, QFont.Bold))
        tt.setDefaultTextColor(QColor(MAGENTA))
        tt.setPos(tx + 4, 28)

        key = self._qdate_iso(self.execution_date.date())
        context = campaign_context_ids(campaigns, key)
        for r, (c, d1, d2) in enumerate(parsed):
            y = top + r * row_h
            state = campaign_presentation(c, self.store)
            font = QFont("Segoe UI", 8)
            font.setItalic(state["readonly"])
            font.setBold(c.id in context)
            label = self.timeline_scene.addText(c.name[:34], font)
            label.setToolTip(state["label"])
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
            self._refresh_catalogue_from_timeline()
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

    def destination_label(self, code: str) -> str:
        code = str(code).strip()
        name = self.dest_map.get(code, "")
        return f"{name} ({code})" if name and name.casefold() != code.casefold() else code

    def _preview_destination_curve(self, item, _previous=None):
        if item is None or not callable(self.on_destination_selected):
            return
        code = str(item.data(Qt.UserRole) or "").strip()
        if code:
            plans = [plan for plan, check in self.plan_checks.items() if check.isChecked()]
            days = [day for day, button in self.day_buttons.items() if button.isChecked()]
            self.on_destination_selected(code, plans, days)

    def _set_monotonic_all(self, enabled):
        self.monotonic_destinations = set(self.selected_destinations) if enabled else set()
        self._render_selected()

    def _monotonic_destination_changed(self, item):
        code = str(item.data(Qt.UserRole))
        if item.checkState() == Qt.Checked:
            self.monotonic_destinations.add(code)
        else:
            self.monotonic_destinations.discard(code)
        self._sync_monotonic_all()

    def _sync_monotonic_all(self):
        if not hasattr(self, "monotonic_all_check"):
            return
        chosen = set(self.selected_destinations)
        count = len(chosen & self.monotonic_destinations)
        self.monotonic_all_check.blockSignals(True)
        self.monotonic_all_check.setTristate(0 < count < len(chosen))
        self.monotonic_all_check.setCheckState(Qt.PartiallyChecked if 0 < count < len(chosen) else Qt.Checked if chosen and count == len(chosen) else Qt.Unchecked)
        self.monotonic_all_check.blockSignals(False)

    def _render_selected(self):
        self.monotonic_destinations.intersection_update(self.selected_destinations)
        self.selected_list.blockSignals(True)
        self.selected_list.clear()
        for code in self.selected_destinations:
            item = QListWidgetItem(self.destination_label(code))
            item.setData(Qt.UserRole, code)
            if not self._campaign_read_only:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if code in self.monotonic_destinations else Qt.Unchecked)
            item.setToolTip("Checked: temporarily lower shorter durations that exceed a longer promotional net price.")
            self.selected_list.addItem(item)
        self.selected_list.blockSignals(False)
        self._sync_monotonic_all()
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

    def _update_campaign_heading(self, name: str):
        self.campaign_heading.setText(name.strip() or "New promotion")

    def _constrain_campaign_dates(self, *_args):
        start, end = self.start_date[1], self.end_date[1]
        if self.already_active.isChecked():
            end.setMinimumDate(QDate(1752, 9, 14))
            if start.date() > end.date():
                start.setDate(end.date())
        else:
            end.setMinimumDate(start.date())

    def _toggle_existing_active(self, checked: bool):
        # Existing live campaigns are registered without a START action, but still
        # always require an end date so they can be terminated through a key date.
        self.start_date[1].setEnabled(not checked and not self.embedded)
        self.end_date[1].setEnabled(True)
        self._constrain_campaign_dates()
        self.save_btn.setText("Register active campaign" if checked else ("Save changes" if self.editing_campaign_id is not None else "Save campaign"))

    def _validate_form(self):
        if self.embedded and self.editing_campaign_id is None and not self._timeline_start_date:
            raise ValueError("Choose the promotion start date using Add dated change in Timeline first.")
        if self.embedded and self._timeline_start_date and self._qdate_iso(self.start_date[1].date()) != self._timeline_start_date:
            raise ValueError("The promotion start date is fixed by Timeline.")
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
            # Below-floor approval is always collected per row at each key date.
            allow_below_cost=False,
            notes=self.notes_edit.toPlainText().strip(),
            status="ACTIVE_IMPORTED" if existing_active else "PLANNED",
            monotonic_all_promos=self.monotonic_other_promos_check.isChecked(),
            monotonic_destinations=sorted(self.monotonic_destinations & set(self.selected_destinations)),
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
                self.store.update_campaign(c)
                cid = int(c.id)
                msg = f"Campaign #{cid} updated."
                self._cancel_edit(clear_form=False)
        except Exception as exc:
            return self._error("Cannot save campaign", str(exc))
        if c.start_date:
            self.timeline.discard_promotion_date((date.fromisoformat(c.start_date) - timedelta(days=1)).isoformat())
        self._cancel_edit(clear_form=False)
        outlook_note = self._sync_outlook(cid)
        self._refresh_campaigns()
        self._refresh_execution_date()
        self.tabs.setCurrentWidget(getattr(self, "campaigns_tab", self.execution_tab))
        if outlook_note:
            msg += "\n\n" + outlook_note
        self._info("Campaigns", msg)
        return True

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
        # Issue decisions must use a real saved campaign ID and the shared date.
        if self.embedded and not self._shared_key_date:
            return self._info("Preview", "Choose a date in Timeline first.")
        # After saving/cancelling an edit, the form is idle. Preview should
        # review the saved date, rather than validate it as a new promotion.
        if (self.embedded and self.editing_campaign_id is None
                and not getattr(self, "_timeline_start_date", None)):
            return self._preview_execution_date()
        try:
            stored = (self.store.get_campaign(self.editing_campaign_id)
                      if self.editing_campaign_id is not None else None)
            candidate = stored if self._campaign_read_only else self._campaign_from_form()
            fields = ("name", "start_date", "end_date", "promo_type", "promo_value",
                      "promo_code", "promo_name", "notes", "status", "monotonic_all_promos")
            selections = ("destinations", "plans", "days", "monotonic_destinations")
            unchanged = stored is not None and (
                self._campaign_read_only or (
                    all(getattr(candidate, field) == getattr(stored, field) for field in fields)
                    and all(set(getattr(candidate, field)) == set(getattr(stored, field))
                            for field in selections)))
        except Exception as exc:
            return self._error("Preview", str(exc))
        if not unchanged:
            if not self._ask("Save campaign and review issues?",
                             "Save your campaign edits first so issue decisions can be recorded "
                             "and shown in the pricing editor at the selected Timeline date?"):
                return
            if not self._save_campaign():
                return
        return self._preview_execution_date()

    def _issue_destination_label(self, iso):
        code = str(iso).upper()
        name = getattr(self, "dest_map", {}).get(code, "")
        return f"{name} ({code})" if name and name != code else code

    def _refresh_campaigns(self):
        if not hasattr(self, "tree"):
            return
        self.tree.clear()
        today = date.today().isoformat()
        campaigns = self.store.list_campaigns()
        key = self._shared_key_date if self.embedded else self._qdate_iso(self.execution_date.date())
        context = campaign_context_ids(campaigns, key)
        for c in campaigns:
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
            presentation = campaign_presentation(c, self.store)
            state = presentation["label"]
            scope = f"{len(c.destinations)} dest · {len(c.plans)} plans · {len(c.days)} durations"
            promo = c.promo_name or c.promo_code
            start_display = c.start_date or "Already live"
            end_display = c.end_date
            item = QTreeWidgetItem([str(c.id), c.name, start_display, end_display, promo, scope, state])
            item.setData(0, Qt.UserRole, int(c.id))
            for col in range(7):
                font = item.font(col)
                font.setItalic(presentation["readonly"])
                font.setBold(c.id in context)
                item.setFont(col, font)
                item.setBackground(col, QBrush(QColor(bg)))
                item.setForeground(col, QBrush(QColor(fg)))
            self.tree.addTopLevelItem(item)
        self._refresh_groups()
        self._render_timeline()
        self._refresh_execution_date()
        self.campaignsChanged.emit()

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
        destination_labels = ", ".join(self.destination_label(code) for code in c.destinations)
        self.detail.setPlainText(
            f"{c.name}\n"
            f"Promotion: {c.promo_name or '-'} | catalogue code: {c.promo_code}\n"
            f"{start_display} → {end_display} | {state}\n"
            f"Destinations: {destination_labels}\n"
            f"Plans: {', '.join(c.plans)} | Days: {', '.join(map(str, c.days))}\n"
            f"Notes: {c.notes or '-'}"
        )

    def _edit_selected(self):
        try:
            c = self._selected_campaign()
        except Exception as exc:
            return self._warn("Edit campaign", str(exc))
        self.open_campaign_for_edit(c.id)

    def open_campaign_for_edit(self, campaign_id: int):
        try:
            c = self.store.get_campaign(int(campaign_id))
        except Exception as exc:
            return self._warn("Edit campaign", str(exc))
        self.editing_campaign_id = c.id
        self._timeline_start_date = c.start_date or None
        self._load_campaign_into_form(c)
        self._set_end_only_edit_mode(False)
        state = campaign_presentation(c, self.store)
        self._set_campaign_read_only(state["readonly"])
        self.save_btn.setText("Published · read only" if state["published"] else "Read only" if state["readonly"] else "Save changes")
        self.edit_state_label.setText(state["label"] + ". Preview and Cancel remain available." if state["readonly"] else "Edit the definition; its start date is managed in Timeline. Save changes before resolving issues.")
        self.tabs.setCurrentWidget(self.create_tab)

    def _set_end_only_edit_mode(self, enabled: bool):
        """The Timeline controls the start date; the remaining definition is editable."""
        self.name_edit.setEnabled(True)
        self.promo_combo.setEnabled(True)
        self.already_active.setEnabled(not self.embedded)
        self.start_date[1].setEnabled(False)
        self.end_date[1].setEnabled(True)
        self.notes_edit.setEnabled(True)
        for cb in self.plan_checks.values():
            cb.setEnabled(True)
        for btn in self.day_buttons.values():
            btn.setEnabled(True)
        for name in ("available_list", "selected_list", "search_edit", "group_combo"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(True)

    def _set_campaign_read_only(self, readonly):
        self._campaign_read_only = bool(readonly)
        if not readonly:
            self._set_end_only_edit_mode(False)
        else:
            for name in ("name_edit", "promo_combo", "already_active", "notes_edit", "available_list", "search_edit", "group_combo"):
                getattr(self, name).setEnabled(False)
            self.end_date[1].setEnabled(False)
            for widget in list(self.plan_checks.values()) + list(self.day_buttons.values()):
                widget.setEnabled(False)
        self.selected_list.setEnabled(True)  # Country navigation also works in read-only campaigns.
        for button in getattr(self, "scope_mutation_buttons", ()):
            button.setEnabled(not readonly)
        if hasattr(self.selected_list, "count"):
            for row in range(self.selected_list.count()):
                item = self.selected_list.item(row)
                flags = item.flags()
                item.setFlags(flags & ~Qt.ItemIsUserCheckable if readonly else flags | Qt.ItemIsUserCheckable)
        if hasattr(self, "monotonic_all_check"):
            self.monotonic_all_check.setEnabled(not readonly)
        if hasattr(self, "monotonic_other_promos_check"):
            self.monotonic_other_promos_check.setEnabled(not readonly)
        self.start_date[1].setEnabled(False)
        self.save_btn.setEnabled(not readonly and (self.editing_campaign_id is not None or not self.embedded or bool(self._timeline_start_date)))
        self.cancel_edit_btn.setEnabled(True)
        self.preview_form_btn.setEnabled(True)
        if not readonly:
            self.edit_state_label.setText("Changes belong to their selected Timeline date. Save changes to update the campaign.")

    def _load_campaign_into_form(self, c: Campaign):
        self.name_edit.setEditText(c.name)
        imported = c.status == "ACTIVE_IMPORTED"
        self.already_active.setChecked(imported)
        if c.start_date:
            self.start_date[1].setDate(QDate.fromString(c.start_date, "yyyy-MM-dd"))
        self.start_date[1].setToolTip("Start unknown / already active. The displayed anchor is for layout only." if not c.start_date else "Start date is set in Timeline.")
        if c.end_date:
            self.end_date[1].setDate(QDate.fromString(c.end_date, "yyyy-MM-dd"))
        else:
            self.end_date[1].setDate(QDate.currentDate())
        if not c.start_date:
            self.start_date[1].setDate(QDate.fromString(c.display_start_date, "yyyy-MM-dd"))
        self._constrain_campaign_dates()
        self.selected_destinations = list(c.destinations)
        self.monotonic_other_promos_check.setChecked(c.monotonic_all_promos)
        self.monotonic_destinations = set(c.monotonic_destinations)
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

    def begin_campaign_on_date(self, effective_date: str, *, label: str = "") -> None:
        self._cancel_edit()
        self._timeline_start_date = date.fromisoformat(effective_date).isoformat()
        self._draft_timeline_key_date = (date.fromisoformat(effective_date) - timedelta(days=1)).isoformat()
        start = QDate.fromString(self._timeline_start_date, "yyyy-MM-dd")
        self.start_date[1].setDate(start)
        self.start_date[1].setEnabled(False)
        self.end_date[1].setDate(start.addDays(6))
        self.name_edit.setEditText(label)
        self.save_btn.setEnabled(True)

    def _cancel_edit_clicked(self, _checked=False):
        self._cancel_edit(clear_form=True)
        self.tabs.setCurrentWidget(getattr(self, "campaigns_tab", self.execution_tab))

    def _cancel_edit(self, *, clear_form: bool = True):
        self._campaign_read_only = False
        self.editing_campaign_id = None
        self._timeline_start_date = None
        self._draft_timeline_key_date = None
        self.save_btn.setEnabled(not self.embedded)
        self._set_end_only_edit_mode(False)
        self._set_campaign_read_only(False)
        self.save_btn.setText("Save campaign")
        if not clear_form:
            return
        self.name_edit.setEditText("")
        self.already_active.setChecked(False)
        self.start_date[1].setDate(QDate.currentDate())
        self.end_date[1].setDate(QDate.currentDate())
        self.selected_destinations = []
        self.monotonic_other_promos_check.setChecked(False)
        self.monotonic_destinations = set()
        self._render_selected()
        self.notes_edit.clear()
        if self.promo_combo.count():
            self.promo_combo.setCurrentIndex(0)
        for cb in self.plan_checks.values():
            cb.setChecked(True)
        for day, b in self.day_buttons.items():
            b.setChecked(False)
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

    def cancel_future_campaign(self, campaign_id: int) -> None:
        campaign = self.store.get_campaign(campaign_id)
        self.store.delete_campaign(campaign_id)
        if self.editing_campaign_id == campaign_id:
            self._cancel_edit()
        try:
            delete_campaign_events(campaign)
        except Exception as exc:
            self._warn("Outlook", f"The promotion was cancelled, but its reminders could not be removed: {exc}")

    def _delete_selected(self):
        try:
            campaign = self._selected_campaign()
        except Exception as exc:
            return self._warn("Delete campaign", str(exc))
        if not self._confirm("Cancel future campaign", f"Cancel '{campaign.name}' and remove its Outlook reminders?"):
            return
        try:
            self.cancel_future_campaign(campaign.id)
        except Exception as exc:
            return self._error("Cancel campaign", str(exc))
        self._refresh_campaigns()
        self.detail.clear()

    # ---------- Key dates / batch execution ----------
    def _key_dates(self) -> list[str]:
        """Return midnight boundaries: an end date and the prior date for a start."""
        dates = set()
        for c in self.store.list_campaigns():
            if c.start_date:
                dates.add((self._parse_iso(c.start_date) - timedelta(days=1)).isoformat())
            if c.end_date:
                dates.add(c.end_date)
        dates.update(
            str(change.get("key_date", ""))
            for change in self.timeline.date_override_records()
            if change.get("key_date")
        )
        dates.update(self.timeline.key_dates(today=(date.today() - timedelta(days=1)).isoformat()))
        return sorted(dates)

    def _key_date_counts(self, key_date: str) -> tuple[int, int]:
        campaigns = self.store.list_campaigns()
        next_day = (self._parse_iso(key_date) + timedelta(days=1)).isoformat()
        return (
            sum(1 for c in campaigns if c.start_date == next_day),
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

            dd = self._parse_iso(kd)
            next_day = dd + timedelta(days=1)
            b = QPushButton(
                f"{dd.strftime('%d %b').upper()} 24:00\n→ {next_day.strftime('%d %b').upper()} 00:00"
            )
            b.setFixedSize(126, 48)
            font = b.font()
            font.setItalic(key_date_read_only(self.store.list_campaigns(), self.store, self.timeline, kd))
            b.setFont(font)
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
            starts, ends = self._key_date_counts(kd)
            price_edits = len(self.timeline.date_override_records(key_date=kd))
            b.setToolTip(
                f"Midnight boundary · {dd.strftime('%d %B')} 24:00 → {next_day.strftime('%d %B %Y')} 00:00\n"
                f"{ends} campaign(s) ending · {starts} campaign(s) starting · {price_edits} editor price override(s)"
            )
            b.clicked.connect(lambda _checked=False, d=kd: self._jump_to_execution_date(d))
            self.key_date_row.addWidget(b)
        self.key_date_row.addStretch(1)

    def schedule_price_change_from_timeline(self) -> bool:
        """Open the price-change date dialog from the editor's shared Timeline toolbar."""
        return self._schedule_price_change_date()

    def _schedule_price_change_date(self) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("Schedule price change")
        layout = QFormLayout(dialog)
        name = QLineEdit(dialog)
        name.setPlaceholderText("For example: Autumn roaming price update")
        effective_date = QDateEdit(QDate.currentDate().addDays(1), dialog)
        effective_date.setCalendarPopup(True)
        effective_date.setDisplayFormat("dd MMM yyyy")
        effective_date.setMinimumDate(QDate.currentDate())
        layout.addRow("Change name", name)
        layout.addRow("Prices take effect", effective_date)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return False
        label = name.text().strip()
        if not label:
            self._warn("Schedule price change", "Enter a name for this price change.")
            return False
        effective = effective_date.date().toPython()
        key_date = (effective - timedelta(days=1)).isoformat()
        try:
            self.timeline.schedule_price_change_date(key_date, label)
        except Exception as exc:
            self._error("Schedule price change", str(exc))
            return False
        self.set_shared_key_date(key_date)
        self._refresh_campaigns()
        self._refresh_execution_date()
        if callable(self.on_timeline_changed):
            self.on_timeline_changed(key_date)
        return True

    def _on_execution_date_changed(self, _date: QDate):
        self._refresh_execution_date()

    def set_shared_key_date(self, key_date: str | None) -> None:
        """Make the editor's single date selector authoritative in embedded mode."""
        if not self.embedded:
            return
        self._shared_key_date = str(key_date).strip() if key_date else None
        if self._shared_key_date:
            qd = QDate.fromString(self._shared_key_date, "yyyy-MM-dd")
            if qd.isValid() and self.execution_date.date() != qd:
                self.execution_date.setDate(qd)
            elif qd.isValid():
                self._refresh_execution_date()
            return
        self._refresh_execution_date()

    def _actions_for_execution_date(self, execution_date: str):
        actions = []
        next_day = (self._parse_iso(execution_date) + timedelta(days=1)).isoformat()
        for c in self.store.list_campaigns():
            started = self.store.has_execution(c.id, "START")
            ended = self.store.has_execution(c.id, "END")
            imported = c.status == "ACTIVE_IMPORTED"
            if c.end_date and c.end_date == execution_date and (started or imported) and not ended:
                actions.append((c, "END"))
            if (not imported) and c.start_date and c.start_date == next_day and not started:
                actions.append((c, "START"))
        return actions

    def _actions_through_execution_date(self, execution_date: str):
        """Project from today's workbook through the selected midnight boundary."""
        boundary = self._parse_iso(execution_date) + timedelta(days=1)
        actions = []
        for campaign in self.store.list_campaigns():
            started = self.store.has_execution(campaign.id, "START")
            ended = self.store.has_execution(campaign.id, "END")
            imported = campaign.status == "ACTIVE_IMPORTED"
            starts_by_boundary = bool(
                not started and not imported and campaign.start_date
                and self._parse_iso(campaign.start_date) <= boundary
            )
            if starts_by_boundary:
                actions.append((campaign, "START"))
            ends_by_boundary = bool(
                not ended and campaign.end_date
                and self._parse_iso(campaign.end_date) < boundary
                and (started or imported or starts_by_boundary)
            )
            if ends_by_boundary:
                actions.append((campaign, "END"))
        return actions

    def _refresh_execution_date(self):
        if not hasattr(self, "execution_date"):
            return
        if self.embedded and not self._shared_key_date:
            transition = today_campaign_transition(self.store.list_campaigns())
            self.execution_date_title.setText(f"{date.today():%d %b %Y} · {transition}")
            self.execution_summary.setText("Read only. Choose a date in Timeline, or add a dated change before editing.")
            self.preview_date_btn.setEnabled(False)
            self.apply_date_btn.setEnabled(False)
            self.undo_btn.setVisible(False)
            return
        if not self.embedded:
            self._render_key_date_strip()
        d = self._qdate_iso(self.execution_date.date())
        self._load_issue_choices(d)
        if hasattr(self, "execution_date_title"):
            try:
                key_date = self._parse_iso(d)
                next_day = key_date + timedelta(days=1)
                pretty = (
                    f"Midnight boundary · {key_date.strftime('%d %b')} 24:00 → "
                    f"{next_day.strftime('%d %b %Y')} 00:00"
                )
            except Exception:
                pretty = d
            self.execution_date_title.setText(pretty)

        self._last_execution_key_date = d
        actions = self._actions_for_execution_date(d)

        ending = [c.name for c, a in actions if a == "END" and c.end_date == d]
        next_day = (self._parse_iso(d) + timedelta(days=1)).isoformat()
        starting_next_day = [c.name for c, a in actions if a == "START" and c.start_date == next_day]
        batch = self.store.get_active_batch_for_date(d)
        date_price_changes = self.timeline.date_override_records(key_date=d)
        planned_events = self.timeline.key_date_labels().get(d, [])
        price_details = [str(label).replace("PRICE CHANGE:", "").strip() for label in planned_events]
        if date_price_changes:
            price_details.append(f"{len(date_price_changes)} saved price row change(s)")

        def summary_section(title: str, values: list[str]) -> str:
            details = " · ".join(escape(value) for value in values) if values else "Nothing planned"
            return f"<b>{title}</b> — {details}"

        parts = [
            summary_section(f"CAMPAIGNS STARTING · {self._parse_iso(next_day).strftime('%d %b').upper()} 00:00", starting_next_day),
            summary_section("PRICE CHANGES", price_details),
            summary_section(f"CAMPAIGNS ENDING · {self._parse_iso(d).strftime('%d %b').upper()} 24:00", ending),
        ]
        if batch:
            parts.append(f"<b>EXECUTED</b><br>Batch #{batch['id']} · {escape(str(batch['executed_at']))}")
        self.execution_summary.setText("<br><br>".join(parts))

        has_reviewable_changes = bool(actions or date_price_changes or planned_events)
        self.preview_date_btn.setEnabled(has_reviewable_changes)
        self.apply_date_btn.setEnabled(bool(actions) and not bool(batch))
        self.apply_date_btn.setText("Apply campaign handover")
        self.undo_btn.setVisible(bool(batch))
        self.undo_btn.setEnabled(bool(batch))
        self.undo_btn.setText(f"Undo changes for {self._parse_iso(d).strftime('%d %b')}")

    def _earliest_outstanding_key_date(self) -> str | None:
        for kd in self._key_dates():
            if (
                self._actions_for_execution_date(kd)
                or self.timeline.date_override_records(key_date=kd)
            ) and not self.store.get_active_batch_for_date(kd):
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
            f"Apply {earliest} first so promo layers and temporary price changes follow their scheduled order."
        )
        go_btn = msg.addButton(f"Go to {earliest}", QMessageBox.AcceptRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is go_btn:
            self._jump_to_execution_date(earliest)
            return False
        return False

    @staticmethod
    def _fmt_price(value) -> str:
        return "—" if value is None else f"{float(value):.2f}"

    def _batch_preview_dialog(self, preview, *, approval: bool = False, editable_issues: bool = True) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle("Approve pricing changes" if approval else "Pricing change preview")
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            dlg.resize(int(available.width() * 0.96), int(available.height() * 0.88))
        else:
            dlg.resize(1600, 850)
        layout = QVBoxLayout(dlg)

        title = QLabel(
            ("Review every affected pricing row before writing to current prices." if approval else
             ("Baseline → selected date, using current campaign definitions and saved price changes." if self.embedded else
              "Projected from today’s manual current prices through this key date. Nothing will be written."))
        )
        title.setStyleSheet("font-size:11pt;font-weight:700;")
        layout.addWidget(title)
        sub = QLabel(
            f"Execution date: {preview.execution_date}   ·   Rows affected: {len(preview.price_changes)}   ·   "
            f"Campaign actions: {len(preview.items)}"
        )
        sub.setStyleSheet(f"color:{MID};")
        layout.addWidget(sub)

        campaigns_by_id = {int(c.id or 0): c for c in self.store.list_campaigns()}
        action_lines = []
        for item in preview.items:
            campaign = campaigns_by_id.get(item.campaign_id)
            scheduled_date = (
                campaign.end_date if item.action == "END" else campaign.start_date
            ) if campaign else ""
            date_label = f" ({scheduled_date})" if scheduled_date else ""
            action_lines.append(
                f"{item.action}: {item.campaign_name}{date_label}  ·  "
                f"matched {item.matched_rows}, would change {item.changed_rows}"
            )
        actions_label = QLabel("Included campaign actions\n" + "\n".join(action_lines))
        actions_label.setWordWrap(True)
        actions_label.setStyleSheet(f"color:{MID};background:#F7F8FA;border:1px solid {BORDER};border-radius:6px;padding:7px;")
        layout.addWidget(actions_label)

        columns = [
            "Destination", "Plan", "Days", "Change",
            "Promo\nbefore", "Promo\nafter", "Below-floor approval\nbefore", "Below-floor approval\nafter",
            "List EUR\nbefore", "List EUR\nafter", "Net EUR\nbefore", "Net EUR\nafter",
            "List USD\nbefore", "List USD\nafter", "Net USD\nbefore", "Net USD\nafter",
        ]
        table = QTableWidget(len(preview.price_changes), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setMinimumHeight(48)
        header.setDefaultAlignment(Qt.AlignCenter)
        header.setTextElideMode(Qt.ElideNone)
        header.setSectionResizeMode(QHeaderView.Stretch)
        for r, ch in enumerate(preview.price_changes):
            vals = [
                f"{ch.country} ({ch.iso})" if ch.country and ch.country != ch.iso else ch.iso,
                ch.plan, str(ch.days), ch.change,
                ch.promo_before, ch.promo_after,
                "TRUE" if ch.allow_below_cost_before else "FALSE",
                "TRUE" if ch.allow_below_cost_after else "FALSE",
                self._fmt_price(ch.list_eur_before), self._fmt_price(ch.list_eur_after),
                self._fmt_price(ch.net_eur_before), self._fmt_price(ch.net_eur_after),
                self._fmt_price(ch.list_usd_before), self._fmt_price(ch.list_usd_after),
                self._fmt_price(ch.net_usd_before), self._fmt_price(ch.net_usd_after),
            ]
            for col, value in enumerate(vals):
                item = QTableWidgetItem(value)
                if col in (5, 7, 9, 11, 13, 15):
                    item.setBackground(QBrush(QColor(MAGENTA_SOFT)))
                table.setItem(r, col, item)
        table.resizeRowsToContents()
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        layout.addWidget(table, 1)

        if preview.collisions:
            collision_text = (
                "Preview found overlapping promos. Choose temporary override to put the new promo on top "
                "and resume the earlier promo when it ends if that promo is still scheduled, or keep the current promo:\n"
                + "\n".join(preview.collisions[:20])
            )
            if len(preview.collisions) > 20:
                collision_text += f"\n...and {len(preview.collisions) - 20} more"
            collisions_label = QLabel(collision_text)
            collisions_label.setWordWrap(True)
            collisions_label.setStyleSheet(f"background:{AMBER_SOFT};color:{AMBER};border:1px solid #E7C46B;border-radius:6px;padding:7px;")
            layout.addWidget(collisions_label)

        if preview.cost_floor_issues:
            warn = QLabel(
                f"Cost-floor review: {len(preview.cost_floor_issues)} currency price(s) need a decision. "
                "Saved choices are reflected in the AFTER columns. Open the issue list to review or change them."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet(f"background:{AMBER_SOFT};color:{AMBER};border:1px solid #E7C46B;border-radius:6px;padding:7px;")
            layout.addWidget(warn)
        if preview.cost_floor_issues or preview.collision_issues:
            item_count = len(preview.cost_floor_issues) + len(preview.collision_issues)
            issues_button = QPushButton(f"Review {item_count} issue decisions")
            issues_button.setToolTip("Unresolved rows are red. Saved decisions are green.")
            issues_button.clicked.connect(
                lambda _checked=False, p=preview, editable=not approval and editable_issues:
                self._batch_issue_choices_dialog(
                    p,
                    editable=editable,
                    on_saved=lambda: self.statusBar().showMessage(
                        "Choices saved. The pricing editor has been refreshed.", 8000
                    ),
                )
            )
            layout.addWidget(issues_button)

        buttons = QDialogButtonBox()
        export_btn = buttons.addButton("Export CSV…", QDialogButtonBox.ActionRole)
        export_btn.clicked.connect(lambda _checked=False, p=preview: self._export_batch_preview_csv(p))
        close_btn = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        close_btn.clicked.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()
        return False

    def _export_batch_preview_csv(self, preview):
        suggested = self.out_dir / f"promo_preview_{preview.execution_date}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export preview to CSV", str(suggested), "CSV files (*.csv)")
        if not path:
            return
        columns = [
            "Destination", "ISO", "Plan", "Days", "Change", "Promo before", "Promo after",
            "Below-floor approval before", "Below-floor approval after",
            "List EUR before", "List EUR after", "Net EUR before", "Net EUR after",
            "List USD before", "List USD after", "Net USD before", "Net USD after",
        ]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for change in preview.price_changes:
                    writer.writerow([
                        change.country, change.iso, change.plan, change.days, change.change,
                        change.promo_before, change.promo_after,
                        change.allow_below_cost_before, change.allow_below_cost_after, change.list_eur_before,
                        change.list_eur_after, change.net_eur_before, change.net_eur_after,
                        change.list_usd_before, change.list_usd_after, change.net_usd_before,
                        change.net_usd_after,
                    ])
            self.statusBar().showMessage(f"Preview CSV exported: {path}", 8000)
        except Exception as exc:
            self._error("Export preview", str(exc))

    def _issue_choices_path(self, execution_date: str) -> Path:
        return self.out_dir / "issue_choices" / f"{execution_date}.json"

    def _load_issue_choices(self, execution_date: str):
        self._pending_cost_floor_choices = {}
        self._pending_conflict_choices = {}
        self._pending_conflict_contexts = {}
        path = self._issue_choices_path(execution_date)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._pending_cost_floor_choices = {
                (int(cid), str(iso).upper(), str(plan), int(days), str(currency).upper()): str(choice)
                for cid, iso, plan, days, currency, choice in data.get("cost_floor", [])
            }
            self._pending_conflict_choices = {}
            self._pending_conflict_contexts = {}
            for record in data.get("conflicts", []):
                if len(record) != 6:
                    continue
                cid, iso, plan, days, current_code, choice = record
                key = (int(cid), str(iso).upper(), str(plan), int(days))
                self._pending_conflict_choices[key] = str(choice)
                self._pending_conflict_contexts[key] = str(current_code)
        except Exception as exc:
            self._warn("Saved issue choices", f"Could not read saved choices for {execution_date}: {exc}")

    def _save_issue_choices(self, execution_date: str):
        path = self._issue_choices_path(execution_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "execution_date": execution_date,
            "cost_floor": [list(key) + [choice] for key, choice in self._pending_cost_floor_choices.items()],
            "conflicts": [
                list(key) + [self._pending_conflict_contexts.get(key, ""), choice]
                for key, choice in self._pending_conflict_choices.items()
            ],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(path)

    def _simulate_with_saved_choices(self, execution_date: str, actions):
        if self.embedded:
            boundary = (self._parse_iso(execution_date) + timedelta(days=1)).isoformat()
            baseline = self.timeline.project(boundary, layers=("base", "import", "override"))
            _, preview = project_dataframe(
                baseline, self.root_dir / "manual_prices_current.xlsx", self.store,
                execution_date, self.root_dir, replay_executed=True,
            )
            return preview
        current = self.timeline.project(execution_date)
        preview = preview_batch(
            current, actions, execution_date,
            cost_floor_choices=self._pending_cost_floor_choices,
            conflict_choices=self._pending_conflict_choices,
            date_price_changes=active_changes(
                self.root_dir,
                (self._parse_iso(execution_date) + timedelta(days=1)).isoformat(),
            ),
            campaigns=self.store.list_campaigns(),
        )
        for _ in range(3):
            current_floors = {
                (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
                for issue in preview.cost_floor_issues
            }
            current_conflicts = {
                (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days): issue.current_promo_code
                for issue in preview.collision_issues
            }
            stale_floors = set(self._pending_cost_floor_choices) - current_floors
            stale_conflicts = set(self._pending_conflict_choices) - set(current_conflicts)
            stale_conflicts.update(
                key for key, promo_code in current_conflicts.items()
                if key in self._pending_conflict_choices
                and promo_code != self._pending_conflict_contexts.get(key, "")
            )
            if not stale_floors and not stale_conflicts:
                break
            for key in stale_floors:
                self._pending_cost_floor_choices.pop(key, None)
            for key in stale_conflicts:
                self._pending_conflict_choices.pop(key, None)
                self._pending_conflict_contexts.pop(key, None)
            self._save_issue_choices(execution_date)
            preview = preview_batch(
                current, actions, execution_date,
                cost_floor_choices=self._pending_cost_floor_choices,
                conflict_choices=self._pending_conflict_choices,
                date_price_changes=active_changes(
                    self.root_dir,
                    (self._parse_iso(execution_date) + timedelta(days=1)).isoformat(),
                ),
                campaigns=self.store.list_campaigns(),
            )
        return preview

    def _set_cost_floor_issue_row_state(self, table, row, resolved):
        background = QColor("#DFF2E1" if resolved else "#FDE8E7")
        foreground = QColor("#1B5E20" if resolved else "#9B1C1C")
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is None:
                continue
            font = item.font()
            font.setBold(not resolved)
            item.setFont(font)
            item.setBackground(background)
            item.setForeground(foreground)
        combo = table.cellWidget(row, table.columnCount() - 1)
        if combo is not None:
            combo.setStyleSheet(
                "QComboBox { background: #DFF2E1; color: #1B5E20; }"
                if resolved else
                "QComboBox { background: #FDE8E7; color: #9B1C1C; font-weight: 700; }"
            )

    def _show_cost_floor_issues(self, issues, *, editable=True, on_saved=None):
        dlg = QDialog(self)
        dlg.setWindowTitle("Cost-floor issues")
        dlg.resize(1180, 600)
        layout = QVBoxLayout(dlg)
        intro = QLabel(
            f"{len(issues)} currency prices would have a promo net price below the cost floor. "
            "This is a preview; no prices have been changed."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        columns = [
            "Campaign", "Destination", "Plan", "Days", "Currency",
            "List now", "Promo net", "Cost floor", "Target net (+10%)", "Required list",
        ]
        if editable:
            columns.append("Decision")
        table = QTableWidget(len(issues), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setStyleSheet(
            "QTableWidget::item:selected { background: #B9BDC3; color: #202124; }"
        )
        choices = []
        saved = getattr(self, "_pending_cost_floor_choices", {})
        for row, issue in enumerate(issues):
            values = [
                issue.campaign_name, issue.iso, issue.plan, str(issue.days), issue.currency,
                self._fmt_price(issue.current_list_price), self._fmt_price(issue.promo_net_price),
                self._fmt_price(issue.cost_floor), self._fmt_price(issue.target_net_price),
                self._fmt_price(issue.required_list_price),
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
            if editable:
                combo = QComboBox()
                combo.addItem("Select action", None)
                combo.addItem("Raise list price to floor + 10%", "raise")
                combo.addItem("Override cost floor", "override")
                key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
                existing = saved.get(key)
                if existing:
                    combo.setCurrentIndex(combo.findData(existing))
                table.setCellWidget(row, len(columns) - 1, combo)
                choices.append((key, combo))
                self._set_cost_floor_issue_row_state(table, row, combo.currentData() in {"raise", "override"})

                def update_row_state(_index=None, r=row, c=combo):
                    self._set_cost_floor_issue_row_state(table, r, c.currentData() in {"raise", "override"})

                combo.currentIndexChanged.connect(update_row_state)
            else:
                key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
                self._set_cost_floor_issue_row_state(table, row, saved.get(key) in {"raise", "override"})
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox()
        if editable:
            cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
            save = buttons.addButton("Save choices & close", QDialogButtonBox.AcceptRole)
            save.setEnabled(bool(choices) and all(combo.currentData() in {"raise", "override"} for _key, combo in choices))
            cancel.clicked.connect(dlg.reject)

            def refresh_save(_index=None):
                save.setEnabled(all(combo.currentData() in {"raise", "override"} for _key, combo in choices))

            for _key, combo in choices:
                combo.currentIndexChanged.connect(refresh_save)
            save.clicked.connect(dlg.accept)
        else:
            close = buttons.addButton("Close", QDialogButtonBox.RejectRole)
            close.clicked.connect(dlg.reject)
        layout.addWidget(buttons)
        if editable and dlg.exec() == QDialog.Accepted:
            self._pending_cost_floor_choices = {
                **saved,
                **{key: combo.currentData() for key, combo in choices},
            }
            if on_saved:
                on_saved()
        elif not editable:
            dlg.exec()

    def _preview_execution_date(self):
        if self.embedded and not self._shared_key_date:
            return self._info("Date preview", "Choose a date in Timeline first.")
        d = self._qdate_iso(self.execution_date.date())
        actions = self._actions_through_execution_date(d)
        boundary = (self._parse_iso(d) + timedelta(days=1)).isoformat()
        scheduled_prices = active_changes(self.root_dir, boundary)
        if not self.embedded and not actions and not scheduled_prices:
            return self._info("Date preview", f"No scheduled price or campaign changes are projected through {d}.")
        try:
            self._load_issue_choices(d)
            p = self._simulate_with_saved_choices(d, actions)
        except Exception as exc:
            return self._error("Date preview", str(exc))
        if p.cost_floor_issues or p.collision_issues:
            self._batch_issue_choices_dialog(p)
        else:
            self._info("Date preview", "No cost-floor issues or promo conflicts at this date.")

    def _persist_issue_decisions(self, floor_choices, conflict_choices, conflict_contexts):
        """Save a complete snapshot, including partial resolutions and cleared choices."""
        previous = (self._pending_cost_floor_choices, self._pending_conflict_choices,
                    self._pending_conflict_contexts)
        self._pending_cost_floor_choices = dict(floor_choices)
        self._pending_conflict_choices = dict(conflict_choices)
        self._pending_conflict_contexts = dict(conflict_contexts)
        try:
            self._save_issue_choices(self._qdate_iso(self.execution_date.date()))
        except Exception as exc:
            (self._pending_cost_floor_choices, self._pending_conflict_choices,
             self._pending_conflict_contexts) = previous
            self._error("Choices not saved", f"Your latest decision could not be saved. Please try again.\n\n{exc}")
            return False
        return True

    def _batch_issue_choices_dialog(self, preview, *, editable=True, on_saved=None):
        """Show cost-floor and promo-conflict rows together and collect their decisions."""
        floor_issues = list(preview.cost_floor_issues or [])
        conflict_issues = list(preview.collision_issues or [])
        if not floor_issues and not conflict_issues:
            return ({}, {}) if editable else None

        dialog = QDialog(self)
        dialog.setWindowTitle("Price and promotion issues")
        dialog.resize(1450, 720)
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            f"Review {len(floor_issues)} cost-floor prices and {len(conflict_issues)} promo conflicts. "
            "Unresolved rows are red and bold; saved decisions are green. "
            + ("Each decision saves immediately. Close to see the updated editor." if editable else
               "This is an unsaved campaign preview. Save the campaign, then use Timeline Preview & resolve to record decisions.")
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        note = QLabel(
            "For every active promo below floor, "
            "raise the list price to reach floor + 10% or explicitly override the floor. "
            "PA rows require below-floor approval or editing/removing the adjustment; raising them alone would break monotonicity. "
            "A promo conflict can temporarily override the current promo; the previous promo resumes at the end "
            "if it is still scheduled."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        columns = [
            "Issue", "Campaign", "Destination", "Plan", "Days", "Currency", "Current promo",
            "List now", "Promo net", "Cost floor", "Target net (+10%)", "Required list", "Resolution",
        ]
        table = QTableWidget(len(floor_issues) + len(conflict_issues), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setStyleSheet("QTableWidget::item:selected { background: #B9BDC3; color: #202124; }")

        saved_floors = getattr(self, "_pending_cost_floor_choices", {})
        saved_conflicts = getattr(self, "_pending_conflict_choices", {})
        saved_conflict_contexts = getattr(self, "_pending_conflict_contexts", {})
        floor_rows = []
        conflict_rows = []
        conflict_contexts = {}
        row = 0
        for issue in floor_issues:
            key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
            values = [
                "PA cost floor" if issue.is_adjustment else "Cost floor", issue.campaign_name, self._issue_destination_label(issue.iso), issue.plan, str(issue.days), issue.currency.upper(), "",
                self._fmt_price(issue.current_list_price), self._fmt_price(issue.promo_net_price),
                self._fmt_price(issue.cost_floor), self._fmt_price(issue.target_net_price),
                self._fmt_price(issue.required_list_price),
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
            combo = QComboBox()
            combo.addItem("Select action", None)
            if not issue.is_adjustment:
                combo.addItem("Raise list price to floor + 10%", "raise")
            combo.addItem("Allow PA below cost floor" if issue.is_adjustment else "Override cost floor", "override")
            if saved_floors.get(key):
                combo.setCurrentIndex(combo.findData(saved_floors[key]))
            table.setCellWidget(row, len(columns) - 1, combo)
            floor_rows.append((key, combo))
            self._set_cost_floor_issue_row_state(table, row, combo.currentData() in {"raise", "override"})
            combo.currentIndexChanged.connect(
                lambda _index=None, r=row, c=combo:
                self._set_cost_floor_issue_row_state(table, r, c.currentData() in {"raise", "override"})
            )
            row += 1

        for issue in conflict_issues:
            key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days)
            conflict_contexts[key] = issue.current_promo_code
            values = [
                "Promo conflict", issue.campaign_name, self._issue_destination_label(issue.iso), issue.plan, str(issue.days), "EUR / USD",
                issue.current_promo_name or issue.current_promo_code, "", "", "", "", "",
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
            combo = QComboBox()
            combo.addItem("Select action", None)
            combo.addItem("Temporarily override; resume if still active", "overwrite")
            combo.addItem("Keep current promo", "keep")
            if saved_conflicts.get(key):
                combo.setCurrentIndex(combo.findData(saved_conflicts[key]))
            table.setCellWidget(row, len(columns) - 1, combo)
            conflict_rows.append((key, combo))
            self._set_cost_floor_issue_row_state(table, row, combo.currentData() in {"overwrite", "keep"})
            combo.currentIndexChanged.connect(
                lambda _index=None, r=row, c=combo:
                self._set_cost_floor_issue_row_state(table, r, c.currentData() in {"overwrite", "keep"})
            )
            row += 1
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox()
        close = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        close.clicked.connect(dialog.reject)
        layout.addWidget(buttons)
        changed = False

        def persist(_index=None):
            nonlocal changed
            floors = dict(self._pending_cost_floor_choices)
            conflicts = dict(self._pending_conflict_choices)
            contexts = dict(self._pending_conflict_contexts)
            for key, combo in floor_rows:
                floors.pop(key, None)
                if combo.currentData() in {"raise", "override"}:
                    floors[key] = combo.currentData()
            for key, combo in conflict_rows:
                conflicts.pop(key, None)
                contexts.pop(key, None)
                if combo.currentData() in {"overwrite", "keep"}:
                    conflicts[key] = combo.currentData()
                    contexts[key] = conflict_contexts[key]
            if self._persist_issue_decisions(floors, conflicts, contexts):
                changed = True
            else:
                # Restore the visible selections to the last durable choices.
                for index, (key, combo) in enumerate(floor_rows + conflict_rows):
                    saved = (self._pending_cost_floor_choices if index < len(floor_rows)
                             else self._pending_conflict_choices)
                    combo.blockSignals(True)
                    combo.setCurrentIndex(max(0, combo.findData(saved.get(key))))
                    combo.blockSignals(False)
                    self._set_cost_floor_issue_row_state(table, index, bool(combo.currentData()))

        for _key, combo in floor_rows + conflict_rows:
            combo.setEnabled(editable)
            if editable:
                combo.currentIndexChanged.connect(persist)
        dialog.exec()
        if changed:
            if self.embedded and callable(self.on_timeline_changed):
                self.on_timeline_changed(self._shared_key_date)
            if on_saved:
                on_saved()
        if editable:
            return dict(self._pending_cost_floor_choices), dict(self._pending_conflict_choices)
        return None

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

    def _cost_floor_choices_dialog(self, preview):
        """Collect an explicit raise-or-override choice for every below-floor currency price."""
        issues = list(getattr(preview, "cost_floor_issues", []) or [])
        if not issues:
            return {}

        dialog = QDialog(self)
        dialog.setWindowTitle("Choose cost-floor actions")
        dialog.resize(1320, 720)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"Choose what to do for each of the {len(issues)} affected currency prices.")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #202124;")
        layout.addWidget(title)
        note = QLabel(
            "Raise list price sets the promo net price to at least cost floor + 10%, rounded up to the next 0.05. "
            "Override keeps the current list price and allows this promo price below cost floor for that pricing row."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        columns = [
            "Campaign", "Destination", "Plan", "Days", "Currency", "List now",
            "Promo net", "Cost floor", "Target net (+10%)", "Required list", "Decision",
        ]
        table = QTableWidget(len(issues), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setStyleSheet(
            "QTableWidget::item:selected { background: #B9BDC3; color: #202124; }"
        )
        choices = []
        saved = getattr(self, "_pending_cost_floor_choices", {})
        for row, issue in enumerate(issues):
            values = [
                issue.campaign_name, issue.iso, issue.plan, str(issue.days), issue.currency,
                self._fmt_price(issue.current_list_price), self._fmt_price(issue.promo_net_price),
                self._fmt_price(issue.cost_floor), self._fmt_price(issue.target_net_price),
                self._fmt_price(issue.required_list_price),
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
            combo = QComboBox()
            combo.addItem("Select action", None)
            if not issue.is_adjustment:
                combo.addItem("Raise list price to floor + 10%", "raise")
            combo.addItem("Allow PA below cost floor" if issue.is_adjustment else "Override cost floor", "override")
            key = (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
            existing = saved.get(key)
            if existing:
                combo.setCurrentIndex(combo.findData(existing))
            table.setCellWidget(row, len(columns) - 1, combo)
            choices.append((issue, key, combo))
            self._set_cost_floor_issue_row_state(table, row, combo.currentData() in {"raise", "override"})

            def update_row_state(_index=None, r=row, c=combo):
                self._set_cost_floor_issue_row_state(table, r, c.currentData() in {"raise", "override"})

            combo.currentIndexChanged.connect(update_row_state)
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        proceed = buttons.addButton("Save choices & continue", QDialogButtonBox.AcceptRole)
        proceed.setEnabled(False)
        cancel.clicked.connect(dialog.reject)
        proceed.clicked.connect(dialog.accept)
        layout.addWidget(buttons)

        def refresh_proceed(_index=None):
            proceed.setEnabled(all(combo.currentData() in {"raise", "override"} for _issue, _key, combo in choices))

        for _issue, _key, combo in choices:
            combo.currentIndexChanged.connect(refresh_proceed)
        refresh_proceed()

        if dialog.exec() != QDialog.Accepted:
            return None
        result = {key: combo.currentData() for _issue, key, combo in choices}
        self._pending_cost_floor_choices = {**saved, **result}
        return result

    def _apply_execution_date(self):
        if self._apply_worker is not None and self._apply_worker.isRunning():
            return
        if self.embedded and callable(self.before_timeline_update) and not self.before_timeline_update():
            return
        d = self._qdate_iso(self.execution_date.date())
        if self.store.get_active_batch_for_date(d):
            return self._info("Already executed", f"An active execution batch already exists for {d}. Undo it first if it was wrong.")
        actions = self._actions_for_execution_date(d)
        date_price_changes = self.timeline.date_override_records(key_date=d)
        if not actions:
            return self._info(
                "No campaign handover",
                "Dated editor price changes are already stored in the shared timeline and take effect in the selected-date projection. "
                "Use Preview & resolve to inspect them, then export the selected date from the pricing editor. "
                "Apply is only needed for campaign starts or ends.",
            )
        if not self._confirm_sequence_override(d):
            return
        self._load_issue_choices(d)
        try:
            p = self._simulate_with_saved_choices(d, actions)
        except Exception as exc:
            return self._error("Execute date", str(exc))
        floor_keys = {
            (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days, issue.currency.upper())
            for issue in p.cost_floor_issues
        }
        missing_floor = {
            key for key in floor_keys
            if self._pending_cost_floor_choices.get(key) not in {"raise", "override"}
        }
        conflict_keys = {
            (issue.campaign_id, issue.iso.upper(), issue.plan, issue.days)
            for issue in p.collision_issues
        }
        missing_conflicts = {
            key for key in conflict_keys
            if self._pending_conflict_choices.get(key) not in {"overwrite", "keep"}
        }
        if missing_floor or missing_conflicts:
            self._batch_issue_choices_dialog(p)
            self.statusBar().showMessage("Save every issue decision, then click Apply changes again.", 8000)
            return
        floor_choices = {key: self._pending_cost_floor_choices[key] for key in floor_keys}
        # Include saved resolutions as well as currently unresolved keys. A saved
        # overwrite/keep makes the corresponding collision disappear from preview.
        conflict_choices = dict(self._pending_conflict_choices)

        self.apply_date_btn.setEnabled(False)
        self.preview_date_btn.setEnabled(False)
        self.undo_btn.setEnabled(False)
        dialog = ApplyProgressDialog(self)
        dialog.setWindowTitle("Applying promo handover")
        dialog.setModal(True)
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        label = QLabel("Starting safe price update…")
        label.setWordWrap(True)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        layout.addWidget(label)
        layout.addWidget(bar)
        dialog.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self._apply_progress_dialog = dialog
        self._apply_progress_label = label
        self._apply_progress_bar = bar
        self._apply_worker = BatchApplyWorker(
            self.timeline, d, actions, floor_choices, conflict_choices,
            self.store.list_campaigns(), self,
        )
        self._active_apply_date = d
        self._apply_actions = list(actions)
        self._apply_worker.progress.connect(self._handle_apply_progress)
        self._apply_worker.succeeded.connect(self._finish_apply_worker)
        self._apply_worker.failed.connect(self._fail_apply_batch)
        self._apply_worker.finished.connect(self._restore_apply_controls)
        dialog.show()
        self._apply_worker.start()

    @staticmethod
    def _update_apply_progress(label, bar, message, value, maximum):
        label.setText(message)
        bar.setRange(0, maximum)
        if maximum:
            bar.setValue(value)

    @Slot(str, int, int)
    def _handle_apply_progress(self, message, value, maximum):
        self._update_apply_progress(
            self._apply_progress_label, self._apply_progress_bar,
            message, value, maximum,
        )

    @Slot(object)
    def _finish_apply_worker(self, result):
        self._finish_apply_batch(self._active_apply_date, self._apply_actions, result)

    def _finish_apply_batch(self, execution_date, actions, result):
        before, campaign_preview = result
        self._update_apply_progress(
            self._apply_progress_label, self._apply_progress_bar,
            "Recording dated changes…", 0, 0,
        )
        change_set_id = None
        try:
            # Store the campaign layer without editor date overrides. Those
            # already live in the higher priority, optionally expiring layer.
            after = pd.DataFrame(campaign_preview.projected_rows)
            effective_from = (date.fromisoformat(execution_date) + timedelta(days=1)).isoformat()
            change_set_id, changed_fields = self.timeline.record_diff(
                before, after,
                effective_from=effective_from,
                layer="campaign",
                source="planner",
                label=f"Promo Planner execution {execution_date}",
                metadata={"execution_date": execution_date},
            )
            changed_map = {
                (int(item.campaign_id), item.action): int(item.changed_rows)
                for item in campaign_preview.items
            }
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
            executions = []
            for c, action in actions:
                scheduled_date = c.end_date if action == "END" else c.start_date
                detail = f"Date batch {execution_date}"
                if scheduled_date and scheduled_date != execution_date:
                    detail += f"; campaign action scheduled for {scheduled_date}"
                executions.append((int(c.id), action, changed_map.get((int(c.id), action), 0), detail))
            batch_id = self.store.create_batch_with_executions(
                execution_date, str(self.store.path), "", details, executions,
                timeline_change_set_id=change_set_id,
            )
        except Exception as exc:
            try:
                if change_set_id:
                    self.timeline.undo_change_set(change_set_id)
            except Exception as rollback_exc:
                self._dismiss_apply_progress()
                return self._error(
                    "Apply needs recovery",
                    f"Timeline bookkeeping failed: {exc}\nAutomatic rollback failed: {rollback_exc}",
                )
            self._dismiss_apply_progress()
            return self._error("Apply rolled back", f"The timeline change was reversed because saving the batch record failed.\n\n{exc}")

        self._pending_cost_floor_choices = {}
        self._pending_conflict_choices = {}
        saved_choices = self._issue_choices_path(execution_date)
        if saved_choices.exists():
            saved_choices.unlink()
        self._refresh_campaigns()
        self._update_apply_progress(
            self._apply_progress_label, self._apply_progress_bar,
            "Saved successfully. Updating the shared price view…", 100, 100,
        )
        self._dismiss_apply_progress()
        if callable(self.on_timeline_changed):
            self.on_timeline_changed()
        editor_started = self._launch_pricing_editor()
        extra = ""
        editor_message = (
            "The open pricing editor now shows the updated shared timeline."
            if self.embedded else
            "Pricing Editor launch started so you can create the partner pack."
            if editor_started else
            "The apply is saved. Open Pricing Editor manually to create the partner pack."
        )
        self._info(
            "Changes applied",
            f"The handover for {execution_date} is saved in the shared pricing timeline.\n\n"
            f"Rows changed: {sum(changed_map.values())}{extra}\n"
            f"Timeline fields changed: {changed_fields}\nBatch: #{batch_id}\n\n"
            + editor_message,
        )

    @Slot(str)
    def _fail_apply_batch(self, message):
        self._dismiss_apply_progress()
        self._error("Apply failed", f"No campaign change was recorded in the pricing timeline.\n\n{message}")

    @Slot()
    def _restore_apply_controls(self):
        self._dismiss_apply_progress()
        self._refresh_execution_date()
        self._apply_worker = None
        self._active_apply_date = None
        self._apply_actions = None

    def _dismiss_apply_progress(self):
        if self._apply_progress_dialog:
            self._apply_progress_dialog.can_close = True
            self._apply_progress_dialog.close()
            self._apply_progress_dialog.deleteLater()
            self._apply_progress_dialog = None
        self._apply_progress_label = None
        self._apply_progress_bar = None
        self._active_apply_date = None
        self._apply_actions = None

    def closeEvent(self, event):
        if self._apply_worker is not None and self._apply_worker.isRunning():
            event.ignore()
            self.statusBar().showMessage("Please wait for the timeline update to finish before closing the planner.")
            return
        super().closeEvent(event)

    def _launch_pricing_editor(self):
        if self.embedded:
            return True
        parent_tabs = self.parentWidget()
        while parent_tabs is not None:
            if self.embedded and isinstance(parent_tabs, QTabWidget):
                parent_tabs.setCurrentIndex(0)
                return True
            parent_tabs = parent_tabs.parentWidget()
        launcher = self.root_dir / "START_HERE" / "2_open_pricing_editor.py"
        if not launcher.exists():
            self.statusBar().showMessage(f"Pricing Editor launcher was not found: {launcher}", 12000)
            return False
        try:
            args = {"cwd": str(self.root_dir), "stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                args["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([sys.executable, str(launcher)], **args)
            return True
        except Exception as exc:
            self.statusBar().showMessage(f"Pricing Editor could not be started: {exc}", 12000)
            return False

    def _undo_execution_date(self):
        if self.embedded and callable(self.before_timeline_update) and not self.before_timeline_update():
            return
        d = self._qdate_iso(self.execution_date.date())
        batch = self.store.get_active_batch_for_date(d)
        if not batch:
            return self._info("Undo execution", f"There is no active execution batch for {d}.")
        if self.store.has_later_active_batch(int(batch["id"])):
            return self._error("Undo blocked", "A later date batch has already been executed. Undo the most recent batch first so current prices are not overwritten with an older state.")
        if not self._confirm("Undo date execution", f"Restore manual prices to the exact state before batch #{batch['id']} for {d}?\n\nThis is intended only for correcting an execution mistake."):
            return
        try:
            self.store.undo_batch(int(batch["id"]))
        except Exception as exc:
            return self._error("Undo failed", str(exc))
        if callable(self.on_timeline_changed):
            self.on_timeline_changed()
        self._refresh_campaigns()
        self._info("Execution undone", f"Batch #{batch['id']} for {d} was undone in the shared pricing timeline.")

    def _preview_selected(self, action: str):
        try:
            p = preview_campaign(self.timeline.project(), self._selected_campaign(), action)
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

    def _export_promotion_calendar(self):
        suggested = self.root_dir / "outputs" / "promo_planner" / "promotion_calendar.html"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export promotion calendar",
            str(suggested),
            "HTML files (*.html *.htm)",
        )
        if not path:
            return
        out_path = Path(path)
        if out_path.suffix.lower() not in {".html", ".htm"}:
            out_path = out_path.with_suffix(".html")
        try:
            generate_promotion_calendar(self.store, out_path, self.timeline.project())
        except Exception as exc:
            return self._error("Export promotion calendar", str(exc))
        QMessageBox.information(
            self,
            "Promotion calendar exported",
            f"Standalone calendar created:\n{out_path}\n\nIt contains a read-only snapshot and can be opened offline in a browser.",
        )

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
    """Route direct planner launches to the integrated pricing app shell."""
    try:
        from automation.pricing_editor.main_window import run
    except ModuleNotFoundError as exc:
        if exc.name != "automation":
            raise
        from pricing_editor.main_window import run
    run(initial_workspace="campaigns")


if __name__ == "__main__":
    main()
