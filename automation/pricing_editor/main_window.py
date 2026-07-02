from __future__ import annotations

import json
from pathlib import Path
import shutil
import pandas as pd
from datetime import datetime

from PySide6.QtPrintSupport import QPrinter
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QColor, QShortcut, QKeySequence, QPainter, QPageSize, QPageLayout
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
    from define_region_prices import generate_region_prices_for_export_folder
except ImportError:
    from automation.define_region_prices import generate_region_prices_for_export_folder
try:
    from partner_export_pack import build_partner_price_pack
except ImportError:
    from automation.partner_export_pack import build_partner_price_pack
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


BASE_DIR = FILES.base_dir
PPG_PATH = FILES.ppg_csv
PROMOS_PATH = FILES.promos_json
SALES_VOLUME_PATH = FILES.sales_volumes_xlsx


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pricing Curve Editor")
        self.resize(1620, 960)

        self.state = EditorState()
        self.current_drag_mode = "inflate"
        self.mode_buttons: dict[str, QToolButton] = {}

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
        self.clear_busy_cursor()

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
        self.side_panel.setMinimumWidth(320)
        self.side_panel.setWidgetResizable(True)

        self.side_panel_content = QWidget()
        self.side_panel_content.setObjectName("SidePanelContent")
        side_layout = QVBoxLayout(self.side_panel_content)

        self.side_panel.setWidget(self.side_panel_content)

        self.country_combo = QComboBox()
        self.country_combo.currentTextChanged.connect(self.on_country_changed)

        # Top load buttons: 5 buttons fitting nicely in 2 rows
        self.load_model_btn = QPushButton("Load model\nfolder")
        self.load_model_btn.setFixedSize(88, 72)
        self.load_model_btn.clicked.connect(self.load_baseline)

        self.load_saved_state_btn = QPushButton("Load saved\nstate")
        self.load_saved_state_btn.setFixedSize(88, 72)
        self.load_saved_state_btn.clicked.connect(self.load_saved_state)

        self.load_market_btn = QPushButton("Load market\nfolder")
        self.load_market_btn.setFixedSize(88, 72)
        self.load_market_btn.clicked.connect(self.load_market)

        self.load_promo_btn = QPushButton("Load promo\ncatalog")
        self.load_promo_btn.setFixedSize(88, 72)
        self.load_promo_btn.clicked.connect(self.load_promo_catalog)

        self.load_sales_btn = QPushButton("Load sales\nvolumes")
        self.load_sales_btn.setFixedSize(88, 72)
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
        load_grid.addWidget(self.load_model_btn, 0, 0)
        load_grid.addWidget(self.load_saved_state_btn, 0, 1)
        load_grid.addWidget(self.load_market_btn, 0, 2)
        load_grid.addWidget(self.load_promo_btn, 1, 0)
        load_grid.addWidget(self.load_sales_btn, 1, 1)

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

        # Action buttons
        reset_zoom_btn = QPushButton("Home")
        reset_zoom_btn.setFixedHeight(38)

        use_baseline_plan_btn = QPushButton("Plan → Model")
        use_baseline_plan_btn.clicked.connect(self.use_baseline_for_selected_plan)

        use_baseline_unit_btn = QPushButton("All → Model")
        use_baseline_unit_btn.clicked.connect(self.use_baseline_for_pricing_unit)
        
        use_loaded_plan_btn = QPushButton("Plan → Loaded")
        use_loaded_plan_btn.clicked.connect(self.use_loaded_for_selected_plan)

        use_loaded_unit_btn = QPushButton("All → Loaded")
        use_loaded_unit_btn.clicked.connect(self.use_loaded_for_pricing_unit)
        
        baseline_grid = QGridLayout()
        baseline_grid.setHorizontalSpacing(6)
        baseline_grid.setVerticalSpacing(6)

        baseline_grid.addWidget(use_baseline_plan_btn, 0, 0)
        baseline_grid.addWidget(use_loaded_plan_btn, 0, 1)
        baseline_grid.addWidget(use_baseline_unit_btn, 1, 0)
        baseline_grid.addWidget(use_loaded_unit_btn, 1, 1)

        brush_start_btn = QPushButton("Set brush\nstart")
        brush_start_btn.clicked.connect(self.set_brush_start)

        brush_end_btn = QPushButton("Set brush\nend")
        brush_end_btn.clicked.connect(self.set_brush_end)

        brush_clear_btn = QPushButton("Clear brush\nrange")
        brush_clear_btn.clicked.connect(self.clear_brush_range)

        brush_start_btn.setToolTip("Select a point, then click to mark the brush start.")
        brush_end_btn.setToolTip("Select another point on the same plan, then click to mark the brush end.")
        brush_clear_btn.setToolTip("Clear the current brush range.")

        for btn in [brush_start_btn, brush_end_btn, brush_clear_btn]:
            btn.setFixedSize(96, 72)
            btn.setToolTipDuration(8000)
            btn.setMouseTracking(True)

        brush_btn_row = QHBoxLayout()
        brush_btn_row.addWidget(brush_start_btn)
        brush_btn_row.addWidget(brush_end_btn)
        brush_btn_row.addWidget(brush_clear_btn)

        export_btn = QPushButton("💾 Export HT prices csv")
        export_btn.clicked.connect(self.export_prices)

        export_pdf_btn = QPushButton("📄 Export PDF report")
        export_pdf_btn.clicked.connect(self.export_all_charts_pdf)

        remove_promo_btn = QPushButton("❌ Remove selected promo")
        remove_promo_btn.clicked.connect(self.remove_selected_promo)

        # Info widgets
        self.country_info_label = QLabel("No country loaded")
        self.country_info_label.setWordWrap(True)

        self.brush_label = QLabel("Brush range: not set")
        self.brush_label.setWordWrap(True)

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
        self.promo_list.itemClicked.connect(self.apply_promo)

        # Build left panel
        side_layout.addLayout(load_grid)
        side_layout.addLayout(currency_grid)
        side_layout.addWidget(self.currency_mode_banner)

        side_layout.addWidget(QLabel("Country"))
        side_layout.addWidget(self.country_combo)

        side_layout.addWidget(QLabel("Drag tools"))
        side_layout.addLayout(mode_row_1)
        side_layout.addLayout(mode_row_2)
        side_layout.addLayout(mode_row_3)


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

        side_layout.addLayout(baseline_grid)

        # 👇 ADD THE ROW HERE (just above remove promo)
        side_layout.addLayout(brush_btn_row)

        # 👇 THEN continue
        for widget in [
            export_btn,
            export_pdf_btn,
            self.brush_label,
            # self.tool_help_label,
            QLabel("Selected point info"), self.selection_label,
            QLabel("Promo options"), self.promo_list,
        ]:
            side_layout.addWidget(widget)

        side_layout.addStretch(1)

        # Canvas
        self.canvas = PriceCurveCanvas()
        reset_zoom_btn.clicked.connect(self.canvas.reset_zoom)
        self.canvas.pointSelected.connect(self.on_point_selected)
        self.canvas.pointDragged.connect(self.on_point_dragged)
        self.canvas.promoSelected.connect(self.on_promo_selected_from_chart)
        self.canvas.statusChanged.connect(self.statusBar().showMessage)

        self.splitter.addWidget(self.side_panel)
        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 1100])

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
            datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
        except ValueError:
            return None
        return timestamp

    @staticmethod
    def _history_timestamp_label(timestamp: str) -> str:
        try:
            return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return timestamp

    def saved_state_timestamps(self) -> list[str]:
        timestamp_sets: list[set[str]] = []
        for currency in CURRENCIES:
            history_dir = FILES.editor_history_dir(FILES.editor_exports_dir, currency)
            price_timestamps = {
                ts for path in history_dir.glob("manual_prices_*.csv")
                for ts in [self._history_timestamp(path, "manual_prices_", ".csv")]
                if ts
            }
            promo_timestamps = {
                ts for path in history_dir.glob("promos_*.json")
                for ts in [self._history_timestamp(path, "promos_", ".json")]
                if ts
            }
            timestamp_sets.append(price_timestamps & promo_timestamps)

        if not timestamp_sets:
            return []
        return sorted(set.intersection(*timestamp_sets), reverse=True)

    def read_promos_from_folder(self, folder: str | Path, names: list[str]) -> tuple[bool, list[dict]]:
        folder = Path(folder)
        for currency in CURRENCIES:
            path = find_currency_file(folder, currency, names)
            if path is None:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data = [data]
                if isinstance(data, list):
                    return True, data
            except Exception:
                continue
        return False, []

    def load_promos_from_folder(self, folder: str | Path) -> list[dict]:
        _found, data = self.read_promos_from_folder(folder, ["promos_current.json"])
        return data

    def load_export_prices_from_folder(self, folder: str | Path, silent: bool = False) -> bool:
        df = self.load_currency_tables_from_folder(
            folder,
            ["manual_prices_current.csv", "model_proposal_latest.csv"],
        )
        if df.empty:
            if not silent:
                QMessageBox.warning(self, "Load failed", "No usable exported price rows found in that folder.")
            return False

        self.state.preload_last_export(df)
        self.refresh_canvas()
        return True

    def load_export_promos_from_folder(self, folder: str | Path, silent: bool = False) -> bool:
        found, data = self.read_promos_from_folder(folder, ["promos_current.json"])
        if not found:
            if not silent:
                QMessageBox.warning(self, "Load failed", "No usable exported promos found in that folder.")
            return False

        self.state.preload_last_exported_promos(data)
        self.refresh_canvas()
        return True

    def load_baseline(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Load baseline HT/model folder",
        )
        if not folder:
            return

        df = self.load_currency_tables_from_folder(
            folder,
            ["model_proposal_latest.csv", "manual_prices_current.csv"],
        )
        if df.empty:
            QMessageBox.warning(self, "Load failed", "No usable model rows found in that folder.")
            return

        self.state.preload_baseline(df)

        if not self.load_export_prices_from_folder(FILES.editor_exports_dir, silent=True):
            self.state.reload_working_from_baseline()

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

        df = pd.read_excel(path)
        df.columns = df.columns.astype(str).str.strip()

        if df.empty:
            QMessageBox.warning(self, "Load failed", "No usable rows found in that file.")
            return

        self.state.preload_sales_volumes(df)
        self.refresh_canvas()

    def load_saved_state(self):
        if not self.state.row_index:
            QMessageBox.warning(self, "Load saved state", "Load a model proposal before loading a saved state.")
            return

        timestamps = self.saved_state_timestamps()
        if not timestamps:
            QMessageBox.information(
                self,
                "Load saved state",
                "No complete saved states were found in outputs/manual_prices/history.",
            )
            return

        labels = [self._history_timestamp_label(ts) for ts in timestamps]
        label_to_timestamp = dict(zip(labels, timestamps))
        selected_label, ok = QInputDialog.getItem(
            self,
            "Load saved state",
            "Saved state:",
            labels,
            0,
            False,
        )
        if not ok or not selected_label:
            return

        timestamp = label_to_timestamp[str(selected_label)]
        history_root = FILES.editor_history_root
        df = self.load_currency_tables_from_folder(
            history_root,
            [f"manual_prices_{timestamp}.csv"],
        )
        if df.empty:
            QMessageBox.warning(self, "Load saved state", "Could not load saved prices for that date.")
            return

        found_promos, promos = self.read_promos_from_folder(
            history_root,
            [f"promos_{timestamp}.json"],
        )
        if not found_promos:
            QMessageBox.warning(self, "Load saved state", "Could not load saved promos for that date.")
            return

        self.state.preload_last_export(df)
        self.state.preload_last_exported_promos(promos)
        self.populate_combos()
        self.refresh_canvas()
        self.statusBar().showMessage(f"Loaded saved state: {selected_label}")

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

    def use_baseline_for_selected_plan(self):
        self.state.reload_selected_plan_from_baseline()
        self.refresh_canvas()

    def use_baseline_for_pricing_unit(self):
        self.state.reload_pricing_unit_from_baseline()
        self.refresh_canvas()
        
    def use_loaded_for_selected_plan(self):
        self.state.reload_selected_plan_from_loaded()
        self.refresh_canvas()


    def use_loaded_for_pricing_unit(self):
        self.state.reload_pricing_unit_from_loaded()
        self.refresh_canvas()

    def save_exports_to_folder(self, export_dir: Path, include_history: bool = False, autosave: bool = False) -> str:
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        for currency in CURRENCIES:
            currency_dir = export_dir / currency
            currency_dir.mkdir(parents=True, exist_ok=True)

            if autosave:
                prices_path = currency_dir / "manual_prices_autosave.csv"
                promos_path = currency_dir / "promos_autosave.json"
            else:
                prices_path = currency_dir / "manual_prices_current.csv"
                promos_path = currency_dir / "promos_current.json"

            self.state.export_prices_csv(prices_path, currency=currency)
            self.state.export_applied_promos_json(promos_path, currency=currency)

            if include_history:
                currency_history_dir = FILES.editor_history_dir(export_dir, currency)
                currency_history_dir.mkdir(parents=True, exist_ok=True)
                self.state.export_prices_csv(currency_history_dir / f"manual_prices_{ts}.csv", currency=currency)
                self.state.export_applied_promos_json(currency_history_dir / f"promos_{ts}.json", currency=currency)

        return ts

    def save_region_prices_to_folder(
        self,
        export_dir: Path,
        *,
        include_history: bool = False,
        timestamp: str | None = None,
    ):
        export_dir = Path(export_dir)
        results = generate_region_prices_for_export_folder(export_dir)
        if include_history:
            ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
            for result in results:
                history_dir = FILES.editor_history_dir(export_dir, result.currency)
                history_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result.output_csv, history_dir / f"region_prices_{ts}.csv")
        return results
        
    def quick_save(self):
        try:
            export_dir = FILES.editor_exports_dir
            self.save_exports_to_folder(export_dir)

            self.statusBar().showMessage("Quick saved to outputs/manual_prices/current")

        except Exception as e:
            print("Quick save failed:", e)
            self.statusBar().showMessage("Quick save failed.")


    def autosave(self):
        try:
            export_dir = FILES.editor_autosave_dir
            self.save_exports_to_folder(export_dir, autosave=True)

            self.statusBar().showMessage("Autosaved")

        except Exception as e:
            print("Autosave failed:", e)


    def export_prices(self):
        cursor_active = False
        try:
            default_zip = f"TT_prices_{datetime.now().strftime('%y%m%d')}.zip"
            FILES.partner_packs_dir.mkdir(parents=True, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save clean price pack ZIP",
                str(FILES.partner_packs_dir / default_zip),
                "Zip files (*.zip)"
            )
            if not path:
                return

            zip_path = Path(path)
            if zip_path.suffix.lower() != ".zip":
                zip_path = zip_path.with_suffix(".zip")
            local_export_dir = FILES.editor_exports_dir

            self.statusBar().showMessage("Saving local export, history, promos, and regions...")
            self.clear_busy_cursor()
            QApplication.setOverrideCursor(Qt.WaitCursor)
            cursor_active = True
            QApplication.processEvents()

            ts = self.save_exports_to_folder(local_export_dir, include_history=True)
            region_results = self.save_region_prices_to_folder(
                local_export_dir,
                include_history=True,
                timestamp=ts,
            )
            pack_result = build_partner_price_pack(local_export_dir, zip_path)

            QApplication.restoreOverrideCursor()
            cursor_active = False

            pack_lines = [
                (
                    f"{result.member_name}: {result.rows_written} rows"
                    f" ({result.rows_removed_below_cost} below-cost removed)"
                )
                for result in pack_result.files
            ]
            excluded = sorted({country for result in region_results for country in result.excluded_countries})
            excluded_text = ", ".join(excluded) if excluded else "none"
            QMessageBox.information(
                self,
                "Export complete",
                f"Local latest files and history were saved in:\n{local_export_dir}\n\n"
                f"The clean partner ZIP was saved as:\n{pack_result.zip_path}\n\n"
                f"CSV files inside ZIP: {len(pack_result.files)}\n"
                + "\n".join(pack_lines)
                + f"\n\nExcluded countries due to cost floor: {excluded_text}",
            )
            self.statusBar().showMessage("Export complete: local history saved and clean ZIP pack created.")

        except Exception as e:
            QMessageBox.warning(self, "Export failed", f"Could not save export: {e}")
            self.statusBar().showMessage("Export failed.")

        finally:
            if cursor_active:
                self.clear_busy_cursor()

    def populate_combos(self):
        self.country_combo.blockSignals(True)
        self.country_combo.clear()
        self.country_combo.addItems(self.state.countries())

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

        # Neutral (baseline numbers)
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
        self.refresh_selection_label()
        self.country_info_label.setText(self.state.country_info())
        self.brush_label.setText(self.state.brush_summary())
        self.refresh_impact_labels()
        

    def on_point_selected(self, row_id: str):
        self.state.selected_row_id = row_id
        self.refresh_canvas()

    def on_promo_selected_from_chart(self, promo_code: str):
        if promo_code == "__REMOVE_PROMO__":
            self.state.remove_selected_promo()
        else:
            self.state.assign_promo_to_selected(str(promo_code))

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

        self.refresh_canvas()

    def refresh_selection_label(self):
        info = self.state.selected_point_info()
        if not info:
            self.selection_label.setText("No point selected")
            return

        promo = f"\nPromo: {info['promo']}" if info.get("promo") else ""
        self.selection_label.setText(
            f"{info['plan']} | {info['days']} days | {info['gb']} GB\n"
            f"Currency: {self.state.active_currency}\n"
            f"Working price: {info['y']:.2f}\n"
            f"Model price: {info['base_y']:.2f}\n"
            f"ISO: {info.get('iso') or '-'}\n"
            f"Pricing unit: {info['pricing_unit_id'] or '-'}\n"
            f"Source: {info['pricing_source'] or '-'} | Region: {info['pricing_region'] or '-'}\n"
            f"Unit countries: {info['pricing_unit_countries'] or '-'}\n"
            f"Countries affected in editor: {info['editor_scope_countries'] or '-'}{promo}"
        )

    def refresh_promo_list(self):
        self.promo_list.clear()

        info = self.state.selected_point_info()

        if info:
            promo_key = str(info.get("promo_scope_key", "")).strip()
            has_promo = promo_key in self.state.promo_store

            if has_promo:
                item = QListWidgetItem("✖  REMOVE APPLIED PROMO")
                item.setData(Qt.UserRole, "__REMOVE_PROMO__")

                font = QFont()
                font.setPointSize(12)
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor("#c62828"))

                self.promo_list.addItem(item)

        for promo in self.state.promo_candidates_for_selected():
            item = QListWidgetItem(
                f"{promo['promo_label']}  ->  {promo['final_price_after_promo']:.2f} {self.state.active_currency}"
            )
            item.setData(Qt.UserRole, promo["promo_code"])
            self.promo_list.addItem(item)

    def apply_promo(self, item: QListWidgetItem):
        promo_code = item.data(Qt.UserRole)

        if promo_code == "__REMOVE_PROMO__":
            self.state.remove_selected_promo()
        else:
            self.state.assign_promo_to_selected(str(promo_code))

        self.refresh_canvas()

    def toggle_left_panel(self):
        if self._left_panel_collapsed:
            self.side_panel.show()
            self.side_panel.setMinimumWidth(320)
            self.side_panel.setMaximumWidth(16777215)
            self.splitter.setSizes([320, 1100])
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
        self.brush_label.setText(self.state.brush_summary())

    def set_brush_end(self):
        self.state.set_brush_end()
        self.brush_label.setText(self.state.brush_summary())

    def clear_brush_range(self):
        self.state.clear_brush()
        self.brush_label.setText(self.state.brush_summary())

    def remove_selected_promo(self):
        self.state.remove_selected_promo()
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

        progress("Loading model proposal...")
        df = self.load_currency_tables_from_folder(
            FILES.proposals_dir,
            ["model_proposal_latest.csv"],
        )
        if not df.empty:
            progress("Preparing model proposal...")
            self.state.preload_baseline(df)

            progress("Loading last local export...")
            if not self.load_export_prices_from_folder(FILES.editor_exports_dir, silent=True):
                self.state.reload_working_from_baseline()

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

        progress("Loading exported promos...")
        self.load_export_promos_from_folder(FILES.editor_exports_dir, silent=True)

        if self.state.countries():
            progress("Drawing editor...")
            self.populate_combos()
            self.refresh_canvas()
            self.statusBar().showMessage("Auto-loaded available files.")
        else:
            self.statusBar().showMessage(
                "Auto-load found nothing usable. Use the buttons to load files."
            )


def run():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    window = MainWindow()
    window.show()
    app.exec()
