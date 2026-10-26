from __future__ import annotations

import json
import copy
import time
from concurrent.futures import ThreadPoolExecutor
import re
from pathlib import Path
import pandas as pd
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QDesktopServices, QFont, QPen, QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QFormLayout,
    QLineEdit,
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
    QMenu,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QScrollArea,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .canvas import PriceCurveCanvas
from .state import EditorState, load_promos, load_table
from .timeline_views import DateViewCache
from .key_date_strip import KeyDateStrip
try:
    from automation.promo_planner.issue_review import review_summary
except ImportError:
    from promo_planner.issue_review import review_summary
from .key_date_strip_model import build_date_markers
try:
    from automation.pricing_timeline import PricingTimeline
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from pricing_timeline import PricingTimeline
try:
    from automation.pricing_book import write_pricing_workbook
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from pricing_book import write_pricing_workbook
try:
    from automation.promo_planner.date_price_changes import (
        key_dates as scheduled_key_dates,
        project_dataframe as project_pricebook_for_key_date,
        remove_key_date_overrides,
        save_key_date_changes,
    )
    from automation.promo_planner.store import PromoStore
    from automation.promo_planner.timeline_labels import campaign_transition, today_campaign_transition, campaign_presentation, campaign_context_ids, key_date_read_only
except ModuleNotFoundError as exc:
    # The START_HERE launcher runs the editor with automation/ as sys.path[0].
    # In that context, Promo Planner is imported as a top-level package.
    if exc.name not in {"automation", "automation.promo_planner"}:
        raise
    from promo_planner.date_price_changes import (
        key_dates as scheduled_key_dates,
        project_dataframe as project_pricebook_for_key_date,
        remove_key_date_overrides,
        save_key_date_changes,
    )
    from promo_planner.store import PromoStore
    from promo_planner.timeline_labels import campaign_transition, today_campaign_transition, campaign_presentation, campaign_context_ids, key_date_read_only
try:
    from partner_export_pack import build_partner_price_pack
except ImportError:
    from automation.partner_export_pack import build_partner_price_pack
try:
    from plan_labels import display_plan_label, partner_display_plan_label
except ImportError:
    from automation.plan_labels import display_plan_label, partner_display_plan_label
try:
    from official_fx import get_official_eur_usd
except ImportError:
    from automation.official_fx import get_official_eur_usd
from currency_support import (
    CURRENCIES,
    DEFAULT_CURRENCY,
    LINKED_USD_MODE,
    find_currency_file,
    merge_currency_tables,
    normalize_currency,
)
from pipeline_files import FILES
try:
    from pricing_recommendations import (
        annotate_price_book_with_recommendations,
        generate_recommendations_from_files,
        recommendation_outputs_are_stale,
    )
except ImportError:
    from automation.pricing_recommendations import (
        annotate_price_book_with_recommendations,
        generate_recommendations_from_files,
        recommendation_outputs_are_stale,
    )
try:
    from market_insights import generate_market_insights, market_insights_is_stale
except ImportError:
    from automation.market_insights import generate_market_insights, market_insights_is_stale
try:
    from automation.promo_planner.app import PromoPlanner
except ModuleNotFoundError as exc:
    if exc.name != "automation":
        raise
    from promo_planner.app import PromoPlanner


BASE_DIR = FILES.base_dir
PPG_PATH = FILES.ppg_csv
PROMOS_PATH = FILES.promos_json
SALES_VOLUME_PATH = FILES.sales_master_csv
RECOMMENDATIONS_PATH = FILES.work_dir / "pricing_recommendations" / "recommendations_latest.csv"
MARKET_INSIGHTS_PATH = FILES.work_dir / "pricing_recommendations" / "market_insights_latest.html"
MAX_SAVED_EXPORT_DROPDOWN_DATES = None


