from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from currency_support import DEFAULT_CURRENCY, normalize_currency


MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent if MODULE_DIR.name == "automation" else MODULE_DIR


@dataclass(frozen=True)
class PipelineFiles:
    base_dir: Path = PROJECT_ROOT

    @property
    def inputs_dir(self) -> Path:
        return self.base_dir / "inputs"

    @property
    def scraper_outputs_dir(self) -> Path:
        return self.base_dir / "outputs"

    @property
    def work_dir(self) -> Path:
        return self.base_dir / "workable_data"

    @property
    def ppg_csv(self) -> Path:
        return self.inputs_dir / "WS_PPG.csv"

    @property
    def ppg_xlsx(self) -> Path:
        return self.inputs_dir / "WS_PPG.xlsx"

    @property
    def pricing_units_json(self) -> Path:
        return self.inputs_dir / "pricing_units.json"

    @property
    def promos_json(self) -> Path:
        return self.inputs_dir / "promos.json"

    @property
    def sales_volumes_xlsx(self) -> Path:
        return self.inputs_dir / "sales_volumes_last_month_test.xlsx"

    @property
    def combined_latest(self) -> Path:
        return self.work_dir / "combined_scrapped_data_latest.csv"

    @property
    def combined_history_dir(self) -> Path:
        return self.work_dir / "history"

    def combined_history(self, day: date | None = None) -> Path:
        day = day or date.today()
        return self.combined_history_dir / f"combined_scrapped_data_{day.isoformat()}.csv"

    @property
    def scraped_diffs_dir(self) -> Path:
        return self.work_dir / "diffs"

    @property
    def market_annotated(self) -> Path:
        return self.work_dir / "market_prices_annotated.csv"

    @property
    def market_outlier_audit(self) -> Path:
        return self.work_dir / "market_prices_outlier_audit_log.csv"

    @property
    def dropped_rows_debug(self) -> Path:
        return self.work_dir / "dropped_rows_debug.csv"

    def model_dir(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.work_dir / normalize_currency(currency)

    def model_latest(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "ht_prices_latest.csv"

    def model_failed_countries(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "ht_failed_countries_latest.csv"

    def model_history_dir(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "history"

    def model_history(self, currency: str = DEFAULT_CURRENCY, day: date | None = None) -> Path:
        day = day or date.today()
        return self.model_history_dir(currency) / f"ht_prices_{day.isoformat()}.csv"

    @property
    def legacy_model_latest(self) -> Path:
        return self.work_dir / "ht_prices_latest.csv"

    @property
    def legacy_model_history_dir(self) -> Path:
        return self.work_dir / "history"

    def legacy_model_history(self, day: date | None = None) -> Path:
        day = day or date.today()
        return self.legacy_model_history_dir / f"ht_prices_{day.isoformat()}.csv"

    @property
    def legacy_failed_countries(self) -> Path:
        return self.work_dir / "ht_failed_countries_latest.csv"

    @property
    def editor_exports_dir(self) -> Path:
        return self.work_dir / "exports"

    @property
    def editor_autosave_dir(self) -> Path:
        return self.work_dir / "autosave"

    def editor_currency_dir(self, root: Path, currency: str = DEFAULT_CURRENCY) -> Path:
        return root / normalize_currency(currency)

    def editor_history_dir(self, root: Path, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.editor_currency_dir(root, currency) / "history"

    @property
    def amdocs_output_dir(self) -> Path:
        return self.scraper_outputs_dir / "amdocs"

    @staticmethod
    def timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def ensure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)


FILES = PipelineFiles()
