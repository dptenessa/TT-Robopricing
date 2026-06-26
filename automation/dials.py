"""
Amplifier-style PyQt6 dashboard for editing config.py.

Project layout:
    project/
    ├─ config_default.py      # your permanent original defaults
    ├─ config.py              # live config written by this dashboard
    └─ config_dashboard.py    # this file

Install:
    pip install PyQt6 numpy

Run:
    python config_dashboard.py

Behavior:
- On launch, defaults are read from config_default.py.
- Current values are read from config.py when it exists.
- Every switch, knob, slider, or text edit immediately writes to config.py.
- Reset reloads config_default.py and overwrites config.py.
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QScrollArea,
    QSlider,
    QSpinBox,
    QDoubleSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent if APP_DIR.name == "automation" else APP_DIR
CONFIG_PY_PATH = APP_DIR / "config.py"
CONFIG_DEFAULT_PY_PATH = APP_DIR / "config_default.py"

CONFIG_KEYS = [
    "K_NEIGHBORS",
    "MIN_NEIGHBORS_REQUIRED",
    "GB_WEIGHT",
    "DAYS_WEIGHT",
    "MAX_DISTANCE",
    "ROW_RATIO_THRESHOLD",
    "MIN_MATCHED_OFFERS",
    "PROVIDER_RATIO_THRESHOLD",
    "USE_LOG_PRICE",
    "DEFAULT_EUR_TO_USD",
    "EDITOR_DUAL_CURRENCY_DEFAULT",
    "BATCH_CURRENCIES",
    "UTILIZATION_OF_GB_IN_PRACTICE",
    "K",
    "PACKAGE_CONFIG",
    "STRATEGY_MAP",
    "CHOSEN_STRATEGY",
    "VAT",
    "COUNTRY_SURFACE_MIN_ROWS",
    "BLEND_SURFACE_MIN_ROWS",
    "DAYS_LOG_OFFSET",
    "GB_LOG_OFFSET",
    "MIN_REL_STEP_GROWTH",
    "MIN_ABS_STEP_GROWTH",
    "CONCAVITY_DECAY_FACTOR",
    "PROMO_CHECK_DAYS",
    "PROMO_EPSILON",
    "PROMOS_PATH_DEFAULT",
    "GB_TOLERANCE_RATIO",
    "PROMO_LOW_PPG_MAX_LOWER_PCT",
    "PROMO_TARGET_COMPETITOR_RANK",
    "PROMO_TARGET_POSITION",
    "PROMO_TARGET_MARGIN_PCT",
    "INPUT_REGIONS",
    "OUTPUT_NAME",
]


def load_config_module(path: Path, module_name: str) -> Any | None:
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RuntimeError(f"Could not import {path.name}: {exc}") from exc
    return module


def normalize_value(value: Any) -> Any:
    if isinstance(value, Path):
        try:
            return str(value.relative_to(APP_DIR))
        except ValueError:
            return str(value)
    return value


def module_to_config(module: Any) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        if hasattr(module, key):
            config[key] = normalize_value(getattr(module, key))

    if hasattr(module, "DAYS_RANGE"):
        days = list(getattr(module, "DAYS_RANGE"))
        if days:
            config["DAYS_RANGE_START"] = int(days[0])
            config["DAYS_RANGE_END"] = int(days[-1])

    return config


def load_default_config() -> dict[str, Any]:
    module = load_config_module(CONFIG_DEFAULT_PY_PATH, "config_default_source")
    if module is None:
        raise FileNotFoundError(
            f"Missing {CONFIG_DEFAULT_PY_PATH.name}. Save your original pasted config as config_default.py."
        )
    config = module_to_config(module)
    config.setdefault("CHOSEN_STRATEGY", "balanced")
    return config


def render_config_py(c: dict[str, Any]) -> str:
    days_start = int(c["DAYS_RANGE_START"])
    days_end = int(c["DAYS_RANGE_END"])

    promo_days = c["PROMO_CHECK_DAYS"]
    if isinstance(promo_days, str):
        promo_days = {int(part.strip()) for part in promo_days.split(",") if part.strip()}

    return f'''import numpy as np
from pathlib import Path

# ---------------------------------
# Configuration for outlier removal
# ---------------------------------
K_NEIGHBORS = {c["K_NEIGHBORS"]}
MIN_NEIGHBORS_REQUIRED = {c["MIN_NEIGHBORS_REQUIRED"]}

# Distance weights in log-space
GB_WEIGHT = {c["GB_WEIGHT"]}
DAYS_WEIGHT = {c["DAYS_WEIGHT"]}

# Optional: prevent comparisons that are too far away
MAX_DISTANCE = {c["MAX_DISTANCE"]}

# Row-level flag
ROW_RATIO_THRESHOLD = {c["ROW_RATIO_THRESHOLD"]}

# Provider-level removal
MIN_MATCHED_OFFERS = {c["MIN_MATCHED_OFFERS"]}
PROVIDER_RATIO_THRESHOLD = {c["PROVIDER_RATIO_THRESHOLD"]}

USE_LOG_PRICE = {c["USE_LOG_PRICE"]}


# ---------------------------------
# Currency management
# ---------------------------------
DEFAULT_EUR_TO_USD = {c.get("DEFAULT_EUR_TO_USD", 1.10)}
EDITOR_DUAL_CURRENCY_DEFAULT = {c.get("EDITOR_DUAL_CURRENCY_DEFAULT", False)}
BATCH_CURRENCIES = {repr(c.get("BATCH_CURRENCIES", ("USD", "EUR")))}


# ---------------------------------
# Configuration for pricing model
# ---------------------------------

UTILIZATION_OF_GB_IN_PRACTICE = {c["UTILIZATION_OF_GB_IN_PRACTICE"]}
K = {c["K"]}
DAYS_RANGE = np.arange({days_start}, {days_end + 1})

PACKAGE_CONFIG = {repr(c["PACKAGE_CONFIG"])}

STRATEGY_MAP = {repr(c["STRATEGY_MAP"])}

CHOSEN_STRATEGY = {repr(c.get("CHOSEN_STRATEGY", "balanced"))}

VAT = {c["VAT"]}

COUNTRY_SURFACE_MIN_ROWS = {c["COUNTRY_SURFACE_MIN_ROWS"]}
BLEND_SURFACE_MIN_ROWS = {c["BLEND_SURFACE_MIN_ROWS"]}

DAYS_LOG_OFFSET = {c["DAYS_LOG_OFFSET"]}
GB_LOG_OFFSET = {c["GB_LOG_OFFSET"]}

MIN_REL_STEP_GROWTH = {c["MIN_REL_STEP_GROWTH"]}
MIN_ABS_STEP_GROWTH = {c["MIN_ABS_STEP_GROWTH"]}

CONCAVITY_DECAY_FACTOR = {c["CONCAVITY_DECAY_FACTOR"]}

PROMO_CHECK_DAYS = {promo_days}
PROMO_EPSILON = {c["PROMO_EPSILON"]}

PROMOS_PATH_DEFAULT = "{c["PROMOS_PATH_DEFAULT"]}"
 
# PROMO MANAGEMENT DIALERS:
GB_TOLERANCE_RATIO = {c["GB_TOLERANCE_RATIO"]}
PROMO_LOW_PPG_MAX_LOWER_PCT = {c["PROMO_LOW_PPG_MAX_LOWER_PCT"]}

# PROMOS TARGETS TWEAKS
PROMO_TARGET_COMPETITOR_RANK = {c["PROMO_TARGET_COMPETITOR_RANK"]}
PROMO_TARGET_POSITION = {repr(c["PROMO_TARGET_POSITION"])}
PROMO_TARGET_MARGIN_PCT = {c["PROMO_TARGET_MARGIN_PCT"]}


# ---------------------------------
# Configuration for region price definition (paths are relative to BASE_DIR)
# ---------------------------------
BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "automation":
    BASE_DIR = BASE_DIR.parent
INPUT_REGIONS = BASE_DIR / "{c["INPUT_REGIONS"]}"
OUTPUT_NAME = "{c["OUTPUT_NAME"]}"
'''


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PY_PATH):
        self.path = path
        self.default_config = load_default_config()
        self.config = self.load_current()

    def load_current(self) -> dict[str, Any]:
        config = deepcopy(self.default_config)
        module = load_config_module(self.path, "config_live_source")
        if module is None:
            self.save_config(config)
            return config

        loaded = module_to_config(module)
        config.update(loaded)
        config.setdefault("CHOSEN_STRATEGY", "balanced")
        self._repair_missing_nested_values(config)
        return config

    def _repair_missing_nested_values(self, config: dict[str, Any]) -> None:
        """Keep old config.py compatible when config_default.py gains new packages/plans."""
        default_packages = self.default_config.get("PACKAGE_CONFIG", {})
        live_packages = config.setdefault("PACKAGE_CONFIG", {})
        for package_name, package_values in default_packages.items():
            live_packages.setdefault(package_name, deepcopy(package_values))
            for field, default_value in package_values.items():
                live_packages[package_name].setdefault(field, default_value)

        default_strategies = self.default_config.get("STRATEGY_MAP", {})
        live_strategies = config.setdefault("STRATEGY_MAP", {})
        for strategy_name, strategy_values in default_strategies.items():
            live_strategies.setdefault(strategy_name, deepcopy(strategy_values))
            live_strategies[strategy_name].setdefault("overall", strategy_values.get("overall", 1.0))
            live_strategies[strategy_name].setdefault("plan", {})
            for plan_name, default_value in strategy_values.get("plan", {}).items():
                live_strategies[strategy_name]["plan"].setdefault(plan_name, default_value)

    def save(self) -> None:
        self._repair_missing_nested_values(self.config)
        self.save_config(self.config)

    def save_config(self, config: dict[str, Any]) -> None:
        self.path.write_text(render_config_py(config), encoding="utf-8")

    def reset(self) -> None:
        self.default_config = load_default_config()
        self.config = deepcopy(self.default_config)
        self.save()


class ToggleSwitch(QCheckBox):
    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(24)
        self.setStyleSheet(
            """
            QCheckBox {
                color: #d7dde8;
                spacing: 10px;
                font-weight: 600;
            }
            QCheckBox::indicator {
                width: 44px;
                height: 22px;
                border-radius: 11px;
                background: #343a46;
                border: 1px solid #4b5260;
            }
            QCheckBox::indicator:checked {
                background: #18a058;
                border: 1px solid #31c978;
            }
            QCheckBox::indicator:unchecked:hover {
                background: #424a58;
            }
            QCheckBox::indicator:checked:hover {
                background: #20b968;
            }
            """
        )


class KnobControl(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        label: str,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int = 3,
        integer: bool = False,
        suffix: str = "",
    ):
        super().__init__()
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.decimals = decimals
        self.integer = integer
        self.suffix = suffix
        self.scale = 1 if integer else max(1, round(1 / step))
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)

        self.label = QLabel(label)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color: #dce3ef; font-weight: 700;")

        self.dial = QSlider(Qt.Orientation.Vertical)
        self.dial.setMinimum(self._to_int(minimum))
        self.dial.setMaximum(self._to_int(maximum))
        self.dial.setTickPosition(QSlider.TickPosition.TicksBothSides)
        self.dial.setTickInterval(max(1, int((self.dial.maximum() - self.dial.minimum()) / 8)))
        self.dial.setFixedHeight(105)
        self.dial.valueChanged.connect(self._dial_changed)

        if integer:
            self.number = QSpinBox()
            self.number.setRange(int(minimum), int(maximum))
            self.number.valueChanged.connect(lambda value: self.setValue(float(value), emit=True))
        else:
            self.number = QDoubleSpinBox()
            self.number.setRange(minimum, maximum)
            self.number.setDecimals(decimals)
            self.number.setSingleStep(step)
            self.number.valueChanged.connect(lambda value: self.setValue(float(value), emit=True))
        self.number.setFixedWidth(88)
        self.number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if suffix:
            self.number.setSuffix(suffix)

        layout.addWidget(self.label)
        layout.addWidget(self.dial, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.number, alignment=Qt.AlignmentFlag.AlignCenter)

        self.setStyleSheet(
            """
            QSlider::groove:vertical {
                background: #242935;
                width: 7px;
                border-radius: 5px;
                border: 1px solid #4b5260;
            }
            QSlider::handle:vertical {
                background: #f2c94c;
                height: 18px;
                margin: 0 -9px;
                border-radius: 9px;
                border: 2px solid #ffe08a;
            }
            QSlider::add-page:vertical { background: #313846; border-radius: 5px; }
            QSlider::sub-page:vertical { background: #52616f; border-radius: 5px; }
            QSpinBox, QDoubleSpinBox {
                color: #f4f7fb;
                background: #151922;
                border: 1px solid #4b5260;
                border-radius: 8px;
                padding: 3px;
                font-weight: 700;
            }
            """
        )

    def _to_int(self, value: float) -> int:
        return int(round(value * self.scale))

    def _from_int(self, value: int) -> float:
        out = value / self.scale
        return float(round(out, self.decimals)) if not self.integer else float(int(round(out)))

    def _dial_changed(self, raw: int) -> None:
        self.setValue(self._from_int(raw), emit=True)

    def setValue(self, value: float, emit: bool = False) -> None:
        value = max(self.minimum, min(self.maximum, value))
        if self.integer:
            value = int(round(value))
        else:
            value = round(value, self.decimals)

        if self._syncing:
            return
        self._syncing = True
        self.dial.setValue(self._to_int(value))
        self.number.setValue(value)
        self._syncing = False
        if emit:
            self.valueChanged.emit(float(value))

    def value(self) -> float:
        return float(self.number.value())


class SliderControl(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(self, label: str, minimum: float, maximum: float, step: float, decimals: int = 3, suffix: str = ""):
        super().__init__()
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.decimals = decimals
        self.scale = max(1, round(1 / step))
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        top = QHBoxLayout()
        name = QLabel(label)
        name.setStyleSheet("color: #dce3ef; font-weight: 700;")
        self.number = QDoubleSpinBox()
        self.number.setRange(minimum, maximum)
        self.number.setDecimals(decimals)
        self.number.setSingleStep(step)
        self.number.setFixedWidth(90)
        if suffix:
            self.number.setSuffix(suffix)
        self.number.valueChanged.connect(lambda value: self.setValue(float(value), emit=True))
        top.addWidget(name)
        top.addStretch()
        top.addWidget(self.number)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(self._to_int(minimum))
        self.slider.setMaximum(self._to_int(maximum))
        self.slider.valueChanged.connect(lambda raw: self.setValue(self._from_int(raw), emit=True))

        layout.addLayout(top)
        layout.addWidget(self.slider)
        self.setStyleSheet(
            """
            QSlider::groove:horizontal {
                height: 7px;
                background: #242935;
                border-radius: 5px;
                border: 1px solid #4b5260;
            }
            QSlider::handle:horizontal {
                background: #4cc9f0;
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
                border: 2px solid #aeefff;
            }
            QSlider::sub-page:horizontal { background: #386b7d; border-radius: 5px; }
            QDoubleSpinBox {
                color: #f4f7fb;
                background: #151922;
                border: 1px solid #4b5260;
                border-radius: 8px;
                padding: 3px;
                font-weight: 700;
            }
            """
        )

    def _to_int(self, value: float) -> int:
        return int(round(value * self.scale))

    def _from_int(self, value: int) -> float:
        return round(value / self.scale, self.decimals)

    def setValue(self, value: float, emit: bool = False) -> None:
        value = round(max(self.minimum, min(self.maximum, value)), self.decimals)
        if self._syncing:
            return
        self._syncing = True
        self.slider.setValue(self._to_int(value))
        self.number.setValue(value)
        self._syncing = False
        if emit:
            self.valueChanged.emit(float(value))


class ConfigDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        try:
            self.store = ConfigStore()
        except Exception as exc:
            QMessageBox.critical(None, "Config error", str(exc))
            raise

        self.widgets: dict[str, QWidget] = {}
        self.package_widgets: dict[tuple[str, str], KnobControl] = {}
        self.strategy_widgets: dict[tuple[str, str, str | None], KnobControl] = {}
        self.strategy_group_widgets: dict[str, QGroupBox] = {}
        self.strategy_radio_widgets: dict[str, QRadioButton] = {}
        self.strategy_button_group = QButtonGroup(self)
        self._building = False

        self.setWindowTitle("Pricing Control Panel")
        self.resize(1080, 720)
        self._build_ui()
        self._load_values_into_widgets()

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Pricing Control Panel")
        title.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #f4f7fb;")
        subtitle = QLabel(f"Defaults: {CONFIG_DEFAULT_PY_PATH.name}  →  Live output: {CONFIG_PY_PATH.name}")
        subtitle.setStyleSheet("color: #9aa7b8;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_row.addLayout(title_box)
        title_row.addStretch()

        reset_btn = QPushButton("RESET TO DEFAULT")
        reset_btn.setObjectName("dangerButton")
        reset_btn.clicked.connect(self.reset_defaults)
        title_row.addWidget(reset_btn)
        outer.addLayout(title_row)

        tabs = QTabWidget()
        tabs.addTab(self._scrollable(self._outlier_tab()), "Outliers")
        tabs.addTab(self._scrollable(self._pricing_tab()), "Pricing")
        tabs.addTab(self._scrollable(self._packages_tab()), "Packages")
        tabs.addTab(self._scrollable(self._strategies_tab()), "Strategies")
        tabs.addTab(self._scrollable(self._promos_tab()), "Promos")
        tabs.addTab(self._scrollable(self._regions_tab()), "Regions")
        outer.addWidget(tabs)

        self.status = QLabel("Ready")
        self.status.setStyleSheet("color: #9aa7b8; padding-top: 4px;")
        outer.addWidget(self.status)

        self.setCentralWidget(root)
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #10131a; }
            QTabWidget::pane {
                border: 1px solid #2d3340;
                background: #151922;
                border-radius: 12px;
            }
            QTabBar::tab {
                color: #c9d3df;
                background: #1a1f2a;
                padding: 7px 12px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #273142;
                color: #ffffff;
            }
            QGroupBox {
                color: #f4f7fb;
                font-weight: 800;
                border: 1px solid #303849;
                border-radius: 14px;
                margin-top: 10px;
                padding: 10px;
                background: #171c26;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 16px;
                padding: 0 8px;
                color: #f2c94c;
            }
            QLabel { color: #dce3ef; font-size: 11px; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                color: #f4f7fb;
                background: #151922;
                border: 1px solid #4b5260;
                border-radius: 8px;
                padding: 4px;
                font-weight: 700;
            }
            QPushButton {
                color: #f4f7fb;
                background: #263142;
                border: 1px solid #3c4658;
                border-radius: 10px;
                padding: 7px 11px;
                font-weight: 800;
            }
            QPushButton:hover { background: #344258; }
            QPushButton#dangerButton {
                background: #5b2333;
                border: 1px solid #9b4059;
                color: #ffd7df;
            }
            QPushButton#dangerButton:hover { background: #7a2d44; }
            QScrollArea { border: none; }
            """
        )

    def _scrollable(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def _tab_container(self) -> tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        return widget, layout

    def _group(self, title: str) -> tuple[QGroupBox, QGridLayout]:
        box = QGroupBox(title)
        grid = QGridLayout(box)
        for col in range(5):
            grid.setColumnMinimumWidth(col, 130)
            grid.setColumnStretch(col, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        return box, grid

    def _add_knob(self, grid: QGridLayout, row: int, col: int, key: str, label: str, *, minimum: float, maximum: float, step: float, decimals: int = 3, integer: bool = False, suffix: str = "") -> None:
        knob = KnobControl(label, minimum, maximum, step, decimals, integer, suffix)
        knob.valueChanged.connect(lambda value, k=key, i=integer: self._set_value(k, int(value) if i else float(value)))
        grid.addWidget(knob, row, col, alignment=Qt.AlignmentFlag.AlignCenter)
        self.widgets[key] = knob

    def _add_slider(self, layout: QVBoxLayout, key: str, label: str, *, minimum: float, maximum: float, step: float, decimals: int = 3, suffix: str = "") -> None:
        slider = SliderControl(label, minimum, maximum, step, decimals, suffix)
        slider.valueChanged.connect(lambda value, k=key: self._set_value(k, float(value)))
        layout.addWidget(slider)
        self.widgets[key] = slider

    def _add_bool(self, layout: QVBoxLayout, key: str, label: str) -> None:
        switch = ToggleSwitch(label)
        switch.stateChanged.connect(lambda _, k=key, w=switch: self._set_value(k, w.isChecked()))
        layout.addWidget(switch)
        self.widgets[key] = switch

    def _add_text_row(self, grid: QGridLayout, row: int, key: str, label: str, browse: bool = False) -> None:
        grid.addWidget(QLabel(label), row, 0)
        edit = QLineEdit()
        edit.textChanged.connect(lambda value, k=key: self._set_value(k, value))
        grid.addWidget(edit, row, 1, 1, 2)
        self.widgets[key] = edit
        if browse:
            btn = QPushButton("BROWSE")
            btn.clicked.connect(self.choose_regions_file)
            grid.addWidget(btn, row, 3)

    def _add_combo_row(self, grid: QGridLayout, row: int, key: str, label: str, values: list[str]) -> None:
        grid.addWidget(QLabel(label), row, 0)
        combo = QComboBox()
        combo.addItems(values)
        combo.currentTextChanged.connect(lambda value, k=key: self._set_value(k, value))
        grid.addWidget(combo, row, 1, 1, 2)
        self.widgets[key] = combo

    def _outlier_tab(self) -> QWidget:
        widget, layout = self._tab_container()
        box, grid = self._group("Outlier removal deck")
        self._add_knob(grid, 0, 0, "K_NEIGHBORS", "K neighbors", minimum=1, maximum=20, step=1, integer=True)
        self._add_knob(grid, 0, 1, "MIN_NEIGHBORS_REQUIRED", "Min neighbors", minimum=0, maximum=20, step=1, integer=True)
        self._add_knob(grid, 0, 2, "MIN_MATCHED_OFFERS", "Min matched offers", minimum=0, maximum=50, step=1, integer=True)
        self._add_knob(grid, 0, 3, "MAX_DISTANCE", "Max distance", minimum=0, maximum=5, step=0.01, decimals=2)
        self._add_knob(grid, 1, 0, "GB_WEIGHT", "GB weight", minimum=0, maximum=5, step=0.01, decimals=2)
        self._add_knob(grid, 1, 1, "DAYS_WEIGHT", "Days weight", minimum=0, maximum=5, step=0.01, decimals=2)
        self._add_knob(grid, 1, 2, "ROW_RATIO_THRESHOLD", "Row ratio threshold", minimum=0, maximum=10, step=0.01, decimals=2)
        self._add_knob(grid, 1, 3, "PROVIDER_RATIO_THRESHOLD", "Provider ratio threshold", minimum=0, maximum=10, step=0.01, decimals=2)
        layout.addWidget(box)

        switch_box = QGroupBox("Switches")
        switch_layout = QVBoxLayout(switch_box)
        self._add_bool(switch_layout, "USE_LOG_PRICE", "Use log price")
        layout.addWidget(switch_box)
        layout.addStretch()
        return widget

    def _pricing_tab(self) -> QWidget:
        widget, layout = self._tab_container()

        sliders = QGroupBox("Main pricing faders")
        slider_layout = QVBoxLayout(sliders)
        self._add_slider(slider_layout, "UTILIZATION_OF_GB_IN_PRACTICE", "Utilization of GB in practice", minimum=0, maximum=1, step=0.001, decimals=3)
        self._add_slider(slider_layout, "VAT", "VAT", minimum=0, maximum=1, step=0.001, decimals=3)
        self._add_slider(slider_layout, "MIN_REL_STEP_GROWTH", "Minimum relative step growth", minimum=0, maximum=0.1, step=0.0005, decimals=4)
        self._add_slider(slider_layout, "MIN_ABS_STEP_GROWTH", "Minimum absolute step growth", minimum=0, maximum=1, step=0.001, decimals=3)
        layout.addWidget(sliders)

        box, grid = self._group("Pricing knobs")
        self._add_knob(grid, 0, 0, "K", "K multiplier", minimum=0, maximum=5, step=0.01, decimals=2)
        self._add_knob(grid, 0, 1, "DAYS_RANGE_START", "Days start", minimum=1, maximum=365, step=1, integer=True)
        self._add_knob(grid, 0, 2, "DAYS_RANGE_END", "Days end", minimum=1, maximum=365, step=1, integer=True)
        self._add_knob(grid, 0, 3, "CONCAVITY_DECAY_FACTOR", "Concavity decay", minimum=0, maximum=5, step=0.01, decimals=2)
        self._add_knob(grid, 1, 0, "COUNTRY_SURFACE_MIN_ROWS", "Country rows", minimum=0, maximum=100, step=1, integer=True)
        self._add_knob(grid, 1, 1, "BLEND_SURFACE_MIN_ROWS", "Blend rows", minimum=0, maximum=100, step=1, integer=True)
        self._add_knob(grid, 1, 2, "DAYS_LOG_OFFSET", "Days log offset", minimum=0, maximum=20, step=0.01, decimals=2)
        self._add_knob(grid, 1, 3, "GB_LOG_OFFSET", "GB log offset", minimum=0, maximum=20, step=0.01, decimals=2)
        layout.addWidget(box)
        layout.addStretch()
        return widget

    def _packages_tab(self) -> QWidget:
        widget, layout = self._tab_container()
        box, grid = self._group("Package channel strip")
        row = 0
        for package_name in self.store.default_config["PACKAGE_CONFIG"]:
            title = QLabel(package_name)
            title.setFont(QFont("Arial", 12, QFont.Weight.Bold))
            title.setStyleSheet("color: #f2c94c;")
            grid.addWidget(title, row, 0, 1, 5)
            row += 1

            avg = KnobControl("Average daily GB", 0, 20, 0.01, 2)
            avg.valueChanged.connect(lambda value, p=package_name: self._set_package(p, "avg_daily", float(value)))
            grid.addWidget(avg, row, 0, alignment=Qt.AlignmentFlag.AlignCenter)
            self.package_widgets[(package_name, "avg_daily")] = avg

            std = KnobControl("Daily std", 0, 10, 0.01, 2)
            std.valueChanged.connect(lambda value, p=package_name: self._set_package(p, "daily_std", float(value)))
            grid.addWidget(std, row, 1, alignment=Qt.AlignmentFlag.AlignCenter)
            self.package_widgets[(package_name, "daily_std")] = std
            row += 1

            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet("color: #303849;")
            grid.addWidget(line, row, 0, 1, 5)
            row += 1
        layout.addWidget(box)
        layout.addStretch()
        return widget

    def _strategies_tab(self) -> QWidget:
        widget, layout = self._tab_container()

        selector_box = QGroupBox("Choose active strategy")
        selector_layout = QHBoxLayout(selector_box)
        selector_layout.setSpacing(18)

        self.strategy_button_group = QButtonGroup(self)
        self.strategy_button_group.setExclusive(True)
        self.strategy_radio_widgets.clear()

        for strategy_name in self.store.default_config["STRATEGY_MAP"]:
            radio = QRadioButton(strategy_name)
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            radio.setStyleSheet(
                """
                QRadioButton {
                    color: #dce3ef;
                    font-weight: 800;
                    padding: 6px 10px;
                }
                QRadioButton::indicator {
                    width: 15px;
                    height: 15px;
                }
                QRadioButton::indicator:checked {
                    background: #f2c94c;
                    border: 2px solid #ffe08a;
                    border-radius: 8px;
                }
                QRadioButton::indicator:unchecked {
                    background: #242935;
                    border: 2px solid #4b5260;
                    border-radius: 8px;
                }
                """
            )
            radio.toggled.connect(lambda checked, s=strategy_name: self._strategy_radio_changed(s, checked))
            self.strategy_button_group.addButton(radio)
            self.strategy_radio_widgets[strategy_name] = radio
            selector_layout.addWidget(radio)

        selector_layout.addStretch()
        layout.addWidget(selector_box)

        self.strategy_group_widgets.clear()
        self.strategy_widgets.clear()

        for strategy_name, strategy in self.store.default_config["STRATEGY_MAP"].items():
            box, grid = self._group(f"{strategy_name} dialers")
            self.strategy_group_widgets[strategy_name] = box

            overall = KnobControl("Overall", 0, 3, 0.01, 2)
            overall.valueChanged.connect(lambda value, s=strategy_name: self._set_strategy_overall(s, float(value)))
            grid.addWidget(overall, 0, 0, alignment=Qt.AlignmentFlag.AlignCenter)
            self.strategy_widgets[(strategy_name, "overall", None)] = overall

            col = 1
            for plan_name in strategy["plan"]:
                knob = KnobControl(plan_name, 0, 3, 0.01, 2)
                knob.valueChanged.connect(lambda value, s=strategy_name, p=plan_name: self._set_strategy_plan(s, p, float(value)))
                grid.addWidget(knob, 0, col, alignment=Qt.AlignmentFlag.AlignCenter)
                self.strategy_widgets[(strategy_name, "plan", plan_name)] = knob
                col += 1

            layout.addWidget(box)

        layout.addStretch()
        return widget

    def _promos_tab(self) -> QWidget:
        widget, layout = self._tab_container()

        sliders = QGroupBox("Promo faders")
        slider_layout = QVBoxLayout(sliders)
        self._add_slider(slider_layout, "GB_TOLERANCE_RATIO", "GB tolerance ratio", minimum=0, maximum=2, step=0.001, decimals=3)
        self._add_slider(slider_layout, "PROMO_LOW_PPG_MAX_LOWER_PCT", "Promo low PPG max lower pct", minimum=0, maximum=2, step=0.001, decimals=3)
        self._add_slider(slider_layout, "PROMO_TARGET_MARGIN_PCT", "Promo target margin pct", minimum=-50, maximum=50, step=0.1, decimals=1, suffix=" %")
        layout.addWidget(sliders)

        box, grid = self._group("Promo controls")
        self._add_knob(grid, 0, 0, "PROMO_TARGET_COMPETITOR_RANK", "Competitor rank", minimum=1, maximum=20, step=1, integer=True)
        self._add_knob(grid, 0, 1, "PROMO_EPSILON", "Promo epsilon", minimum=0, maximum=0.0001, step=0.000001, decimals=8)
        layout.addWidget(box)

        text_box = QGroupBox("Promo routing")
        text_grid = QGridLayout(text_box)
        self._add_text_row(text_grid, 0, "PROMO_CHECK_DAYS", "Promo check days")
        self._add_text_row(text_grid, 1, "PROMOS_PATH_DEFAULT", "Promos path")
        self._add_combo_row(text_grid, 2, "PROMO_TARGET_POSITION", "Target position", ["below", "above"])
        layout.addWidget(text_box)
        layout.addStretch()
        return widget

    def _regions_tab(self) -> QWidget:
        widget, layout = self._tab_container()
        box = QGroupBox("Region file routing")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self._add_text_row(grid, 0, "INPUT_REGIONS", "Input regions YAML", browse=True)
        self._add_text_row(grid, 1, "OUTPUT_NAME", "Output CSV name")
        layout.addWidget(box)
        layout.addStretch()
        return widget

    def _strategy_radio_changed(self, strategy_name: str, checked: bool) -> None:
        if self._building or not checked:
            return
        self.store.config["CHOSEN_STRATEGY"] = strategy_name
        self._refresh_strategy_visibility()
        self._save_status()

    def _refresh_strategy_visibility(self) -> None:
        chosen = self.store.config.get("CHOSEN_STRATEGY", "balanced")
        if chosen not in self.strategy_group_widgets and self.strategy_group_widgets:
            chosen = next(iter(self.strategy_group_widgets))
            self.store.config["CHOSEN_STRATEGY"] = chosen

        for strategy_name, box in self.strategy_group_widgets.items():
            box.setVisible(strategy_name == chosen)

        radio = self.strategy_radio_widgets.get(chosen)
        if radio is not None and not radio.isChecked():
            radio.setChecked(True)

    def _set_value(self, key: str, value: Any) -> None:
        if self._building:
            return
        if isinstance(value, str):
            value = value.strip()
        self.store.config[key] = value
        self._save_status()

    def _set_package(self, package_name: str, key: str, value: float) -> None:
        if self._building:
            return
        self.store.config["PACKAGE_CONFIG"][package_name][key] = value
        self._save_status()

    def _set_strategy_overall(self, strategy: str, value: float) -> None:
        if self._building:
            return
        self.store.config["STRATEGY_MAP"][strategy]["overall"] = value
        self._save_status()

    def _set_strategy_plan(self, strategy: str, plan: str, value: float) -> None:
        if self._building:
            return
        self.store.config["STRATEGY_MAP"][strategy]["plan"][plan] = value
        self._save_status()

    def _save_status(self) -> None:
        self.store.save()
        self.status.setText(f"Saved to {self.store.path.name}")

    def _load_values_into_widgets(self) -> None:
        self._building = True
        try:
            for key, widget in self.widgets.items():
                value = self.store.config[key]
                if key == "PROMO_CHECK_DAYS" and isinstance(value, set):
                    value = ", ".join(str(x) for x in sorted(value))

                if isinstance(widget, ToggleSwitch):
                    widget.setChecked(bool(value))
                elif isinstance(widget, KnobControl):
                    widget.setValue(float(value))
                elif isinstance(widget, SliderControl):
                    widget.setValue(float(value))
                elif isinstance(widget, QComboBox):
                    index = widget.findText(str(value))
                    widget.setCurrentIndex(max(index, 0))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))

            for (package_name, key), widget in self.package_widgets.items():
                widget.setValue(float(self.store.config["PACKAGE_CONFIG"][package_name][key]))

            for (strategy_name, kind, plan_name), widget in self.strategy_widgets.items():
                if kind == "overall":
                    widget.setValue(float(self.store.config["STRATEGY_MAP"][strategy_name]["overall"]))
                else:
                    widget.setValue(float(self.store.config["STRATEGY_MAP"][strategy_name]["plan"][plan_name]))

            chosen = self.store.config.get("CHOSEN_STRATEGY", "balanced")
            if chosen in self.strategy_radio_widgets:
                self.strategy_radio_widgets[chosen].setChecked(True)
            self._refresh_strategy_visibility()
        finally:
            self._building = False

    def reset_defaults(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset live config",
            "Overwrite config.py with values from config_default.py?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.reset()
        self._load_values_into_widgets()
        self.status.setText("Reset config.py from config_default.py")

    def choose_regions_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose regions YAML", str(APP_DIR), "YAML files (*.yaml *.yml);;All files (*)")
        if not path:
            return
        chosen = Path(path)
        try:
            value = str(chosen.relative_to(APP_DIR))
        except ValueError:
            value = str(chosen)
        widget = self.widgets["INPUT_REGIONS"]
        if isinstance(widget, QLineEdit):
            widget.setText(value)


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ConfigDashboard()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
