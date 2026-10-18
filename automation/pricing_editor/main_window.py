from __future__ import annotations

import json
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
    QProgressDialog,
    QPushButton,
    QSplitter,
    QScrollArea,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .canvas import PriceCurveCanvas
from .state import EditorState, load_promos, load_table
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
        self.current_drag_mode = "inflate"
        self.mode_buttons: dict[str, QToolButton] = {}
        self.autosave_dirty = False
        self.autosave_in_progress = False
        self._manual_pricebook_df = pd.DataFrame()
        self._manual_pricebook_path: Path | None = None
        self._active_key_date: str | None = None
        self.initial_workspace = initial_workspace
        self._date_view_baseline_df = pd.DataFrame()
        self._switching_price_view = False
        self._using_history_snapshot = False
        self.selected_promo_code_for_range: str | None = None
        self._promo_range_context_key: tuple[str, str] | None = None
        self._build_ui()
        self.statusBar().showMessage("Opening editor...")
        QTimer.singleShot(100, self.try_auto_load)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.timeout.connect(self.autosave)
        self.autosave_timer.start(30000)  # every 30 seconds

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
        self.autosave_dirty = True

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

        self.side_panel_content = QWidget()
        self.side_panel_content.setObjectName("SidePanelContent")
        side_layout = QVBoxLayout(self.side_panel_content)

        self.side_panel.setWidget(self.side_panel_content)

        self.country_combo = QComboBox()
        self.country_combo.currentTextChanged.connect(self.on_country_changed)

        self.saved_state_combo = QComboBox()
        self.saved_state_combo.setPlaceholderText("Select exported date")
        self.saved_state_combo.setEnabled(False)
        self.saved_state_combo.currentIndexChanged.connect(self.on_saved_state_selected)

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

        load_grid = QGridLayout()
        load_grid.setHorizontalSpacing(6)
        load_grid.setVerticalSpacing(6)
        load_grid.addWidget(QLabel("Previous export"), 0, 0)
        load_grid.addWidget(self.saved_state_combo, 0, 1)

        self.key_date_combo = QComboBox()
        self.key_date_combo.addItem("Current manual prices", "")
        self.key_date_combo.setEnabled(False)
        self.key_date_combo.setMinimumWidth(300)
        self.key_date_combo.setToolTip("Choose current prices or a scheduled campaign or price-change date.")
        self.key_date_combo.setStyleSheet(
            "QComboBox { background:#3D424A; color:#FFFFFF; border:1px solid #FFFFFF; border-radius:5px; "
            "padding:5px 8px; min-height:24px; }"
            "QComboBox:hover { background:#4A5059; }"
            "QComboBox::drop-down { border:0; width:24px; }"
            "QComboBox QAbstractItemView { background:#3D424A; color:#FFFFFF; "
            "selection-color:#FFFFFF; selection-background-color:#59616B; }"
        )
        self.key_date_combo.currentIndexChanged.connect(self.on_key_date_view_changed)
        self.key_date_combo.setMinimumWidth(560)
        self.key_date_combo.setToolTip(
            "Shared chronological timeline: choose a campaign start/end or a planned price change."
        )
        self.date_toolbar = QToolBar("Shared pricing and campaign timeline", self)
        self.date_toolbar.setMovable(False)
        self.date_toolbar.setFloatable(False)
        self.date_toolbar.addWidget(QLabel("Timeline"))
        self.date_toolbar.addSeparator()
        self.date_toolbar.addWidget(self.key_date_combo)
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

        self.currency_combo = QComboBox()
        self.currency_combo.addItems(list(CURRENCIES))
        self.currency_combo.setCurrentText(self.state.active_currency)
        self.currency_combo.currentTextChanged.connect(self.on_currency_changed)

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
        currency_grid.addWidget(QLabel("Active currency"), 0, 0)
        currency_grid.addWidget(self.currency_combo, 0, 1)
        currency_grid.addWidget(QLabel("Pricing EUR/USD"), 1, 0)
        currency_grid.addWidget(self.exchange_rate_spin, 1, 1)
        currency_grid.addWidget(QLabel("Official cost EUR/USD"), 2, 0)
        currency_grid.addWidget(self.official_cost_rate_label, 2, 1)
        currency_grid.addWidget(self.dual_currency_check, 3, 0, 1, 2)

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

        self.recommendation_status_label = QLabel("Recommendations: not loaded")
        self.recommendation_status_label.setWordWrap(True)
        self.recommendation_status_label.setStyleSheet("color: #444444;")
        self.reload_recommendations_btn = QPushButton("Refresh recommendations")
        self.reload_recommendations_btn.setFixedHeight(32)
        self.reload_recommendations_btn.setToolTip("Recalculate recommendations from the current price book and latest market data.")
        self.reload_recommendations_btn.clicked.connect(lambda: self.refresh_pricing_intelligence(force=True, refresh=True))

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

        self.export_btn = QPushButton("Export prices…")
        self.export_btn.setToolTip(
            "Create an optional Excel copy of the shared timeline, or create the official partner pack."
        )
        self.export_btn.clicked.connect(self.export_prices)
        self.import_excel_btn = QPushButton("Import Excel…")
        self.import_excel_btn.setToolTip(
            "Review changes from an Excel price book and record accepted changes in the shared timeline."
        )
        self.import_excel_btn.clicked.connect(self.load_pricebook)

        remove_promo_btn = QPushButton("❌ Remove selected promo")
        remove_promo_btn.clicked.connect(self.remove_selected_promo)

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

        self.promo_list = QListWidget()
        self.promo_list.setMinimumHeight(100)
        self.promo_list.setMaximumHeight(160)
        self.promo_list.itemClicked.connect(self.apply_promo)

        self.promo_range_selected_label = QLabel("Selected promo: —")
        self.promo_range_selected_label.setWordWrap(True)

        self.promo_from_combo = QComboBox()
        self.promo_to_combo = QComboBox()

        promo_range_grid = QGridLayout()
        promo_range_grid.setHorizontalSpacing(6)
        promo_range_grid.setVerticalSpacing(4)
        promo_range_grid.addWidget(QLabel("From"), 0, 0)
        promo_range_grid.addWidget(self.promo_from_combo, 0, 1)
        promo_range_grid.addWidget(QLabel("To"), 0, 2)
        promo_range_grid.addWidget(self.promo_to_combo, 0, 3)

        self.apply_promo_range_btn = QPushButton("Apply selected promo to range")
        self.apply_promo_range_btn.clicked.connect(self.apply_selected_promo_to_range)
        self.remove_promo_range_btn = QPushButton("Remove promo from range")
        self.remove_promo_range_btn.clicked.connect(self.remove_promo_from_range)

        promo_range_buttons = QHBoxLayout()
        promo_range_buttons.addWidget(self.apply_promo_range_btn)
        promo_range_buttons.addWidget(self.remove_promo_range_btn)

        self.promo_range_box = QWidget()
        promo_range_layout = QVBoxLayout(self.promo_range_box)
        promo_range_layout.setContentsMargins(0, 0, 0, 0)
        promo_range_layout.addWidget(self.promo_range_selected_label)
        promo_range_layout.addLayout(promo_range_grid)
        promo_range_layout.addLayout(promo_range_buttons)

        # Sales impact is global context: it stays visible above both working tabs.
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

        # Two compact working modes: normal price modelling vs promos/setup.
        self.side_tabs = QTabWidget()
        self.side_tabs.setStyleSheet(
            "QTabBar::tab { background:#3D424A; color:#FFFFFF; border:1px solid #FFFFFF; "
            "padding:7px 15px; margin-right:3px; border-top-left-radius:5px; border-top-right-radius:5px; }"
            "QTabBar::tab:selected { background:#59616B; color:#FFFFFF; font-weight:600; }"
            "QTabBar::tab:hover:!selected { background:#4A5059; }"
            "QTabWidget::pane { border:1px solid #FFFFFF; border-radius:4px; top:-1px; }"
        )

        model_tab = QWidget()
        model_layout = QVBoxLayout(model_tab)
        model_layout.setContentsMargins(6, 6, 6, 6)
        model_layout.setSpacing(6)
        model_layout.addWidget(self.country_info_label)
        model_layout.addWidget(QLabel("Curve tools"))
        model_layout.addWidget(self.drag_safety_label)
        model_layout.addLayout(mode_row_1)
        model_layout.addLayout(mode_row_2)
        model_layout.addLayout(mode_row_3)
        model_layout.addWidget(self.recommendation_status_label)
        recommendation_buttons = QHBoxLayout()
        recommendation_buttons.addWidget(self.reload_recommendations_btn)
        recommendation_buttons.addWidget(self.open_market_insights_btn)
        model_layout.addLayout(recommendation_buttons)
        model_layout.addWidget(self.selection_label)
        model_layout.addWidget(self.region_floor_label)
        model_layout.addWidget(reset_zoom_btn)
        model_layout.addLayout(reset_grid)
        model_layout.addStretch(1)

        promo_tab = QWidget()
        promo_layout = QVBoxLayout(promo_tab)
        promo_layout.setContentsMargins(6, 6, 6, 6)
        promo_layout.setSpacing(6)
        promo_layout.addWidget(QLabel("Promo range"))
        promo_layout.addWidget(self.promo_range_box)
        promo_layout.addWidget(QLabel("Promo options"))
        promo_layout.addWidget(self.promo_list)
        promo_layout.addStretch(1)

        self.side_tabs.addTab(model_tab, "Model")
        self.side_tabs.addTab(promo_tab, "Promos")

        campaigns_tab = QWidget()
        campaigns_tab.setMinimumWidth(0)
        campaigns_tab.setStyleSheet("QWidget { font-size: 8pt; } QPushButton { padding: 3px 5px; }")
        campaigns_layout = QVBoxLayout(campaigns_tab)
        campaigns_layout.setContentsMargins(6, 6, 6, 6)
        campaigns_layout.setSpacing(6)
        campaigns_layout.addWidget(QLabel("Campaign timeline · dates follow the shared selector above"))
        self.campaign_timeline_scene = QGraphicsScene(self)
        self.campaign_timeline_view = QGraphicsView(self.campaign_timeline_scene)
        self._campaigns_cache = []
        self.campaign_timeline_view.setMinimumHeight(165)
        self.campaign_timeline_view.setMaximumHeight(230)
        self.campaign_timeline_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.campaign_timeline_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.campaign_timeline_view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.campaign_timeline_view.setFrameShape(QFrame.NoFrame)
        campaigns_layout.addWidget(self.campaign_timeline_view)
        campaigns_layout.addWidget(QLabel("Campaigns"))
        self.campaign_list = QListWidget()
        self.campaign_list.setMinimumHeight(110)
        self.campaign_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.campaign_list.currentItemChanged.connect(self._show_campaign_details)
        campaigns_layout.addWidget(self.campaign_list, 2)
        self.campaign_detail_label = QLabel("Select a campaign to see its dates and affected destinations.")
        self.campaign_detail_label.setWordWrap(True)
        campaigns_layout.addWidget(self.campaign_detail_label)
        campaigns_layout.addWidget(QLabel("Affected destinations · select to open in editor"))
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
        campaigns_layout.addLayout(campaign_actions)

        campaign_scroll = QScrollArea()
        campaign_scroll.setWidgetResizable(True)
        campaign_scroll.setFrameShape(QFrame.NoFrame)
        campaign_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        campaign_scroll.setWidget(campaigns_tab)
        self.campaigns_panel = campaign_scroll
        self.side_tabs.addTab(campaign_scroll, "Campaigns")

        # Currency/data and export actions affect both modelling and promos,
        # so keep them outside the tabs. This also avoids duplicating controls
        # and leaves each tab focused on the work that is specific to it.
        global_actions = QHBoxLayout()
        global_actions.setSpacing(6)
        global_actions.addWidget(self.import_excel_btn)
        global_actions.addWidget(self.export_btn)

        side_layout.addWidget(QLabel("Destination"))
        side_layout.addWidget(self.country_combo)
        side_layout.addWidget(impact_box)
        side_layout.addWidget(QLabel("Currency & data"))
        side_layout.addLayout(currency_grid)
        side_layout.addWidget(self.currency_mode_banner)
        side_layout.addWidget(self.key_date_status)
        side_layout.addLayout(load_grid)
        side_layout.addLayout(global_actions)
        side_layout.addWidget(self.side_tabs, 1)

        # Canvas
        self.canvas = PriceCurveCanvas()
        reset_zoom_btn.clicked.connect(self.canvas.reset_zoom)
        self.canvas.pointSelected.connect(self.on_point_selected)
        self.canvas.pointDragged.connect(self.on_point_dragged)
        self.canvas.recommendationSelected.connect(self.on_recommendation_selected)
        self.canvas.promoSelected.connect(self.on_promo_selected_from_chart)
        self.canvas.statusChanged.connect(self.statusBar().showMessage)

        self.splitter.addWidget(self.side_panel)
        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([350, 1130])
        self.splitter.setCollapsible(0, True)
        self.splitter.setCollapsible(1, False)
        self.side_tabs.currentChanged.connect(self._refresh_campaign_context_on_tab)
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
            planner.campaignsChanged.connect(self._on_campaigns_changed)
            self.side_tabs.removeTab(2)
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
            planner.tabs.setTabText(planner.tabs.indexOf(planner.create_tab), "Create promotion")
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
        self._refresh_key_date_views(selected=self.key_date_combo.currentData() or "")
        self.refresh_campaign_panel()

    def _reload_editor_from_shared_timeline(self, selected_date: str | None = None) -> None:
        current = self.timeline.project()
        if current.empty:
            return
        selected_country = self.state.selected_country
        self._manual_pricebook_df = current.copy()
        self._using_history_snapshot = False
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
            self.state.preload_pricebook(current)
            self.state.preload_last_export(current)
            self.populate_combos()
            if selected_country:
                country_index = self.country_combo.findText(selected_country, Qt.MatchFixedString)
                if country_index >= 0:
                    self.country_combo.setCurrentIndex(country_index)
            self._refresh_key_date_views()
            self.refresh_canvas()
        self.autosave_dirty = False
        self.refresh_campaign_panel()

    def _sync_planner_date_from_editor(self, _index: int = -1) -> None:
        selected = str(self.key_date_combo.currentData() or "")
        if self.planner_workspace is not None:
            self.planner_workspace.set_shared_key_date(selected or None)
        if hasattr(self, "campaign_timeline_view"):
            self._render_campaign_timeline(self._campaigns_cache)

    def _refresh_campaign_context_on_tab(self, index: int) -> None:
        self.refresh_campaign_panel()
        if not self._manual_pricebook_df.empty:
            self._refresh_key_date_views()

    def refresh_campaign_panel(self) -> None:
        """Refresh the compact campaign browser from the shared planner database."""
        if not hasattr(self, "campaign_list"):
            return
        current = self.campaign_list.currentItem()
        selected_id = current.data(Qt.UserRole) if current else None
        try:
            campaigns = self._promo_store().list_campaigns()
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
        for campaign in sorted(campaigns, key=lambda item: (item.start_date, item.name.casefold())):
            item = QListWidgetItem(campaign.name)
            item.setToolTip(f"{campaign.start_date} – {campaign.end_date} · {campaign.status}")
            item.setData(Qt.UserRole, campaign.id)
            self.campaign_list.addItem(item)
            if selected_id is not None and campaign.id == selected_id:
                self.campaign_list.setCurrentItem(item)
        if self.campaign_list.count() and self.campaign_list.currentRow() < 0:
            self.campaign_list.setCurrentRow(0)
        elif not self.campaign_list.count():
            self.campaign_detail_label.setText("No campaigns yet. Use the Create promotion tab to schedule one.")
            self.campaign_destinations_list.clear()
        self.edit_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)
        self.duplicate_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)
        self.delete_campaign_btn.setEnabled(self.campaign_list.currentItem() is not None)

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
            start = date.fromisoformat(campaign.start_date) if campaign.start_date else today
            end = date.fromisoformat(campaign.end_date) if campaign.end_date else today + timedelta(days=90)
            if end < start:
                end = start
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

        for row, (campaign, start, end) in enumerate(parsed):
            y = top + row * row_height
            label = scene.addText(campaign.name[:24], QFont("Segoe UI", 8, QFont.Bold))
            label.setDefaultTextColor(QColor("#252A31"))
            label.setPos(4, y + 2)
            label.setToolTip(campaign.name)
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
        promo = campaign.promo_name or campaign.promo_code or campaign.promo_type
        self.campaign_detail_label.setText(
            f"<b>{campaign.name}</b><br>{campaign.start_date} → {campaign.end_date}<br>"
            f"Promo: {promo} · {campaign.status}<br>Plans: {', '.join(campaign.plans) or 'All plans'}"
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
        self.side_tabs.setCurrentIndex(0)

    def open_promo_planner(self) -> None:
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
        if self.planner_workspace is None:
            self._retry_planner_workspace()
        if self.campaign_dock is not None and self.planner_workspace is not None:
            self._set_workspace_mode("campaigns")
            self.planner_workspace._cancel_edit()
            self.planner_workspace.tabs.setCurrentWidget(self.planner_workspace.create_tab)

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

    def duplicate_selected_campaign(self) -> None:
        item = self.campaign_list.currentItem()
        if item and self.planner_workspace and self._select_planner_campaign(int(item.data(Qt.UserRole))):
            self.planner_workspace._duplicate_selected()

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
            datetime.strptime(timestamp, "%Y%m%d")
        except ValueError:
            return None
        return timestamp

    @staticmethod
    def _history_timestamp_label(timestamp: str) -> str:
        try:
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
        return label

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
            "name": str(name).strip() if official else "",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "partner_pack_created": bool(official and partner_zip is not None),
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

    def refresh_saved_state_combo(self, selected_timestamp: str | None = None) -> None:
        if not hasattr(self, "saved_state_combo"):
            return

        timestamps = self.saved_state_timestamps(official_only=False)
        self.saved_state_combo.blockSignals(True)
        self.saved_state_combo.clear()
        self.saved_state_combo.setEnabled(bool(timestamps))
        self.saved_state_combo.setPlaceholderText(
            "Select saved history" if timestamps else "No saved price history"
        )

        for timestamp in timestamps:
            self.saved_state_combo.addItem(self.export_history_label(timestamp), timestamp)

        selected_index = -1
        if selected_timestamp:
            selected_index = self.saved_state_combo.findData(selected_timestamp)
        self.saved_state_combo.setCurrentIndex(selected_index)
        self.saved_state_combo.blockSignals(False)

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
        campaign_events: dict[str, list[str]] = {}
        today = date.today()
        for campaign in campaigns:
            imported = campaign.status == "ACTIVE_IMPORTED"
            if campaign.start_date and not imported and not store.has_execution(campaign.id, "START"):
                try:
                    key = (date.fromisoformat(campaign.start_date) - timedelta(days=1)).isoformat()
                    if key in dates and date.fromisoformat(key) >= today - timedelta(days=1):
                        campaign_events.setdefault(key, []).append(f"START: {campaign.name}")
                except ValueError:
                    pass
            if campaign.end_date and not store.has_execution(campaign.id, "END"):
                try:
                    key = date.fromisoformat(campaign.end_date).isoformat()
                    if key in dates and date.fromisoformat(key) >= today - timedelta(days=1):
                        campaign_events.setdefault(key, []).append(f"END: {campaign.name}")
                except ValueError:
                    pass
        combo = self.key_date_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Current prices", "")
        for key_date in dates:
            boundary = date.fromisoformat(key_date)
            next_day = boundary + timedelta(days=1)
            events = campaign_events.get(key_date, []) + labels_by_date.get(key_date, [])
            event_text = " · ".join(dict.fromkeys(events)) or "Scheduled change"
            combo.addItem(
                f"{boundary.strftime('%d %b %Y')} → {next_day.strftime('%d %b %Y')}  |  {event_text}",
                key_date,
            )
        wanted = selected or self._active_key_date or ""
        index = combo.findData(wanted)
        combo.setCurrentIndex(max(0, index))
        combo.setEnabled(not self._manual_pricebook_df.empty and not self._using_history_snapshot)
        combo.blockSignals(False)
        self._update_price_view_controls()
        self._sync_planner_date_from_editor()
    def _update_price_view_controls(self) -> None:
        if self._active_key_date:
            self.key_date_status.setText(
                f"Projected midnight view. Save writes dated changes to the shared SQLite pricing timeline; "
                "manual_prices_current stays unchanged."
            )
            self.key_date_status.setStyleSheet("color:#8a4b08; font-weight:600; padding:3px 0;")
        elif self._using_history_snapshot:
            self.key_date_status.setText("A historical saved snapshot is loaded. Reload current prices to browse planned dates.")
            self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        elif not self._manual_pricebook_df.empty:
            self.key_date_status.setText("Current prices come from the shared timeline. Excel import/export is optional.")
            self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        else:
            self.key_date_status.setText("Load current prices to browse scheduled dates.")
            self.key_date_status.setStyleSheet("color:#555555; padding:3px 0;")
        if hasattr(self, "export_btn"):
            self.export_btn.setText("Save key-date prices" if self._active_key_date else "Export prices…")
        if hasattr(self, "saved_state_combo"):
            self.saved_state_combo.setEnabled(not bool(self._active_key_date) and self.saved_state_combo.count() > 0)
        if hasattr(self, "side_tabs"):
            self.side_tabs.setTabEnabled(1, not bool(self._active_key_date))

    def _refresh_manual_baseline(self) -> None:
        df = self.timeline.project()
        if not df.empty:
            self._manual_pricebook_df = df
            self._manual_pricebook_path = FILES.editor_exports_dir / "manual_prices_current.xlsx"

    def _confirm_price_view_switch(self) -> bool:
        if not self.autosave_dirty:
            return True
        if self._active_key_date:
            self._save_key_date_prices(silent=True)
            return True
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
            progress = QProgressDialog("Loading the selected promo date…", "", 0, 0, self)
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
            view = self._manual_pricebook_df.copy()
            self._date_view_baseline_df = pd.DataFrame()
            self._active_key_date = None
            warning_text = "Current manual prices"
        else:
            store = self._promo_store()
            # Rebuild the selected date from the shared timeline boundary in
            # memory, then reverse only the selected editor layer to obtain a
            # clean comparison base without simulating the campaign twice.
            boundary = (date.fromisoformat(key_date) + timedelta(days=1)).isoformat()
            date_base = self.timeline.project(boundary)
            projected, preview = project_pricebook_for_key_date(
                date_base,
                self._manual_pricebook_path or (FILES.editor_exports_dir / "manual_prices_current.xlsx"),
                store,
                key_date,
                FILES.base_dir,
            )
            baseline = remove_key_date_overrides(
                projected, FILES.base_dir, key_date, boundary,
            )
            view = projected
            self._date_view_baseline_df = baseline
            self._active_key_date = key_date
            warnings = []
            if preview.collisions:
                warnings.append(f"{len(preview.collisions)} promo conflict(s) still need a decision in Promo Planner")
            if preview.cost_floor_issues:
                warnings.append(f"{len(preview.cost_floor_issues)} active price(s) need cost-floor review")
            boundary = date.fromisoformat(key_date) + timedelta(days=1)
            warning_text = f"After midnight: {boundary.strftime('%d %b %Y')} 00:00"
            if warnings:
                warning_text += " · " + " · ".join(warnings)
        self.state.preload_pricebook(view)
        self.state.preload_last_export(view)
        self.populate_combos()
        self.autosave_dirty = False
        self._update_price_view_controls()
        self.refresh_canvas()
        self.statusBar().showMessage(warning_text, 12000)

    def _price_date_event_name(self, key_date: str) -> str:
        for label in self.timeline.key_date_labels().get(key_date, []):
            if label.startswith("PRICE CHANGE:"):
                return label.partition(":")[2].strip()
        return ""
    def _save_key_date_prices(self, *, silent: bool = False) -> int:
        if not self._active_key_date:
            return 0
        count, unlinked = save_key_date_changes(
            FILES.base_dir,
            self._active_key_date,
            self.state.export_prices_dataframe(),
            self._date_view_baseline_df,
            self._promo_store().list_campaigns(),
            event_label=self._price_date_event_name(self._active_key_date),
        )
        self.autosave_dirty = False
        message = (
            f"Saved scheduled price changes for the midnight after {self._active_key_date}. "
            f"{count} row override(s) are stored in the shared pricing timeline; current prices were not changed."
        )
        self.statusBar().showMessage(message, 12000)
        if not silent:
            QMessageBox.information(self, "Key-date prices saved", message)
        return count

    def load_pricebook(self):
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Import Excel price changes",
            str(FILES.editor_exports_dir),
            "Excel workbooks (*.xlsx *.xlsm *.xls);;CSV files (*.csv)",
        )
        if not source:
            return
        source = Path(source)

        df = load_table(
            source,
            currency_hint="EUR",
            eur_to_usd=self.state.eur_to_usd,
        )
        if df.empty:
            QMessageBox.warning(self, "Import failed", "No usable pricing rows were found in that file.")
            return

        if not self.timeline.baseline_info():
            self.timeline.initialize_baseline(
                df, source_path=source, effective_from=self.timeline.current_as_of(),
            )
            canonical = self.timeline.project()
            changed_fields = changed_rows = 0
        else:
            canonical = self.timeline.project()
            try:
                changed_fields, changed_rows = self.timeline.diff_summary(canonical, df)
            except ValueError as exc:
                QMessageBox.warning(
                    self, "Excel import needs catalogue migration",
                    f"This workbook changes the set of pricing points, so it cannot be merged as a price update.\n\n{exc}",
                )
                return
        if changed_fields:
            answer = QMessageBox.question(
                self,
                "Review Excel price changes",
                f"Import {changed_fields:,} changed fields across {changed_rows:,} pricing points from:\n\n{source}\n\n"
                "These changes will be recorded in the shared timeline. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            self.timeline.record_diff(
                canonical, df,
                effective_from=self.timeline.current_as_of(),
                layer="import",
                source="excel_import",
                label=f"Excel import: {source.name}",
                metadata={"source_path": str(source)},
            )
            canonical = self.timeline.project()

        self._manual_pricebook_df = canonical.copy()
        self._manual_pricebook_path = source
        self._using_history_snapshot = False
        self.state.preload_pricebook(canonical)
        self.state.preload_last_export(canonical)

        self.populate_combos()
        self._active_key_date = None
        self._refresh_key_date_views()
        self.refresh_canvas()
        if self.planner_workspace is None:
            self._retry_planner_workspace()
        else:
            self.planner_workspace._reload_catalogue()

    def load_saved_state_timestamp(self, timestamp: str, selected_label: str | None = None) -> bool:
        if not self.state.row_index:
            QMessageBox.warning(self, "Load saved state", "Load the current price book before loading a saved state.")
            return False

        timestamp = str(timestamp).strip()
        selected_label = selected_label or self.export_history_label(timestamp)
        consolidated = FILES.editor_history_root / f"manual_prices_{timestamp}.xlsx"
        if not consolidated.exists():
            legacy_consolidated = FILES.editor_history_root / f"manual_prices_{timestamp}.csv"
            consolidated = legacy_consolidated if legacy_consolidated.exists() else consolidated
        if consolidated.exists():
            df = load_table(
                consolidated,
                currency_hint="EUR",
                eur_to_usd=self.state.eur_to_usd,
            )
        else:
            df = self.load_currency_tables_from_folder(
                FILES.editor_history_root,
                [f"manual_prices_{timestamp}.csv"],
            )

        if df.empty:
            QMessageBox.warning(self, "Load saved state", "Could not load saved prices for that date.")
            return False

        # A history snapshot is a complete historical price book, not merely an
        # overlay on today's structure. Rebuild the editor from the snapshot so
        # rows that existed then cannot be suppressed by today's price book.
        self.state.preload_pricebook(df)
        self.state.preload_last_export(df)
        self._using_history_snapshot = True
        self.populate_combos()
        self.autosave_dirty = False
        self._refresh_key_date_views()
        self.refresh_canvas()
        self.statusBar().showMessage(f"Loaded saved state: {selected_label}")
        return True

    def on_saved_state_selected(self, index: int) -> None:
        if index < 0:
            return
        timestamp = self.saved_state_combo.itemData(index)
        if not timestamp:
            return
        self.load_saved_state_timestamp(str(timestamp), self.saved_state_combo.itemText(index))

    def select_partner_compare_timestamp(self, current_timestamp: str) -> str | None:
        # Partner packs are official outputs, so compare only with prior official snapshots.
        timestamps = [
            ts for ts in self.saved_state_timestamps(official_only=True)
            if ts != current_timestamp
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

    def ask_official_export(self) -> bool | None:
        message = QMessageBox(self)
        message.setWindowTitle("Save pricing state")
        message.setText("Save current prices and history")
        message.setInformativeText(
            "If this snapshot is official, a partner pack will also be created."
        )
        official_check = QCheckBox("Make this snapshot official and create partner pack")
        official_check.setChecked(False)
        message.setCheckBox(official_check)
        message.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        message.setDefaultButton(QMessageBox.Ok)
        if message.exec() != QMessageBox.Ok:
            return None
        return official_check.isChecked()

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
        self.state.reload_selected_plan_from_loaded()
        self.mark_dirty()
        self.refresh_canvas()
        if show_status:
            scope = "USD/EUR" if self.state.is_linked_currency_mode() else self.state.active_currency
            self.statusBar().showMessage(f"Package reset to loaded state ({scope})")

    def reset_current_package_to_loaded(self):
        self.use_loaded_for_selected_plan(show_status=True)


    def use_loaded_for_pricing_unit(self):
        self.state.reload_pricing_unit_from_loaded()
        self.mark_dirty()
        self.refresh_canvas()

    def save_exports_to_folder(
        self,
        export_dir: Path,
        include_history: bool = False,
        autosave: bool = False,
        timestamp: str | None = None,
    ) -> str:
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = timestamp or datetime.now().strftime("%Y%m%d")
        prices = self.state.export_prices_dataframe()
        if not self.timeline.baseline_info():
            seed = self._manual_pricebook_df if not self._manual_pricebook_df.empty else prices
            self.timeline.initialize_baseline(
                seed,
                source_path=self._manual_pricebook_path or "",
                effective_from=self.timeline.current_as_of(),
            )
        if not self._using_history_snapshot and not self._active_key_date and not self._manual_pricebook_df.empty:
            _change_set_id, changed_fields = self.timeline.record_diff(
                self._manual_pricebook_df,
                prices,
                effective_from=self.timeline.current_as_of(),
                source="editor",
                label="Pricing Editor save",
            )
            if changed_fields:
                self._manual_pricebook_df = prices.copy()

        if not autosave:
            # Materialize the current database projection for an explicit export
            # or for the unchanged partner-pack generation flow.
            prices_path = export_dir / "manual_prices_current.xlsx"
            write_pricing_workbook(prices, prices_path, promo_catalog=self.state.promo_catalog)

        if export_dir == FILES.editor_exports_dir:
            self._manual_pricebook_df = prices.copy()

        if include_history:
            # History is an immutable data snapshot; it does not need workbook UX/formulas.
            FILES.editor_history_root.mkdir(parents=True, exist_ok=True)
            self.state.export_prices_csv(
                FILES.editor_history_root / f"manual_prices_{ts}.csv"
            )

        return ts

    def quick_save(self):
        if self._active_key_date:
            self._save_key_date_prices()
            return
        try:
            # Persist the compact changeset immediately. XLSX generation is
            # reserved for explicit export; the shared Planner reads SQLite.
            self.save_exports_to_folder(FILES.editor_autosave_dir, autosave=True)
            self.autosave_dirty = False

            self.statusBar().showMessage("Timeline saved. Excel export is optional.")

        except Exception as e:
            print("Quick save failed:", e)
            self.statusBar().showMessage("Quick save failed.")


    def autosave(self):
        if not self.autosave_dirty or self.autosave_in_progress or self.canvas.is_dragging:
            return

        try:
            self.autosave_in_progress = True
            if self._active_key_date:
                self._save_key_date_prices(silent=True)
                self.statusBar().showMessage("Scheduled date prices saved to the pricing timeline")
                return
            export_dir = FILES.editor_autosave_dir
            self.save_exports_to_folder(export_dir, autosave=True)
            self.autosave_dirty = False

            self.statusBar().showMessage("Autosaved to the shared pricing timeline")

        except Exception as e:
            print("Autosave failed:", e)
        finally:
            self.autosave_in_progress = False


    def export_prices(self):
        if self._active_key_date:
            self._save_key_date_prices()
            return
        cursor_active = False
        try:
            official_export = self.ask_official_export()
            if official_export is None:
                return
            official_name = ""
            if official_export:
                official_name, accepted = QInputDialog.getText(
                    self,
                    "Name official save",
                    "Save name (shown in the saved-prices dropdown):",
                )
                official_name = official_name.strip()
                if not accepted or not official_name:
                    return

            ts = datetime.now().strftime("%Y%m%d")
            local_export_dir = FILES.editor_exports_dir

            self.statusBar().showMessage("Saving consolidated prices, autosave and history...")
            self.clear_busy_cursor()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            cursor_active = True
            QApplication.processEvents()

            # Explicit export creates an optional workbook and one dated CSV
            # recovery point. Quick saves remain database-only.
            ts = self.save_exports_to_folder(
                local_export_dir,
                include_history=True,
                timestamp=ts,
            )
            self._refresh_manual_baseline()
            self.save_exports_to_folder(
                FILES.editor_autosave_dir,
                autosave=True,
                timestamp=ts,
            )

            # The canonical workbook is now saved, so regenerate the disposable
            # recommendation annotation + CSVs + HTML insight view from that exact state.
            # self.refresh_pricing_intelligence(force=True, refresh=True)

            if not official_export:
                self.save_export_metadata(
                    timestamp=ts,
                    official=False,
                )
                self.autosave_dirty = False
                self.refresh_saved_state_combo(selected_timestamp=ts)

                QApplication.restoreOverrideCursor()
                cursor_active = False
                QMessageBox.information(
                    self,
                    "Save complete",
                    "Current prices and autosave were saved.\n\n"
                    "This snapshot was not marked official, so no partner pack was created.",
                )
                self.statusBar().showMessage(
                    "Save complete: current and autosave saved; no partner pack created."
                )
                return

            # Official snapshots additionally create the partner pack.
            compare_timestamp = self.select_partner_compare_timestamp(ts)
            default_zip = self.official_partner_pack_filename(ts, official_name)
            partner_pack_dir = self.partner_pack_dir_for_status(True)
            partner_pack_dir.mkdir(parents=True, exist_ok=True)

            # Release the wait cursor while the native save dialog is open.
            QApplication.restoreOverrideCursor()
            cursor_active = False
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save official partner price pack ZIP",
                str(partner_pack_dir / default_zip),
                "Zip files (*.zip)",
            )
            if not path:
                # Pricing state is already saved. Keep it non-official if partner-pack creation is cancelled.
                self.save_export_metadata(timestamp=ts, official=False)
                self.refresh_saved_state_combo(selected_timestamp=ts)
                self.statusBar().showMessage(
                    "Prices saved, but partner-pack creation was cancelled; snapshot remains non-official."
                )
                return

            zip_path = self.partner_zip_path_in_status_folder(Path(path), True)

            QApplication.setOverrideCursor(Qt.WaitCursor)
            cursor_active = True
            QApplication.processEvents()

            pack_result = build_partner_price_pack(
                local_export_dir,
                zip_path,
                compare_timestamp=compare_timestamp,
                current_timestamp=ts,
                destination_table_json=FILES.destination_table_json,
                regions_yaml=FILES.regions_yaml,
                ppg_csv=FILES.ppg_csv,
            )
            self.save_export_metadata(
                timestamp=ts,
                official=True,
                partner_zip=pack_result.zip_path,
                compare_timestamp=compare_timestamp,
                pack_result=pack_result,
                name=official_name,
            )
            self.autosave_dirty = False
            self.refresh_saved_state_combo(selected_timestamp=ts)

            QApplication.restoreOverrideCursor()
            cursor_active = False

            pack_lines = [
                (
                    f"{result.member_name}: {result.rows_written} rows"
                    f" ({result.rows_removed_below_cost} below-cost removed)"
                )
                for result in pack_result.files
            ]
            diff_lines = [
                f"{result.member_name}: {result.rows_written} changed rows"
                for result in pack_result.diff_files
            ]
            diff_text = "\n\nDiff CSVs:\n" + "\n".join(diff_lines) if diff_lines else ""
            QMessageBox.information(
                self,
                "Official export complete",
                f"Current prices, autosave and history were saved.\n\n"
                f"The official partner ZIP was saved as:\n{pack_result.zip_path}\n\n"
                f"CSV files inside ZIP: {len(pack_result.files)}\n"
                + "\n".join(pack_lines)
                + diff_text,
            )
            self.statusBar().showMessage(
                "Official export complete: pricing history saved and partner pack created."
            )

        except Exception as e:
            QMessageBox.warning(self, "Export failed", f"Could not save export: {e}")
            self.statusBar().showMessage("Export failed.")

        finally:
            if cursor_active:
                self.clear_busy_cursor()

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
        self.refresh_promo_list()
        self.refresh_promo_range_controls()
        self.refresh_selection_label()
        self._refresh_recommendation_status()
        self.country_info_label.setText(self.state.country_info())
        self.refresh_impact_labels()
        

    def refresh_pricing_intelligence(self, *, force: bool = False, refresh: bool = True) -> bool:
        """Keep Excel annotations, recommendation CSVs and HTML insights in sync.

        Opening the editor only recalculates when the workbook/market/config is newer
        than the recommendation CSV. Explicit saves and the Refresh button force a
        recalculation. Autosave intentionally does not, so the 30-second recovery
        cycle stays lightweight.
        """
        price_book = FILES.editor_exports_dir / "manual_prices_current.xlsx"
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
                if RECOMMENDATIONS_PATH.exists():
                    recs = pd.read_csv(RECOMMENDATIONS_PATH, low_memory=False)
                    annotate_price_book_with_recommendations(price_book, recs)
                    # The annotation is derived from this exact CSV. Touching the
                    # CSV records that synchronization so the workbook annotation
                    # itself does not make recommendations look stale next launch.
                    RECOMMENDATIONS_PATH.touch()
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
        # Always check staleness before opening. Previously an existing HTML file
        # was opened unconditionally, so dashboard-code changes could leave the UI
        # showing an old report indefinitely.
        needs_refresh = not MARKET_INSIGHTS_PATH.exists()
        if not needs_refresh:
            try:
                needs_refresh = market_insights_is_stale(
                    FILES,
                    recommendations_path=RECOMMENDATIONS_PATH,
                    output_path=MARKET_INSIGHTS_PATH,
                )
            except Exception as exc:
                print("Market insights staleness check failed; regenerating:", exc)
                needs_refresh = True

        if needs_refresh:
            self.refresh_pricing_intelligence(force=False, refresh=False)

        if not MARKET_INSIGHTS_PATH.exists():
            QMessageBox.warning(self, "Market insights", "The market insights HTML could not be generated.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(MARKET_INSIGHTS_PATH.resolve())))

    def load_recommendations(self, *_args, refresh: bool = True):
        if not RECOMMENDATIONS_PATH.exists():
            self.state.preload_recommendations(pd.DataFrame(), source=RECOMMENDATIONS_PATH)
            if hasattr(self, "recommendation_status_label"):
                self.recommendation_status_label.setText("Recommendations: file not found. Run pricing_recommendations.py first.")
            if refresh and self.state.countries():
                self.refresh_canvas()
            return False

        try:
            df = pd.read_csv(RECOMMENDATIONS_PATH)
            self.state.preload_recommendations(df, source=RECOMMENDATIONS_PATH)
        except Exception as exc:
            self.state.preload_recommendations(pd.DataFrame(), source=RECOMMENDATIONS_PATH)
            if hasattr(self, "recommendation_status_label"):
                self.recommendation_status_label.setText(f"Recommendations: could not load ({exc})")
            if refresh and self.state.countries():
                self.refresh_canvas()
            return False

        self._refresh_recommendation_status()
        if refresh and self.state.countries():
            self.refresh_canvas()
        self.statusBar().showMessage(
            f"Loaded {self.state.recommendations_actionable_rows} actionable recommendations."
        )
        return True

    def _refresh_recommendation_status(self):
        if not hasattr(self, "recommendation_status_label"):
            return
        if self.state.recommendations_loaded_rows <= 0:
            self.recommendation_status_label.setText("Recommendations: not loaded")
            return

        points = self.state.current_points()
        visible = sum(1 for p in points if p.get("recommendation"))
        stale = sum(1 for p in points if p.get("recommendation_stale"))
        text = (
            f"{visible} actionable here in {self.state.active_currency} | "
            f"{self.state.recommendations_actionable_rows} total rows actionable"
        )
        if stale:
            text += f" | {stale} stale here"
        text += "\nClick ▲/▼ to apply. Curve dragging always requires SHIFT."
        self.recommendation_status_label.setText(text)

    def on_recommendation_selected(self, row_id: str):
        self.state.selected_row_id = str(row_id)
        result = self.state.apply_recommendation(str(row_id))

        if result.get("needs_confirmation"):
            answer = QMessageBox.question(
                self,
                "Shared promo recommendation",
                str(result.get("message", "Apply shared promo?")),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.refresh_canvas()
                self.statusBar().showMessage("Recommendation not applied.")
                return
            result = self.state.apply_recommendation(str(row_id), force_shared_promo=True)

        if not result.get("ok"):
            message = str(result.get("message", "Recommendation could not be applied."))
            if result.get("stale"):
                QMessageBox.warning(self, "Stale recommendation", message)
            else:
                QMessageBox.warning(self, "Recommendation", message)
            self.refresh_canvas()
            return

        self.mark_dirty()
        self.refresh_canvas()
        self.statusBar().showMessage(str(result.get("message", "Recommendation applied.")))

    def on_point_selected(self, row_id: str):
        self.state.selected_row_id = str(row_id)
        self.refresh_promo_range_controls()
        self.refresh_canvas()

    def on_promo_selected_from_chart(self, promo_code: str):
        if promo_code == "__REMOVE_PROMO__":
            self.state.remove_selected_promo()
        else:
            self.selected_promo_code_for_range = str(promo_code)
            self.state.assign_promo_to_selected(str(promo_code))

        self.mark_dirty()
        self.refresh_canvas()

    def on_point_dragged(self, row_id: str, new_price: float, point_index: int):
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

    @staticmethod
    def _rec_value(rec: dict, key: str):
        value = pd.to_numeric(rec.get(key, None), errors="coerce")
        return None if pd.isna(value) else float(value)

    @staticmethod
    def _human_recommendation_reason(rec: dict) -> str:
        reason = str(rec.get("Reason", "") or "").strip().lower()
        direction = str(rec.get("Direction", "") or "").upper()
        if reason == "local_signal_list_price":
            side = "cheap" if direction == "UP" else "expensive"
            return (
                f"The permanent list price looks locally {side} versus the adjacent duration anchors "
                "after adjusting for the competitor market."
            )
        if reason == "isolated_anchor_local_signal":
            side = "expensive" if direction == "DOWN" else "cheap"
            return (
                f"This appears to be an isolated {side} anchor rather than a structural curve issue, "
                "so the engine recommends a promo/net-price adjustment instead of changing the list-price curve."
            )
        if reason == "consecutive_anchor_local_signal":
            side = "high" if direction == "DOWN" else "low"
            return (
                f"The same structural signal appears across consecutive anchors, suggesting the list-price segment is {side}."
            )
        return str(rec.get("Reason", "") or "").replace("_", " ") or "Local market-position signal."

    def _recommendation_detail_text(self, rec: dict) -> str:
        direction = str(rec.get("Direction", "") or "").upper()
        arrow = "▲" if direction == "UP" else "▼" if direction == "DOWN" else "•"
        mechanism = str(rec.get("Mechanism", "") or "").upper()
        is_promo = mechanism == "PROMO"
        kind = "P" if is_promo else "L"
        mechanism_label = "PROMO / NET PRICE" if is_promo else "LIST PRICE"
        currency = self.state.active_currency

        current_list = self._rec_value(rec, "CurrentListPriceNow")
        if current_list is None:
            current_list = self._rec_value(rec, "CurrentListPrice")
        current_net = self._rec_value(rec, "CurrentNetPriceNow")
        if current_net is None:
            current_net = self._rec_value(rec, "CurrentNetPrice")
        target = self._rec_value(rec, "MarketTargetPrice")
        providers = int(self._rec_value(rec, "MarketProviderCount") or 0)
        neighbours = int(self._rec_value(rec, "NeighborAnchorCount") or 0)
        decision_basis = str(rec.get("DecisionBasis", "") or "").replace("_", " ")
        confidence = str(rec.get("Confidence", "") or "").upper()
        promo_code = str(rec.get("CurrentPromoCodeNow", rec.get("CurrentPromoCode", "")) or "").strip()
        if promo_code.lower() in {"nan", "none"}:
            promo_code = ""

        if is_promo:
            suggested = self._rec_value(rec, "SuggestedPromoFinalPrice")
            if suggested is None:
                suggested = self._rec_value(rec, "SuggestedNetPrice")
            deviation = self._rec_value(rec, "PositionDeviationPct")
            suggested_promo = str(rec.get("SuggestedPromoCode", "") or "").strip()
            if suggested_promo.lower() in {"nan", "none"}:
                suggested_promo = ""
            action = f"Set net price to {suggested:.2f} {currency}" if suggested is not None else "Adjust net price"
            if suggested_promo:
                action += f" via promo {suggested_promo}"
            action += "; list price stays unchanged."
        else:
            suggested = self._rec_value(rec, "SuggestedListPrice")
            if suggested is None:
                suggested = self._rec_value(rec, "SuggestedNetPrice")
            deviation = self._rec_value(rec, "ListPositionDeviationPct")
            action = f"Set list price to {suggested:.2f} {currency}." if suggested is not None else "Adjust list price."

        current_parts = []
        if current_list is not None:
            current_parts.append(f"list {current_list:.2f}")
        if current_net is not None:
            current_parts.append(f"net {current_net:.2f}")
        current_line = f"Current: {' | '.join(current_parts)}" if current_parts else "Current price unavailable"
        if promo_code:
            current_line += f" | active promo {promo_code}"

        evidence = []
        if target is not None:
            evidence.append(f"market reference {target:.2f} {currency}")
        if deviation is not None:
            if deviation < 0:
                evidence.append(f"{abs(deviation):.0%} cheaper vs market than neighboring anchors")
            elif deviation > 0:
                evidence.append(f"{abs(deviation):.0%} more expensive vs market than neighboring anchors")
        if providers:
            evidence.append(f"{providers} competitor providers")
        if neighbours:
            evidence.append(f"{neighbours} neighboring anchors")

        secondary = str(rec.get("SecondarySignal", "") or "").upper()
        priority = str(rec.get("PriorityCountry", "") or "").strip()
        market_context = ""
        if priority and priority.lower() not in {"nan", "none"}:
            market_context = f"\nMini-region: priority market {priority}"
            if secondary and secondary not in {"NONE", "NAN"}:
                market_context += f" | secondary-market signal {secondary}"

        scope_note = "This applies only to this anchor SKU"
        countries = str(rec.get("Countries", "") or "").strip()
        if countries and "," in countries:
            scope_note += f", across the shared pricing unit ({countries})"
        scope_note += ". It does not move the rest of the curve."

        reason_text = self._human_recommendation_reason(rec)
        evidence_line = " | ".join(evidence) if evidence else "local market/neighbor evidence"
        return (
            f"\n\n{arrow}{kind}  {direction} — {mechanism_label} | Confidence: {confidence}\n"
            f"Action: {action}\n"
            f"{current_line}\n"
            f"Why: {reason_text}\n"
            f"Evidence: {evidence_line}\n"
            f"Signal basis: {decision_basis or '-'}{market_context}\n"
            f"Scope: {scope_note}"
        )

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

        rec = self.state.recommendation_for_point(info, currency, actionable_only=True)
        if rec is not None:
            if rec.get("applied"):
                lines.append("Recommendation applied")
            elif rec.get("stale"):
                lines.append("Recommendation stale — refresh")
            else:
                direction = str(rec.get("Direction", "") or "").upper()
                arrow = "▲" if direction == "UP" else "▼" if direction == "DOWN" else "•"
                suggested = self._rec_value(rec, "SuggestedListPrice")
                if suggested is None:
                    suggested = self._rec_value(rec, "SuggestedPromoFinalPrice")
                if suggested is None:
                    suggested = self._rec_value(rec, "SuggestedNetPrice")
                confidence = str(rec.get("Confidence", "") or "").upper()
                target_text = f" {suggested:.2f} {currency}" if suggested is not None else ""
                conf_text = f" · {confidence}" if confidence else ""
                lines.append(f"Rec {arrow}{target_text}{conf_text}")

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

    def refresh_promo_list(self):
        self.promo_list.clear()

        info = self.state.selected_point_info()

        if info and self.state.has_promo_for_selected():
            item = QListWidgetItem("✖  REMOVE APPLIED PROMO")
            item.setData(Qt.UserRole, "__REMOVE_PROMO__")

            font = QFont()
            font.setPointSize(12)
            font.setBold(True)
            item.setFont(font)
            item.setForeground(QColor("#c62828"))

            self.promo_list.addItem(item)

        selected_row = -1
        for promo in self.state.promo_candidates_for_selected():
            item = QListWidgetItem(
                f"{promo['promo_label']}  ->  {promo['final_price_after_promo']:.2f} {self.state.active_currency}"
            )
            item.setData(Qt.UserRole, promo["promo_code"])
            self.promo_list.addItem(item)
            if promo["promo_code"] == self.selected_promo_code_for_range:
                selected_row = self.promo_list.count() - 1

        if selected_row >= 0:
            self.promo_list.setCurrentRow(selected_row)

    @staticmethod
    def _promo_day_label(day: float) -> str:
        day = float(day)
        return str(int(day)) if day.is_integer() else f"{day:g}"

    def refresh_promo_range_controls(self) -> None:
        if not hasattr(self, "promo_from_combo"):
            return

        info = self.state.selected_point_info()

        # No selected price point = no Plan/range context
        if not info:
            self.promo_from_combo.blockSignals(True)
            self.promo_to_combo.blockSignals(True)

            self.promo_from_combo.clear()
            self.promo_to_combo.clear()

            self.promo_from_combo.setEnabled(False)
            self.promo_to_combo.setEnabled(False)
            self.apply_promo_range_btn.setEnabled(False)
            self.remove_promo_range_btn.setEnabled(False)

            self.promo_from_combo.blockSignals(False)
            self.promo_to_combo.blockSignals(False)

            self._promo_range_context_key = None

            currency_scope = "EUR + USD"
            promo_text = self.selected_promo_code_for_range or "—"
            self.promo_range_selected_label.setText(
                f"Selected promo: {promo_text} | "
                f"Plan: — | Applies to: {currency_scope}"
            )
            return

        # Determine context directly from the selected blue point.
        selected_unit = str(info.get("pricing_unit_id", "")).strip()
        selected_plan = str(info.get("plan", "")).strip()

        context_key = (selected_unit, selected_plan)

        # Find all available durations for this exact Plan + pricing unit.
        days = sorted({
            float(point.get("days"))
            for point in self.state.current_points()
            if point.get("days") is not None
            and str(point.get("plan", "")).strip() == selected_plan
            and str(point.get("pricing_unit_id", "")).strip() == selected_unit
        })

        old_from = self.promo_from_combo.currentData()
        old_to = self.promo_to_combo.currentData()

        preserve = context_key == self._promo_range_context_key

        self.promo_from_combo.blockSignals(True)
        self.promo_to_combo.blockSignals(True)

        self.promo_from_combo.clear()
        self.promo_to_combo.clear()

        for day in days:
            label = self._promo_day_label(day)
            self.promo_from_combo.addItem(label, day)
            self.promo_to_combo.addItem(label, day)

        enabled = bool(days)

        self.promo_from_combo.setEnabled(enabled)
        self.promo_to_combo.setEnabled(enabled)

        self.remove_promo_range_btn.setEnabled(enabled)

        has_selected_promo = bool(self.selected_promo_code_for_range)
        self.apply_promo_range_btn.setEnabled(
            enabled and has_selected_promo
        )

        if days:
            # Preserve the range while staying on the same Plan.
            # When changing Plan/country, default to the complete available range.
            if preserve and old_from in days:
                from_day = float(old_from)
            else:
                from_day = days[0]

            if preserve and old_to in days:
                to_day = float(old_to)
            else:
                to_day = days[-1]

            from_idx = self.promo_from_combo.findData(from_day)
            to_idx = self.promo_to_combo.findData(to_day)

            self.promo_from_combo.setCurrentIndex(
                from_idx if from_idx >= 0 else 0
            )
            self.promo_to_combo.setCurrentIndex(
                to_idx if to_idx >= 0 else len(days) - 1
            )

        self.promo_from_combo.blockSignals(False)
        self.promo_to_combo.blockSignals(False)

        self._promo_range_context_key = context_key

        promo_text = self.selected_promo_code_for_range or "—"
        currency_scope = "EUR + USD"
        plan_text = display_plan_label(selected_plan)

        self.promo_range_selected_label.setText(
            f"Selected promo: {promo_text} | "
            f"Plan: {plan_text} | Applies to: {currency_scope}"
        )

    def _selected_promo_range(self) -> tuple[float, float] | None:
        start = self.promo_from_combo.currentData()
        end = self.promo_to_combo.currentData()
        if start is None or end is None:
            return None
        return float(start), float(end)

    def apply_selected_promo_to_range(self) -> None:
        promo_code = str(self.selected_promo_code_for_range or "").strip()
        if not promo_code:
            QMessageBox.information(
                self,
                "Promo range",
                "Select a promo from Promo options first.",
            )
            return

        day_range = self._selected_promo_range()
        if day_range is None:
            return

        start_day, end_day = day_range
        affected = self.state.assign_promo_to_range(promo_code, start_day, end_day)
        if affected <= 0:
            QMessageBox.information(
                self,
                "Promo range",
                "No matching price points were found for this plan and duration range.",
            )
            return

        self.mark_dirty()
        self.refresh_canvas()
        scope = "EUR + USD"
        self.statusBar().showMessage(
            f"Applied {promo_code} to {affected} price points from "
            f"{self._promo_day_label(start_day)} to {self._promo_day_label(end_day)} days ({scope})."
        )

    def remove_promo_from_range(self) -> None:
        day_range = self._selected_promo_range()
        if day_range is None:
            return

        start_day, end_day = day_range
        affected = self.state.remove_promo_from_range(start_day, end_day)
        self.mark_dirty()
        self.refresh_canvas()
        scope = "EUR + USD"
        self.statusBar().showMessage(
            f"Removed promos from {affected} price points from "
            f"{self._promo_day_label(start_day)} to {self._promo_day_label(end_day)} days ({scope})."
        )

    def apply_promo(self, item: QListWidgetItem):
        promo_code = item.data(Qt.UserRole)

        if promo_code == "__REMOVE_PROMO__":
            self.state.remove_selected_promo()
            self.selected_promo_code_for_range = None
            self.mark_dirty()
            self.refresh_canvas()
            return

        # Only select the promo here.
        # Do not apply it yet to the selected price point.
        self.selected_promo_code_for_range = str(promo_code)

        self.refresh_promo_range_controls()

        self.statusBar().showMessage(
            f"Selected promo for range: {self.selected_promo_code_for_range}"
        )

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
        self.state.remove_selected_promo()
        self.mark_dirty()
        self.refresh_canvas()

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
            self._using_history_snapshot = False
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
        # If prices are edited later, explicit Save/Quick Save and the Refresh
        # recommendations button still force a full recalculation.
        progress("Loading pricing recommendations...")
        self.load_recommendations(refresh=False)

        self.refresh_saved_state_combo()

        if self.state.countries():
            progress("Drawing editor...")
            self.populate_combos()
            self._active_key_date = None
            self._refresh_key_date_views()
            self.refresh_canvas()
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
