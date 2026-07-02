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
        return self.base_dir / "scrapes"

    @property
    def work_dir(self) -> Path:
        return self.base_dir / "outputs"

    @property
    def diagnostics_dir(self) -> Path:
        return self.work_dir / "diagnostics"

    @property
    def logs_dir(self) -> Path:
        return self.diagnostics_dir / "logs"

    @property
    def scrape_status_latest(self) -> Path:
        return self.diagnostics_dir / "scrape_status_latest.csv"

    @property
    def scrape_status_history_dir(self) -> Path:
        return self.diagnostics_dir / "scrape_status_history"

    @property
    def combined_dir(self) -> Path:
        return self.work_dir / "combined_scrapes"

    @property
    def market_dir(self) -> Path:
        return self.work_dir / "market_analysis"

    @property
    def proposals_dir(self) -> Path:
        return self.work_dir / "model_proposals"

    @property
    def manual_prices_dir(self) -> Path:
        return self.work_dir / "manual_prices"

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
        return self.combined_dir / "combined_scrape_latest.csv"

    @property
    def combined_history_dir(self) -> Path:
        return self.combined_dir / "history"

    def combined_history(self, day: date | None = None) -> Path:
        day = day or date.today()
        return self.combined_history_dir / f"combined_scrape_{day.isoformat()}.csv"

    @property
    def scraped_diffs_dir(self) -> Path:
        return self.combined_dir / "diffs"

    @property
    def market_annotated(self) -> Path:
        return self.market_dir / "market_prices_annotated_latest.csv"

    @property
    def market_outlier_audit(self) -> Path:
        return self.market_dir / "outlier_audit_latest.csv"

    @property
    def dropped_rows_debug(self) -> Path:
        return self.market_dir / "dropped_rows_debug.csv"

    def model_dir(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.proposals_dir / normalize_currency(currency)

    def model_latest(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "model_proposal_latest.csv"

    def model_failed_countries(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "model_failed_countries_latest.csv"

    def model_history_dir(self, currency: str = DEFAULT_CURRENCY) -> Path:
        return self.model_dir(currency) / "history"

    def model_history(self, currency: str = DEFAULT_CURRENCY, day: date | None = None) -> Path:
        day = day or date.today()
        return self.model_history_dir(currency) / f"model_proposal_{day.isoformat()}.csv"

    @property
    def editor_exports_dir(self) -> Path:
        return self.manual_prices_dir / "current"

    @property
    def editor_autosave_dir(self) -> Path:
        return self.manual_prices_dir / "autosave"

    @property
    def editor_history_root(self) -> Path:
        return self.manual_prices_dir / "history"

    @property
    def partner_packs_dir(self) -> Path:
        return self.work_dir / "partner_packs"

    def editor_currency_dir(self, root: Path, currency: str = DEFAULT_CURRENCY) -> Path:
        return root / normalize_currency(currency)

    def editor_history_dir(self, root: Path, currency: str = DEFAULT_CURRENCY) -> Path:
        if Path(root) == self.editor_exports_dir:
            return self.editor_history_root / normalize_currency(currency)
        return self.editor_currency_dir(root, currency) / "history"

    @property
    def amdocs_output_dir(self) -> Path:
        return self.manual_prices_dir / "change_reports"

    @staticmethod
    def timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    @staticmethod
    def ensure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)


FILES = PipelineFiles()