class MainWindow(QMainWindow):
    def __init__(self, initial_workspace: str = "prices"):
        super().__init__()
        self.setWindowTitle(("DEV ? " if (BASE_DIR / "outputs" / ".development_workspace").exists() else "") + "T-Travel Pricing & Campaigns")
        self.resize(1620, 960)

        self.state = EditorState()
        self.timeline = PricingTimeline(FILES.work_dir / "promo_planner" / "promo_schedule.db")
        self._price_view_cache = DateViewCache(
            self.timeline.path, FILES.work_dir / "promo_planner" / "issue_choices"
        )
        self.current_drag_mode = "inflate"
        self.mode_buttons: dict[str, QToolButton] = {}
        self.autosave_dirty = False
        self.autosave_in_progress = False
        self._save_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pricing-save")
        self._save_future = None
        self._edit_revision = 0
        self._last_edit_at = 0.0
        self._dirty_since = 0.0
        self._manual_pricebook_df = pd.DataFrame()
        self._manual_pricebook_path: Path | None = None
        self._active_key_date: str | None = None
        self.initial_workspace = initial_workspace
        self._date_view_baseline_df = pd.DataFrame()
        self._date_view_loaded_df = pd.DataFrame()
        self._switching_price_view = False
        self._promotion_review_summary = None
        self._promotion_review_key = None
        self._build_ui()
        self.statusBar().showMessage("Opening editor...")
        QTimer.singleShot(100, self.try_auto_load)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.autosave_timer.start(1000)  # Poll worker; save after idle, never during a drag.

        self.save_shortcut = QShortcut(QKeySequence.Save, self)
        self.save_shortcut.activated.connect(self.quick_save)
        self.currency_shortcut = QShortcut(QKeySequence("Ctrl+E"), self)
        self.currency_shortcut.setContext(Qt.ApplicationShortcut)
        self.currency_shortcut.activated.connect(self.toggle_active_currency)
        self.currency_letter_shortcut = QShortcut(QKeySequence("C"), self)
        self.currency_letter_shortcut.setContext(Qt.ApplicationShortcut)
        self.currency_letter_shortcut.activated.connect(self.toggle_active_currency)
        self.toggle_price_labels_shortcut = QShortcut(QKeySequence("Q"), self)
        self.toggle_price_labels_shortcut.setContext(Qt.ApplicationShortcut)
        self.toggle_price_labels_shortcut.activated.connect(self.toggle_canvas_price_labels)
        self.toggle_competitors_shortcut = QShortcut(QKeySequence("S"), self)
        self.toggle_competitors_shortcut.setContext(Qt.ApplicationShortcut)
        self.toggle_competitors_shortcut.activated.connect(self.toggle_canvas_competitors)
        self.reset_zoom_shortcut = QShortcut(QKeySequence("H"), self)
        self.reset_zoom_shortcut.setContext(Qt.ApplicationShortcut)
        self.reset_zoom_shortcut.activated.connect(self.reset_canvas_zoom)
        self.reset_package_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.reset_package_shortcut.setContext(Qt.ApplicationShortcut)
        self.reset_package_shortcut.activated.connect(self.reset_current_package_to_loaded)
        self.clear_busy_cursor()

    def mark_dirty(self) -> None:
        was_dirty = self.autosave_dirty
        self._promotion_review_summary = None
        if not was_dirty and hasattr(self, "campaign_preview_btn"):
            self._refresh_campaign_review_status()
        self.autosave_dirty = bool(self._active_key_date)
        self._edit_revision = getattr(self, "_edit_revision", 0) + 1
        self._last_edit_at = time.monotonic()
        if not was_dirty:
            self._dirty_since = self._last_edit_at

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        layout = QHBoxLayout(root)

        self.toggle_side_btn = QPushButton("◀")
        self.toggle_side_btn.setFixedWidth(28)
        self.toggle_side_btn.setToolTip("Hide or show left panel")
        self.toggle_side_btn.clicked.connect(self.toggle_left_panel)
        self._left_panel_collapsed = False
        layout.addWidget(self.toggle_side_btn)

        self.splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(self.splitter)

        self.side_panel = QScrollArea()
        self.side_panel.setMinimumWidth(0)
        self.side_panel.setWidgetResizable(True)
        self.side_panel.setFrameShape(QFrame.NoFrame)

        self.side_panel_content = QWidget()
        self.side_panel_content.setObjectName("SidePanelContent")
        side_layout = QVBoxLayout(self.side_panel_content)

        self.side_panel.setWidget(self.side_panel_content)

        self.country_combo = QComboBox()
        self.country_combo.currentTextChanged.connect(self.on_country_changed)

        self.impact_label = QLabel("—")
        self.total_impact_label = QLabel("—")

        self.impact_label.setStyleSheet("color: #999999;")
        self.total_impact_label.setStyleSheet("color: #999999;")

        self.unit_last_month_label = QLabel("—")
        self.total_last_month_label = QLabel("—")
        self.unit_projected_label = QLabel("—")
        self.total_projected_label = QLabel("—")

        for lbl in [
            self.impact_label,
            self.total_impact_label,
            self.unit_last_month_label,
            self.total_last_month_label,
            self.unit_projected_label,
            self.total_projected_label,
        ]:
            lbl.setStyleSheet("color: #999999;")

        self.key_date_combo = QComboBox()
        self.key_date_combo.addItem("Today / current prices", "")
        self.key_date_combo.setEnabled(False)
        self.key_date_combo.setMinimumWidth(300)
        self.key_date_combo.setToolTip("Choose current prices or a scheduled campaign or price-change date.")
        self.key_date_combo.setStyleSheet(
            "QComboBox { background:#303842; color:#FFFFFF; border:1px solid #AAB3BE; border-radius:5px; "
            "padding:5px 8px; min-height:24px; }"
            "QComboBox:hover { background:#3B4652; }"
            "QComboBox::drop-down { border:0; width:30px; background:#242B33; border-top-right-radius:5px; border-bottom-right-radius:5px; }"
            "QComboBox QAbstractItemView { background:#242B33; color:#F4F6F8; "
            "selection-color:#FFFFFF; selection-background-color:#8E1454; border:1px solid #727D89; "
            "outline:0; padding:3px; }"
            "QComboBox QAbstractItemView::item { min-height:28px; padding:4px 8px; }"
        )
        self.key_date_combo.setMaxVisibleItems(8)
        self.key_date_combo.view().setMaximumHeight(280)
        self.key_date_combo.currentIndexChanged.connect(self.on_key_date_view_changed)
        self.key_date_combo.setMinimumWidth(300)
        self.key_date_combo.setMaximumWidth(480)
        self.key_date_combo.setMinimumContentsLength(28)
        self.key_date_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.key_date_combo.view().setMinimumWidth(560)
        self.key_date_combo.setToolTip(
            "Shared chronological timeline: choose a campaign start/end or a planned price change."
        )
        self.currency_combo = QComboBox()
        self.currency_combo.addItems(list(CURRENCIES))
        self.currency_combo.setCurrentText(self.state.active_currency)
        self.currency_combo.currentTextChanged.connect(self.on_currency_changed)
        self.country_combo.setMinimumWidth(190)
        self.country_combo.setMaximumWidth(280)
        self.country_combo.setMinimumContentsLength(18)
        self.country_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.country_combo.setToolTip("Choose the destination shown in the pricing curve while working in Pricing or Campaigns.")
        self.country_combo.setAccessibleName("Timeline destination")
        self.currency_combo.setFixedWidth(76)
        self.currency_combo.setToolTip("Choose the currency shown in the pricing curve and sales impact.")
        self.currency_combo.setAccessibleName("Timeline currency")

        self.date_toolbar = QToolBar("Shared pricing and campaign timeline", self)
        self.date_toolbar.setMovable(False)
        self.date_toolbar.setFloatable(False)
        self.date_toolbar.addWidget(QLabel("Destination"))
        self.date_toolbar.addWidget(self.country_combo)
        self.date_toolbar.addWidget(self.currency_combo)
        self.timeline_section_divider = QLabel("|")
        self.timeline_section_divider.setAlignment(Qt.AlignCenter)
        self.timeline_section_divider.setFixedWidth(26)
        self.timeline_section_divider.setStyleSheet("color:#8A939E; font-size:24px; font-weight:600; padding:0 5px;")
        self.date_toolbar.addWidget(self.timeline_section_divider)
        self.date_toolbar.addWidget(QLabel("Timeline"))
        self.date_toolbar.addSeparator()
        self.date_toolbar.addWidget(self.key_date_combo)
        self.overview_toggle_btn = QToolButton()
        self.overview_toggle_btn.setArrowType(Qt.DownArrow)
        self.overview_toggle_btn.setCheckable(True)
        self.overview_toggle_btn.setChecked(True)
        self.overview_toggle_btn.setToolTip("Hide the key-date overview")
        self.overview_toggle_btn.setAccessibleName("Show or hide key-date overview")
        self.overview_toggle_btn.toggled.connect(self._toggle_key_date_overview)
        self.date_toolbar.addWidget(self.overview_toggle_btn)
        self.date_toolbar.addSeparator()
        self.add_timeline_date_btn = QToolButton()
        self.add_timeline_date_btn.setText("＋ Add dated change")
        self.add_timeline_date_btn.setMinimumWidth(185)
        self.add_timeline_date_btn.setMinimumHeight(34)
        self.add_timeline_date_btn.setPopupMode(QToolButton.InstantPopup)
        self.add_timeline_date_btn.setToolTip("Add a promotion or a price-change date to the shared timeline.")
        add_date_menu = QMenu(self.add_timeline_date_btn)
        add_promotion_action = add_date_menu.addAction("Add promotion…")
        add_price_change_action = add_date_menu.addAction("Add price-change date…")
        add_promotion_action.triggered.connect(self._add_timeline_promotion)
        add_price_change_action.triggered.connect(self._add_timeline_price_change)
        self.add_timeline_date_btn.setMenu(add_date_menu)
        self.date_toolbar.addWidget(self.add_timeline_date_btn)
        self.delete_timeline_date_btn = QToolButton()
        self.delete_timeline_date_btn.setText("Delete…")
        self.delete_timeline_date_btn.setToolTip("Cancel a future promotion or price change on the selected date.")
        self.delete_timeline_date_btn.clicked.connect(self._delete_timeline_event)
        self.delete_timeline_date_btn.setEnabled(False)
        self.date_toolbar.addWidget(self.delete_timeline_date_btn)
        self.move_timeline_date_btn = QToolButton()
        self.move_timeline_date_btn.setText("Move start date…")
        self.move_timeline_date_btn.setToolTip("Choose a planned price change or promotion and move its start using a calendar.")
        self.move_timeline_date_btn.clicked.connect(self._move_timeline_event)
        self.move_timeline_date_btn.setEnabled(False)
        self.date_toolbar.addWidget(self.move_timeline_date_btn)
        self.timeline_preview_btn = QToolButton()
        self.timeline_preview_btn.setText("Preview & resolve")
        self.timeline_preview_btn.setToolTip("Review before/after prices and resolve promotion overlaps or cost-floor issues for the selected date.")
        self.timeline_preview_btn.clicked.connect(self._preview_timeline_changes)
        self.timeline_preview_btn.setEnabled(False)
        self.date_toolbar.addWidget(self.timeline_preview_btn)
        self.date_toolbar.addSeparator()
        self.pricing_toggle_btn = QToolButton()
        self.pricing_toggle_btn.setText("Pricing")
        self.pricing_toggle_btn.setToolTip("Show pricing controls and fold the campaign panel")
        self.pricing_toggle_btn.setCheckable(True)
        self.pricing_toggle_btn.setChecked(True)
        self.pricing_toggle_btn.clicked.connect(self.open_pricing_panel)
        self.date_toolbar.addWidget(self.pricing_toggle_btn)
        self.campaigns_toggle_btn = QToolButton()
        self.campaigns_toggle_btn.setText("Campaigns")
        self.campaigns_toggle_btn.setToolTip("Show or fold the right campaign panel")
        self.campaigns_toggle_btn.setCheckable(True)
        self.campaigns_toggle_btn.clicked.connect(self.open_promo_planner)
        self.date_toolbar.addWidget(self.campaigns_toggle_btn)
        self.addToolBar(Qt.TopToolBarArea, self.date_toolbar)
        self.key_date_status = QLabel("Load current prices to browse scheduled dates.")
        self.key_date_status.setWordWrap(True)
        self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        self.country_combo.setStyleSheet(
            "QComboBox { background:#3D424A; color:#FFFFFF; border:1px solid #FFFFFF; border-radius:5px; "
            "padding:5px 8px; min-height:24px; }"
            "QComboBox:hover { background:#4A5059; }"
            "QComboBox::drop-down { border:0; width:24px; }"
            "QComboBox QAbstractItemView { background:#3D424A; color:#FFFFFF; "
            "selection-color:#FFFFFF; selection-background-color:#59616B; }"
        )

        self.addToolBarBreak(Qt.TopToolBarArea)
        self.overview_toolbar = QToolBar("Key-date overview", self)
        self.overview_toolbar.setMovable(False)
        self.overview_toolbar.setFloatable(False)
        self.overview_toolbar.setContentsMargins(0, 0, 0, 0)
        self.overview_toolbar.setStyleSheet("QToolBar { border:0; spacing:0; padding:0; background:#F5F6F9; }")
        self.key_date_strip = KeyDateStrip(self)
        self.key_date_strip.dateActivated.connect(self._activate_overview_date)
        self.overview_toolbar.addWidget(self.key_date_strip)
        self.addToolBar(Qt.TopToolBarArea, self.overview_toolbar)

        self.dual_currency_check = QCheckBox("Edit USD/EUR together")
        self.dual_currency_check.setChecked(self.state.currency_mode == LINKED_USD_MODE)
        self.dual_currency_check.stateChanged.connect(self.on_currency_mode_changed)

        self.exchange_rate_spin = QDoubleSpinBox()
        self.exchange_rate_spin.setDecimals(4)
        self.exchange_rate_spin.setRange(0.1000, 5.0000)
        self.exchange_rate_spin.setSingleStep(0.0100)
        self.exchange_rate_spin.setValue(float(self.state.eur_to_usd))
        self.exchange_rate_spin.valueChanged.connect(self.on_exchange_rate_changed)

        self.official_cost_rate_label = QLabel("Loading official rate...")

        currency_grid = QGridLayout()
        currency_grid.setHorizontalSpacing(6)
        currency_grid.setVerticalSpacing(4)
        currency_grid.addWidget(QLabel("Pricing EUR/USD"), 0, 0)
        currency_grid.addWidget(self.exchange_rate_spin, 0, 1)
        currency_grid.addWidget(QLabel("Official cost EUR/USD"), 1, 0)
        currency_grid.addWidget(self.official_cost_rate_label, 1, 1)
        currency_grid.addWidget(self.dual_currency_check, 2, 0, 1, 2)

        self.currency_mode_banner = QLabel("")
        self.currency_mode_banner.setAlignment(Qt.AlignCenter)
        self.currency_mode_banner.setStyleSheet(
            "font-weight: bold; color: #f7f8fa; background-color: #30343b; "
            "border: 1px solid #666d78; border-left: 5px solid #d6a400; "
            "border-radius: 4px; padding: 6px;"
        )
        self.currency_mode_banner.setVisible(False)

        # Drag tools
        mode_row_1 = QHBoxLayout()
        mode_row_2 = QHBoxLayout()
        mode_row_3 = QHBoxLayout()

        mode_specs = [
            ("inflate", "Inflate"),
            ("shift_abs_up", "+Abs"),
            ("shift_pct_up", "+%"),
            ("rotate_left", "Rotate L"),
            ("rotate_right", "Rotate R"),
            ("rotate_both", "Rotate B"),
            ("concave", "Concave"),
            ("neighbors", "Neighbors"),
            ("move", "Move"),
        ]

        tooltips = {
                "inflate": "Drag one point to bulge the whole curve.",
                "shift_abs_up": "Drag to shift the selected plan by an absolute amount.",
                "shift_pct_up": "Drag to shift the selected plan by a percentage.",
                "rotate_left": "Select pivot point, then drag to rotate the left side.",
                "rotate_right": "Select pivot point, then drag to rotate the right side.",
                "rotate_both": "Select pivot point, then drag to rotate both sides.",
                "concave": "Drag one point to make a smooth concave day 1 to day 30 curve.",
                "neighbors": "Drag one point and softly move nearby points.",
                "move": "Move only the selected point.",
            }

        for i, (key, label) in enumerate(mode_specs):
            btn = QToolButton()
            btn.setText(label)
            btn.setCheckable(True)
            btn.setFixedSize(76, 34)
            btn.clicked.connect(lambda checked, k=key: self.set_drag_mode(k))
            self.mode_buttons[key] = btn

            # IF TOOL TIPS NEEDED #
            # btn.setToolTip(tooltips.get(key, label))
            # btn.setToolTipDuration(8000)
            # btn.setMouseTracking(True)

            if i < 3:
                mode_row_1.addWidget(btn)
            elif i < 6:
                mode_row_2.addWidget(btn)
            else:
                mode_row_3.addWidget(btn)

        self.mode_buttons["inflate"].setChecked(True)

        self.drag_safety_label = QLabel("Hold SHIFT while dragging.")
        self.drag_safety_label.setWordWrap(True)
        self.drag_safety_label.setStyleSheet(
            "font-weight: bold; color: #8a4b08; background: #fff4df; "
            "border: 1px solid #e6b566; border-radius: 4px; padding: 5px;"
        )

        self.open_market_insights_btn = QPushButton("Open market insights")
        self.open_market_insights_btn.setFixedHeight(32)
        self.open_market_insights_btn.setToolTip("Open the latest competitor-market movement dashboard in your browser.")
        self.open_market_insights_btn.clicked.connect(self.open_market_insights)

        # Action buttons
        reset_zoom_btn = QPushButton("Home")
        reset_zoom_btn.setFixedHeight(38)

        reset_plan_btn = QPushButton("↩ Reset plan")
        reset_plan_btn.setToolTip("Restore the selected plan to the prices and promos loaded when this price book was opened.")
        reset_plan_btn.clicked.connect(self.use_loaded_for_selected_plan)

        reset_unit_btn = QPushButton("↩ Reset pricing unit")
        reset_unit_btn.setToolTip("Restore every plan in the selected pricing unit to the loaded price-book state.")
        reset_unit_btn.clicked.connect(self.use_loaded_for_pricing_unit)

        reset_grid = QGridLayout()
        reset_grid.setHorizontalSpacing(6)
        reset_grid.setVerticalSpacing(6)
        reset_grid.addWidget(reset_plan_btn, 0, 0)
        reset_grid.addWidget(reset_unit_btn, 0, 1)

        self.export_btn = QPushButton("Export Excel…")
        self.export_btn.setToolTip("Export the selected Timeline date to an Excel you can edit and import back.")
        self.export_btn.clicked.connect(self._export_timeline_excel)
        self.partner_pack_btn = QPushButton("Create partner pack…")
        self.partner_pack_btn.setToolTip("Create an official partner pack for the selected Timeline date.")
        self.partner_pack_btn.clicked.connect(self._create_timeline_partner_pack)
        self.import_excel_btn = QPushButton("Import Excel…")
        self.import_excel_btn.setToolTip("Import edited list prices into the selected Timeline date; confirmation shows the target date.")
        self.import_excel_btn.clicked.connect(self.load_pricebook)

        self.remove_promo_btn = QPushButton("❌ Remove selected promo")
        self.remove_promo_btn.setMinimumHeight(38)
        self.remove_promo_btn.setStyleSheet("QPushButton { color:#a71930; background:#fff0f2; border:1px solid #d96375; border-radius:6px; font-weight:700; padding:8px; } QPushButton:hover { background:#ffdde3; } QPushButton:disabled { color:#999; border-color:#ddd; background:#f5f5f5; }")
        self.remove_promo_btn.setToolTip(
            "Select a price point, then remove its manual promo and any shorter-duration "
            "adjustments linked to it. Scheduled campaigns are managed in Campaigns."
        )
        self.remove_promo_btn.clicked.connect(self.remove_selected_promo)

        # Info widgets
        self.country_info_label = QLabel("No country loaded")
        self.country_info_label.setWordWrap(True)

        # self.tool_help_label = QLabel(
        #     "How to use tools:\n"
        #     "• Inflate: drag one point to bulge the whole curve.\n"
        #     "• +Abs: shift selected plan by an absolute amount.\n"
        #     "• +%: shift selected plan by a percentage.\n"
        #     "• Rotate L/R/B: select pivot point, then drag.\n"
        #     "• Brush: set start + end, then drag inside the span."
        # )
        # self.tool_help_label.setWordWrap(True)

        self.selection_label = QLabel("No point selected")
        self.selection_label.setWordWrap(True)
        self.region_floor_label = QLabel("")
        self.region_floor_label.setWordWrap(True)
        self.region_floor_label.setTextFormat(Qt.RichText)
        self.region_floor_label.setStyleSheet(
            "padding: 6px; border: 1px solid #e3e3e3; border-radius: 4px; "
            "background: #fcfcfc; color: #333333;"
        )
        self.region_floor_label.setVisible(False)

        # Sales impact stays visible above the pricing controls.
        impact_box = QWidget()
        impact_box.setObjectName("SalesImpactBox")
        impact_box.setStyleSheet("""
            QWidget#SalesImpactBox {
                border: 1px solid #cccccc;
                border-radius: 6px;
                background-color: #fafafa;
            }
            QWidget#SalesImpactBox QLabel {
                border: none;
                background: transparent;
                color: #333333;
            }
        """)

        impact_layout = QGridLayout(impact_box)
        impact_layout.setContentsMargins(7, 5, 7, 5)
        impact_layout.setHorizontalSpacing(5)
        impact_layout.setVerticalSpacing(2)
        impact_layout.setColumnStretch(0, 2)
        impact_layout.setColumnStretch(1, 2)
        impact_layout.setColumnStretch(2, 2)
        impact_layout.setColumnStretch(3, 2)

        self.impact_title = QLabel("30-day sales impact")
        self.impact_title.setStyleSheet("font-weight: bold; color: #333333;")
        impact_layout.addWidget(self.impact_title, 0, 0, 1, 4)

        header_style = "font-size: 10px; color: #666666;"
        for col, text in enumerate(("Scope", "Actual 30d", "Δ top line", "Simulated")):
            lbl = QLabel(text)
            lbl.setStyleSheet(header_style)
            impact_layout.addWidget(lbl, 1, col)

        selected_scope = QLabel("Selected")
        selected_scope.setToolTip("Currently selected destination / pricing scope")
        portfolio_scope = QLabel("Portfolio")
        portfolio_scope.setToolTip("All destinations")
        impact_layout.addWidget(selected_scope, 2, 0)
        impact_layout.addWidget(self.unit_last_month_label, 2, 1)
        impact_layout.addWidget(self.impact_label, 2, 2)
        impact_layout.addWidget(self.unit_projected_label, 2, 3)
        impact_layout.addWidget(portfolio_scope, 3, 0)
        impact_layout.addWidget(self.total_last_month_label, 3, 1)
        impact_layout.addWidget(self.total_impact_label, 3, 2)
        impact_layout.addWidget(self.total_projected_label, 3, 3)

        pricing_controls = QWidget()
        controls_layout = QVBoxLayout(pricing_controls)
        controls_layout.setContentsMargins(6, 6, 6, 6)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(self.country_info_label)
        controls_layout.addWidget(QLabel("Curve tools"))
        controls_layout.addWidget(self.drag_safety_label)
        controls_layout.addLayout(mode_row_1)
        controls_layout.addLayout(mode_row_2)
        controls_layout.addLayout(mode_row_3)
        controls_layout.addWidget(self.open_market_insights_btn)
        controls_layout.addWidget(self.selection_label)
        controls_layout.addWidget(self.remove_promo_btn)
        controls_layout.addWidget(self.region_floor_label)
        controls_layout.addWidget(reset_zoom_btn)
        controls_layout.addLayout(reset_grid)
        controls_layout.addStretch(1)


        campaigns_tab = QWidget()
        campaigns_tab.setMinimumWidth(0)
        campaigns_tab.setStyleSheet("QWidget { font-size: 8pt; } QPushButton { padding: 3px 5px; }")
        campaigns_layout = QVBoxLayout(campaigns_tab)
        campaigns_layout.setContentsMargins(6, 6, 6, 6)
        campaigns_layout.setSpacing(6)
        self.campaign_preview_btn = QPushButton("Preview & resolve")
        self.campaign_preview_btn.setMinimumHeight(36)
        self.campaign_preview_btn.clicked.connect(self._preview_timeline_changes)
        self.campaign_review_status = QLabel("Choose a date in Timeline to review promo issues.")
        self.campaign_review_status.setWordWrap(True)
        self.campaign_calendar_btn = QPushButton("🖨 Calendar HTML")
        self.campaign_calendar_btn.setToolTip("Export a detailed, printable campaign calendar from today onwards.")
        self.campaign_calendar_btn.clicked.connect(self.export_campaign_calendar)
        campaigns_layout.addWidget(self.campaign_calendar_btn)
        campaigns_layout.addWidget(self.campaign_preview_btn)
        campaigns_layout.addWidget(self.campaign_review_status)
        self._refresh_campaign_review_status()

        campaign_timeline_title = QLabel("Campaign timeline · dates follow the shared selector above")
        campaign_timeline_title.setStyleSheet("font-weight:600; color:#343B44;")
        campaigns_layout.addWidget(campaign_timeline_title)
        self.campaign_timeline_scene = QGraphicsScene(self)
        self.campaign_timeline_view = QGraphicsView(self.campaign_timeline_scene)
        self._campaigns_cache = []
        self.campaign_timeline_view.setMinimumHeight(115)
        self.campaign_timeline_view.setMaximumHeight(155)
        self.campaign_timeline_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.campaign_timeline_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.campaign_timeline_view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.campaign_timeline_view.setFrameShape(QFrame.NoFrame)
        campaigns_layout.addWidget(self.campaign_timeline_view)
        campaign_list_title = QLabel("Campaigns · italic = read only · bold = selected date")
        campaign_list_title.setStyleSheet("font-weight:700; color:#252A31;")
        campaigns_layout.addWidget(campaign_list_title)
        self.campaign_list = QListWidget()
        self.campaign_list.setMinimumHeight(110)
        self.campaign_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.campaign_list.currentItemChanged.connect(self._show_campaign_details)
        campaigns_layout.addWidget(self.campaign_list, 2)
        self.campaign_detail_label = QLabel("Select a campaign to see its dates and affected destinations.")
        self.campaign_detail_label.setWordWrap(True)
        campaigns_layout.addWidget(self.campaign_detail_label)
        destinations_title = QLabel("Affected destinations · select to open in editor")
        destinations_title.setStyleSheet("font-weight:700; color:#252A31;")
        campaigns_layout.addWidget(destinations_title)
        self.campaign_destinations_list = QListWidget()
        self.campaign_destinations_list.setMaximumHeight(150)
        self.campaign_destinations_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.campaign_destinations_list.itemClicked.connect(self._open_campaign_destination)
        campaigns_layout.addWidget(self.campaign_destinations_list, 1)
        campaign_actions = QGridLayout()
        campaign_actions.setHorizontalSpacing(4)
        campaign_actions.setVerticalSpacing(4)
        self.refresh_campaigns_btn = QPushButton("Refresh")
        self.refresh_campaigns_btn.clicked.connect(self.refresh_campaign_panel)
        self.edit_campaign_btn = QPushButton("Edit selected")
        self.edit_campaign_btn.clicked.connect(self.edit_selected_campaign)
        self.duplicate_campaign_btn = QPushButton("Duplicate")
        self.duplicate_campaign_btn.clicked.connect(self.duplicate_selected_campaign)
        self.delete_campaign_btn = QPushButton("Delete")
        self.delete_campaign_btn.clicked.connect(self.delete_selected_campaign)
        campaign_actions.addWidget(self.refresh_campaigns_btn, 0, 0)
        campaign_actions.addWidget(self.edit_campaign_btn, 0, 1)
        campaign_actions.addWidget(self.duplicate_campaign_btn, 1, 0)
        campaign_actions.addWidget(self.delete_campaign_btn, 1, 1)
        self.rename_campaign_btn = QPushButton("Rename")
        self.rename_campaign_btn.setEnabled(False)
        self.rename_campaign_btn.clicked.connect(self.rename_selected_campaign)
        campaign_actions.addWidget(self.rename_campaign_btn, 2, 0, 1, 2)
        campaigns_layout.addLayout(campaign_actions)

        campaign_scroll = QScrollArea()
        campaign_scroll.setWidgetResizable(True)
        campaign_scroll.setFrameShape(QFrame.NoFrame)
        campaign_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        campaign_scroll.setWidget(campaigns_tab)
        self.campaigns_panel = campaign_scroll

        # Currency/data and export actions affect both modelling and promos,
        # so keep them outside the tabs. This also avoids duplicating controls
        # and leaves each tab focused on the work that is specific to it.
        global_actions = QGridLayout()
        global_actions.setSpacing(6)
        global_actions.addWidget(self.import_excel_btn, 0, 0)
        global_actions.addWidget(self.export_btn, 0, 1)
        global_actions.addWidget(self.partner_pack_btn, 1, 0, 1, 2)

        side_layout.addWidget(impact_box)
        side_layout.addWidget(QLabel("Currency & data"))
        side_layout.addLayout(currency_grid)
        side_layout.addWidget(self.currency_mode_banner)
        side_layout.addWidget(self.key_date_status)
        side_layout.addLayout(global_actions)
        side_layout.addWidget(pricing_controls, 1)

        # Canvas
        self.canvas = PriceCurveCanvas()
        reset_zoom_btn.clicked.connect(self.canvas.reset_zoom)
        self.canvas.pointSelected.connect(self.on_point_selected)
        self.canvas.pointDragged.connect(self.on_point_dragged)
        self.canvas.priceDragFinished.connect(self.on_price_drag_finished)
        self.canvas.promoSelected.connect(self.on_promo_selected_from_chart)
        self.canvas.manualPromoSelected.connect(self.on_manual_promo_selected_from_chart)
        self.canvas.belowCostApprovalRequested.connect(self.approve_point_below_cost)
        self.canvas.statusChanged.connect(self.statusBar().showMessage)

        self.splitter.addWidget(self.side_panel)
        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([350, 1130])
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)
        self.refresh_campaign_panel()

        self.planner_workspace = None
        self.campaign_dock = None
        self._workspace_mode = "prices"
        self._install_planner_workspace()
        self.key_date_combo.currentIndexChanged.connect(self._sync_planner_date_from_editor)
        self._sync_planner_date_from_editor()

    def _install_planner_workspace(self) -> None:
        try:
            planner = PromoPlanner(parent=self, embedded=True)
            self.planner_workspace = planner
            planner.on_timeline_changed = self._reload_editor_from_shared_timeline
            planner.before_timeline_update = self._confirm_price_view_switch
            planner.on_review_status = self._set_campaign_review_status
            planner.on_destination_selected = self._select_campaign_destination_curve
            planner.campaignsChanged.connect(self._on_campaigns_changed)
            self.campaign_panel = self.campaigns_panel
            if self.campaign_panel is not None:
                self.campaigns_panel = self.campaign_panel
                # The shared-date summary and actions live inside Campaigns, so
                # there is one date context rather than a separate Selected date tab.
                planner.tabs.removeTab(planner.tabs.indexOf(planner.timeline_tab))
                planner.tabs.removeTab(planner.tabs.indexOf(planner.execution_tab))
                campaign_content = self.campaign_panel.widget()
                campaign_layout = campaign_content.layout() if campaign_content else None
                if campaign_layout is not None:
                    campaign_layout.insertWidget(0, planner.timeline_tab)
                planner.tabs.insertTab(0, self.campaign_panel, "Campaigns")
                planner.tabs.setCurrentWidget(self.campaign_panel)
                planner.campaigns_tab = self.campaign_panel
            planner.tabs.setTabText(planner.tabs.indexOf(planner.create_tab), "Edit promotion")
            planner.tabs.currentChanged.connect(self._on_campaign_panel_tab_changed)
            self.campaign_dock = QFrame(self.splitter)
            self.campaign_dock.setObjectName("CampaignWorkspacePanel")
            self.campaign_dock.setStyleSheet(
                "QFrame#CampaignWorkspacePanel { background:#F4F5F7; "
                "border-left:1px solid #C9CDD3; }"
            )
            overlay_layout = QVBoxLayout(self.campaign_dock)
            overlay_layout.setContentsMargins(0, 0, 0, 0)
            overlay_layout.setSpacing(0)
            panel_header = QWidget(self.campaign_dock)
            panel_header.setObjectName("CampaignPanelHeader")
            panel_header.setStyleSheet(
                "QWidget#CampaignPanelHeader { background:#FFFFFF; border-bottom:1px solid #D8DBE0; }"
            )
            panel_header_layout = QHBoxLayout(panel_header)
            panel_header_layout.setContentsMargins(12, 4, 8, 4)
            panel_header_layout.addWidget(QLabel("Campaign workspace"))
            panel_header_layout.addStretch(1)
            pricing_panel_btn = QPushButton("Pricing controls")
            pricing_panel_btn.setToolTip("Close the campaign panel and restore pricing controls")
            pricing_panel_btn.clicked.connect(self.open_pricing_panel)
            panel_header_layout.addWidget(pricing_panel_btn)
            overlay_layout.addWidget(panel_header)
            planner.setWindowFlags(Qt.Widget)
            overlay_layout.addWidget(planner, 1)
            self.splitter.addWidget(self.campaign_dock)
            self.splitter.setCollapsible(2, True)
            self.campaign_dock.hide()
            self._apply_workspace_sizes()
            if self.initial_workspace.casefold() in {"campaign", "campaigns", "planner"}:
                self.open_promo_planner()
        except Exception as exc:
            self.planner_workspace = None
            self.statusBar().showMessage(f"Campaign workspace could not be opened: {exc}", 12000)

    def _retry_planner_workspace(self) -> None:
        if self.planner_workspace is not None:
            return
        self._install_planner_workspace()

    def _apply_workspace_sizes(self) -> None:
        if not hasattr(self, "splitter") or self.splitter.width() <= 0:
            return
        total = self.splitter.width()
        if self._workspace_mode == "campaigns":
            right = round(total * 0.45)
            self.splitter.setSizes([0, total - right, right])
        elif self._workspace_mode == "canvas":
            self.splitter.setSizes([0, total, 0])
        else:
            left = round(total * 0.25)
            self.splitter.setSizes([left, total - left, 0])

    def _set_workspace_mode(self, mode: str) -> None:
        if mode == "campaigns" and (self.autosave_dirty or getattr(self, "_save_future", None) is not None):
            if not self._confirm_price_view_switch():
                return
        self._workspace_mode = mode if mode in {"campaigns", "canvas"} else "prices"
        if self.campaign_dock is not None:
            self.campaign_dock.setVisible(self._workspace_mode == "campaigns")
        self.side_panel.setVisible(self._workspace_mode == "prices")
        self.pricing_toggle_btn.setChecked(self._workspace_mode == "prices")
        self.campaigns_toggle_btn.setChecked(self._workspace_mode == "campaigns")
        self._left_panel_collapsed = self._workspace_mode == "canvas"
        self._apply_workspace_sizes()

    def open_pricing_panel(self) -> None:
        self._set_workspace_mode("prices")

    def _ask_timeline_date(self, promotion: bool = False):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add promotion date" if promotion else "Add price-change date")
        layout = QFormLayout(dialog)
        control = QDateEdit(QDate.currentDate().addDays(1), dialog)
        control.setCalendarPopup(True)
        control.setMinimumDate(QDate.currentDate())
        control.setDisplayFormat("dd MMM yyyy")
        layout.addRow("Promotion starts" if promotion else "Prices take effect", control)
        name = QLineEdit(dialog)
        name.setPlaceholderText("Optional name")
        layout.addRow("Name", name)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        effective = control.date().toPython()
        label = name.text().strip() or (
            f"Promotion {effective:%d %b %Y}" if promotion else f"Price update {effective:%d %b %Y}"
        )
        return effective, label

    def _add_timeline_price_change(self) -> None:
        chosen = self._ask_timeline_date()
        if chosen is None or not self._confirm_price_view_switch():
            return
        effective, label = chosen
        key_date = (effective - timedelta(days=1)).isoformat()
        try:
            self.timeline.schedule_price_change_date(key_date, label)
            self._reload_editor_from_shared_timeline(key_date)
        except Exception as exc:
            QMessageBox.warning(self, "Add price-change date", str(exc))
            return
        self.open_pricing_panel()

    def _add_timeline_promotion(self) -> None:
        chosen = self._ask_timeline_date(promotion=True)
        if chosen is None or not self._confirm_price_view_switch():
            return
        if self.planner_workspace is None:
            self._retry_planner_workspace()
        planner = self.planner_workspace
        if planner is None:
            return
        effective, label = chosen
        key_date = (effective - timedelta(days=1)).isoformat()
        self.timeline.schedule_promotion_date(key_date, label)
        self._reload_editor_from_shared_timeline(key_date)
        planner.begin_campaign_on_date(effective.isoformat(), label=label)
        self._set_workspace_mode("campaigns")
        planner.tabs.setCurrentWidget(planner.create_tab)

    def _move_timeline_event(self) -> None:
        if getattr(self, "_save_future", None) is not None:
            self._finish_background_save(wait=True)
        options = []
        today = date.today()
        for marker in self.timeline.event_markers():
            key = marker["metadata"]["key_date"]
            effective = date.fromisoformat(key) + timedelta(days=1)
            if effective < today or (marker["metadata"].get("official") and effective <= today):
                continue
            kind = "price" if marker["source"] == "price_date" else "draft"
            options.append((kind, marker["id"], key, effective, marker["label"], marker["metadata"].get("official", False)))
        store = self._promo_store()
        for campaign in store.list_campaigns():
            if campaign.status == "ACTIVE_IMPORTED" or not campaign.start_date or campaign.start_date < today.isoformat():
                continue
            if store.has_execution(campaign.id, "START") or store.has_execution(campaign.id, "END"):
                continue
            effective = date.fromisoformat(campaign.start_date)
            options.append(("campaign", campaign.id, (effective - timedelta(days=1)).isoformat(), effective, campaign.name, False))
        options.sort(key=lambda item: (item[3], item[0], item[1]))
        if not options:
            QMessageBox.information(self, "Move dated change", "There are no future price changes or promotions to move.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Move dated change")
        layout = QFormLayout(dialog)
        events = QComboBox(dialog)
        events.setMinimumWidth(400)
        for kind, _id, _key, effective, label, _official in options:
            events.addItem(f"{effective:%d %b %Y} · {'Price change' if kind == 'price' else 'Promotion'} · {label}")
        chosen_date = QDateEdit(QDate.currentDate(), dialog)
        chosen_date.setMinimumDate(QDate.currentDate())
        chosen_date.setCalendarPopup(True)
        chosen_date.setDisplayFormat("dd MMM yyyy")
        keep_duration = QCheckBox("Keep promotion duration (move its end too)", dialog)
        keep_duration.setChecked(True)
        note = QLabel("An official price change moved to another date becomes a draft and needs a new official export.", dialog)
        note.setWordWrap(True)
        layout.addRow("Change", events)
        layout.addRow("Move to", chosen_date)
        layout.addRow(keep_duration)
        layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.button(QDialogButtonBox.Ok).setText("Move")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        def select_event(index):
            item = options[index]
            chosen_date.setDate(QDate(item[3].year, item[3].month, item[3].day))
            keep_duration.setVisible(item[0] != "price")
            note.setVisible(item[0] == "price" and bool(item[5]))
        events.currentIndexChanged.connect(select_event)
        initial = next((i for i, item in enumerate(options) if item[2] == self._active_key_date), 0)
        events.setCurrentIndex(initial)
        select_event(initial)
        if dialog.exec() != QDialog.Accepted:
            return
        kind, event_id, old_key, old_effective, _label, _official = options[events.currentIndex()]
        effective = chosen_date.date().toPython()
        if effective == old_effective:
            return
        try:
            if not self._confirm_price_view_switch():
                return
            if kind == "price":
                new_key = self.timeline.move_price_change_date(old_key, effective.isoformat())
            elif kind == "draft":
                new_key = self.timeline.move_promotion_date(old_key, effective.isoformat())
            else:
                moved = store.move_campaign_start(event_id, effective.isoformat(), keep_duration=keep_duration.isChecked())
                new_key = (effective - timedelta(days=1)).isoformat()
            planner = self.planner_workspace
            if planner is not None:
                editing = (kind == "campaign" and planner.editing_campaign_id == event_id)
                drafting = (kind == "draft" and planner._draft_timeline_key_date == old_key)
                if editing or drafting:
                    planner._timeline_start_date = effective.isoformat()
                    if drafting:
                        planner._draft_timeline_key_date = new_key
                    planner.start_date[1].setDate(QDate(effective.year, effective.month, effective.day))
                    if keep_duration.isChecked():
                        planner.end_date[1].setDate(planner.end_date[1].date().addDays((effective - old_effective).days))
                if kind == "campaign":
                    planner._sync_outlook(event_id)
            self._clear_moved_issue_choices(old_key, new_key, campaign_id=event_id if kind == "campaign" else None)
            self._reload_editor_from_shared_timeline(new_key)
            if kind == "price":
                self.open_pricing_panel()
            self.statusBar().showMessage(f"Moved to {effective:%d %b %Y}. Preview the changes to review conflicts and cost floors.", 12000)
        except Exception as exc:
            QMessageBox.warning(self, "Move dated change", str(exc))

    def _toggle_key_date_overview(self, visible: bool) -> None:
        self.overview_toolbar.setVisible(visible)
        self.overview_toggle_btn.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.overview_toggle_btn.setToolTip("Hide the key-date overview" if visible else "Show the key-date overview")

    def _activate_overview_date(self, key: str) -> None:
        if self._manual_pricebook_df.empty:
            return
        index = self.key_date_combo.findData(key)
        if index >= 0:
            # Reuse the existing save/date-switch path; clicking never creates an event.
            self.key_date_combo.setCurrentIndex(index)

    def _preview_timeline_changes(self) -> None:
        if not self._active_key_date:
            QMessageBox.information(self, "Preview changes", "Choose a date in Timeline first.")
            return
        if not self._confirm_price_view_switch():
            return
        planner = self.planner_workspace
        if planner is None:
            return
        planner.set_shared_key_date(self._active_key_date)
        planner._preview_execution_date()

    def _clear_moved_issue_choices(self, old_key: str, new_key: str, *, campaign_id=None) -> None:
        folder = FILES.work_dir / "promo_planner" / "issue_choices"
        for path in folder.glob("*.json"):
            if campaign_id is None and path.stem not in {old_key, new_key}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            for field in ("cost_floor", "conflicts"):
                payload[field] = [row for row in payload.get(field, []) if campaign_id is not None and int(row[0]) != campaign_id]
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)

    def _delete_timeline_event(self) -> None:
        if getattr(self, "_save_future", None) is not None:
            self._finish_background_save(wait=True)
        key_date = self._active_key_date
        if not key_date:
            return
        effective = date.fromisoformat(key_date) + timedelta(days=1)
        if effective < date.today():
            return
        options = {}
        for marker in self.timeline.event_markers(key_date):
            if marker["source"] == "price_date":
                if not marker["metadata"].get("official") or effective > date.today():
                    options[f"Price change: {marker['label']}"] = ("price", None)
            elif marker["source"] == "promotion_date":
                options[f"Unfinished promotion: {marker['label']}"] = ("draft", None)
        store = self._promo_store()
        for campaign in store.list_campaigns():
            if campaign.start_date == effective.isoformat() and campaign.status != "ACTIVE_IMPORTED" and not store.has_execution(campaign.id, "START"):
                options[f"Promotion: {campaign.name} (#{campaign.id})"] = ("campaign", campaign.id)
        if not options:
            QMessageBox.information(self, "Delete dated change", "There are no cancellable future changes on this date.")
            return
        label, ok = QInputDialog.getItem(self, "Delete dated change", "Choose the change to cancel:", list(options), 0, False)
        if not ok:
            return
        if QMessageBox.question(self, "Cancel dated change", f"Cancel {label}?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        kind, campaign_id = options[label]
        # Discard pending edits belonging to the cancelled event, so switching
        # dates cannot autosave and accidentally recreate it.
        if kind == "price":
            self.autosave_dirty = False
        elif not self._confirm_price_view_switch():
            return
        try:
            if kind == "price":
                self.timeline.cancel_price_change_date(key_date)
            elif kind == "draft":
                self.timeline.discard_promotion_date(key_date)
                if self.planner_workspace is not None:
                    self.planner_workspace._cancel_edit()
            else:
                if self.planner_workspace is not None:
                    self.planner_workspace.cancel_future_campaign(campaign_id)
                else:
                    store.delete_campaign(campaign_id)
            self._price_view_cache.clear()
            self._active_key_date = None
            self._reload_editor_from_shared_timeline()
            self._load_price_view_data(None)
            if self.planner_workspace is not None:
                self.planner_workspace._refresh_campaigns()
        except Exception as exc:
            QMessageBox.warning(self, "Cancel dated change", str(exc))

    def _dated_edit_allowed(self) -> bool:
        return bool(
            self._active_key_date
            and date.fromisoformat(self._active_key_date) + timedelta(days=1) >= date.today()
            and not self.timeline.is_price_date_official(self._active_key_date)
        )

    def _require_dated_edit(self) -> bool:
        if not self._dated_edit_allowed():
            self.statusBar().showMessage("Choose an editable date in Timeline, or use Add dated change first.", 8000)
            return False
        return True

    def _ensure_price_event(self) -> None:
        # A price edit at a campaign boundary creates its own base-price event.
        markers = self.timeline.event_markers(self._active_key_date)
        if not any(m["source"] == "price_date" for m in markers):
            effective = date.fromisoformat(self._active_key_date) + timedelta(days=1)
            self.timeline.schedule_price_change_date(self._active_key_date, f"Price update {effective:%d %b %Y}")
            self._refresh_key_date_views(selected=self._active_key_date)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._refresh_workspace_layout)

    def _refresh_workspace_layout(self) -> None:
        self._apply_workspace_sizes()
        if hasattr(self, "campaign_timeline_view"):
            self._render_campaign_timeline(self._campaigns_cache)

    def _on_campaign_panel_tab_changed(self, index: int) -> None:
        if index == 1:
            self.refresh_campaign_panel()
        elif self.planner_workspace is not None:
            self.planner_workspace._refresh_execution_date()

    def _on_campaigns_changed(self) -> None:
        if self._manual_pricebook_df.empty:
            self._refresh_key_date_views()
            self.refresh_campaign_panel()
            return
        if self._confirm_price_view_switch():
            self._reload_editor_from_shared_timeline(self._active_key_date)

    def _reload_editor_from_shared_timeline(self, selected_date: str | None = None) -> None:
        if getattr(self, "_save_future", None) is not None:
            self._finish_background_save(wait=True)
        if getattr(self, "autosave_dirty", False) and self._dated_edit_allowed():
            self._save_key_date_prices(silent=True)
        self._price_view_cache.clear()
        current = self.timeline.project()
        if current.empty:
            return
        selected_country = self.state.selected_country
        self._manual_pricebook_df = current.copy()
        selected_date = selected_date or self._active_key_date
        if selected_date:
            self._active_key_date = selected_date
            self._refresh_key_date_views(selected=selected_date)
            index = self.key_date_combo.findData(selected_date)
            if index >= 0:
                self.key_date_combo.blockSignals(True)
                self.key_date_combo.setCurrentIndex(index)
                self.key_date_combo.blockSignals(False)
            self._load_price_view_data(selected_date)
        else:
            self._refresh_key_date_views()
            self._load_price_view_data(None)
        self.autosave_dirty = False
        self.refresh_campaign_panel()

    def _sync_planner_date_from_editor(self, _index: int = -1) -> None:
        selected = str(self.key_date_combo.currentData() or "")
        self.key_date_strip.set_selected_key(selected)
        if self.planner_workspace is not None:
            self.planner_workspace.set_shared_key_date(selected or None)
            if selected and self.campaign_panel is not None and self._workspace_mode == "campaigns":
                QTimer.singleShot(
                    0,
                    lambda: self.campaign_panel.ensureWidgetVisible(
                        self.planner_workspace.timeline_tab, 0, 8
                    ) if self.planner_workspace is not None else None,
                )
        if hasattr(self, "campaign_timeline_view"):
            self.refresh_campaign_panel()

    def _set_campaign_review_status(self, key_date, summary):
        if key_date != self._active_key_date:
            return
        self._promotion_review_key = key_date
        self._promotion_review_summary = dict(summary)
        self._refresh_campaign_review_status()

    def _refresh_campaign_review_status(self):
        if not hasattr(self, "campaign_preview_btn"):
            return
        enabled = bool(self._active_key_date)
        self.campaign_preview_btn.setEnabled(enabled)
        summary = (self._promotion_review_summary
                   if self._promotion_review_key == self._active_key_date else None)
        if not enabled:
            text, message, color = "Preview & resolve", "Choose a date in Timeline to review promo issues.", "#666666"
        elif summary is None:
            text, message, color = "Preview & resolve · check needed", "Review promos for the selected date. Ordinary price edits are excluded.", "#666666"
        elif summary["total"]:
            text = f"Preview & resolve · {summary['total']} unresolved"
            message = (f"{summary['floor_products']} promo product(s) below floor without approval · "
                       f"{summary['conflicts']} promo conflict(s). Unanswered issues stay visible.")
            color = "#9B1C1C"
        else:
            text, message, color = "Preview & resolve · no unresolved issues", "No unresolved promo issues for the selected date. Ordinary price edits are excluded.", "#1B5E20"
        self.campaign_preview_btn.setText(text)
        self.campaign_preview_btn.setStyleSheet(f"color:{color};font-weight:700;")
        self.campaign_review_status.setText(message)
        self.campaign_review_status.setStyleSheet(f"color:{color};")

    def refresh_campaign_panel(self) -> None:
        """Refresh the compact campaign browser from the shared planner database."""
        if not hasattr(self, "campaign_list"):
            return
        self._refresh_campaign_review_status()
        current = self.campaign_list.currentItem()
        selected_id = current.data(Qt.UserRole) if current else None
        try:
            store = self._promo_store()
            campaigns = store.list_campaigns()
        except Exception as exc:
            self.campaign_timeline_scene.clear()
            self.campaign_timeline_scene.addText(f"Could not read campaigns: {exc}")
            self.campaign_list.clear()
            self.campaign_detail_label.setText(f"Could not read campaigns: {exc}")
            self.campaign_destinations_list.clear()
            return
        self._campaigns_cache = campaigns
        self._render_campaign_timeline(campaigns)
        self.campaign_list.clear()
        context = campaign_context_ids(campaigns, self._active_key_date)
        for campaign in sorted(campaigns, key=lambda item: (item.start_date, item.name.casefold())):
            state = campaign_presentation(campaign, store)
            item = QListWidgetItem(f"{campaign.name} · {state['label']}")
            font = item.font()
            font.setItalic(state["readonly"])
            font.setBold(campaign.id in context)
            item.setFont(font)
            item.setToolTip(f"{campaign.start_date} – {campaign.end_date} · {state['label']}")
            item.setData(Qt.UserRole, campaign.id)
            self.campaign_list.addItem(item)
            if selected_id is not None and campaign.id == selected_id:
                self.campaign_list.setCurrentItem(item)
        if self.campaign_list.count() and self.campaign_list.currentRow() < 0:
            self.campaign_list.setCurrentRow(0)
        elif not self.campaign_list.count():
            self.campaign_detail_label.setText("No campaigns yet. Use Add dated change in Timeline to schedule a promotion.")
            self.campaign_destinations_list.clear()
        self.edit_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)
        self.duplicate_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)
        self.delete_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)
        current = self.campaign_list.currentItem()
        if current is not None:
            self._show_campaign_details(current)
        date_changed = getattr(self, "_campaign_context_date", object()) != self._active_key_date
        self._campaign_context_date = self._active_key_date
        if date_changed:
            for row in range(self.campaign_list.count()):
                item = self.campaign_list.item(row)
                if item.data(Qt.UserRole) in context:
                    self.campaign_list.setCurrentItem(item)
                    self.campaign_list.scrollToItem(item)
                    break

    def _render_campaign_timeline(self, campaigns) -> None:
        if not hasattr(self, "campaign_timeline_scene"):
            return
        scene = self.campaign_timeline_scene
        scene.clear()
        if not campaigns:
            scene.addText("No planned campaigns yet.", QFont("Segoe UI", 9))
            scene.setSceneRect(0, 0, max(360, self.campaign_timeline_view.viewport().width()), 40)
            return

        store = self._promo_store()
        today = date.today()
        parsed = []
        for campaign in campaigns:
            start = date.fromisoformat(campaign.display_start_date)
            end = date.fromisoformat(campaign.end_date) if campaign.end_date else today + timedelta(days=90)
            start = min(start, end)
            parsed.append((campaign, start, end))

        first = min([start for _, start, _ in parsed] + [today]) - timedelta(days=3)
        last = max([end for _, _, end in parsed] + [today]) + timedelta(days=3)
        width = max(360, self.campaign_timeline_view.viewport().width())
        left = min(175, max(105, int(width * 0.28)))
        axis_width = max(1, width - left - 12)
        total_days = max(1, (last - first).days)
        day_width = axis_width / total_days
        top, row_height = 38, 27
        height = top + len(parsed) * row_height + 10
        scene.setSceneRect(0, 0, width, height)

        def x_for(day):
            return left + (day - first).days * day_width

        month = first.replace(day=1)
        while month <= last:
            x = x_for(month)
            scene.addLine(x, 22, x, height, QPen(QColor("#E5E7EB")))
            if total_days < 600 or month.month in {1, 4, 7, 10}:
                label = scene.addText(month.strftime("%b %Y"), QFont("Segoe UI", 7))
                label.setDefaultTextColor(QColor("#626873"))
                label.setPos(x + 2, 3)
            month = (month + timedelta(days=32)).replace(day=1)

        today_x = x_for(today)
        scene.addLine(today_x, 20, today_x, height, QPen(QColor("#E5007D"), 2))
        selected_key_date = str(self.key_date_combo.currentData() or "")
        if selected_key_date:
            selected_boundary = date.fromisoformat(selected_key_date) + timedelta(days=1)
            if first <= selected_boundary <= last:
                scene.addLine(
                    x_for(selected_boundary), 20, x_for(selected_boundary), height,
                    QPen(QColor("#5A6573"), 1, Qt.DashLine),
                )

        context = campaign_context_ids(campaigns, selected_key_date or None)
        for row, (campaign, start, end) in enumerate(parsed):
            y = top + row * row_height
            state = campaign_presentation(campaign, store)
            font = QFont("Segoe UI", 8)
            font.setItalic(state["readonly"])
            font.setBold(campaign.id in context)
            label = scene.addText(campaign.name[:24], font)
            label.setDefaultTextColor(QColor("#252A31"))
            label.setPos(4, y + 2)
            label.setToolTip(f"{campaign.name} · {state['label']}")
            started = store.has_execution(campaign.id, "START")
            ended = store.has_execution(campaign.id, "END")
            if ended:
                color = "#A4A9B1"
            elif started or campaign.status == "ACTIVE_IMPORTED":
                color = "#208A5B"
            elif end < today:
                color = "#8C6F9E"
            else:
                color = "#6B8FDC"
            x1, x2 = x_for(start), x_for(end + timedelta(days=1))
            bar = scene.addRect(x1, y + 3, max(3, x2 - x1), 18, QPen(Qt.NoPen), QBrush(QColor(color)))
            bar.setToolTip(f"{campaign.name}\n{start:%d %b %Y} – {end:%d %b %Y}")

    def _show_campaign_details(self, current, _previous=None) -> None:
        self.campaign_destinations_list.clear()
        if current is None:
            self.edit_campaign_btn.setEnabled(False)
            self.rename_campaign_btn.setEnabled(False)
            self.duplicate_campaign_btn.setEnabled(False)
            self.delete_campaign_btn.setEnabled(False)
            return
        self.edit_campaign_btn.setEnabled(True)
        self.duplicate_campaign_btn.setEnabled(True)
        self.delete_campaign_btn.setEnabled(True)
        campaign_id = current.data(Qt.UserRole)
        campaign = next(
            (item for item in self._promo_store().list_campaigns() if item.id == campaign_id),
            None,
        )
        if campaign is None:
            self.campaign_detail_label.setText("This campaign changed in the planner. Refresh to reload it.")
            return
        state = campaign_presentation(campaign, self._promo_store())
        self.edit_campaign_btn.setText("View" if state["readonly"] else "Edit")
        self.delete_campaign_btn.setEnabled(not state["readonly"])
        self.rename_campaign_btn.setEnabled(not state["readonly"])
        promo = campaign.promo_name or campaign.promo_code or campaign.promo_type
        self.campaign_detail_label.setText(
            f"<b>{campaign.name}</b><br>{campaign.start_date} → {campaign.end_date}<br>"
            f"Promo: {promo} · {state['label']}<br>Plans: {', '.join(campaign.plans) or 'All plans'}"
        )
        planner = getattr(self, "planner_workspace", None)
        for destination in campaign.destinations or []:
            code = str(destination)
            label = (
                planner.destination_label(code)
                if planner is not None else code
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, code)
            self.campaign_destinations_list.addItem(item)

    def _open_campaign_destination(self, item: QListWidgetItem) -> None:
        destination = str(item.data(Qt.UserRole) or item.text()).strip()
        self._select_campaign_destination_curve(destination)

    def _select_campaign_destination_curve(self, destination: str, plans=None, days=None) -> None:
        index = self.country_combo.findText(destination, Qt.MatchFixedString)
        if index < 0:
            normalized = destination.casefold()
            for country, iso in self.state.country_iso_map.items():
                if str(iso).strip().casefold() == normalized:
                    index = self.country_combo.findText(country, Qt.MatchFixedString)
                    if index >= 0:
                        break
        if index < 0:
            self.statusBar().showMessage(
                f"No editor destination matches campaign destination '{destination}'.", 8000
            )
            return
        self.country_combo.setCurrentIndex(index)
        points = self.state.current_points()
        matching = [point for point in points if not plans or point.get("plan") in plans]
        selected = next((point for point in matching if not days or int(point.get("days") or 0) in days), matching[0] if matching else None)
        if selected is not None:
            self.state.selected_row_id = str(selected["row_id"])
        self.refresh_canvas()

    def open_promo_planner(self) -> None:
        if getattr(self, "_save_future", None) is not None:
            self._finish_background_save(wait=True)
        if self.planner_workspace is None:
            self._retry_planner_workspace()
        if self.campaign_dock is not None:
            if self._workspace_mode == "campaigns":
                self.open_pricing_panel()
                return
            self._set_workspace_mode("campaigns")
            self.planner_workspace.tabs.setCurrentWidget(self.campaign_panel)
            self.refresh_campaign_panel()

    def open_new_campaign(self) -> None:
        self._add_timeline_promotion()

    def edit_selected_campaign(self) -> None:
        if self.planner_workspace is None:
            return
        item = self.campaign_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Edit campaign", "Select a campaign first.")
            return
        self._set_workspace_mode("campaigns")
        self.planner_workspace.open_campaign_for_edit(int(item.data(Qt.UserRole)))

    def _select_planner_campaign(self, campaign_id: int) -> bool:
        planner = self.planner_workspace
        if planner is None or not hasattr(planner, "tree"):
            return False
        for index in range(planner.tree.topLevelItemCount()):
            tree_item = planner.tree.topLevelItem(index)
            if int(tree_item.data(0, Qt.UserRole)) == int(campaign_id):
                planner.tree.setCurrentItem(tree_item)
                return True
        return False

    def export_campaign_calendar(self):
        if self.planner_workspace is None:
            return
        self.planner_workspace._export_promotion_calendar()

    def rename_selected_campaign(self):
        item = self.campaign_list.currentItem()
        if item is None:
            return
        store = self._promo_store()
        campaign = store.get_campaign(int(item.data(Qt.UserRole)))
        if campaign_presentation(campaign, store)["readonly"]:
            QMessageBox.information(self, "Rename campaign", "Published or finished campaigns are read-only.")
            return
        name, ok = QInputDialog.getText(self, "Rename campaign", "Campaign name:", text=campaign.name)
        if not ok or not name.strip() or name.strip() == campaign.name:
            return
        try:
            store.rename_campaign(campaign.id, name)
        except Exception as exc:
            QMessageBox.warning(self, "Rename campaign", str(exc))
            return
        planner = self.planner_workspace
        if planner is not None and planner.editing_campaign_id == campaign.id:
            planner.name_edit.setEditText(name.strip())
        self._refresh_key_date_views(selected=self._active_key_date)
        self.refresh_campaign_panel()

    def duplicate_selected_campaign(self) -> None:
        item = self.campaign_list.currentItem()
        if item is None or self.planner_workspace is None:
            return
        chosen = self._ask_timeline_date(promotion=True)
        if chosen is None or not self._confirm_price_view_switch():
            return
        from dataclasses import replace
        original = self._promo_store().get_campaign(int(item.data(Qt.UserRole)))
        effective, label = chosen
        key_date = (effective - timedelta(days=1)).isoformat()
        clone = replace(original, id=None, status="PLANNED", name=label.strip() or original.name + " - Copy",
                        start_date=effective.isoformat(), end_date=(effective + timedelta(days=6)).isoformat())
        self.timeline.schedule_promotion_date(key_date, clone.name)
        self._reload_editor_from_shared_timeline(key_date)
        planner = self.planner_workspace
        planner.begin_campaign_on_date(effective.isoformat(), label=clone.name)
        planner._load_campaign_into_form(clone)
        self._set_workspace_mode("campaigns")
        planner.tabs.setCurrentWidget(planner.create_tab)

    def delete_selected_campaign(self) -> None:
        item = self.campaign_list.currentItem()
        if item and self.planner_workspace and self._select_planner_campaign(int(item.data(Qt.UserRole))):
            self.planner_workspace._delete_selected()

    def clear_busy_cursor(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()

    def closeEvent(self, event):
        worker = getattr(getattr(self, "planner_workspace", None), "_apply_worker", None)
        if worker is not None and worker.isRunning():
            event.ignore()
            self.statusBar().showMessage("Wait for the campaign timeline update to finish before closing.")
            return
        if not self._confirm_price_view_switch():
            event.ignore()
            return
        if hasattr(self, "_save_executor"):
            self._save_executor.shutdown(wait=True)
        super().closeEvent(event)
    
    def set_drag_mode(self, mode_key: str):
        self.current_drag_mode = mode_key

        for key, btn in self.mode_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(key == mode_key)
            btn.blockSignals(False)

    def load_currency_tables_from_folder(self, folder: str | Path, names: list[str]) -> pd.DataFrame:
        tables: dict[str, pd.DataFrame] = {}
        folder = Path(folder)

        for currency in CURRENCIES:
            path = find_currency_file(folder, currency, names)
            if path is None:
                continue
            df = load_table(path, currency_hint=currency, eur_to_usd=self.state.eur_to_usd)
            if not df.empty:
                tables[currency] = df

        if not tables:
            return pd.DataFrame()

        if len(tables) == 1:
            return next(iter(tables.values()))

        return merge_currency_tables(tables)

    @staticmethod
    def _history_timestamp(path: Path, prefix: str, suffix: str) -> str | None:
        name = path.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            return None
        timestamp = name[len(prefix):-len(suffix)]
        try:
            datetime.strptime(timestamp, "%Y%m%d_%H%M%S_%f" if "_" in timestamp else "%Y%m%d")
        except ValueError:
            return None
        return timestamp

    @staticmethod
    def _history_timestamp_label(timestamp: str) -> str:
        try:
            if "_" in timestamp:
                return datetime.strptime(timestamp, "%Y%m%d_%H%M%S_%f").strftime("%Y-%m-%d · export %H:%M:%S.%f")
            return datetime.strptime(timestamp, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return timestamp

    def export_metadata_path(self, timestamp: str) -> Path:
        return FILES.editor_history_root / f"export_{timestamp}.json"

    def read_export_metadata(self, timestamp: str) -> dict:
        path = self.export_metadata_path(timestamp)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def export_history_label(self, timestamp: str) -> str:
        label = self._history_timestamp_label(timestamp)
        metadata = self.read_export_metadata(timestamp)
        if metadata.get("official") is True:
            name = str(metadata.get("name") or "").strip()
            return f"{name} — {label} (Official)" if name else f"{label} (Official)"
        return f"{label} (Reference)" if metadata.get("export_kind") == "reference" else label

    def save_export_metadata(
        self,
        *,
        timestamp: str,
        official: bool,
        partner_zip: Path | None = None,
        compare_timestamp: str | None = None,
        pack_result=None,
        name: str = "",
    ) -> None:
        path = self.export_metadata_path(timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "date": timestamp,
            "official": bool(official),
            "name": str(name).strip(),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "partner_pack_created": partner_zip is not None,
            "export_kind": "official" if official else "reference",
            "partner_zip_name": partner_zip.name if partner_zip is not None else "",
            "partner_zip_path": str(partner_zip) if partner_zip is not None else "",
            "compare_date": compare_timestamp or "",
            "price_files": [item.member_name for item in pack_result.files] if pack_result is not None else [],
            "diff_files": [item.member_name for item in pack_result.diff_files] if pack_result is not None else [],
        }
        path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def saved_state_timestamps(self, *, official_only: bool = False) -> list[str]:
        # New consolidated history lives directly under history/.
        timestamps: set[str] = set()
        for suffix in (".xlsx", ".csv"):
            timestamps.update({
                ts for path in FILES.editor_history_root.glob(f"manual_prices_*{suffix}")
                for ts in [self._history_timestamp(path, "manual_prices_", suffix)]
                if ts
            })

        # Backward-compatible discovery of legacy EUR/USD history.
        legacy_sets: list[set[str]] = []
        for currency in CURRENCIES:
            history_dir = FILES.editor_history_root / currency
            legacy_sets.append({
                ts for path in history_dir.glob("manual_prices_*.csv")
                for ts in [self._history_timestamp(path, "manual_prices_", ".csv")]
                if ts
            })
        if legacy_sets:
            timestamps.update(set.intersection(*legacy_sets))

        ordered = sorted(timestamps, reverse=True)
        if official_only:
            ordered = [
                ts for ts in ordered
                if self.read_export_metadata(ts).get("official") is True
            ]
        return ordered

    def load_export_prices_from_folder(self, folder: str | Path, silent: bool = False) -> bool:
        folder = Path(folder)
        consolidated = None
        for name in ("manual_prices_current.xlsx", "manual_prices_current.csv"):
            candidate = folder / name
            if candidate.exists():
                consolidated = candidate
                break

        if consolidated is not None:
            df = load_table(
                consolidated,
                currency_hint="EUR",
                eur_to_usd=self.state.eur_to_usd,
            )
        else:
            # Legacy read-only fallback. New saves never use currency folders.
            df = self.load_currency_tables_from_folder(
                folder,
                ["manual_prices_current.xlsx", "manual_prices_current.csv"],
            )

        if df.empty:
            if not silent:
                QMessageBox.warning(self, "Load failed", "No usable exported price rows found in that folder.")
            return False

        self.state.preload_last_export(df)
        self.refresh_canvas()
        return True

    def _promo_store(self) -> PromoStore:
        return PromoStore(FILES.work_dir / "promo_planner" / "promo_schedule.db")

    def _refresh_key_date_views(self, selected: str | None = None) -> None:
        if not hasattr(self, "key_date_combo"):
            return
        store = self._promo_store()
        campaigns = store.list_campaigns()
        dates = scheduled_key_dates(campaigns, store=store)
        labels_by_date = self.timeline.key_date_labels()
        today = date.today()
        combo = self.key_date_combo
        combo.blockSignals(True)
        combo.clear()
        current_transition = today_campaign_transition(campaigns, today)
        combo.addItem(f"Today / current prices · {today:%d %b %Y}  |  {current_transition}", "")
        for key_date in dates:
            boundary = date.fromisoformat(key_date)
            next_day = boundary + timedelta(days=1)
            events = labels_by_date.get(key_date, [])
            transition = campaign_transition(campaigns, key_date)
            event_text = transition
            if events:
                event_text += " · " + " · ".join(dict.fromkeys(events))
            combo.addItem(
                f"{next_day.strftime('%d %b %Y')}  |  {event_text}",
                key_date,
            )
        wanted = selected or self._active_key_date or ""
        index = combo.findData(wanted)
        readonly_keys = set()
        for row in range(combo.count()):
            key = combo.itemData(row)
            font = QFont("Segoe UI", 9)
            font.setBold(row == max(0, index))
            readonly = not key or key_date_read_only(campaigns, store, self.timeline, key, today=today)
            font.setItalic(readonly)
            if readonly and key:
                readonly_keys.add(key)
            combo.setItemData(row, font, Qt.FontRole)
        self.key_date_strip.set_markers(build_date_markers(campaigns, dates, labels_by_date, readonly_keys), today=today)
        combo.setCurrentIndex(max(0, index))
        combo.setEnabled(not self._manual_pricebook_df.empty)
        combo.blockSignals(False)
        self._update_price_view_controls()
        self._sync_planner_date_from_editor()
    def _update_price_view_controls(self) -> None:
        editable = self._dated_edit_allowed()
        self.key_date_strip.setEnabled(not self._manual_pricebook_df.empty)
        self.key_date_strip.set_selected_key(self._active_key_date or "")
        self.move_timeline_date_btn.setEnabled(not self._manual_pricebook_df.empty)
        self.timeline_preview_btn.setEnabled(bool(self._active_key_date) and not self._manual_pricebook_df.empty)
        self.delete_timeline_date_btn.setEnabled(bool(self._active_key_date) and date.fromisoformat(self._active_key_date) + timedelta(days=1) >= date.today())
        for button in self.mode_buttons.values():
            button.setEnabled(editable)
        self.exchange_rate_spin.setEnabled(editable)
        if hasattr(self, "remove_promo_btn"):
            self.remove_promo_btn.setEnabled(editable)
        if hasattr(self, "canvas"):
            self.canvas.editing_allowed = editable
        self.drag_safety_label.setText(
            f"Editing prices for {(date.fromisoformat(self._active_key_date) + timedelta(days=1)):%d %b %Y} · Hold SHIFT to drag."
            if editable else "Read only · choose an editing date in Timeline first."
        )
        if self._active_key_date:
            effective = date.fromisoformat(self._active_key_date) + timedelta(days=1)
            self.key_date_status.setText(
                f"Editing prices effective {effective:%d %b %Y}. Changes are saved for this date."
                if editable else f"{effective:%d %b %Y} · Read only. Add a new dated change to revise prices."
            )
            self.key_date_status.setStyleSheet("color:#8a4b08; font-weight:600; padding:3px 0;")
        elif not self._manual_pricebook_df.empty:
            self.key_date_status.setText("Read only. Choose a date in Timeline to change prices or create a promotion.")
            self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        else:
            self.key_date_status.setText("Load current prices to browse scheduled dates.")
            self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        has_prices = not self._manual_pricebook_df.empty
        self.export_btn.setEnabled(has_prices)
        self.partner_pack_btn.setEnabled(has_prices)
        self.partner_pack_btn.setToolTip("Create an official pack for this editable date." if editable
                                        else "Export a reference pack of the displayed prices; nothing is published.")
        self.import_excel_btn.setEnabled(editable)

    def _refresh_manual_baseline(self) -> None:
        df = self.timeline.project()
        if not df.empty:
            self._manual_pricebook_df = df
            self._manual_pricebook_path = FILES.editor_exports_dir / "manual_prices_current.xlsx"

    def _confirm_price_view_switch(self) -> bool:
        if getattr(self, "_save_future", None) is not None:
            try:
                self._finish_background_save(wait=True)
            except Exception as exc:
                QMessageBox.warning(self, "Save pending prices", str(exc))
                return False
        if not self.autosave_dirty:
            return True
        if self._active_key_date:
            try:
                self._save_key_date_prices(silent=True)
                return True
            except Exception as exc:
                QMessageBox.warning(self, "Save pending prices", str(exc))
                return False
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved current prices")
        box.setText("Save your current-price edits before switching to a scheduled date?")
        save = box.addButton("Save current prices", QMessageBox.AcceptRole)
        discard = box.addButton("Discard edits", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            self.quick_save()
            return not self.autosave_dirty
        if clicked is discard:
            if not self._manual_pricebook_df.empty:
                self.state.preload_pricebook(self._manual_pricebook_df)
                self.state.preload_last_export(self._manual_pricebook_df)
                self.populate_combos()
                self.refresh_canvas()
            self.autosave_dirty = False
            return True
        return False

    def on_key_date_view_changed(self, index: int) -> None:
        if self._switching_price_view or index < 0 or self._manual_pricebook_df.empty:
            return
        selected = str(self.key_date_combo.itemData(index) or "")
        if selected == (self._active_key_date or ""):
            return
        if not self._confirm_price_view_switch():
            self._switching_price_view = True
            old_index = self.key_date_combo.findData(self._active_key_date or "")
            self.key_date_combo.setCurrentIndex(max(0, old_index))
            self._switching_price_view = False
            return
        try:
            self._load_price_view(selected or None)
        except Exception as exc:
            QMessageBox.warning(self, "Price view", f"Could not load that key date:\n\n{exc}")
            self._switching_price_view = True
            old_index = self.key_date_combo.findData(self._active_key_date or "")
            self.key_date_combo.setCurrentIndex(max(0, old_index))
            self._switching_price_view = False

    def _load_price_view(self, key_date: str | None) -> None:
        progress = None
        if key_date is not None:
            progress = QProgressDialog("Loading the selected pricing date…", "", 0, 0, self)
            progress.setWindowTitle("Loading prices")
            progress.setCancelButton(None)
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)
            progress.show()
            QApplication.processEvents()

        try:
            self._load_price_view_data(key_date)
        finally:
            if progress is not None:
                progress.close()

    def _load_price_view_data(self, key_date: str | None) -> None:
        if key_date is None:
            today_key = (date.today() - timedelta(days=1)).isoformat()
            cache_key = "live:" + today_key
            cached = self._price_view_cache.get(cache_key)
            if cached is None:
                revision = self._price_view_cache.revision()
                view, _baseline, _warning = self._project_key_date_view(today_key, include_drafts=False)
                self._price_view_cache.put(cache_key, view, pd.DataFrame(), "", revision=revision)
            else:
                view = cached[0]
            self._date_view_baseline_df = pd.DataFrame()
            self._active_key_date = None
            warning_text = self.key_date_combo.itemText(0) + " · Read only"
        else:
            cached = self._price_view_cache.get(key_date)
            if cached is None:
                revision = self._price_view_cache.revision()
                view, baseline, warning_text = self._project_key_date_view(key_date)
                self._price_view_cache.put(
                    key_date, view, baseline, warning_text, revision=revision
                )
            else:
                view, baseline, warning_text = cached
            self._date_view_baseline_df = baseline
            self._active_key_date = key_date
        self._promotion_review_key = key_date
        self._promotion_review_summary = view.attrs.get("promotion_review") if key_date else None
        self._refresh_campaign_review_status()
        self.state.load_timeline_view(view)
        self._date_view_loaded_df = view.copy()
        self.populate_combos()
        self.autosave_dirty = False
        self._update_price_view_controls()
        self.refresh_canvas()
        self.statusBar().showMessage(warning_text, 12000)

    def _project_key_date_view(self, key_date: str, *, include_drafts: bool = True):
        store = self._promo_store()
        # Rebuild the selected date from the shared timeline boundary in
        # memory, then reverse only the selected editor layer to obtain a
        # clean comparison base without simulating the campaign twice.
        boundary = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
        date_base = self.timeline.project(boundary, layers=("base", "import", "override"), include_drafts=include_drafts)
        projected, preview = project_pricebook_for_key_date(
            date_base,
            self._manual_pricebook_path or (FILES.editor_exports_dir / "manual_prices_current.xlsx"),
            store,
            key_date,
            FILES.base_dir,
            replay_executed=True,
            include_drafts=include_drafts,
        )
        baseline = remove_key_date_overrides(
            projected, FILES.base_dir, key_date, boundary,
        )
        view = projected
        projected.attrs["promotion_review"] = review_summary(preview)
        warnings = []
        if preview.collisions:
            warnings.append(f"{len(preview.collisions)} promo conflict(s) still need a decision in Promo Planner")
        pending_promos = projected.attrs["promotion_review"]["floor_products"]
        if pending_promos:
            warnings.append(f"{pending_promos} promo product(s) need cost-floor review")
        boundary = date.fromisoformat(key_date) + timedelta(days=1)
        warning_text = f"After midnight: {boundary.strftime('%d %b %Y')} 00:00"
        if warnings:
            warning_text += " · " + " · ".join(warnings)
        return view, baseline, warning_text

    def _price_date_event_name(self, key_date: str) -> str:
        for label in self.timeline.key_date_labels().get(key_date, []):
            if label.startswith("PRICE CHANGE:"):
                return label.partition(":")[2].strip()
        return ""
    def _save_key_date_prices(self, *, silent: bool = False) -> int:
        if not self._active_key_date:
            return 0
        if getattr(self, "_save_future", None) is not None:
            completed = self._finish_background_save(wait=True)
            if not self.autosave_dirty:
                return completed
        self._ensure_price_event()
        edited = self.state.export_prices_dataframe(refresh=False)
        campaigns = self._promo_store().list_campaigns()
        count, unlinked = save_key_date_changes(
            FILES.base_dir,
            self._active_key_date,
            edited,
            self._date_view_baseline_df,
            campaigns,
            event_label=self._price_date_event_name(self._active_key_date),
            displayed_df=self._date_view_loaded_df,
            normalize_price=self.state.round_regular_price,
        )
        self._date_view_loaded_df = edited.copy()
        self.autosave_dirty = False
        effective = (date.fromisoformat(self._active_key_date) + timedelta(days=1)).isoformat()
        if any(c.monotonic_destinations and (not c.start_date or c.start_date <= effective) and effective <= c.end_date for c in campaigns):
            # Editing an anchor changes dependent PA nets. Refresh them before
            # this saved view can be used for an Excel or partner-pack export.
            self._price_view_cache.clear()
            self._load_price_view_data(self._active_key_date)
        message = (
            f"Saved scheduled price changes for the midnight after {self._active_key_date}. "
            f"{count} row override(s) are stored in the shared pricing timeline; current prices were not changed."
        )
        self.statusBar().showMessage(message, 12000)
        if not silent:
            QMessageBox.information(self, "Key-date prices saved", message)
        return count

    def load_pricebook(self):
        if not self._require_dated_edit():
            return
        source, _ = QFileDialog.getOpenFileName(
            self, "Import prices for the selected date", str(FILES.editor_exports_dir),
            "Excel workbooks (*.xlsx *.xlsm *.xls);;CSV files (*.csv)",
        )
        if not source:
            return
        try:
            frame = load_table(Path(source), currency_hint="EUR", eur_to_usd=self.state.eur_to_usd)
            if frame.empty:
                raise ValueError("No usable pricing rows were found in that file.")
            fields, rows = self.timeline.diff_summary(self.state.export_prices_dataframe(), frame)
            if not fields:
                return
            effective = date.fromisoformat(self._active_key_date) + timedelta(days=1)
            if QMessageBox.question(
                self, "Import dated prices",
                f"Import list prices for {effective:%d %b %Y} from {source}?\n{rows:,} pricing rows differ.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            key_date = self._active_key_date
            self.state.load_timeline_view(frame)
            self._save_key_date_prices(silent=True)
            self._reload_editor_from_shared_timeline(key_date)
        except Exception as exc:
            QMessageBox.warning(self, "Import dated prices", str(exc))

    def select_partner_compare_timestamp(self, current_timestamp: str) -> str | None:
        # Partner packs are official outputs, so compare only with prior official snapshots.
        timestamps = [
            ts for ts in self.saved_state_timestamps(official_only=True)
        ]
        if not timestamps:
            return None

        labels = [self.export_history_label(ts) for ts in timestamps]
        label_to_timestamp = dict(zip(labels, timestamps))
        selected_label, ok = QInputDialog.getItem(
            self,
            "Compare partner pack",
            "Compare partner prices with previous official date:",
            labels,
            0,
            False,
        )
        if not ok or not selected_label:
            return None
        return label_to_timestamp.get(str(selected_label))

    @staticmethod
    def official_partner_pack_filename(timestamp: str, save_name: str) -> str:
        date_label = datetime.strptime(timestamp, "%Y%m%d").strftime("%Y-%m-%d")
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(save_name)).strip(" ._")
        safe_name = safe_name[:100].rstrip(" ._") or "Official"
        return f"TT_prices_{date_label}_{safe_name}.zip"

    @staticmethod
    def partner_zip_path_with_status(zip_path: Path, official: bool) -> Path:
        if zip_path.suffix.lower() != ".zip":
            zip_path = zip_path.with_suffix(".zip")

        clean_stem = MainWindow._strip_partner_status_suffix(zip_path.stem)
        if official:
            return zip_path.with_name(f"{clean_stem}{zip_path.suffix}")
        return zip_path.with_name(f"{clean_stem}_DRAFT{zip_path.suffix}")

    @staticmethod
    def _strip_partner_status_suffix(stem: str) -> str:
        stem_upper = stem.upper()
        for suffix in ("_OFFICIAL", "_DRAFT"):
            if stem_upper.endswith(suffix):
                return stem[: -len(suffix)]
        return stem

    @staticmethod
    def partner_pack_dir_for_status(official: bool) -> Path:
        return FILES.partner_packs_dir / ("official" if official else "draft")

    @staticmethod
    def partner_zip_path_in_status_folder(zip_path: Path, official: bool) -> Path:
        zip_path = MainWindow.partner_zip_path_with_status(zip_path, official)
        target_dir = MainWindow.partner_pack_dir_for_status(official)
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / zip_path.name

    def load_market(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Load market/competitor folder",
        )
        if not folder:
            return

        df = self.load_currency_tables_from_folder(folder, ["market_prices_annotated_latest.csv"])
        if df.empty:
            QMessageBox.warning(self, "Load failed", "No usable market rows found in that folder.")
            return
        self.state.preload_market(df)
        self.refresh_canvas()

    def load_promo_catalog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load promo catalog",
            "",
            "JSON Files (*.json)",
        )
        if not path:
            return

        self.state.promo_catalog = load_promos(path)
        self.refresh_canvas()

    def use_loaded_for_selected_plan(self, show_status: bool = False):
        if not self._require_dated_edit():
            return
        self._ensure_price_event()
        self.state.reload_selected_plan_from_loaded()
        self.mark_dirty()
        self.refresh_canvas()
        if show_status:
            scope = "USD/EUR" if self.state.is_linked_currency_mode() else self.state.active_currency
            self.statusBar().showMessage(f"Package reset to loaded state ({scope})")

    def reset_current_package_to_loaded(self):
        self.use_loaded_for_selected_plan(show_status=True)


    def use_loaded_for_pricing_unit(self):
        if not self._require_dated_edit():
            return
        self._ensure_price_event()
        self.state.reload_pricing_unit_from_loaded()
        self.mark_dirty()
        self.refresh_canvas()

    def quick_save(self):
        """Ctrl+S flushes dated edits; it never creates a pack or history snapshot."""
        if not self._dated_edit_allowed():
            self.statusBar().showMessage("Read only. Choose an editing date in Timeline first.", 8000)
            return
        try:
            self._save_key_date_prices(silent=True)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not save dated prices: {exc}", 12000)

    def _finish_background_save(self, *, wait=False):
        future = getattr(self, "_save_future", None)
        if future is None or (not wait and not future.done()):
            return 0
        self._save_future = None
        self.autosave_in_progress = False
        result = future.result()  # Propagate errors; dirty edits remain pending.
        self._price_view_cache.clear()
        if self._active_key_date == self._save_key:
            self._date_view_loaded_df = result["edited"].copy()
            self._date_view_baseline_df = result["baseline"]
            if self._edit_revision == self._save_revision:
                self.autosave_dirty = False
                self._promotion_review_key = self._active_key_date
                self._promotion_review_summary = result.get("promotion_review")
                if hasattr(self, "campaign_preview_btn"):
                    self._refresh_campaign_review_status()
                if result.get("model_changed", True):
                    self.state.load_timeline_view(result["view"])
                    self.refresh_canvas()
        self.statusBar().showMessage("Dated prices saved." + (" Newer edits are still pending." if self.autosave_dirty else ""), 5000)
        return result["count"]

    def _start_background_save(self):
        if self._save_future is not None or not self.autosave_dirty or not self._dated_edit_allowed() or self.canvas.is_dragging:
            return
        try:
            try:
                from automation.pricing_editor.background_save import save_editor_snapshot
            except ImportError:
                from pricing_editor.background_save import save_editor_snapshot
            # Freeze mutable model state before handing it to the worker. It never touches live Qt/state objects.
            snapshot = copy.deepcopy(self.state)
            self._save_key = self._active_key_date
            self._save_revision = self._edit_revision
            label = self._price_date_event_name(self._active_key_date)
            self._save_future = self._save_executor.submit(save_editor_snapshot, snapshot, FILES.base_dir,
                self._save_key, self._date_view_baseline_df.copy(), self._date_view_loaded_df.copy(), label)
            self.autosave_in_progress = True
            self.statusBar().showMessage("Saving dated prices in the background…", 3000)
        except Exception as exc:
            self.autosave_in_progress = False
            self.statusBar().showMessage(f"Could not start saving: {exc}", 12000)

    def autosave(self):
        try:
            if getattr(self, "_save_future", None) is not None:
                self._finish_background_save()
            if not self._dated_edit_allowed() or not self.autosave_dirty or self.autosave_in_progress or self.canvas.is_dragging:
                return
            now = time.monotonic()
            if now - self._last_edit_at >= 15 or now - self._dirty_since >= 90:
                self._start_background_save()
        except Exception as exc:
            self._last_edit_at = time.monotonic()  # Retry after a pause, not every timer tick.
            self._dirty_since = self._last_edit_at
            self.statusBar().showMessage(f"Could not autosave; edits remain pending: {exc}", 12000)

    def export_prices(self):
        """Create a pack for the displayed date, publishing only editable dates."""
        self._create_timeline_partner_pack()

    def _record_partner_pack_history(self, prices, timestamp, result, name, compare_timestamp, *, official=True):
        """Keep every successful pack's exact input, without overwriting earlier packs."""
        history = FILES.editor_history_root
        history.mkdir(parents=True, exist_ok=True)
        token = timestamp
        if any((history / f"manual_prices_{token}{suffix}").exists() for suffix in (".csv", ".xlsx")) or self.export_metadata_path(token).exists():
            token = f"{timestamp}_{datetime.now():%H%M%S_%f}"
        path = history / f"manual_prices_{token}.csv"
        with path.open("x", encoding="utf-8", newline="") as stream:
            prices.to_csv(stream, index=False)
        self.save_export_metadata(
            timestamp=token, official=official, partner_zip=result.zip_path,
            compare_timestamp=compare_timestamp, pack_result=result, name=name,
        )
        return path

    def _export_timeline_excel(self) -> None:
        self._export_key_date_prices(official=False)

    def _create_timeline_partner_pack(self) -> None:
        self._export_key_date_prices(official=True)

    def _export_key_date_prices(self, *, official: bool = True) -> None:
        """Download displayed prices; only editable dated packs publish changes."""
        key_date = self._active_key_date
        create_pack = official  # Retain compatibility with the two button callbacks.
        publish = create_pack and self._dated_edit_allowed()
        if key_date is None:
            # Reload today's official projection so a long-running app or a
            # date rollover cannot export stale current prices.
            self._price_view_cache.clear()
            self._load_price_view_data(None)
        if self.autosave_dirty:
            try:
                self._save_key_date_prices(silent=True)
            except Exception as exc:
                return QMessageBox.warning(self, "Export prices", str(exc))
        effective_date = date.fromisoformat(key_date) + timedelta(days=1) if key_date else date.today()
        timestamp = effective_date.strftime("%Y%m%d")
        pack_name = ""
        if create_pack:
            prompt = ("Pack name (publishes the included dated price changes):" if publish else
                      "Reference pack name (exports displayed prices; nothing is published):")
            pack_name, accepted = QInputDialog.getText(self, "Create partner pack", prompt)
            pack_name = pack_name.strip()
            if not accepted or not pack_name:
                return
        date_dir = (FILES.editor_history_root.parent / f"partner_pack_stage_{timestamp}"
                    if create_pack else FILES.work_dir / "promo_planner" / "key_date_exports" / timestamp)
        date_dir.mkdir(parents=True, exist_ok=True)
        stage_book = date_dir / "manual_prices_current.xlsx"
        export_path = stage_book
        if not create_pack:
            suggested = FILES.editor_exports_dir / f"manual_prices_{effective_date.isoformat()}.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, f"Export Excel for {effective_date:%d %b %Y}",
                                                str(suggested), "Excel workbooks (*.xlsx)")
            if not path:
                return
            export_path = Path(path).with_suffix(".xlsx")
        try:
            prices = self.state.export_prices_dataframe()
            if prices.empty:
                raise ValueError("There are no displayed prices to export.")
            pending_pa = prices.get("_PromoPlannerPAUnresolved", pd.Series(False, index=prices.index))
            if publish and pending_pa.astype(str).str.lower().isin({"true", "1", "1.0"}).any():
                raise ValueError("PA adjustments below cost need approval in Preview & resolve, or removal before publication.")
            write_pricing_workbook(prices, stage_book, promo_catalog=self.state.promo_catalog)
            if export_path.resolve() != stage_book.resolve():
                write_pricing_workbook(prices, export_path, promo_catalog=self.state.promo_catalog)
        except Exception as exc:
            return QMessageBox.critical(self, "Export failed", str(exc))
        if not create_pack:
            QMessageBox.information(self, "Prices exported", f"Prices for {effective_date:%d %b %Y} saved to:\n{export_path}\n\nNothing was published.")
            return
        compare_timestamp = self.select_partner_compare_timestamp(timestamp)
        default_zip = self.official_partner_pack_filename(timestamp, pack_name)
        if not publish:
            default_zip = Path(default_zip).stem + "_REFERENCE.zip"
        default_dir = self.partner_pack_dir_for_status(True) if publish else FILES.partner_packs_dir / "reference"
        path, _ = QFileDialog.getSaveFileName(self, "Save partner pack ZIP", str(default_dir / default_zip), "Zip files (*.zip)")
        if not path:
            return
        try:
            zip_path = (self.partner_zip_path_in_status_folder(Path(path), True) if publish
                        else Path(path).with_suffix(".zip"))
            if not publish and not zip_path.stem.upper().endswith("_REFERENCE"):
                zip_path = zip_path.with_name(zip_path.stem + "_REFERENCE.zip")
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            result = build_partner_price_pack(date_dir, zip_path, compare_timestamp=compare_timestamp,
                current_timestamp=timestamp, destination_table_json=FILES.destination_table_json,
                regions_yaml=FILES.regions_yaml, ppg_csv=FILES.ppg_csv)
            history_path = self._record_partner_pack_history(prices, timestamp, result, pack_name,
                                                            compare_timestamp, official=publish)
        except Exception as exc:
            return QMessageBox.critical(self, "Partner pack failed", str(exc))
        if publish:
            try:
                self.timeline.mark_price_changes_official_through(key_date, partner_pack=str(result.zip_path))
                self._promo_store().mark_campaigns_official(effective_date.isoformat(), str(result.zip_path))
                self._price_view_cache.clear()
                self._refresh_manual_baseline()
                self._refresh_key_date_views(selected=key_date)
                self._update_price_view_controls()
                self.refresh_campaign_panel()
            except Exception as exc:
                return QMessageBox.critical(self, "Official price update", f"The pack and history were created, but publishing failed:\n{exc}")
        message = ("Official pack created. Dated price changes are published; promotions keep their scheduled dates."
                   if publish else "Reference pack created. No prices or campaigns were published or changed.")
        QMessageBox.information(self, "Partner pack complete", f"{message}\n\nPartner pack: {result.zip_path}\nHistory CSV: {history_path}")

    def populate_combos(self):
        self.country_combo.blockSignals(True)
        self.country_combo.clear()
        countries = self.state.country_destinations()
        regions = self.state.region_destinations()
        self.country_combo.addItems(countries)
        if countries and regions:
            self.country_combo.insertSeparator(self.country_combo.count())
        if regions:
            self.country_combo.addItems(regions)

        if self.state.selected_country:
            idx = self.country_combo.findText(self.state.selected_country)
            if idx >= 0:
                self.country_combo.setCurrentIndex(idx)

        self.country_combo.blockSignals(False)

    def refresh_impact_labels(self):
        currency = self.state.active_currency
        self.impact_title.setText(f"30d sales impact · {currency}")
        # No sales data → everything grey
        if not self.state.sales_by_scope:
            self.impact_label.setText("—")
            self.total_impact_label.setText("—")
            self.unit_last_month_label.setText("—")
            self.total_last_month_label.setText("—")
            self.unit_projected_label.setText("—")
            self.total_projected_label.setText("—")

            for lbl in [
                self.impact_label,
                self.total_impact_label,
                self.unit_last_month_label,
                self.total_last_month_label,
                self.unit_projected_label,
                self.total_projected_label,
            ]:
                lbl.setStyleSheet("color: #999999;")

            return

        # Compute values
        unit_impact = self.state.revenue_impact_selected_pricing_unit()
        total_impact = self.state.revenue_impact_total()

        unit_last = self.state.revenue_last_month_selected_pricing_unit()
        total_last = self.state.revenue_last_month_total()

        unit_projected = unit_last + unit_impact
        total_projected = total_last + total_impact

        # Set text
        self.impact_label.setText(f"{unit_impact:+,.2f}")
        self.total_impact_label.setText(f"{total_impact:+,.2f}")

        self.unit_last_month_label.setText(f"{unit_last:,.2f}")
        self.total_last_month_label.setText(f"{total_last:,.2f}")

        self.unit_projected_label.setText(f"{unit_projected:,.2f}")
        self.total_projected_label.setText(f"{total_projected:,.2f}")
        impact_box_tip = f"Values shown in {currency}"
        for lbl in [self.unit_last_month_label, self.impact_label, self.unit_projected_label,
                    self.total_last_month_label, self.total_impact_label, self.total_projected_label]:
            lbl.setToolTip(impact_box_tip)

        # Colors
        def color(v):
            return "#1b8f3a" if v >= 0 else "#c62828"

        self.impact_label.setStyleSheet(f"color: {color(unit_impact)}; font-weight: bold;")
        self.total_impact_label.setStyleSheet(f"color: {color(total_impact)}; font-weight: bold;")

        # Neutral (loaded price-book numbers)
        self.unit_last_month_label.setStyleSheet("color: #333333;")
        self.total_last_month_label.setStyleSheet("color: #333333;")

        # Projected colored vs change
        self.unit_projected_label.setStyleSheet(f"color: {color(unit_impact)}; font-weight: bold;")
        self.total_projected_label.setStyleSheet(f"color: {color(total_impact)}; font-weight: bold;")

    def refresh_currency_visuals(self):
        is_linked = self.state.currency_mode == LINKED_USD_MODE
        self.currency_mode_banner.setVisible(False)
        self.side_panel_content.setStyleSheet(
            """
            QWidget#SidePanelContent {
                border-left: 5px solid #d6a400;
            }
            """ if is_linked else ""
        )

    def on_currency_changed(self, currency: str):
        self.state.set_active_currency(currency)
        self.refresh_currency_visuals()
        self.refresh_canvas()

    def on_currency_mode_changed(self, *_):
        self.state.set_linked_currency_mode(self.dual_currency_check.isChecked())
        self.refresh_currency_visuals()
        self.refresh_canvas()

    def on_exchange_rate_changed(self, rate: float):
        if not self._require_dated_edit():
            return
        self._ensure_price_event()
        self.state.set_eur_to_usd(float(rate))
        self.mark_dirty()
        self.refresh_canvas()

    def toggle_active_currency(self):
        current = normalize_currency(self.currency_combo.currentText())
        next_currency = "EUR" if current == "USD" else "USD"
        self.currency_combo.setCurrentText(next_currency)
        self.statusBar().showMessage(f"Editing currency: {next_currency}")

    def toggle_canvas_price_labels(self):
        self.canvas.show_prices = not self.canvas.show_prices
        self.canvas.update()
        mode = "prices" if self.canvas.show_prices else "GB"
        self.statusBar().showMessage(f"Chart labels: {mode}")

    def toggle_canvas_competitors(self):
        self.canvas.show_competitors = not self.canvas.show_competitors
        self.canvas.update()
        mode = "shown" if self.canvas.show_competitors else "hidden"
        self.statusBar().showMessage(f"Competitors: {mode}")

    def reset_canvas_zoom(self):
        self.canvas.reset_zoom()
        self.statusBar().showMessage("Zoom reset")

    def load_official_cost_exchange_rate(self) -> None:
        cache_path = FILES.diagnostics_dir / "fx_rates" / "official_eur_usd_latest.json"
        rate = get_official_eur_usd(
            cache_path,
            fallback_rate=self.state.cost_eur_to_usd,
            timeout_seconds=4.0,
        )
        self.state.set_official_cost_eur_to_usd(
            rate.rate,
            source=rate.source,
            date=rate.date,
            status=rate.status,
        )
        self.official_cost_rate_label.setText(rate.label)
        self.official_cost_rate_label.setToolTip(
            f"ECB reference-rate download failed: {rate.error}\nUsing the {rate.status} rate."
            if rate.error else "ECB euro reference rate downloaded successfully."
        )

    def on_country_changed(self, country: str):
        self.state.selected_country = country

        points = self.state.current_points()

        self.state.selected_row_id = (
            str(points[0]["row_id"]) if points else None
        )

        self.refresh_canvas()

    def refresh_canvas(self):
        if self.canvas.is_dragging:
            # Keep prices/floor markers live; defer global totals and unchanged UI.
            self.canvas.set_curve_points(self.state.current_points(), self.state.selected_row_id)
            return
        promo_markers = []
        if self.canvas.show_promo_markers and not self.canvas.is_dragging:
            promo_markers = self.state.promo_candidate_markers()

        self.canvas.set_data(
            self.state.current_competitors(),
            self.state.current_points(),
            promo_markers,
            self.state.selected_row_id,
            title=f"{self.state.selected_country or ''} ({self.state.active_currency})",
        )

        self.refresh_currency_visuals()
        self.refresh_selection_label()
        self.country_info_label.setText(self.state.country_info())
        self.refresh_impact_labels()
        

    def refresh_pricing_intelligence(self, *, force: bool = False, refresh: bool = True) -> bool:
        """Refresh BI from published database prices effective today."""
        price_book = FILES.base_dir / "outputs/promo_planner/promo_schedule.db"
        if not price_book.exists() or not FILES.market_annotated.exists():
            return False

        cursor_active = False
        try:
            self.statusBar().showMessage("Refreshing pricing recommendations and market insights...")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            cursor_active = True
            QApplication.processEvents()

            stale = recommendation_outputs_are_stale(
                FILES, price_book_path=price_book, recommendations_path=RECOMMENDATIONS_PATH
            )
            if force or stale:
                generate_recommendations_from_files(
                    FILES,
                    price_book_path=price_book,
                    market_path=FILES.market_annotated,
                    output_path=RECOMMENDATIONS_PATH,
                )
            else:
                if force or market_insights_is_stale(
                    FILES,
                    recommendations_path=RECOMMENDATIONS_PATH,
                    output_path=MARKET_INSIGHTS_PATH,
                ):
                    generate_market_insights(FILES, recommendations_path=RECOMMENDATIONS_PATH)

            self.load_recommendations(refresh=refresh)
            self.statusBar().showMessage("Pricing recommendations and market insights are up to date.")
            return True
        except Exception as exc:
            print("Pricing intelligence refresh failed:", exc)
            self.statusBar().showMessage(f"Pricing intelligence refresh failed: {exc}")
            return False
        finally:
            if cursor_active:
                self.clear_busy_cursor()

    def open_market_insights(self):
        # Intelligence is refreshed by the BI pipeline, never by this button.
        if not MARKET_INSIGHTS_PATH.is_file():
            QMessageBox.information(
                self, "Market insights",
                "No Market Insights report is available yet. Run Market Insights in the BI pipeline first.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(MARKET_INSIGHTS_PATH.resolve())))

    def load_recommendations(self, *_args, refresh: bool = True):
        if not RECOMMENDATIONS_PATH.exists():
            self.state.preload_recommendations(pd.DataFrame(), source=RECOMMENDATIONS_PATH)
            if refresh and self.state.countries():
                self.refresh_canvas()
            return False

        try:
            df = pd.read_csv(RECOMMENDATIONS_PATH)
            self.state.preload_recommendations(df, source=RECOMMENDATIONS_PATH)
        except Exception as exc:
            self.state.preload_recommendations(pd.DataFrame(), source=RECOMMENDATIONS_PATH)
            self.statusBar().showMessage(f"Could not load recommendation arrows: {exc}")
            if refresh and self.state.countries():
                self.refresh_canvas()
            return False

        if refresh and self.state.countries():
            self.refresh_canvas()
        self.statusBar().showMessage(
            f"Loaded {self.state.recommendations_actionable_rows} actionable recommendations."
        )
        return True


    def approve_point_below_cost(self, row_id: str):
        self.state.selected_row_id = str(row_id)
        point = self.state.selected_point_info()
        if point is None:
            return
        key = self._active_key_date or (date.today() - timedelta(days=1)).isoformat()
        if date.fromisoformat(key) + timedelta(days=1) < date.today():
            self.statusBar().showMessage("Choose Today or a future date to approve below-cost export.", 8000)
            return
        try:
            try:
                from automation.pricing_editor.floor_approval import approve_below_cost_point
            except ImportError:
                from pricing_editor.floor_approval import approve_below_cost_point
            if self.autosave_dirty and self._active_key_date:
                self._save_key_date_prices(silent=True)
                point = self.state.selected_point_info()
                if point is None:
                    return
            count = approve_below_cost_point(FILES.base_dir, key, point, self._promo_store().list_campaigns())
            if not count:
                self.statusBar().showMessage("This point does not need a below-cost override.", 8000)
                return
            self._price_view_cache.clear()
            self._load_price_view_data(self._active_key_date)
            self.statusBar().showMessage("Below-cost export approved for this product in its below-floor currencies. Green tick = approved.", 10000)
        except Exception as exc:
            QMessageBox.warning(self, "Approve below-cost export", str(exc))

    def on_point_selected(self, row_id: str):
        self.state.selected_row_id = str(row_id)
        self.refresh_canvas()

    def _open_promotion_for_selected_date(self, promo_code: str) -> None:
        if not self._require_dated_edit():
            return
        if promo_code == "__REMOVE_PROMO__":
            self.on_manual_promo_selected_from_chart(promo_code, False)
            return
        planner = self.planner_workspace
        if planner is None:
            return
        effective = date.fromisoformat(self._active_key_date) + timedelta(days=1)
        point = self.state.selected_point_info()
        label = f"Promotion {effective:%d %b %Y}"
        self.timeline.schedule_promotion_date(self._active_key_date, label)
        planner.begin_campaign_on_date(effective.isoformat(), label=label)
        if point:
            planner.selected_destinations = [str(point.get("iso", ""))]
            planner._render_selected()
            for plan, check in planner.plan_checks.items():
                check.setChecked(plan == point.get("plan"))
            for day, button in planner.day_buttons.items():
                button.setChecked(day == int(point.get("days") or 0))
        for index, option in enumerate(planner._promo_by_display.values()):
            if option.code == promo_code:
                planner.promo_combo.setCurrentIndex(index)
                break
        self._refresh_key_date_views(selected=self._active_key_date)
        self._set_workspace_mode("campaigns")
        planner.tabs.setCurrentWidget(planner.create_tab)

    def on_promo_selected_from_chart(self, promo_code: str):
        self.on_manual_promo_selected_from_chart(promo_code, False)

    def on_manual_promo_selected_from_chart(self, promo_code: str, align_shorter: bool):
        if not self._require_dated_edit():
            return
        if promo_code == "__REMOVE_PROMO__":
            point = self.state.selected_point_info()
            if point is None:
                self.statusBar().showMessage("Select a price point to remove its manual promo.", 8000)
                return
            if point.get("promo_planner_owner") and not self.state.selected_has_manual_pa_dependents():
                self.open_promo_planner()
                self.statusBar().showMessage("This promo belongs to a scheduled campaign; manage its dates in Campaigns.", 8000)
                return
        before = self.state.export_prices_dataframe(refresh=False)
        try:
            effective = (date.fromisoformat(self._active_key_date) + timedelta(days=1)).isoformat()
            active_ids = {str(int(c.id or 0)) for c in self._promo_store().list_campaigns()
                          if (not c.start_date or c.start_date <= effective) and c.end_date >= effective}
            count = self.state.assign_manual_promo(promo_code, align_shorter=align_shorter,
                                                  active_campaign_ids=active_ids, effective_date=effective)
            if not count:
                return
            self.mark_dirty()
            self._price_view_cache.clear()
            self.refresh_canvas()
            self.statusBar().showMessage(
                ("Manual promo removed: " if promo_code == "__REMOVE_PROMO__" else "Manual promo applied: ") +
                f"{count} price point(s) updated" +
                ("; shorter durations aligned." if align_shorter else ".") + " Autosave pending; Ctrl+S saves now.", 8000)
        except Exception as exc:
            self.state.load_timeline_view(before)
            self.refresh_canvas()
            QMessageBox.warning(self, "Apply manual promo", str(exc))

    def on_price_drag_finished(self):
        self._drag_price_event_key = None
        self.refresh_canvas()

    def on_point_dragged(self, row_id: str, new_price: float, point_index: int):
        if not self._require_dated_edit():
            return
        # The date marker only needs checking once per gesture, not per mouse move.
        if not self.canvas.is_dragging or getattr(self, "_drag_price_event_key", None) != self._active_key_date:
            self._ensure_price_event()
            if self.canvas.is_dragging:
                self._drag_price_event_key = self._active_key_date
        self.state.selected_row_id = row_id
        mode = self.current_drag_mode

        if mode == "inflate":
            self.state.inflate_curve_at_point(point_index, new_price)
        elif mode == "concave":
            self.state.apply_concave_curve(point_index, new_price)
        elif mode == "rotate_left":
            self.state.rotate_curve_legacy(point_index, new_price, side="left")
        elif mode == "rotate_right":
            self.state.rotate_curve_legacy(point_index, new_price, side="right")
        elif mode == "rotate_both":
            self.state.rotate_curve_legacy(point_index, new_price, side="both")
        elif mode == "neighbors":
            self.state.nudge_neighbors(point_index, new_price)
        elif mode == "shift_abs_up":
            delta = new_price - self.state.current_points()[point_index]["working_y"]
            self.state.shift_curve_absolute(point_index, delta)
        elif mode == "shift_pct_up":
            current = self.state.current_points()[point_index]["working_y"]
            pct = 0.0 if current == 0 else (new_price - current) / current
            self.state.shift_curve_percent(point_index, pct)
        else:
            self.state.set_scope_price(row_id, new_price)

        self.mark_dirty()
        self.refresh_canvas()


    def refresh_selection_label(self):
        info = self.state.selected_point_info()
        if not info:
            self.selection_label.setText("No point selected")
            self.region_floor_label.clear()
            self.region_floor_label.setVisible(False)
            return

        plan = partner_display_plan_label(info["plan"])
        days = float(info.get("days") or 0)
        days_text = str(int(days)) if days.is_integer() else str(days)
        gb = info.get("gb")
        gb_text = "—" if gb is None else f"{float(gb):g} GB"
        currency = self.state.active_currency
        working = float(info.get("y", 0.0))
        loaded = float(info.get("base_y", 0.0))
        promo = str(info.get("promo", "") or "").strip()

        lines = [
            f"{plan} · {days_text}d · {gb_text}",
            f"Price {working:.2f} {currency} · loaded {loaded:.2f}",
        ]
        if promo:
            lines[-1] += f" · promo {promo}"

        self.selection_label.setText("\n".join(lines))

        floor_info = self.state.regional_floor_driver_info(info, currency)
        if floor_info:
            driver = str(floor_info.get("driver_label", ""))
            current_floor = float(floor_info.get("current_floor", 0.0))
            without_floor = floor_info.get("without_driver_floor")
            floor_line = f"Floor {current_floor:.2f} {currency} · driver {driver}"
            if without_floor is not None:
                floor_line += f" · without {float(without_floor):.2f}"
            self.region_floor_label.setText(floor_line)
            self.region_floor_label.setVisible(True)
        else:
            self.region_floor_label.clear()
            self.region_floor_label.setVisible(False)


    def toggle_left_panel(self):
        if self._left_panel_collapsed:
            self.side_panel.show()
            self.side_panel.setMinimumWidth(0)
            self.side_panel.setMaximumWidth(16777215)
            self._set_workspace_mode("prices")
            self.toggle_side_btn.setText("◀")
            self._left_panel_collapsed = False
        else:
            self.side_panel.setMinimumWidth(0)
            self.side_panel.setMaximumWidth(16777215)
            self._set_workspace_mode("canvas")
            self.toggle_side_btn.setText("▶")
            self._left_panel_collapsed = True

    
    def set_brush_start(self):
        self.state.set_brush_start()

    def set_brush_end(self):
        self.state.set_brush_end()

    def clear_brush_range(self):
        self.state.clear_brush()

    def remove_selected_promo(self):
        self._open_promotion_for_selected_date("__REMOVE_PROMO__")

    def try_auto_load(self):
        self.timeline.set_current_as_of(date.today().isoformat())

        def progress(message: str) -> None:
            self.statusBar().showMessage(message)
            QApplication.processEvents()

        progress("Loading official EUR/USD cost rate...")
        self.load_official_cost_exchange_rate()

        if PPG_PATH.exists():
            try:
                progress("Loading PPG cost lookup...")
                self.state.ppg_df = pd.read_csv(PPG_PATH, keep_default_na=False)
                self.state.ppg_df.columns = self.state.ppg_df.columns.astype(str).str.strip()

                ppg = self.state.ppg_df.copy()
                ppg["ISO_Code_A2"] = ppg["ISO_Code_A2"].astype(str).str.strip().str.upper()
                ppg["Min of Min"] = pd.to_numeric(ppg["Min of Min"], errors="coerce")

                self.state.ppg_cost_by_iso = (
                    ppg.dropna(subset=["ISO_Code_A2", "Min of Min"])
                    .set_index("ISO_Code_A2")["Min of Min"]
                    .astype(float)
                    .to_dict()
                )

                print("Loaded PPG cost lookup:", len(self.state.ppg_cost_by_iso), "countries")

            except Exception as e:
                print(f"Could not load PPG file: {e}")
        else:
            print("PPG file not found:", PPG_PATH)
        
        if PROMOS_PATH.exists():
            progress("Loading promo catalog...")
            self.state.promo_catalog = load_promos(PROMOS_PATH)

        progress("Loading current price book...")
        current_path = FILES.editor_exports_dir / "manual_prices_current.xlsx"
        if not current_path.exists():
            legacy_current = FILES.editor_exports_dir / "manual_prices_current.csv"
            current_path = legacy_current if legacy_current.exists() else current_path
        canonical = self.timeline.project()
        if canonical.empty and current_path.exists():
            # One-time migration path for an existing dev workspace. Once the
            # baseline is in SQLite, normal launches never read this workbook.
            imported = load_table(
                current_path,
                currency_hint="EUR",
                eur_to_usd=self.state.eur_to_usd,
            )
            if not imported.empty:
                self.timeline.initialize_baseline(
                    imported,
                    source_path=current_path,
                    effective_from=date.today().isoformat(),
                )
                canonical = self.timeline.project()
                self._manual_pricebook_path = current_path

        if canonical.empty:
            progress("No pricing baseline found. Use Import Excel to review and initialize the timeline.")
        else:
            progress("Loading current prices from the shared timeline...")
            self._manual_pricebook_df = canonical.copy()
            self.state.preload_pricebook(canonical)
            self.state.preload_last_export(canonical)

        progress("Loading competitor market data...")
        df_market = self.load_currency_tables_from_folder(
            FILES.market_dir,
            ["market_prices_annotated_latest.csv"],
        )
        if not df_market.empty:
            progress("Preparing competitor market data...")
            self.state.preload_market(df_market)

        if SALES_VOLUME_PATH.exists():
            progress("Loading sales volumes...")
            if SALES_VOLUME_PATH.suffix.lower() == ".csv":
                sales_df = pd.read_csv(SALES_VOLUME_PATH, low_memory=False)
            else:
                sales_df = pd.read_excel(SALES_VOLUME_PATH)
            sales_df.columns = sales_df.columns.astype(str).str.strip()
            if not sales_df.empty:
                self.state.preload_sales_volumes(sales_df)
        else:
            print("Sales master not found:", SALES_VOLUME_PATH)

        # Recommendations and Market Insights are precomputed by the weekly-pack
        # importer.  Startup must stay lightweight: only load the existing CSV.
        # Market Insights refreshes intelligence separately from price editing.
        progress("Loading pricing recommendations...")
        self.load_recommendations(refresh=False)


        if self.state.countries():
            progress("Drawing editor...")
            self.populate_combos()
            self._active_key_date = None
            self._refresh_key_date_views()
            self._load_price_view_data(None)
            self.statusBar().showMessage("Auto-loaded available files.")
        else:
            self.statusBar().showMessage(
                "Auto-load found nothing usable. Check the current price book and market files."
            )
        self.autosave_dirty = False


def run(initial_workspace: str = "prices"):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    window = MainWindow(initial_workspace=initial_workspace)
    window.show()
    app.exec()
