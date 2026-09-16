from __future__ import annotations

import json
from pathlib import Path
import pandas as pd
from datetime import datetime

from PySide6.QtPrintSupport import QPrinter
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QColor, QShortcut, QKeySequence, QPainter, QPageSize, QPageLayout
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .canvas import PriceCurveCanvas
from .state import EditorState, load_promos, load_table
try:
    from partner_export_pack import build_partner_price_pack
except ImportError:
    from automation.partner_export_pack import build_partner_price_pack
try:
    from plan_labels import display_plan_label
except ImportError:
    from automation.plan_labels import display_plan_label
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


BASE_DIR = FILES.base_dir
PPG_PATH = FILES.ppg_csv
PROMOS_PATH = FILES.promos_json
SALES_VOLUME_PATH = FILES.sales_volumes_xlsx
RECOMMENDATIONS_PATH = FILES.work_dir / "pricing_recommendations" / "recommendations_latest.csv"
MARKET_INSIGHTS_PATH = FILES.work_dir / "pricing_recommendations" / "market_insights_latest.html"
MAX_SAVED_EXPORT_DROPDOWN_DATES = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pricing Curve Editor")
        self.resize(1620, 960)

        self.state = EditorState()
        self.current_drag_mode = "inflate"
        self.mode_buttons: dict[str, QToolButton] = {}
        self.autosave_dirty = False
        self.autosave_in_progress = False
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
        self.side_panel.setMinimumWidth(380)
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

        self.load_sales_btn = QPushButton("Load sales volumes")
        self.load_sales_btn.setFixedHeight(34)
        self.load_sales_btn.clicked.connect(self.load_sales_volumes)

        self.impact_label = QLabel("Pricing unit impact: —")
        self.total_impact_label = QLabel("Total impact: —")

        self.impact_label.setStyleSheet("color: #999999;")
        self.total_impact_label.setStyleSheet("color: #999999;")

        self.unit_last_month_label = QLabel("Unit last month: —")
        self.total_last_month_label = QLabel("Total last month: —")
        self.unit_projected_label = QLabel("Unit projected: —")
        self.total_projected_label = QLabel("Total projected: —")

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
        load_grid.addWidget(self.load_sales_btn, 1, 0, 1, 2)

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
            btn.setFixedSize(82, 40)
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

        self.drag_safety_label = QLabel("Protected editing: hold SHIFT while dragging any curve tool.")
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

        export_btn = QPushButton("💾 Save pricing")
        export_btn.clicked.connect(self.export_prices)

        export_pdf_btn = QPushButton("📄 Export PDF report")
        export_pdf_btn.clicked.connect(self.export_all_charts_pdf)

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

        # Build left panel
        side_layout.addLayout(load_grid)
        side_layout.addLayout(currency_grid)
        side_layout.addWidget(self.currency_mode_banner)

        side_layout.addWidget(QLabel("Country / Region"))
        side_layout.addWidget(self.country_combo)

        # Promo controls high in the panel for normal editing
        side_layout.addWidget(QLabel("Promo range"))
        side_layout.addWidget(self.promo_range_box)

        side_layout.addWidget(QLabel("Promo options"))
        side_layout.addWidget(self.promo_list)

        side_layout.addWidget(QLabel("Drag tools"))
        side_layout.addWidget(self.drag_safety_label)
        side_layout.addLayout(mode_row_1)
        side_layout.addLayout(mode_row_2)
        side_layout.addLayout(mode_row_3)

        side_layout.addWidget(QLabel("Pricing recommendations"))
        side_layout.addWidget(self.recommendation_status_label)
        recommendation_buttons = QHBoxLayout()
        recommendation_buttons.addWidget(self.reload_recommendations_btn)
        recommendation_buttons.addWidget(self.open_market_insights_btn)
        side_layout.addLayout(recommendation_buttons)


        impact_box = QWidget()
        impact_box.setStyleSheet("""
            QWidget {
                border: 1px solid #cccccc;
                border-radius: 6px;
                background-color: #fafafa;
            }
        """)

        impact_layout = QVBoxLayout(impact_box)
        impact_layout.setContentsMargins(8, 8, 8, 8)

        impact_title = QLabel("Impact summary")
        impact_title.setStyleSheet("font-weight: bold; color: #333333; border: none;")
        impact_layout.addWidget(impact_title)

        for lbl in [
            self.unit_last_month_label,
            self.impact_label,
            self.unit_projected_label,
            self.total_last_month_label,
            self.total_impact_label,
            self.total_projected_label,
        ]:
            lbl.setStyleSheet(lbl.styleSheet() + " border: none;")
            impact_layout.addWidget(lbl)

        for widget in [
            QLabel("Current scope"), self.country_info_label,
            impact_box,
            reset_zoom_btn,
        ]:
            side_layout.addWidget(widget)

        side_layout.addLayout(reset_grid)

        for widget in [
            export_btn,
            export_pdf_btn,
            # self.tool_help_label,
            QLabel("Selected point info"), self.selection_label,
        ]:
            side_layout.addWidget(widget)

        side_layout.addStretch(1)

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
        self.splitter.setSizes([380, 1100])

    def export_all_charts_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export all charts to PDF",
            "pricing_charts_report.pdf",
            "PDF Files (*.pdf)",
        )
        if not path:
            return
        
        print("Saving PDF to:", path)

        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        printer.setPageSize(QPageSize(QPageSize.A4))
        printer.setPageOrientation(QPageLayout.Landscape)

        painter = QPainter(printer)

        old_country = self.state.selected_country

        for i, country in enumerate(self.state.countries()):
            if i > 0:
                printer.newPage()

            self.state.selected_country = country
            self.country_combo.setCurrentText(country)
            self.refresh_canvas()
            QApplication.processEvents()

            page_rect = printer.pageRect(QPrinter.DevicePixel).toRect()
            pixmap = self.canvas.grab()

            scaled = pixmap.scaled(
                page_rect.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )

            x = page_rect.x() + (page_rect.width() - scaled.width()) // 2
            y = page_rect.y() + (page_rect.height() - scaled.height()) // 2

            painter.drawPixmap(x, y, scaled)

        painter.end()

        if old_country:
            self.state.selected_country = old_country
            self.country_combo.setCurrentText(old_country)
            self.refresh_canvas()

        print("PDF export finished")
        self.statusBar().showMessage(f"PDF exported: {path}")
    
    
    def clear_busy_cursor(self):
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
    
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
            return f"{label} (Official)"
        return label

    def save_export_metadata(
        self,
        *,
        timestamp: str,
        official: bool,
        partner_zip: Path | None = None,
        compare_timestamp: str | None = None,
        pack_result=None,
    ) -> None:
        path = self.export_metadata_path(timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "date": timestamp,
            "official": bool(official),
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

    def load_pricebook(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Load current price-book folder",
        )
        if not folder:
            return

        folder = Path(folder)
        source = None
        for name in ("manual_prices_current.xlsx", "manual_prices_current.csv"):
            candidate = folder / name
            if candidate.exists():
                source = candidate
                break

        if source is None:
            QMessageBox.warning(
                self,
                "Load failed",
                "No manual_prices_current.xlsx or manual_prices_current.csv was found in that folder.",
            )
            return

        df = load_table(
            source,
            currency_hint="EUR",
            eur_to_usd=self.state.eur_to_usd,
        )
        if df.empty:
            QMessageBox.warning(self, "Load failed", "No usable current price-book rows found.")
            return

        # The current price book is authoritative for both the set of points and
        # their current values/promos/overrides.
        self.state.preload_pricebook(df)
        self.state.preload_last_export(df)

        self.populate_combos()
        self.refresh_canvas()

    def load_sales_volumes(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load sales volumes",
            "",
            "Data Files (*.csv *.xlsx *.xls)",
        )
        if not path:
            return

        if Path(path).suffix.lower() == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
        df.columns = df.columns.astype(str).str.strip()

        if df.empty:
            QMessageBox.warning(self, "Load failed", "No usable rows found in that file.")
            return

        self.state.preload_sales_volumes(df)
        self.refresh_canvas()

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
        self.populate_combos()
        self.autosave_dirty = False
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

        if autosave:
            # Recovery snapshots stay lightweight CSVs.
            prices_path = export_dir / "manual_prices_autosave.csv"
            self.state.export_prices_csv(prices_path)
        else:
            # The manually maintained current price book is the rich Excel workbook.
            prices_path = export_dir / "manual_prices_current.xlsx"
            self.state.export_prices_xlsx(prices_path)

        if include_history:
            # History is an immutable data snapshot; it does not need workbook UX/formulas.
            FILES.editor_history_root.mkdir(parents=True, exist_ok=True)
            self.state.export_prices_csv(
                FILES.editor_history_root / f"manual_prices_{ts}.csv"
            )

        return ts

    def quick_save(self):
        try:
            export_dir = FILES.editor_exports_dir
            self.save_exports_to_folder(export_dir)
            self.refresh_pricing_intelligence(force=True, refresh=True)
            self.autosave_dirty = False

            self.statusBar().showMessage("Quick saved; recommendations and market insights refreshed.")

        except Exception as e:
            print("Quick save failed:", e)
            self.statusBar().showMessage("Quick save failed.")


    def autosave(self):
        if not self.autosave_dirty or self.autosave_in_progress or self.canvas.is_dragging:
            return

        try:
            self.autosave_in_progress = True
            export_dir = FILES.editor_autosave_dir
            self.save_exports_to_folder(export_dir, autosave=True)
            self.autosave_dirty = False

            self.statusBar().showMessage("Autosaved")

        except Exception as e:
            print("Autosave failed:", e)
        finally:
            self.autosave_in_progress = False


    def export_prices(self):
        cursor_active = False
        try:
            official_export = self.ask_official_export()
            if official_export is None:
                return

            ts = datetime.now().strftime("%Y%m%d")
            local_export_dir = FILES.editor_exports_dir

            self.statusBar().showMessage("Saving consolidated prices, autosave and history...")
            self.clear_busy_cursor()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            cursor_active = True
            QApplication.processEvents()

            # Every explicit save writes the canonical current file, autosave and history.
            ts = self.save_exports_to_folder(
                local_export_dir,
                include_history=True,
                timestamp=ts,
            )
            self.save_exports_to_folder(
                FILES.editor_autosave_dir,
                autosave=True,
                timestamp=ts,
            )

            # The canonical workbook is now saved, so regenerate the disposable
            # recommendation annotation + CSVs + HTML insight view from that exact state.
            self.refresh_pricing_intelligence(force=True, refresh=True)

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
                    "Current prices, autosave and history were saved.\n\n"
                    "This snapshot was not marked official, so no partner pack was created.",
                )
                self.statusBar().showMessage(
                    "Save complete: current, autosave and history saved; no partner pack created."
                )
                return

            # Official snapshots additionally create the partner pack.
            compare_timestamp = self.select_partner_compare_timestamp(ts)
            default_zip = f"TT_prices_{datetime.now().strftime('%y%m%d')}.zip"
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
        # No sales data → everything grey
        if not self.state.sales_by_scope:
            self.impact_label.setText("Pricing unit impact: —")
            self.total_impact_label.setText("Total impact: —")
            self.unit_last_month_label.setText("Unit last month: —")
            self.total_last_month_label.setText("Total last month: —")
            self.unit_projected_label.setText("Unit projected: —")
            self.total_projected_label.setText("Total projected: —")

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
        self.impact_label.setText(f"Pricing unit impact: {unit_impact:,.2f} {currency}")
        self.total_impact_label.setText(f"Total impact: {total_impact:,.2f} {currency}")

        self.unit_last_month_label.setText(f"Unit last month: {unit_last:,.2f} {currency}")
        self.total_last_month_label.setText(f"Total last month: {total_last:,.2f} {currency}")

        self.unit_projected_label.setText(f"Unit projected: {unit_projected:,.2f} {currency}")
        self.total_projected_label.setText(f"Total projected: {total_projected:,.2f} {currency}")

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

    def on_country_changed(self, country: str):
        self.state.selected_country = country

        # Promo range requires a selected Plan/price point.
        # Automatically select the first point when changing country.
        points = self.state.current_points()
        current = self.state.selected_point_info()

        if (
            not current
            or str(current.get("country", "")).strip() != str(country).strip()
        ):
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
        if not MARKET_INSIGHTS_PATH.exists():
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
            return

        promo = f"\nPromo: {info['promo']}" if info.get("promo") else ""
        below_by_currency = info.get("below_cost_floor_by_currency") or {}
        below_currencies = [
            currency for currency, is_below in below_by_currency.items() if bool(is_below)
        ]
        allow_below_cost = bool(info.get("allow_below_cost", False))
        if below_currencies:
            if allow_below_cost:
                export_status = f"eligible by override (below floor: {','.join(below_currencies)})"
            else:
                export_status = f"excluded below floor ({','.join(below_currencies)})"
        else:
            export_status = "eligible"
        entry_status = "NEW - no previous saved/exported price" if info.get("is_new_entry") else "Existing"

        recommendation_text = ""
        rec = self.state.recommendation_for_point(
            info, self.state.active_currency, actionable_only=True
        )
        if rec is not None:
            if rec.get("applied"):
                recommendation_text = "\nRecommendation: applied in this session"
            elif rec.get("stale"):
                recommendation_text = "\nRecommendation: STALE - rerun pricing_recommendations.py"
            else:
                recommendation_text = self._recommendation_detail_text(rec)

        self.selection_label.setText(
            f"{display_plan_label(info['plan'])} | {info['days']} days | {info['gb']} GB\n"
            f"Entry: {entry_status}\n"
            f"Currency: {self.state.active_currency}\n"
            f"Working price: {info['y']:.2f}\n"
            f"Loaded price: {info['base_y']:.2f}\n"
            f"ISO: {info.get('iso') or '-'}\n"
            f"Pricing unit: {info['pricing_unit_id'] or '-'}\n"
            f"Source: {info['pricing_source'] or '-'} | Region: {info['pricing_region'] or '-'}\n"
            f"Unit countries: {info['pricing_unit_countries'] or '-'}\n"
            f"Countries affected in editor: {info['editor_scope_countries'] or '-'}\n"
            f"Partner export: {export_status}{promo}"
            f"{recommendation_text}"
        )

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
            self.side_panel.setMinimumWidth(380)
            self.side_panel.setMaximumWidth(16777215)
            self.splitter.setSizes([380, 1100])
            self.toggle_side_btn.setText("◀")
            self._left_panel_collapsed = False
        else:
            self.side_panel.setMinimumWidth(0)
            self.side_panel.setMaximumWidth(0)
            self.splitter.setSizes([0, 1420])
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
        def progress(message: str) -> None:
            self.statusBar().showMessage(message)
            QApplication.processEvents()

        progress("Loading official EUR/USD cost rate...")
        self.load_official_cost_exchange_rate()

        if PPG_PATH.exists():
            try:
                progress("Loading PPG cost lookup...")
                self.state.ppg_df = pd.read_csv(PPG_PATH)
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

        if current_path.exists():
            df = load_table(
                current_path,
                currency_hint="EUR",
                eur_to_usd=self.state.eur_to_usd,
            )
            if not df.empty:
                progress("Preparing current price book...")
                # The canonical current workbook defines the complete editor
                # structure and the initial/reset state for this session.
                self.state.preload_pricebook(df)
                self.state.preload_last_export(df)
        else:
            progress("Current price book not found. The editor requires manual_prices_current.xlsx.")

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
            sales_df = pd.read_excel(SALES_VOLUME_PATH)
            sales_df.columns = sales_df.columns.astype(str).str.strip()
            if not sales_df.empty:
                self.state.preload_sales_volumes(sales_df)

        progress("Synchronizing pricing recommendations...")
        self.refresh_pricing_intelligence(force=False, refresh=False)
        # refresh_pricing_intelligence already loads the recommendation CSV when possible.
        if self.state.recommendations_loaded_rows <= 0:
            self.load_recommendations(refresh=False)

        self.refresh_saved_state_combo()

        if self.state.countries():
            progress("Drawing editor...")
            self.populate_combos()
            self.refresh_canvas()
            self.statusBar().showMessage("Auto-loaded available files.")
        else:
            self.statusBar().showMessage(
                "Auto-load found nothing usable. Check the current price book and market files."
            )
        self.autosave_dirty = False


def run():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    window = MainWindow()
    window.show()
    app.exec()
