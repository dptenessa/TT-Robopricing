
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import math

import numpy as np
import pandas as pd
try:
    from pricing_book import read_pricing_workbook, write_pricing_workbook
except ImportError:
    from automation.pricing_book import read_pricing_workbook, write_pricing_workbook
from config import UTILIZATION_OF_GB_IN_PRACTICE, VAT, HT_REV_SHARE
from costing import cost_per_gb_from_eur, ipg_fee
try:
    from config import DEFAULT_EUR_TO_USD, EDITOR_DUAL_CURRENCY_DEFAULT
except Exception:
    DEFAULT_EUR_TO_USD = 1.10
    EDITOR_DUAL_CURRENCY_DEFAULT = False

from currency_support import (
    CURRENCIES,
    DEFAULT_CURRENCY,
    DUAL_CURRENCY_MODE,
    LINKED_USD_MODE,
    add_currency_price_columns,
    convert_price,
    currency_price_column,
    normalize_currency,
)


DEFAULT_MAX_DAYS = 30


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() in {"true", "t", "yes", "y", "1"}


def iso_to_a3(code: Any) -> str:
    """Return ISO-3166 alpha-3 for alpha-2/alpha-3 input when possible.

    The editor now treats the CSV ISO column as the primary country code.
    Some legacy inputs, especially PPG, still use alpha-3, so this helper
    keeps cost lookup backward-compatible.
    """
    code = str(code).strip().upper()
    if not code or code == "NAN":
        return ""
    if len(code) == 3:
        return code
    if len(code) != 2:
        return code
    try:
        import pycountry  # type: ignore
        country = pycountry.countries.get(alpha_2=code)
        return str(country.alpha_3).upper() if country else code
    except Exception:
        return code


def build_sku_scope_key(pricing_unit_id, package, days) -> str:
    pricing_unit = str(pricing_unit_id).strip()
    package = str(package).strip()
    if pricing_unit.lower() == "nan":
        pricing_unit = ""
    if package.lower() == "nan":
        package = ""
    if pd.isna(days):
        days_part = ""
    else:
        days_f = float(days)
        days_part = str(int(days_f)) if days_f.is_integer() else str(days_f)
    return f"{pricing_unit}|{package}|{days_part}"


def _identity_part(value: Any, *, upper: bool = False) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    try:
        number = float(text)
        text = str(int(number)) if number.is_integer() else f"{number:g}"
    except Exception:
        pass
    return text.upper() if upper else text


def build_price_row_key(iso: Any, plan: Any, days: Any, gb: Any) -> str:
    return "|".join([
        _identity_part(iso, upper=True),
        _identity_part(plan, upper=True),
        _identity_part(days),
        _identity_part(gb),
    ])


def calculate_promo_price(base_price: float, promo_type: str, promo_value: float) -> float:
    promo_type = str(promo_type).strip().lower()
    if promo_type in {"percentage", "%"}:
        promo_type = "percent"
    if promo_type == "absolute":
        final_price = base_price - float(promo_value)
    elif promo_type == "percent":
        final_price = base_price * (1.0 - float(promo_value) / 100.0)
    else:
        final_price = base_price
    return max(round(final_price, 4), 0.0)


def convert_absolute_promo_value(
    promo_value: float,
    from_currency: str,
    to_currency: str,
    eur_to_usd: float,
) -> float:
    return float(convert_price(promo_value, from_currency, to_currency, eur_to_usd))


def load_promos(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    promos: list[dict[str, Any]] = []
    for item in data:
        promo_type = str(item.get("promo_type", "")).strip().lower()
        if promo_type in {"percentage", "%"}:
            promo_type = "percent"
        if promo_type not in {"absolute", "percent"}:
            continue
        promos.append({
            "promo_code": str(item.get("promo_code", "")).strip(),
            "promo_type": promo_type,
            "promo_value": float(item.get("promo_value", 0)),
            "promo_currency": "",
            "label": str(item.get("label", "")).strip() or str(item.get("promo_code", "")).strip(),
        })
    return [p for p in promos if p["promo_code"]]


def load_table(
    path: str | Path,
    currency_hint: str | None = None,
    eur_to_usd: float = DEFAULT_EUR_TO_USD,
) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    if path.suffix.lower() == ".xlsx":
        try:
            df = read_pricing_workbook(path)
        except Exception:
            try:
                df = pd.read_excel(path, sheet_name="All_Data")
            except Exception:
                df = pd.read_excel(path)
    elif path.suffix.lower() == ".xls":
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    needed = [
        "Provider", "ReferenceProvider", "Country", "ISO", "ISO3", "SKU", "GB", "Days", "Price",
        "Currency", "Price_USD", "Price_EUR", "usd_price", "eur_price", "EUR_TO_USD", "COST_EUR_TO_USD",
        "Cost", "IsBelowCostFloor", "Plan", "PricingUnitIdUsed", "PricingSourceUsed",
        "PricingRegionUsed", "PricingUnitCountriesUsed", "PromoScopeKey", "PromoCode",
        "PromoType", "PromoValue", "PromoCurrency", "PromoLabel", "PromoBasePrice", "FinalPriceAfterPromo",
        "FinalPriceAfterPromo_USD", "FinalPriceAfterPromo_EUR", "CalculatedCostFloor",
        "CostFloor_USD", "CostFloor_EUR", "USD_IsBelowCostFloor", "EUR_IsBelowCostFloor",
        "AllowBelowCost", "IsPartnerExportBlocked", "PartnerExportBlockReason",
    ]
    if "IsBelowCostFloor" not in df.columns:
        if "IsBelowCalculatedCostFloor" in df.columns:
            df["IsBelowCostFloor"] = df["IsBelowCalculatedCostFloor"]
        elif "Is_Below_Cost_Floor" in df.columns:
            df["IsBelowCostFloor"] = df["Is_Below_Cost_Floor"]
    for col in needed:
        if col not in df.columns:
            df[col] = np.nan

    df = df[needed].copy()
    df = add_currency_price_columns(
        df,
        currency_hint=currency_hint or DEFAULT_CURRENCY,
        eur_to_usd=eur_to_usd,
        fill_missing_with_conversion=True,
    )

    for col in [
        "GB", "Days", "Price", "Price_USD", "Price_EUR", "Cost", "PromoValue",
        "PromoBasePrice", "FinalPriceAfterPromo", "FinalPriceAfterPromo_USD", "FinalPriceAfterPromo_EUR",
        "EUR_TO_USD", "COST_EUR_TO_USD", "CalculatedCostFloor", "CostFloor_USD", "CostFloor_EUR",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in [
        "Provider", "ReferenceProvider", "Country", "ISO", "ISO3", "SKU", "Plan", "PricingUnitIdUsed",
        "PricingSourceUsed", "PricingRegionUsed", "PricingUnitCountriesUsed", "PromoScopeKey",
        "PromoCode", "PromoType", "PromoCurrency", "PromoLabel", "Currency"
    ]:
        df[col] = df[col].astype(str).replace({"nan": "", "None": "", "<NA>": "", "NaT": ""}).str.strip()

    # ISO is the primary country code for editor logic. Keep ISO3 only as
    # a legacy fallback / export compatibility column.
    df["ISO"] = df["ISO"].where(df["ISO"].astype(str).str.strip().ne(""), df["ISO3"])
    df["ISO"] = df["ISO"].astype(str).replace({"nan": "", "None": "", "<NA>": "", "NaT": ""}).str.strip().str.upper()
    df["ISO3"] = df["ISO3"].astype(str).replace({"nan": "", "None": "", "<NA>": "", "NaT": ""}).str.strip().str.upper()

    df = df[
        (df["Country"].ne("") | df["ISO"].ne(""))
        & (df["Price_USD"].notna() | df["Price_EUR"].notna() | df["Price"].notna())
    ].copy().reset_index(drop=True)
    df["row_id"] = df.index.astype(str)
    df["sku_scope_key"] = df.apply(
        lambda row: build_sku_scope_key(row.get("PricingUnitIdUsed", ""), row.get("Plan", ""), row.get("Days", None)),
        axis=1,
    )
    df["PromoScopeKey"] = df["PromoScopeKey"].where(df["PromoScopeKey"].astype(str).str.strip().ne(""), df["sku_scope_key"])
    return df


@dataclass
class EditorState:
    pricebook_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    market_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    ppg_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    ppg_cost_by_iso: dict[str, float] = field(default_factory=dict)
    units_sold_by_scope: dict[str, float] = field(default_factory=dict)
    sales_by_scope: dict[str, dict[str, float]] = field(default_factory=dict)
    promo_catalog: list[dict[str, Any]] = field(default_factory=list)
    recommendations_by_key: dict[tuple[str, str, float, float | None, str], dict[str, Any]] = field(default_factory=dict)
    recommendations_source: str = ""
    recommendations_loaded_rows: int = 0
    recommendations_actionable_rows: int = 0
    applied_recommendation_keys: set[tuple[str, str, float, float | None, str]] = field(default_factory=set)
    selected_country: str | None = None
    selected_row_id: str | None = None
    max_days: int = DEFAULT_MAX_DAYS
    active_currency: str = DEFAULT_CURRENCY
    currency_mode: str = DUAL_CURRENCY_MODE if EDITOR_DUAL_CURRENCY_DEFAULT else LINKED_USD_MODE
    eur_to_usd: float = DEFAULT_EUR_TO_USD
    cost_eur_to_usd: float = DEFAULT_EUR_TO_USD
    cost_eur_to_usd_source: str = "ECB"
    cost_eur_to_usd_date: str = "not loaded"
    cost_eur_to_usd_status: str = "fallback"
    

    country_info_map: dict[str, str] = field(default_factory=dict)
    country_iso_map: dict[str, str] = field(default_factory=dict)
    points_by_country: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    competitors_by_country: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    row_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    scope_to_row_ids: dict[str, list[str]] = field(default_factory=dict)
    country_floor_points_by_key: dict[tuple[str, str, float], list[dict[str, Any]]] = field(default_factory=dict)

    working_prices: dict[str, float] = field(default_factory=dict)
    working_price_by_scope: dict[str, float] = field(default_factory=dict)
    working_prices_by_currency: dict[str, dict[str, float]] = field(default_factory=lambda: {c: {} for c in CURRENCIES})

    loaded_prices: dict[str, float] = field(default_factory=dict)
    loaded_prices_by_currency: dict[str, dict[str, float]] = field(default_factory=lambda: {c: {} for c in CURRENCIES})
    loaded_promo_store_by_currency: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {c: {} for c in CURRENCIES}
    )

    promo_store_by_currency: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {c: {} for c in CURRENCIES}
    )

    brush_start_row_id: str | None = None
    brush_end_row_id: str | None = None

    def is_region_destination(self, destination: str) -> bool:
        points = self.points_by_country.get(str(destination), [])
        return any(
            str(point.get("pricing_source", "")).strip().lower() == "region_max"
            for point in points
        )

    @staticmethod
    def _recommendation_float_key(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return round(number, 6)

    @staticmethod
    def _recommendation_text(value: Any) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        text = str(value).strip()
        return "" if text.lower() in {"nan", "none", "<na>", "nat"} else text

    def _recommendation_key(
        self,
        pricing_unit_id: Any,
        plan: Any,
        days: Any,
        gb: Any,
        currency: Any,
    ) -> tuple[str, str, float, float | None, str] | None:
        day_value = self._recommendation_float_key(days)
        if day_value is None:
            return None
        unit = self._recommendation_text(pricing_unit_id).upper()
        plan_text = self._recommendation_text(plan).upper()
        currency_text = normalize_currency(currency)
        if not unit or not plan_text:
            return None
        return (unit, plan_text, day_value, self._recommendation_float_key(gb), currency_text)

    def preload_recommendations(self, df: pd.DataFrame, source: str | Path | None = None) -> None:
        self.recommendations_by_key = {}
        self.recommendations_source = str(source or "")
        self.recommendations_loaded_rows = 0
        self.recommendations_actionable_rows = 0
        self.applied_recommendation_keys = set()

        if df is None or df.empty:
            for point in self.row_index.values():
                point.pop("recommendation", None)
                point.pop("recommendation_stale", None)
                point.pop("recommendation_applied", None)
            return

        required = {"PricingUnitIdUsed", "Plan", "Days", "GB", "Currency", "Actionable"}
        missing = sorted(required - set(df.columns))
        if missing:
            raise ValueError(f"Recommendation file is missing required columns: {', '.join(missing)}")

        loaded = 0
        actionable = 0
        for _, row in df.iterrows():
            key = self._recommendation_key(
                row.get("PricingUnitIdUsed", ""),
                row.get("Plan", ""),
                row.get("Days", None),
                row.get("GB", None),
                row.get("Currency", DEFAULT_CURRENCY),
            )
            if key is None:
                continue
            record = row.to_dict()
            record["Actionable"] = _boolish(record.get("Actionable", False))
            record["Direction"] = self._recommendation_text(record.get("Direction", "")).upper()
            record["Confidence"] = self._recommendation_text(record.get("Confidence", "")).upper()
            record["Mechanism"] = self._recommendation_text(record.get("Mechanism", "")).upper()
            record["Currency"] = normalize_currency(record.get("Currency", DEFAULT_CURRENCY))
            self.recommendations_by_key[key] = record
            loaded += 1
            actionable += int(bool(record["Actionable"]))

        self.recommendations_loaded_rows = loaded
        self.recommendations_actionable_rows = actionable
        for point in self.row_index.values():
            self._attach_recommendation_state(point)

    def recommendation_for_point(
        self,
        point: dict[str, Any],
        currency: str | None = None,
        *,
        actionable_only: bool = False,
    ) -> dict[str, Any] | None:
        currency = normalize_currency(currency or self.active_currency)
        key = self._recommendation_key(
            point.get("pricing_unit_id", ""),
            point.get("plan", ""),
            point.get("days", None),
            point.get("gb", None),
            currency,
        )
        if key is None:
            return None
        raw = self.recommendations_by_key.get(key)
        if raw is None:
            return None
        if actionable_only and not bool(raw.get("Actionable", False)):
            return None

        rec = dict(raw)
        applied = key in self.applied_recommendation_keys
        current_list = self._working_price_for_currency(point, currency)
        current_net = self._final_price_for_currency(point, currency, current_list)
        promo = self._promo_for_point(point, currency) or {}
        current_promo = self._recommendation_text(promo.get("promo_code", ""))

        expected_list = pd.to_numeric(rec.get("CurrentListPrice", None), errors="coerce")
        expected_net = pd.to_numeric(rec.get("CurrentNetPrice", None), errors="coerce")
        expected_promo = self._recommendation_text(rec.get("CurrentPromoCode", ""))

        tolerance = 0.051
        list_ok = pd.isna(expected_list) or abs(float(expected_list) - float(current_list)) <= tolerance
        net_ok = pd.isna(expected_net) or abs(float(expected_net) - float(current_net)) <= tolerance
        promo_ok = expected_promo == current_promo
        stale = not (list_ok and net_ok and promo_ok)

        rec["applied"] = applied
        rec["fresh"] = (not stale) and (not applied)
        rec["stale"] = stale and not applied
        rec["display_actionable"] = bool(rec.get("Actionable", False)) and not stale and not applied
        rec["CurrentListPriceNow"] = float(current_list)
        rec["CurrentNetPriceNow"] = float(current_net)
        rec["CurrentPromoCodeNow"] = current_promo
        return rec

    def _clear_applied_recommendations_for_point(
        self,
        point: dict[str, Any],
        currencies: tuple[str, ...] | list[str],
    ) -> None:
        """Make recommendations eligible again after a reset restores their loaded baseline."""
        for currency in currencies:
            key = self._recommendation_key(
                point.get("pricing_unit_id", ""),
                point.get("plan", ""),
                point.get("days", None),
                point.get("gb", None),
                currency,
            )
            if key is not None:
                self.applied_recommendation_keys.discard(key)

    def _attach_recommendation_state(self, point: dict[str, Any]) -> None:
        rec = self.recommendation_for_point(point, self.active_currency, actionable_only=True)
        if rec is None:
            point.pop("recommendation", None)
            point.pop("recommendation_stale", None)
            point.pop("recommendation_applied", None)
            return
        point["recommendation_stale"] = bool(rec.get("stale", False))
        point["recommendation_applied"] = bool(rec.get("applied", False))
        if rec.get("display_actionable"):
            point["recommendation"] = rec
        else:
            point.pop("recommendation", None)

    def apply_recommendation(
        self,
        row_id: str,
        *,
        force_shared_promo: bool = False,
    ) -> dict[str, Any]:
        point = self.row_index.get(str(row_id))
        if point is None:
            return {"ok": False, "message": "Price point no longer exists."}

        active_currency = self.normalize_current_currency()
        rec = self.recommendation_for_point(point, active_currency, actionable_only=True)
        if rec is None:
            return {"ok": False, "message": "No actionable recommendation for this point."}
        if rec.get("stale"):
            return {
                "ok": False,
                "stale": True,
                "message": "This recommendation no longer matches the current price/promo. Rerun the recommendation engine.",
            }

        mechanism = self._recommendation_text(rec.get("Mechanism", "")).upper()
        direction = self._recommendation_text(rec.get("Direction", "")).upper()
        other_currency = "USD" if active_currency == "EUR" else "EUR"
        other_rec = self.recommendation_for_point(point, other_currency, actionable_only=True)

        if mechanism.startswith("LIST_PRICE"):
            suggested = pd.to_numeric(rec.get("SuggestedListPrice", None), errors="coerce")
            if pd.isna(suggested):
                suggested = pd.to_numeric(rec.get("SuggestedNetPrice", None), errors="coerce")
            if pd.isna(suggested):
                return {"ok": False, "message": "Recommendation has no usable list-price target."}

            updates = {active_currency: float(suggested)}
            if (
                other_rec
                and other_rec.get("display_actionable")
                and self._recommendation_text(other_rec.get("Mechanism", "")).upper().startswith("LIST_PRICE")
                and self._recommendation_text(other_rec.get("Direction", "")).upper() == direction
            ):
                other_suggested = pd.to_numeric(other_rec.get("SuggestedListPrice", None), errors="coerce")
                if pd.isna(other_suggested):
                    other_suggested = pd.to_numeric(other_rec.get("SuggestedNetPrice", None), errors="coerce")
                if pd.notna(other_suggested):
                    updates[other_currency] = float(other_suggested)

            self.set_scope_prices(str(row_id), updates)
            for currency in updates:
                applied_key = self._recommendation_key(
                    point.get("pricing_unit_id", ""),
                    point.get("plan", ""),
                    point.get("days", None),
                    point.get("gb", None),
                    currency,
                )
                if applied_key is not None:
                    self.applied_recommendation_keys.add(applied_key)
            currencies = "/".join(updates.keys())
            return {
                "ok": True,
                "mechanism": mechanism,
                "direction": direction,
                "currencies": list(updates.keys()),
                "message": f"Applied {direction} list-price recommendation for {currencies}.",
            }

        if mechanism == "PROMO":
            promo_code = self._recommendation_text(rec.get("SuggestedPromoCode", ""))
            if not promo_code:
                return {"ok": False, "message": "Promo recommendation has no promo code."}

            other_same = bool(
                other_rec
                and other_rec.get("display_actionable")
                and self._recommendation_text(other_rec.get("Mechanism", "")).upper() == "PROMO"
                and self._recommendation_text(other_rec.get("SuggestedPromoCode", "")) == promo_code
            )
            if not other_same and not force_shared_promo:
                if other_rec and other_rec.get("display_actionable"):
                    other_desc = (
                        f"{other_currency} recommends "
                        f"{self._recommendation_text(other_rec.get('Mechanism', ''))} "
                        f"{self._recommendation_text(other_rec.get('SuggestedPromoCode', '')) or self._recommendation_text(other_rec.get('Direction', ''))}."
                    )
                else:
                    other_desc = f"{other_currency} has no matching actionable promo recommendation."
                return {
                    "ok": False,
                    "needs_confirmation": True,
                    "promo_code": promo_code,
                    "message": (
                        f"Promo {promo_code} is shared by EUR and USD. "
                        f"{other_desc} Apply the shared promo anyway?"
                    ),
                }

            self.selected_row_id = str(row_id)
            affected = self.assign_promo_to_selected(promo_code)
            if not affected:
                return {"ok": False, "message": f"Promo {promo_code} is not available in the promo catalog."}
            active_key = self._recommendation_key(
                point.get("pricing_unit_id", ""),
                point.get("plan", ""),
                point.get("days", None),
                point.get("gb", None),
                active_currency,
            )
            if active_key is not None:
                self.applied_recommendation_keys.add(active_key)
            if other_same:
                other_key = self._recommendation_key(
                    point.get("pricing_unit_id", ""),
                    point.get("plan", ""),
                    point.get("days", None),
                    point.get("gb", None),
                    other_currency,
                )
                if other_key is not None:
                    self.applied_recommendation_keys.add(other_key)
            return {
                "ok": True,
                "mechanism": mechanism,
                "direction": direction,
                "promo_code": promo_code,
                "message": f"Applied recommended promo {promo_code} (shared EUR/USD).",
            }

        return {"ok": False, "message": f"Unsupported recommendation mechanism: {mechanism or 'unknown'}."}

    def country_destinations(self) -> list[str]:
        return sorted(
            destination
            for destination in self.points_by_country.keys()
            if not self.is_region_destination(destination)
        )

    def region_destinations(self) -> list[str]:
        return sorted(
            destination
            for destination in self.points_by_country.keys()
            if self.is_region_destination(destination)
        )

    def countries(self) -> list[str]:
        # Backward-compatible combined list; UI can render regions separately.
        return self.country_destinations() + self.region_destinations()

    def normalize_current_currency(self) -> str:
        self.active_currency = normalize_currency(self.active_currency)
        return self.active_currency

    def is_dual_currency_mode(self) -> bool:
        return self.currency_mode == DUAL_CURRENCY_MODE

    def is_linked_currency_mode(self) -> bool:
        return self.currency_mode == LINKED_USD_MODE

    def set_currency_mode(self, dual: bool) -> None:
        self.set_linked_currency_mode(not dual)

    def set_linked_currency_mode(self, linked: bool) -> None:
        self.currency_mode = LINKED_USD_MODE if linked else DUAL_CURRENCY_MODE
        self._refresh_all_display_prices()

    def set_active_currency(self, currency: str) -> None:
        self.active_currency = normalize_currency(currency)
        self._refresh_all_display_prices()

    def set_eur_to_usd(self, rate: float) -> None:
        try:
            rate = float(rate)
        except Exception:
            rate = DEFAULT_EUR_TO_USD
        self.eur_to_usd = rate if rate > 0 else DEFAULT_EUR_TO_USD
        self._refresh_all_display_prices()

    def set_official_cost_eur_to_usd(
        self,
        rate: float,
        *,
        source: str = "ECB",
        date: str = "not loaded",
        status: str = "fallback",
    ) -> None:
        try:
            rate = float(rate)
        except Exception:
            rate = DEFAULT_EUR_TO_USD
        self.cost_eur_to_usd = rate if rate > 0 else DEFAULT_EUR_TO_USD
        self.cost_eur_to_usd_source = str(source or "ECB")
        self.cost_eur_to_usd_date = str(date or "not loaded")
        self.cost_eur_to_usd_status = str(status or "fallback")
        self._refresh_all_display_prices()

    def _working_prices_for(self, currency: str | None = None) -> dict[str, float]:
        currency = normalize_currency(currency or self.active_currency)
        self.working_prices_by_currency.setdefault(currency, {})
        return self.working_prices_by_currency[currency]

    def _loaded_prices_for(self, currency: str | None = None) -> dict[str, float]:
        currency = normalize_currency(currency or self.active_currency)
        self.loaded_prices_by_currency.setdefault(currency, {})
        return self.loaded_prices_by_currency[currency]

    def _promo_store_for(self, currency: str | None = None) -> dict[str, dict[str, Any]]:
        currency = normalize_currency(currency or self.active_currency)
        self.promo_store_by_currency.setdefault(currency, {})
        return self.promo_store_by_currency[currency]

    def _loaded_promo_store_for(self, currency: str | None = None) -> dict[str, dict[str, Any]]:
        currency = normalize_currency(currency or self.active_currency)
        self.loaded_promo_store_by_currency.setdefault(currency, {})
        return self.loaded_promo_store_by_currency[currency]

    def _currencies_for_promo_edit(self) -> tuple[str, ...]:
        # Promo assignment is shared by EUR and USD.
        return CURRENCIES

    def _promo_for_point(
        self,
        point: dict[str, Any],
        currency: str | None = None,
    ) -> dict[str, Any] | None:
        promo_key = str(point.get("promo_scope_key", "")).strip()
        if not promo_key:
            return None
        return self._promo_store_for(currency).get(promo_key)

    def has_promo_for_selected(self, currency: str | None = None) -> bool:
        point = self.selected_point_info()
        return bool(point and self._promo_for_point(point, currency))

    def _currencies_for_current_edit(self) -> tuple[str, ...]:
        if self.is_dual_currency_mode():
            return (self.normalize_current_currency(),)
        return CURRENCIES

    def _price_for_currency_from_row(self, row: pd.Series, currency: str) -> float:
        currency = normalize_currency(currency)
        price = pd.to_numeric(row.get(currency_price_column(currency), np.nan), errors="coerce")
        if pd.notna(price):
            return float(price)

        row_currency = normalize_currency(row.get("Currency", DEFAULT_CURRENCY))
        legacy_price = pd.to_numeric(row.get("Price", np.nan), errors="coerce")
        if pd.notna(legacy_price):
            return float(convert_price(legacy_price, row_currency, currency, self.eur_to_usd))

        other = "EUR" if currency == "USD" else "USD"
        other_price = pd.to_numeric(row.get(currency_price_column(other), np.nan), errors="coerce")
        if pd.notna(other_price):
            return float(convert_price(other_price, other, currency, self.eur_to_usd))

        return 0.0

    def _final_price_for_currency_from_row(self, row: pd.Series, currency: str, base_price: float) -> float:
        currency = normalize_currency(currency)
        currency_specific = pd.to_numeric(
            row.get(f"FinalPriceAfterPromo_{currency}", np.nan),
            errors="coerce",
        )
        if pd.notna(currency_specific):
            return float(currency_specific)

        # Backward compatibility for legacy one-currency files.
        row_currency = normalize_currency(row.get("Currency", DEFAULT_CURRENCY))
        final_price = pd.to_numeric(row.get("FinalPriceAfterPromo", np.nan), errors="coerce")
        if pd.notna(final_price):
            return float(convert_price(final_price, row_currency, currency, self.eur_to_usd))
        return base_price

    def _promo_value_for_currency(self, promo: dict[str, Any], currency: str | None = None) -> float:
        # Absolute promo values are currency-agnostic: 4 means -4 EUR and -4 USD.
        return float(promo.get("promo_value", 0) or 0)

    def sync_linked_eur_from_usd(self) -> None:
        for p in self.row_index.values():
            prices = p.setdefault("working_prices", {})
            usd = self.round_regular_price(float(prices.get("USD", p.get("working_y", p.get("base_y", 0.0)))))
            eur = self.round_regular_price(convert_price(usd, "USD", "EUR", self.eur_to_usd))
            prices["USD"] = usd
            prices["EUR"] = eur
            self.working_prices_by_currency.setdefault("USD", {})[str(p["row_id"])] = usd
            self.working_prices_by_currency.setdefault("EUR", {})[str(p["row_id"])] = eur

    def allowed_ppg_isos(self) -> set[str]:
        return {
            str(k).strip().upper()
            for k in self.ppg_cost_by_iso.keys()
            if str(k).strip()
        }

    def ppg_country_name_map(self) -> dict[str, str]:
        """Return the authoritative ISO A2 -> country display-name mapping."""
        if self.ppg_df.empty:
            return {}

        ppg = self.ppg_df.copy()
        ppg.columns = ppg.columns.astype(str).str.strip()

        iso_col = next(
            (
                col for col in ["ISO_Code_A2", "ISO", "ISO_A2", "Country_Code_A2"]
                if col in ppg.columns
            ),
            None,
        )
        country_col = next(
            (
                col for col in ppg.columns
                if str(col).strip().lower() in {
                    "country",
                    "country name",
                    "country_name",
                    "destination",
                }
            ),
            None,
        )
        if iso_col is None or country_col is None:
            raise ValueError(
                "PPG must contain an ISO A2 column and a canonical country-name column."
            )

        ppg["_iso"] = (
            ppg[iso_col]
            .astype("string")
            .fillna("")
            .astype(str)
            .str.strip()
            .str.upper()
        )
        ppg["_country"] = (
            ppg[country_col]
            .astype("string")
            .fillna("")
            .astype(str)
            .str.strip()
        )
        ppg = ppg[
            ppg["_iso"].str.len().eq(2)
            & ppg["_country"].ne("")
        ].copy()

        conflicts = (
            ppg.groupby("_iso")["_country"]
            .nunique(dropna=True)
            .loc[lambda values: values > 1]
        )
        if not conflicts.empty:
            raise ValueError(
                "PPG contains conflicting country names for ISO codes: "
                + ", ".join(conflicts.index.tolist())
            )

        return (
            ppg.drop_duplicates(subset=["_iso"], keep="last")
            .set_index("_iso")["_country"]
            .to_dict()
        )

    def _canonicalize_pricebook_country_names(self) -> None:
        """Force loaded price-book country names to the current PPG value by ISO."""
        if self.pricebook_df.empty or "ISO" not in self.pricebook_df.columns:
            return

        country_names = self.ppg_country_name_map()
        if not country_names:
            return

        self.pricebook_df["ISO"] = (
            self.pricebook_df["ISO"]
            .astype("string")
            .fillna("")
            .astype(str)
            .str.strip()
            .str.upper()
        )
        mapped = self.pricebook_df["ISO"].map(country_names)

        ht_mask = self.pricebook_df.get(
            "Provider",
            pd.Series("", index=self.pricebook_df.index),
        ).astype(str).str.strip().eq("HT")
        missing_ht = sorted(
            self.pricebook_df.loc[
                ht_mask & self.pricebook_df["ISO"].str.len().eq(2) & mapped.isna(),
                "ISO",
            ]
            .dropna()
            .unique()
            .tolist()
        )
        if missing_ht:
            raise ValueError(
                "HT price-book rows contain ISO codes without a canonical PPG country name: "
                + ", ".join(missing_ht)
            )

        replace_mask = mapped.notna()
        self.pricebook_df.loc[replace_mask, "Country"] = mapped.loc[replace_mask]

    def set_selection_defaults(self) -> None:
        countries = self.countries()
        if countries and self.selected_country not in countries:
            self.selected_country = countries[0]

    def clear_runtime(self) -> None:
        self.country_info_map = {}
        self.country_iso_map = {}
        self.points_by_country = {}
        self.competitors_by_country = {}
        self.row_index = {}
        self.scope_to_row_ids = {}
        self.country_floor_points_by_key = {}
        self.working_prices = {}
        self.working_price_by_scope = {}
        self.working_prices_by_currency = {c: {} for c in CURRENCIES}
        self.promo_store_by_currency = {c: {} for c in CURRENCIES}
        self.loaded_prices = {}
        self.loaded_prices_by_currency = {c: {} for c in CURRENCIES}
        self.loaded_promo_store_by_currency = {c: {} for c in CURRENCIES}
        self.selected_row_id = None
        self.brush_start_row_id = None
        self.brush_end_row_id = None

    def _base_promos_from_df(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in CURRENCIES}
        if self.pricebook_df.empty:
            return out
        for _, row in self.pricebook_df.iterrows():
            code = str(row.get("PromoCode", "")).strip()
            if not code:
                continue
            key = str(row.get("PromoScopeKey", "")).strip() or str(row.get("sku_scope_key", "")).strip()
            if not key:
                continue
            catalog_item = next(
                (item for item in self.promo_catalog if str(item.get("promo_code", "")).strip() == code),
                None,
            ) or {}
            promo_type = str(row.get("PromoType", "")).strip().lower() or str(catalog_item.get("promo_type", "")).strip().lower()
            promo_value_raw = pd.to_numeric(row.get("PromoValue", None), errors="coerce")
            promo_value = float(promo_value_raw) if pd.notna(promo_value_raw) else float(catalog_item.get("promo_value", 0) or 0)
            promo_label = str(row.get("PromoLabel", "")).strip() or str(catalog_item.get("label", "")).strip() or code
            promo = {
                "promo_scope_key": key,
                "promo_code": code,
                "promo_type": promo_type,
                "promo_value": promo_value,
                "promo_currency": "",
                "promo_label": promo_label,
            }
            # Older saved rows did not distinguish promo assignment by currency.
            # Preserve that behavior by seeding both currency stores. Explicit
            # saved promo JSON files loaded later can then override each currency.
            for currency in CURRENCIES:
                out[currency][key] = dict(promo)
        return out

    def preload_pricebook(self, df: pd.DataFrame) -> None:
        self.clear_runtime()
        self.pricebook_df = df.copy()
        self._canonicalize_pricebook_country_names()
        allowed_isos = self.allowed_ppg_isos()

        if allowed_isos and "ISO" in self.pricebook_df.columns:
            self.pricebook_df["ISO"] = self.pricebook_df["ISO"].astype(str).str.strip().str.upper()
            pricing_source = self.pricebook_df.get(
                "PricingSourceUsed",
                pd.Series("", index=self.pricebook_df.index),
            ).astype(str).str.strip().str.lower()
            # Countries still obey the PPG whitelist. Locked regions are first-class
            # pricing units and must not be dropped simply because their ISO field
            # contains a region code instead of an ISO2 country code.
            keep = self.pricebook_df["ISO"].isin(allowed_isos) | pricing_source.eq("region_max")
            self.pricebook_df = self.pricebook_df[keep].copy()
        self.promo_store_by_currency = self._base_promos_from_df()

        unit_lookup: dict[str, set[str]] = {}
        for _, row in self.pricebook_df.iterrows():
            unit_id = str(row.get("PricingUnitIdUsed", "")).strip()
            if unit_id:
                unit_lookup.setdefault(unit_id, set()).add(str(row.get("Country", "")).strip())

        for country, cdf in self.pricebook_df.groupby("Country", sort=True):
            country = str(country).strip()
            cdf = cdf[pd.to_numeric(cdf["Days"], errors="coerce").le(self.max_days)].copy()
            if cdf.empty:
                continue

            ht = cdf[cdf["Provider"].astype(str).str.strip().eq("HT")].copy()
            ht = ht.sort_values(["Plan", "Days", "GB", "Price"])
            points: list[dict[str, Any]] = []
            for _, row in ht.iterrows():
                unit_id = str(row.get("PricingUnitIdUsed", "")).strip()
                scope_key = str(row.get("sku_scope_key", "")).strip()
                base_prices = {
                    currency: self._price_for_currency_from_row(row, currency)
                    for currency in CURRENCIES
                }
                base_display_prices = {
                    currency: (
                        self._final_price_for_currency_from_row(row, currency, base_prices[currency])
                        if str(row.get("PromoCode", "")).strip()
                        else base_prices[currency]
                    )
                    for currency in CURRENCIES
                }
                active_currency = self.normalize_current_currency()
                pt = {
                    "row_id": str(row["row_id"]),
                    "x": float(row["Days"]),
                    "base_prices": base_prices,
                    "base_display_prices": base_display_prices,
                    "working_prices": dict(base_prices),
                    "base_y": float(base_prices[active_currency]),
                    "base_display_y": float(base_display_prices[active_currency]),
                    "working_y": float(base_prices[active_currency]),
                    "y": float(base_prices[active_currency]),
                    "gb": float(row["GB"]) if pd.notna(row["GB"]) else None,
                    "days": float(row["Days"]) if pd.notna(row["Days"]) else None,
                    "plan": str(row["Plan"]),
                    "promo": "",
                    "scope_key": scope_key,
                    "promo_scope_key": str(row.get("PromoScopeKey", "")).strip() or scope_key,
                    "iso": str(row.get("ISO", "")).strip().upper(),
                    "iso3": str(row.get("ISO3", "")).strip().upper(),
                    "country": country,
                    "provider": str(row.get("Provider", "")).strip() or "HT",
                    "pricing_unit_id": unit_id,
                    "pricing_source": str(row.get("PricingSourceUsed", "")).strip(),
                    "pricing_region": str(row.get("PricingRegionUsed", "")).strip(),
                    "pricing_unit_countries": str(row.get("PricingUnitCountriesUsed", "")).strip(),
                    "source_currency": normalize_currency(row.get("Currency", DEFAULT_CURRENCY)),
                    "editor_scope_countries": ", ".join(sorted(c for c in unit_lookup.get(unit_id, set()) if c)),
                    "entry_key": build_price_row_key(
                        row.get("ISO", ""),
                        row.get("Plan", ""),
                        row.get("Days", None),
                        row.get("GB", None),
                    ),
                    "is_new_entry": False,
                    "allow_below_cost": _boolish(row.get("AllowBelowCost", False)),
                }
                points.append(pt)
                self.row_index[pt["row_id"]] = pt
                self.scope_to_row_ids.setdefault(scope_key, []).append(pt["row_id"])
                if str(pt.get("pricing_source", "")).strip().lower() != "region_max":
                    floor_key = (
                        str(pt.get("provider", "HT")).strip(),
                        str(pt.get("plan", "")).strip(),
                        float(pt.get("days") or 0.0),
                    )
                    self.country_floor_points_by_key.setdefault(floor_key, []).append(pt)
                for currency in CURRENCIES:
                    self.working_prices_by_currency.setdefault(currency, {})[pt["row_id"]] = float(base_prices[currency])
            self.points_by_country[country] = points

            row0 = ht.iloc[0] if not ht.empty else cdf.iloc[0]
            country_iso = str(row0.get("ISO", "")).strip().upper()
            if country_iso:
                self.country_iso_map[country] = country_iso

            self.country_info_map[country] = (
                f"Country: {country} | ISO: {str(row0.get('ISO','')).strip() or '-'}\n"
                f"Pricing unit: {str(row0.get('PricingUnitIdUsed','')).strip() or '-'} | "
                f"Source: {str(row0.get('PricingSourceUsed','')).strip() or '-'} | "
                f"Region: {str(row0.get('PricingRegionUsed','')).strip() or '-'}\n"
                f"Covered countries: {str(row0.get('PricingUnitCountriesUsed','')).strip() or '-'}"
            )

        self._refresh_all_display_prices()
        self.set_selection_defaults()

    def preload_market(self, df: pd.DataFrame) -> None:
        self.market_df = df.copy()
        allowed_isos = self.allowed_ppg_isos()

        if "ISO" in self.market_df.columns:
            self.market_df["ISO"] = (
                self.market_df["ISO"]
                .astype(str)
                .replace("nan", "")
                .str.strip()
                .str.upper()
            )

        if allowed_isos and "ISO" in self.market_df.columns:
            self.market_df = self.market_df[self.market_df["ISO"].isin(allowed_isos)].copy()

        # Competitor data is indexed by ISO, not by display country name.
        # This avoids mismatches such as "USA" versus "United States".
        self.competitors_by_country = {}
        if self.market_df.empty:
            return

        market_with_iso = self.market_df[
            self.market_df["ISO"].astype(str).str.strip().ne("")
        ].copy()

        for iso_code, cdf in market_with_iso.groupby("ISO", sort=True):
            iso_code = str(iso_code).strip().upper()
            cdf = cdf[pd.to_numeric(cdf["Days"], errors="coerce").le(self.max_days)].copy()
            pts = []
            for _, row in cdf.iterrows():
                prices = {
                    currency: self._price_for_currency_from_row(row, currency)
                    for currency in CURRENCIES
                }
                active_currency = self.normalize_current_currency()
                pts.append({
                    "provider": str(row.get("Provider", "")).strip() or "Market",
                    "x": float(row["Days"]),
                    "price_by_currency": prices,
                    "y": float(prices[active_currency]),
                    "gb": float(row["GB"]) if pd.notna(row["GB"]) else None,
                    "plan": str(row.get("Plan", "")).strip(),
                    "days": float(row["Days"]) if pd.notna(row["Days"]) else None,
                    "country": str(row.get("Country", "")).strip(),
                    "iso": iso_code,
                })
            self.competitors_by_country[iso_code] = pts

    def preload_last_exported_promos(
        self,
        data: list[dict[str, Any]],
        currency: str | None = None,
    ) -> None:
        currencies = (normalize_currency(currency),) if currency else CURRENCIES

        for target_currency in currencies:
            current_store = self._promo_store_for(target_currency)
            loaded_store = self._loaded_promo_store_for(target_currency)
            current_store.clear()
            loaded_store.clear()

            for item in data or []:
                promo_scope_key = str(item.get("PromoScopeKey", "")).strip()
                promo_code = str(item.get("PromoCode", "")).strip()

                if not promo_scope_key or not promo_code:
                    continue

                promo = {
                    "promo_scope_key": promo_scope_key,
                    "promo_code": promo_code,
                    "promo_type": str(item.get("PromoType", "")).strip().lower(),
                    "promo_value": float(item.get("PromoValue", 0) or 0),
                    "promo_currency": "",
                    "promo_label": str(item.get("PromoLabel", "")).strip() or promo_code,
                }

                current_store[promo_scope_key] = promo
                loaded_store[promo_scope_key] = dict(promo)

        for q in self.row_index.values():
            self._apply_point_display(q)


    def preload_sales_volumes(self, df: pd.DataFrame) -> None:
        self.sales_by_scope = {}

        if df.empty:
            return

        if "UnitsSoldLastMonth" not in df.columns or "Price" not in df.columns:
            return

        df = add_currency_price_columns(
            df,
            currency_hint=DEFAULT_CURRENCY,
            eur_to_usd=self.eur_to_usd,
            fill_missing_with_conversion=True,
        )

        for _, row in df.iterrows():
            scope_key = build_sku_scope_key(
                row.get("PricingUnitIdUsed", ""),
                row.get("Plan", row.get("Package", "")),
                row.get("Days", None),
            )

            units = pd.to_numeric(row.get("UnitsSoldLastMonth", 0), errors="coerce")
            old_price = pd.to_numeric(row.get("Price", 0), errors="coerce")

            if scope_key and pd.notna(units) and pd.notna(old_price):
                if scope_key not in self.sales_by_scope:
                    self.sales_by_scope[scope_key] = {
                        "units": 0.0,
                        "old_revenue": 0.0,
                        "old_revenue_by_currency": {c: 0.0 for c in CURRENCIES},
                    }

                self.sales_by_scope[scope_key]["units"] += float(units)
                for currency in CURRENCIES:
                    price = pd.to_numeric(row.get(currency_price_column(currency), np.nan), errors="coerce")
                    if pd.isna(price):
                        price = convert_price(old_price, DEFAULT_CURRENCY, currency, self.eur_to_usd)
                    self.sales_by_scope[scope_key]["old_revenue_by_currency"][currency] += float(units) * float(price)
                self.sales_by_scope[scope_key]["old_revenue"] = self.sales_by_scope[scope_key]["old_revenue_by_currency"]["USD"]

    
    def _scope_price_for_impact(self, scope_key: str) -> float | None:
        scope_key = str(scope_key).strip()
        row_ids = self.scope_to_row_ids.get(scope_key, [])

        for rid in row_ids:
            p = self.row_index.get(str(rid))
            if p is None:
                continue

            base_price = float(self.working_prices.get(str(rid), p.get("working_y", p.get("base_y", 0.0))))

            promo = self._promo_for_point(p, self.active_currency)
            if promo:
                final_price = calculate_promo_price(
                    base_price,
                    str(promo.get("promo_type", "")),
                    float(promo.get("promo_value", 0) or 0),
                )
            else:
                final_price = base_price

            return final_price

        return None
    
    def pricing_unit_country_codes(self, country_text: str) -> list[str]:
        text = str(country_text).strip().upper()
        text = text.replace("[", "").replace("]", "")
        text = text.replace('"', "").replace("'", "")
        for sep in [";", "/", "|"]:
            text = text.replace(sep, ",")
        if "," not in text and "-" in text:
            text = text.replace("-", ",")

        return [
            part.strip()
            for part in text.split(",")
            if part.strip()
        ]
    

    def calculate_cost_floor(
        self,
        point: dict[str, Any],
        country_key: str,
        currency: str | None = None,
        price_override: float | None = None,
    ) -> float:
        gb = point.get("gb") or 0.0
        price = price_override if price_override is not None else point.get("y") or point.get("working_y") or 0.0

        try:
            gb = float(gb)
        except Exception:
            gb = 0.0

        try:
            price = float(price)
        except Exception:
            price = 0.0

        country_text = str(country_key).strip().upper()

        # PricingUnitCountriesUsed may contain multiple countries.
        # Support common separators: comma, semicolon, slash, pipe.
        for sep in [";", "/", "|"]:
            country_text = country_text.replace(sep, ",")

        country_codes = self.pricing_unit_country_codes(country_key)

        ws_cost_per_gb_eur = max(
            [self.ppg_cost_by_iso.get(str(code).strip().upper(), 0.0) for code in country_codes]
            or [0.0]
        )
        currency = normalize_currency(currency or self.active_currency)
        ws_cost_per_gb = cost_per_gb_from_eur(ws_cost_per_gb_eur, currency, self.cost_eur_to_usd)

        cost_gb = gb * UTILIZATION_OF_GB_IN_PRACTICE * ws_cost_per_gb

        fee = ipg_fee(price, currency=currency, eur_to_usd=self.cost_eur_to_usd)

        return cost_gb + fee + HT_REV_SHARE * price/(1 + VAT)

    def _floor_country_key_for_point(self, point: dict[str, Any]) -> str:
        covered_countries = str(point.get("pricing_unit_countries", "")).strip()
        fallback_country = str(point.get("iso", "")).strip() or str(point.get("iso3", "")).strip()
        return covered_countries or fallback_country

    def _is_region_point(self, point: dict[str, Any]) -> bool:
        return str(point.get("pricing_source", "")).strip().lower() == "region_max"

    def _regional_binding_floor(self, point: dict[str, Any], currency: str) -> float:
        """Return the floor used by the former regional eligibility guardrail.

        The old generator compared a regional anchor price with every locked
        member country's CostFloor for the same Provider + Plan + Days. The
        binding regional floor is therefore the maximum of those country floors.
        It is deliberately based on the current country rows, not a new regional
        wholesale-cost calculation.
        """
        members = set(self.pricing_unit_country_codes(point.get("pricing_unit_countries", "")))
        key = (
            str(point.get("provider", "HT")).strip(),
            str(point.get("plan", "")).strip(),
            float(point.get("days") or 0.0),
        )
        candidates = self.country_floor_points_by_key.get(key, [])
        floors: list[float] = []
        for candidate in candidates:
            iso = str(candidate.get("iso", "")).strip().upper()
            if members and iso not in members:
                continue
            country_price = self.round_regular_price(
                self._working_price_for_currency(candidate, currency)
            )
            country_final = self._final_price_for_currency(
                candidate, currency, country_price
            )
            floors.append(
                float(
                    self.calculate_cost_floor(
                        candidate,
                        self._floor_country_key_for_point(candidate),
                        currency=currency,
                        price_override=country_final,
                    )
                )
            )
        if floors:
            return max(floors)

        # Defensive fallback for malformed legacy data.
        return float(
            self.calculate_cost_floor(
                point,
                self._floor_country_key_for_point(point),
                currency=currency,
                price_override=self._final_price_for_currency(
                    point,
                    currency,
                    self._working_price_for_currency(point, currency),
                ),
            )
        )

    def _refresh_point_floor_status(self, point: dict[str, Any]) -> None:
        country_key = self._floor_country_key_for_point(point)
        floors: dict[str, float] = {}
        final_prices: dict[str, float] = {}
        below: dict[str, bool] = {}
        is_region = self._is_region_point(point)

        for currency in CURRENCIES:
            working_price = self.round_regular_price(self._working_price_for_currency(point, currency))
            final_price = self._final_price_for_currency(point, currency, working_price)
            if is_region:
                floor = self._regional_binding_floor(point, currency)
            else:
                floor = self.calculate_cost_floor(
                    point,
                    country_key,
                    currency=currency,
                    price_override=final_price,
                )
            floors[currency] = float(floor)
            final_prices[currency] = float(final_price)
            below[currency] = bool(final_price < floor - 1e-9)

        active_currency = self.normalize_current_currency()
        point["cost_floor_by_currency"] = floors
        point["final_price_by_currency"] = final_prices
        point["below_cost_floor_by_currency"] = below
        point["active_currency"] = active_currency
        point["cost_floor"] = floors.get(active_currency)
        point["is_below_cost_floor"] = bool(below.get(active_currency, False))



    def selected_pricing_unit_id(self) -> str | None:
        p = self.selected_point_info()
        if p:
            return str(p.get("pricing_unit_id", "")).strip() or None

        points = self.current_points()
        if points:
            return str(points[0].get("pricing_unit_id", "")).strip() or None

        return None


    def scope_keys_for_selected_pricing_unit(self) -> list[str]:
        unit_id = self.selected_pricing_unit_id()
        if not unit_id:
            return []

        keys = set()

        for p in self.row_index.values():
            if str(p.get("pricing_unit_id", "")).strip() != unit_id:
                continue

            scope_key = str(p.get("scope_key", "")).strip()
            if scope_key:
                keys.add(scope_key)

        return sorted(keys)


    def revenue_impact_for_scope(self, scope_key: str) -> float:
        sales = self.sales_by_scope.get(scope_key)
        if not sales:
            return 0.0

        parts = str(scope_key).split("|")
        if len(parts) < 3:
            return 0.0

        unit_id, plan, _sales_days = parts[0], parts[1], parts[2]

        matching_points = [
            p for p in self.row_index.values()
            if str(p.get("pricing_unit_id", "")).strip() == unit_id
            and str(p.get("plan", "")).strip() == plan
        ]

        if not matching_points:
            return 0.0

        old_avg_price = sum(float(p.get("base_y", 0.0)) for p in matching_points) / len(matching_points)

        new_prices = []
        for p in matching_points:
            base_price = float(p.get("working_y", p.get("base_y", 0.0)))
            promo = self._promo_for_point(p, self.active_currency)

            if promo:
                base_price = calculate_promo_price(
                    base_price,
                    str(promo.get("promo_type", "")),
                    float(promo.get("promo_value", 0) or 0),
                )

            new_prices.append(base_price)

        new_avg_price = sum(new_prices) / len(new_prices)

        units = float(sales["units"])

        return (new_avg_price - old_avg_price) * units

    def revenue_impact_selected_pricing_unit(self) -> float:
        unit_id = self.selected_pricing_unit_id()
        if not unit_id:
            return 0.0

        total = 0.0

        for scope_key in self.sales_by_scope.keys():
            parts = str(scope_key).split("|")
            if len(parts) < 2:
                continue

            if parts[0] == unit_id:
                total += self.revenue_impact_for_scope(scope_key)

        return total


    def revenue_impact_total(self) -> float:
        """
        Total impact:
        sum of all SKU impacts across all pricing units.
        """
        return sum(
            self.revenue_impact_for_scope(scope_key)
            for scope_key in self.sales_by_scope.keys()
        )


    def revenue_last_month_for_scope(self, scope_key: str) -> float:
        sales = self.sales_by_scope.get(scope_key)
        if not sales:
            return 0.0

        by_currency = sales.get("old_revenue_by_currency", {})
        currency = self.normalize_current_currency()
        if currency in by_currency:
            return float(by_currency.get(currency, 0.0))
        return float(sales.get("old_revenue", 0.0))


    def revenue_last_month_selected_pricing_unit(self) -> float:
        unit_id = self.selected_pricing_unit_id()
        if not unit_id:
            return 0.0

        total = 0.0

        for scope_key, sales in self.sales_by_scope.items():
            parts = str(scope_key).split("|")
            if len(parts) < 2:
                continue

            if parts[0] == unit_id:
                total += self.revenue_last_month_for_scope(scope_key)

        return total

    def revenue_last_month_total(self) -> float:
        return sum(
            self.revenue_last_month_for_scope(scope_key)
            for scope_key in self.sales_by_scope.keys()
        )


    def revenue_projected_selected_pricing_unit(self) -> float:
        return (
            self.revenue_last_month_selected_pricing_unit()
            + self.revenue_impact_selected_pricing_unit()
        )


    def revenue_projected_total(self) -> float:
        return self.revenue_last_month_total() + self.revenue_impact_total()


    def preload_last_export(self, df: pd.DataFrame) -> None:
        if df.empty:
            for q in self.row_index.values():
                q["is_new_entry"] = True
                self._apply_point_display(q)
            return

        self.loaded_prices = {}
        self.loaded_prices_by_currency = {c: {} for c in CURRENCIES}
        self.loaded_promo_store_by_currency = {c: {} for c in CURRENCIES}
        # The consolidated saved file is authoritative for promo assignment too.
        self.promo_store_by_currency = {c: {} for c in CURRENCIES}
        matched_entry_keys: set[str] = set()

        for _, row in df.iterrows():
            scope_key = str(row.get("sku_scope_key", "")).strip()
            entry_key = build_price_row_key(
                row.get("ISO", ""),
                row.get("Plan", ""),
                row.get("Days", None),
                row.get("GB", None),
            )
            if entry_key.strip("|"):
                matched_entry_keys.add(entry_key)

            if not scope_key:
                scope_key = build_sku_scope_key(
                    row.get("PricingUnitIdUsed", ""),
                    row.get("Plan", ""),
                    row.get("Days", None),
                )

            if not scope_key:
                continue

            for currency in CURRENCIES:
                price = self._price_for_currency_from_row(row, currency)
                if pd.isna(price):
                    continue
                self.loaded_prices_by_currency.setdefault(currency, {})[scope_key] = float(price)

                for rid in self.scope_to_row_ids.get(scope_key, []):
                    q = self.row_index.get(str(rid))
                    if q is None:
                        continue

                    q.setdefault("working_prices", {})[currency] = float(price)
                    self.working_prices_by_currency.setdefault(currency, {})[str(rid)] = float(price)

            allow_below_cost = _boolish(row.get("AllowBelowCost", False))
            for rid in self.scope_to_row_ids.get(scope_key, []):
                q = self.row_index.get(str(rid))
                if q is not None:
                    q["allow_below_cost"] = allow_below_cost

            promo_code = str(row.get("PromoCode", "")).strip()
            if promo_code:
                promo_key = str(row.get("PromoScopeKey", "")).strip() or scope_key
                catalog_item = next(
                    (item for item in self.promo_catalog if str(item.get("promo_code", "")).strip() == promo_code),
                    None,
                ) or {}
                promo_type = str(row.get("PromoType", "")).strip().lower() or str(catalog_item.get("promo_type", "")).strip().lower()
                promo_value_raw = pd.to_numeric(row.get("PromoValue", None), errors="coerce")
                promo_value = float(promo_value_raw) if pd.notna(promo_value_raw) else float(catalog_item.get("promo_value", 0) or 0)
                promo_label = str(row.get("PromoLabel", "")).strip() or str(catalog_item.get("label", "")).strip() or promo_code
                promo = {
                    "promo_scope_key": promo_key,
                    "promo_code": promo_code,
                    "promo_type": promo_type,
                    "promo_value": promo_value,
                    "promo_currency": "",
                    "promo_label": promo_label,
                }

                for target_currency in CURRENCIES:
                    self._loaded_promo_store_for(target_currency)[promo_key] = dict(promo)
                    self._promo_store_for(target_currency)[promo_key] = dict(promo)

        self.loaded_prices = self._loaded_prices_for(self.active_currency)
        for q in self.row_index.values():
            entry_key = str(q.get("entry_key", "")).strip()
            q["is_new_entry"] = bool(entry_key) and entry_key not in matched_entry_keys
            self._apply_point_display(q)

    def _apply_point_display(self, point: dict[str, Any]) -> None:
        currency = self.normalize_current_currency()
        working_prices = point.setdefault("working_prices", {})
        base_prices = point.setdefault("base_prices", {})
        base_display_prices = point.setdefault("base_display_prices", {})

        if currency not in working_prices:
            source_currency = "USD" if currency == "EUR" else "EUR"
            source_price = working_prices.get(source_currency, base_prices.get(source_currency, point.get("working_y", 0.0)))
            working_prices[currency] = self.round_regular_price(
                convert_price(source_price, source_currency, currency, self.eur_to_usd)
            )

        point["base_y"] = self.round_regular_price(float(base_prices.get(currency, working_prices.get(currency, 0.0))))
        point["base_display_y"] = self.round_regular_price(float(base_display_prices.get(currency, point["base_y"])))
        point["working_y"] = self.round_regular_price(float(working_prices.get(currency, point["base_y"])))
        working_prices[currency] = float(point["working_y"])
        point["y"] = float(point["working_y"])
        promo = self._promo_for_point(point, currency)
        point["promo"] = ""
        if promo:
            point["promo"] = str(promo.get("promo_code", "")).strip()
            promo_value = self._promo_value_for_currency(promo, currency)
            point["y"] = self.round_promo_price(
            calculate_promo_price(
                float(point["working_y"]),
                str(promo.get("promo_type", "")),
                promo_value,
            )
        )
        point.setdefault("display_prices", {})[currency] = float(point["y"])
        self._refresh_point_floor_status(point)

    def _refresh_all_display_prices(self) -> None:
        currency = self.normalize_current_currency()
        for points in self.points_by_country.values():
            for p in points:
                prices = p.setdefault("working_prices", {})
                active_working = self.working_prices_by_currency.setdefault(currency, {}).get(
                    p["row_id"],
                    prices.get(currency, p.get("base_prices", {}).get(currency, p.get("base_y", 0.0))),
                )
                prices[currency] = float(active_working)
                self._apply_point_display(p)
        self.working_prices = self._working_prices_for(currency)
        self.loaded_prices = self._loaded_prices_for(currency)

    def current_points(self) -> list[dict[str, Any]]:
        if not self.selected_country:
            return []

        points = self.points_by_country.get(str(self.selected_country), [])

        for p in points:
            sales = self.sales_by_scope.get(str(p.get("scope_key", "")), {})
            p["last_month_revenue"] = self.revenue_last_month_for_scope(str(p.get("scope_key", "")))

            if "cost_floor_by_currency" not in p:
                self._refresh_point_floor_status(p)
            self._attach_recommendation_state(p)

        return points

    def current_competitors(self) -> list[dict[str, Any]]:
        if not self.selected_country:
            return []

        currency = self.normalize_current_currency()
        selected_name = str(self.selected_country).strip()
        iso_code = str(self.country_iso_map.get(selected_name, "")).strip().upper()

        if not iso_code:
            points = []
        else:
            points = self.competitors_by_country.get(iso_code, [])

        for point in points:
            prices = point.get("price_by_currency", {})
            if currency in prices and pd.notna(prices[currency]):
                point["y"] = float(prices[currency])
        return points

    def country_info(self) -> str:
        if not self.selected_country:
            return "No country loaded"
        base = self.country_info_map.get(str(self.selected_country), "No country loaded")
        mode = "Edit active currency only" if self.is_dual_currency_mode() else "Edit USD/EUR together"
        points = self.points_by_country.get(str(self.selected_country), [])
        new_count = sum(1 for p in points if p.get("is_new_entry"))
        if not points or new_count == 0:
            entry_status = "Existing saved/exported prices"
        elif new_count == len(points):
            entry_status = "NEW - no previous saved/exported prices"
        else:
            entry_status = f"PARTLY NEW - {new_count}/{len(points)} price points were not previously saved/exported"
        return (
            f"{base}\nEntry status: {entry_status}\n"
            f"Currency: {self.normalize_current_currency()} | Mode: {mode} | "
            f"Pricing EUR/USD: {self.eur_to_usd:.4f} | "
            f"Official cost EUR/USD: {self.cost_eur_to_usd:.4f} "
            f"({self.cost_eur_to_usd_source} {self.cost_eur_to_usd_date}, {self.cost_eur_to_usd_status})"
        )

    def selected_point_info(self) -> dict[str, Any] | None:
        if not self.selected_row_id:
            return None
        return self.row_index.get(str(self.selected_row_id))

    def set_scope_prices(self, row_id: str, prices_by_currency: dict[str, float]) -> None:
        row_id = str(row_id)
        p = self.row_index.get(row_id)
        if p is None:
            return

        updates = {
            normalize_currency(currency): max(self.round_regular_price(price), 0.0)
            for currency, price in prices_by_currency.items()
        }
        if not updates:
            return

        scope_key = str(p.get("scope_key", "")).strip()
        active_currency = self.normalize_current_currency()

        for rid in self.scope_to_row_ids.get(scope_key, [row_id]):
            q = self.row_index.get(str(rid))
            if q is None:
                continue

            prices = q.setdefault("working_prices", {})
            for currency, value in updates.items():
                prices[currency] = value
                self.working_prices_by_currency.setdefault(currency, {})[str(rid)] = value

            q["working_y"] = float(prices.get(active_currency, q.get("working_y", 0.0)))
            self._apply_point_display(q)
        self.working_prices = self._working_prices_for(active_currency)

    def set_scope_price(self, row_id: str, new_price: float) -> None:
        v = max(self.round_regular_price(new_price), 0.0)
        active_currency = self.normalize_current_currency()

        if self.is_dual_currency_mode():
            self.set_scope_prices(row_id, {active_currency: v})
            return

        if active_currency == "USD":
            usd_value = v
            eur_value = self.round_regular_price(convert_price(v, "USD", "EUR", self.eur_to_usd))
        else:
            eur_value = v
            usd_value = self.round_regular_price(convert_price(v, "EUR", "USD", self.eur_to_usd))
        self.set_scope_prices(row_id, {"USD": usd_value, "EUR": eur_value})

    def _base_price_for_currency(self, point: dict[str, Any], currency: str) -> float:
        currency = normalize_currency(currency)
        base_prices = point.get("base_prices", {}) or {}
        price = base_prices.get(currency)
        if pd.notna(price):
            return float(price)

        other_currency = "EUR" if currency == "USD" else "USD"
        other_price = base_prices.get(other_currency)
        if pd.notna(other_price):
            return float(convert_price(other_price, other_currency, currency, self.eur_to_usd))

        return float(point.get("base_y", 0.0))

    def _loaded_or_base_price_for_currency(self, point: dict[str, Any], currency: str) -> float:
        currency = normalize_currency(currency)
        scope_key = str(point.get("scope_key", "")).strip()
        loaded_prices = self._loaded_prices_for(currency)
        if scope_key in loaded_prices:
            return float(loaded_prices[scope_key])
        return self._base_price_for_currency(point, currency)

    def _base_price_updates_for_point(self, point: dict[str, Any], currencies: tuple[str, ...]) -> dict[str, float]:
        return {currency: self._base_price_for_currency(point, currency) for currency in currencies}

    def _loaded_price_updates_for_point(self, point: dict[str, Any], currencies: tuple[str, ...]) -> dict[str, float]:
        return {currency: self._loaded_or_base_price_for_currency(point, currency) for currency in currencies}

    def _same_plan_points(self, row_id: str) -> list[dict[str, Any]]:
        p = self.row_index.get(str(row_id))
        if p is None or not self.selected_country:
            return []
        same = [x for x in self.current_points() if x["plan"] == p["plan"]]
        same.sort(key=lambda x: (x["x"], x["gb"] if x["gb"] is not None else -1))
        return same

    def inflate_curve_at_point(self, center_index: int, new_price: float) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        same = self._same_plan_points(center["row_id"])
        local_index = next((i for i, p in enumerate(same) if p["row_id"] == center["row_id"]), None)
        if local_index is None:
            self.set_scope_price(center["row_id"], new_price)
            return

        delta = float(new_price) - float(center["working_y"])

        # Whole-curve bulge: ends move least, middle moves most.
        n = len(same)
        for idx, p in enumerate(same):
            t = idx / max(n - 1, 1)
            arch = 4.0 * t * (1.0 - t)  # 0 at ends, 1 at middle
            # preserve sign of drag and apply smoothly across entire curve
            self.set_scope_price(p["row_id"], float(p["working_y"]) + delta * arch)

    def shift_curve_absolute(self, center_index: int, delta_amount: float) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        same = self._same_plan_points(center["row_id"])
        for p in same:
            self.set_scope_price(p["row_id"], float(p["working_y"]) + delta_amount)

    def shift_curve_percent(self, center_index: int, pct: float) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        same = self._same_plan_points(center["row_id"])
        factor = 1.0 + pct
        for p in same:
            self.set_scope_price(p["row_id"], float(p["working_y"]) * factor)

    def rotate_curve_legacy(self, center_index: int, new_price: float, side: str = "both") -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        same = self._same_plan_points(center["row_id"])
        local_index = next((i for i, p in enumerate(same) if p["row_id"] == center["row_id"]), None)
        if local_index is None:
            self.set_scope_price(center["row_id"], new_price)
            return

        pivot_y = float(center["working_y"])
        delta = float(new_price) - pivot_y
        self.set_scope_price(center["row_id"], pivot_y)  # keep pivot fixed while rotating nearby

        for idx, p in enumerate(same):
            if idx == local_index:
                continue
            if idx < local_index and side not in {"left", "both"}:
                continue
            if idx > local_index and side not in {"right", "both"}:
                continue
                
            if idx < local_index:
                dist = local_index - idx
                span = max(local_index, 1)
                weight = (dist / span) ** 2
                signed = -delta * weight * 0.05
            else:
                dist = idx - local_index
                span = max(len(same) - local_index - 1, 1)
                weight = (dist / span) ** 2
                signed = delta * weight * 0.05

            self.set_scope_price(p["row_id"], float(p["working_y"]) + signed)

    def nudge_neighbors(self, center_index: int, new_price: float, strength: float = 0.32) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        same = self._same_plan_points(center["row_id"])
        local_index = next((i for i, p in enumerate(same) if p["row_id"] == center["row_id"]), None)
        if local_index is None:
            self.set_scope_price(center["row_id"], new_price)
            return
        delta = float(new_price) - float(center["working_y"])
        self.set_scope_price(center["row_id"], new_price)
        for offset, falloff in [(-2, 0.12), (-1, strength), (1, strength), (2, 0.12)]:
            idx = local_index + offset
            if 0 <= idx < len(same):
                p = same[idx]
                self.set_scope_price(p["row_id"], float(p["working_y"]) + delta * falloff)

    def set_brush_start(self) -> None:
        if self.selected_row_id:
            self.brush_start_row_id = str(self.selected_row_id)

    def set_brush_end(self) -> None:
        if self.selected_row_id:
            self.brush_end_row_id = str(self.selected_row_id)

    def clear_brush(self) -> None:
        self.brush_start_row_id = None
        self.brush_end_row_id = None

    def brush_summary(self) -> str:
        if not self.brush_start_row_id or not self.brush_end_row_id:
            return "Brush range: not set"
        a = self.row_index.get(self.brush_start_row_id)
        b = self.row_index.get(self.brush_end_row_id)
        if not a or not b:
            return "Brush range: not set"
        return f"Brush range: {a['plan']} {a['days']}d -> {b['days']}d"

    def apply_brush_between(self, center_index: int, new_price: float, strength: float = 1.0) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return
        center = points[center_index]
        if not self.brush_start_row_id or not self.brush_end_row_id:
            self.nudge_neighbors(center_index, new_price)
            return

        start = self.row_index.get(self.brush_start_row_id)
        end = self.row_index.get(self.brush_end_row_id)
        if start is None or end is None:
            self.nudge_neighbors(center_index, new_price)
            return
        if start["plan"] != center["plan"] or end["plan"] != center["plan"]:
            self.nudge_neighbors(center_index, new_price)
            return

        same = self._same_plan_points(center["row_id"])
        idx_map = {p["row_id"]: i for i, p in enumerate(same)}
        if start["row_id"] not in idx_map or end["row_id"] not in idx_map or center["row_id"] not in idx_map:
            self.nudge_neighbors(center_index, new_price)
            return

        i0 = min(idx_map[start["row_id"]], idx_map[end["row_id"]])
        i1 = max(idx_map[start["row_id"]], idx_map[end["row_id"]])
        ic = idx_map[center["row_id"]]
        if not (i0 <= ic <= i1) or i1 == i0:
            self.nudge_neighbors(center_index, new_price)
            return

        center_delta = float(new_price) - float(center["working_y"])

        # fixed anchors, smooth falloff to anchors, strongest at dragged point
        for idx in range(i0, i1 + 1):
            p = same[idx]
            if idx == i0 or idx == i1:
                continue
            dist = abs(idx - ic)
            span = max(ic - i0, i1 - ic, 1)
            influence = max(0.0, 1.0 - dist / span)
            influence = 0.5 - 0.5 * math.cos(math.pi * influence)
            anchor_falloff = min((idx - i0) / max(ic - i0, 1) if idx <= ic else (i1 - idx) / max(i1 - ic, 1), 1.0)
            total = influence * anchor_falloff * strength
            self.set_scope_price(p["row_id"], float(p["working_y"]) + center_delta * total)

    def apply_concave_curve(self, center_index: int, new_price: float) -> None:
        points = self.current_points()
        if not points or center_index < 0 or center_index >= len(points):
            return

        center = points[center_index]
        same = self._same_plan_points(center["row_id"])

        if len(same) < 2:
            self.set_scope_price(center["row_id"], new_price)
            return

        same.sort(key=lambda p: float(p["x"]))

        first = same[0]
        last = same[-1]

        min_day = float(first["x"])
        max_day = float(last["x"])

        if max_day == min_day:
            return

        first_price = float(first["working_y"])
        last_price = float(last["working_y"])

        # How strong the curve is. Increase 0.18 to 0.25 if you want more bend.
        curve_strength = 0.18

        for p in same:
            day = float(p["x"])
            t = (day - min_day) / (max_day - min_day)

            # Smooth concave-down curve between first and last price
            linear_price = first_price + t * (last_price - first_price)

            # Middle points get lifted most, endpoints stay fixed
            bend = 4.0 * t * (1.0 - t)

            price_span = abs(last_price - first_price)
            if price_span < 1:
                price_span = max(first_price, last_price, 1.0)

            new_y = linear_price + curve_strength * price_span * bend

            self.set_scope_price(p["row_id"], new_y)
        
        
    def reload_selected_plan_from_loaded(self) -> None:
        p = self.selected_point_info()
        if p is None:
            return

        currencies = self._currencies_for_current_edit()
        selected_plan = str(p.get("plan", "")).strip()
        unit_id = str(p.get("pricing_unit_id", "")).strip()

        for q in self.row_index.values():
            if str(q.get("pricing_unit_id", "")).strip() != unit_id:
                continue
            if str(q.get("plan", "")).strip() != selected_plan:
                continue

            rid = str(q["row_id"])
            self.set_scope_prices(rid, self._loaded_price_updates_for_point(q, currencies))

            promo_key = str(q.get("promo_scope_key", "")).strip()
            for currency in currencies:
                loaded_store = self._loaded_promo_store_for(currency)
                current_store = self._promo_store_for(currency)
                if promo_key in loaded_store:
                    current_store[promo_key] = dict(loaded_store[promo_key])
                else:
                    current_store.pop(promo_key, None)

            self._clear_applied_recommendations_for_point(q, currencies)
            self._apply_point_display(q)


    def reload_pricing_unit_from_loaded(self) -> None:
        p = self.selected_point_info()
        if p is None:
            return

        currencies = self._currencies_for_current_edit()
        unit_id = str(p.get("pricing_unit_id", "")).strip()
        if not unit_id:
            return

        for q in self.row_index.values():
            if str(q.get("pricing_unit_id", "")).strip() != unit_id:
                continue

            rid = str(q["row_id"])
            self.set_scope_prices(rid, self._loaded_price_updates_for_point(q, currencies))

            promo_key = str(q.get("promo_scope_key", "")).strip()
            for currency in currencies:
                loaded_store = self._loaded_promo_store_for(currency)
                current_store = self._promo_store_for(currency)
                if promo_key in loaded_store:
                    current_store[promo_key] = dict(loaded_store[promo_key])
                else:
                    current_store.pop(promo_key, None)

            self._clear_applied_recommendations_for_point(q, currencies)
            self._apply_point_display(q)

    def promo_candidates_for_selected(self) -> list[dict[str, Any]]:
        p = self.selected_point_info()
        if p is None:
            return []
        base_price = float(p["working_y"])
        out = []
        for promo in self.promo_catalog:
            promo_value = self._promo_value_for_currency(promo)
            final_price = self.round_promo_price(
                calculate_promo_price(base_price, promo["promo_type"], promo_value)
            )
            if final_price <= base_price:
                out.append({
                    "promo_scope_key": str(p["promo_scope_key"]),
                    "promo_code": promo["promo_code"],
                    "promo_type": promo["promo_type"],
                    "promo_value": float(promo["promo_value"]),
                    "promo_currency": "",
                    "display_promo_value": float(promo_value),
                    "promo_label": promo["label"],
                    "final_price_after_promo": final_price,
                })
        out.sort(key=lambda x: x["final_price_after_promo"])
        return out

    def promo_candidate_markers(self) -> list[dict[str, Any]]:
        p = self.selected_point_info()
        if p is None:
            return []

        base_x = float(p["x"])
        markers = []

        for c in self.promo_candidates_for_selected()[:8]:
            markers.append({
                "x": base_x,
                "y": float(c["final_price_after_promo"]),
                "promo_code": c["promo_code"],
                "label": c["promo_label"],
                "is_remove": False,
            })

        if self.has_promo_for_selected():
            markers.append({
                "x": base_x,
                "y": float(p["y"]) + 2.0,
                "promo_code": "__REMOVE_PROMO__",
                "label": "Remove",
                "is_remove": True,
            })

        return markers

    def available_promo_days_for_selected(self) -> list[float]:
        p = self.selected_point_info()
        if p is None:
            return []

        unit_id = str(p.get("pricing_unit_id", "")).strip()
        plan = str(p.get("plan", "")).strip()
        days: set[float] = set()
        for q in self.row_index.values():
            if str(q.get("pricing_unit_id", "")).strip() != unit_id:
                continue
            if str(q.get("plan", "")).strip() != plan:
                continue
            try:
                day = float(q.get("days"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(day):
                days.add(day)
        return sorted(days)

    def _matching_promo_range_points(
        self,
        start_day: float,
        end_day: float,
    ) -> list[dict[str, Any]]:
        p = self.selected_point_info()
        if p is None:
            return []

        low, high = sorted((float(start_day), float(end_day)))
        unit_id = str(p.get("pricing_unit_id", "")).strip()
        plan = str(p.get("plan", "")).strip()
        matches = []
        for q in self.row_index.values():
            if str(q.get("pricing_unit_id", "")).strip() != unit_id:
                continue
            if str(q.get("plan", "")).strip() != plan:
                continue
            try:
                day = float(q.get("days"))
            except (TypeError, ValueError):
                continue
            if low <= day <= high:
                matches.append(q)
        return matches

    def _apply_promo_template_to_points(
        self,
        promo_template: dict[str, Any],
        points: list[dict[str, Any]],
    ) -> int:
        currencies = self._currencies_for_promo_edit()
        affected_scope_keys: set[str] = set()

        for q in points:
            promo_key = str(q.get("promo_scope_key", "")).strip()
            scope_key = str(q.get("scope_key", "")).strip()
            if not promo_key:
                continue
            affected_scope_keys.add(scope_key or promo_key)
            for currency in currencies:
                scoped = dict(promo_template)
                scoped["promo_scope_key"] = promo_key
                self._promo_store_for(currency)[promo_key] = scoped

        refreshed: set[str] = set()
        for q in points:
            rid = str(q.get("row_id", ""))
            if rid and rid not in refreshed:
                self._apply_point_display(q)
                refreshed.add(rid)

        return len(affected_scope_keys)

    def assign_promo_to_selected(self, promo_code: str) -> int:
        p = self.selected_point_info()
        if p is None:
            return 0
        match = next(
            (c for c in self.promo_candidates_for_selected() if c["promo_code"] == promo_code),
            None,
        )
        if not match:
            return 0

        scope_key = str(p["scope_key"])
        points = [
            self.row_index[rid]
            for rid in self.scope_to_row_ids.get(scope_key, [])
            if rid in self.row_index
        ]
        return self._apply_promo_template_to_points(match, points)

    def assign_promo_to_range(
        self,
        promo_code: str,
        start_day: float,
        end_day: float,
    ) -> int:
        match = next(
            (c for c in self.promo_candidates_for_selected() if c["promo_code"] == promo_code),
            None,
        )
        if not match:
            return 0
        return self._apply_promo_template_to_points(
            match,
            self._matching_promo_range_points(start_day, end_day),
        )

    def _remove_promos_from_points(self, points: list[dict[str, Any]]) -> int:
        currencies = self._currencies_for_promo_edit()
        affected_scope_keys: set[str] = set()
        changed = False

        for q in points:
            promo_key = str(q.get("promo_scope_key", "")).strip()
            scope_key = str(q.get("scope_key", "")).strip()
            if not promo_key:
                continue
            removed_here = False
            for currency in currencies:
                store = self._promo_store_for(currency)
                if promo_key in store:
                    store.pop(promo_key, None)
                    removed_here = True
                    changed = True
            if removed_here:
                affected_scope_keys.add(scope_key or promo_key)

        if changed:
            for q in points:
                self._apply_point_display(q)

        return len(affected_scope_keys)

    def remove_selected_promo(self) -> int:
        p = self.selected_point_info()
        if p is None:
            return 0
        scope_key = str(p["scope_key"])
        points = [
            self.row_index[rid]
            for rid in self.scope_to_row_ids.get(scope_key, [])
            if rid in self.row_index
        ]
        return self._remove_promos_from_points(points)

    def remove_promo_from_range(self, start_day: float, end_day: float) -> int:
        return self._remove_promos_from_points(
            self._matching_promo_range_points(start_day, end_day)
        )

    def workbench_scope_points(
        self,
        pricing_unit_id: str,
        plan: str,
        days: float,
        gb: float | None,
    ) -> list[dict[str, Any]]:
        """Return all country rows belonging to one Excel workbench pricing scope."""
        unit = str(pricing_unit_id).strip()
        plan_text = str(plan).strip()
        try:
            day_value = float(days)
        except (TypeError, ValueError):
            return []

        scope_key = build_sku_scope_key(unit, plan_text, day_value)
        candidate_ids = self.scope_to_row_ids.get(scope_key, [])
        matches: list[dict[str, Any]] = []
        for row_id in candidate_ids:
            point = self.row_index.get(str(row_id))
            if point is None:
                continue
            point_gb = point.get("gb")
            if gb is None:
                if point_gb is not None:
                    continue
            else:
                try:
                    if point_gb is None or not math.isclose(float(point_gb), float(gb), abs_tol=1e-9):
                        continue
                except (TypeError, ValueError):
                    continue
            matches.append(point)
        return matches

    def set_workbench_scope_prices(
        self,
        pricing_unit_id: str,
        plan: str,
        days: float,
        gb: float | None,
        *,
        price_eur: float,
        price_usd: float,
    ) -> int:
        """Set explicit independent EUR and USD list prices for a whole pricing unit scope."""
        matches = self.workbench_scope_points(pricing_unit_id, plan, days, gb)
        if not matches:
            return 0
        self.set_scope_prices(
            str(matches[0]["row_id"]),
            {"EUR": float(price_eur), "USD": float(price_usd)},
        )
        return len(matches)

    def set_workbench_scope_promo(
        self,
        pricing_unit_id: str,
        plan: str,
        days: float,
        gb: float | None,
        *,
        promo_code: str,
    ) -> int:
        """Assign one approved promo to both EUR and USD for a whole workbench scope."""
        matches = self.workbench_scope_points(pricing_unit_id, plan, days, gb)
        if not matches:
            return 0

        promo_code = str(promo_code or "").strip()
        if not promo_code:
            changed = False
            for point in matches:
                promo_key = str(point.get("promo_scope_key", "")).strip()
                if not promo_key:
                    continue
                for currency in CURRENCIES:
                    store = self._promo_store_for(currency)
                    if promo_key in store:
                        store.pop(promo_key, None)
                        changed = True
            if changed:
                for point in matches:
                    self._apply_point_display(point)
            return len(matches)

        catalog_item = next(
            (
                item for item in self.promo_catalog
                if str(item.get("promo_code", "")).strip() == promo_code
            ),
            None,
        )
        if catalog_item is None:
            raise ValueError(f"Unknown promo code in pricing workbench: {promo_code}")

        template = {
            "promo_code": promo_code,
            "promo_type": str(catalog_item.get("promo_type", "")).strip().lower(),
            "promo_value": float(catalog_item.get("promo_value", 0) or 0),
            "promo_currency": "",
            "promo_label": str(catalog_item.get("label", "")).strip() or promo_code,
        }
        for point in matches:
            promo_key = str(point.get("promo_scope_key", "")).strip()
            if not promo_key:
                continue
            for currency in CURRENCIES:
                scoped = dict(template)
                scoped["promo_scope_key"] = promo_key
                self._promo_store_for(currency)[promo_key] = scoped

        for point in matches:
            self._apply_point_display(point)
        return len(matches)

    def round_regular_price(self, price: float) -> float:
        return round(float(price) * 20) / 20


    def round_promo_price(self, price: float) -> float:
        return math.floor(float(price) * 20) / 20

    def _working_price_for_currency(self, point: dict[str, Any], currency: str) -> float:
        currency = normalize_currency(currency)
        prices = point.setdefault("working_prices", {})

        if currency in prices and pd.notna(prices[currency]):
            return self.round_regular_price(float(prices[currency]))

        source_currency = "USD" if currency == "EUR" else "EUR"
        source = prices.get(source_currency, point.get("base_prices", {}).get(source_currency, point.get("working_y", 0.0)))
        converted = self.round_regular_price(convert_price(source, source_currency, currency, self.eur_to_usd))
        prices[currency] = converted
        self.working_prices_by_currency.setdefault(currency, {})[str(point.get("row_id", ""))] = converted
        return converted

    def _final_price_for_currency(self, point: dict[str, Any], currency: str, working_price: float) -> float:
        promo = self._promo_for_point(point, currency)
        if not promo:
            return self.round_regular_price(float(working_price))

        return self.round_promo_price(
            calculate_promo_price(
                float(working_price),
                str(promo.get("promo_type", "")),
                self._promo_value_for_currency(promo, currency),
            )
        )

    def export_prices_dataframe(self) -> pd.DataFrame:
        """Return one consolidated pricing snapshot containing EUR and USD."""
        rows = []
        self._refresh_all_display_prices()

        for point in self.row_index.values():
            price_usd = self.round_regular_price(self._working_price_for_currency(point, "USD"))
            price_eur = self.round_regular_price(self._working_price_for_currency(point, "EUR"))
            final_usd = self._final_price_for_currency(point, "USD", price_usd)
            final_eur = self._final_price_for_currency(point, "EUR", price_eur)

            self._refresh_point_floor_status(point)
            below_by_currency = point.get("below_cost_floor_by_currency", {}) or {}
            floors_by_currency = point.get("cost_floor_by_currency", {}) or {}
            allow_below_cost = bool(point.get("allow_below_cost", False))

            promo = self._promo_for_point(point, "EUR") or self._promo_for_point(point, "USD")
            promo_value = float(promo.get("promo_value", 0) or 0) if promo else ""

            rows.append({
                "Provider": "HT",
                "ReferenceProvider": "",
                "Country": point.get("country", ""),
                "ISO": point.get("iso", ""),
                "ISO3": point.get("iso3", ""),
                "GB": point.get("gb", ""),
                "Days": point.get("days", ""),
                "Price_USD": price_usd,
                "Price_EUR": price_eur,
                "Plan": point.get("plan", ""),
                "PricingUnitIdUsed": point.get("pricing_unit_id", ""),
                "PricingSourceUsed": point.get("pricing_source", ""),
                "PricingRegionUsed": point.get("pricing_region", ""),
                "PricingUnitCountriesUsed": point.get("pricing_unit_countries", ""),
                "PromoScopeKey": point.get("promo_scope_key", ""),
                "PromoCode": promo.get("promo_code", "") if promo else "",
                "PromoType": promo.get("promo_type", "") if promo else "",
                "PromoValue": promo_value,
                "PromoLabel": promo.get("promo_label", "") if promo else "",
                "PromoBasePrice": price_eur if promo else "",
                "FinalPriceAfterPromo_EUR": final_eur,
                "FinalPriceAfterPromo_USD": final_usd,
                "CostFloor_USD": floors_by_currency.get("USD", ""),
                "CostFloor_EUR": floors_by_currency.get("EUR", ""),
                "USD_IsBelowCostFloor": bool(below_by_currency.get("USD", False)),
                "EUR_IsBelowCostFloor": bool(below_by_currency.get("EUR", False)),
                "AllowBelowCost": allow_below_cost,
            })

        return pd.DataFrame(rows)

    def export_prices_csv(self, path: str | Path, currency: str | None = None) -> None:
        """Legacy CSV writer retained for backward compatibility."""
        out = self.export_prices_dataframe()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(path, index=False)

    def export_prices_xlsx(self, path: str | Path) -> None:
        """Write the canonical editable Excel price book."""
        out = self.export_prices_dataframe()
        write_pricing_workbook(out, path, promo_catalog=self.promo_catalog)

    def export_applied_promos_json(self, path: str | Path, currency: str | None = None) -> None:
        currency = normalize_currency(currency or self.active_currency)
        rows = []
        for promo_key, promo in self._promo_store_for(currency).items():
            promo_type = str(promo.get("promo_type", "")).strip().lower()
            rows.append({
                "PromoScopeKey": promo_key,
                "PromoCode": promo.get("promo_code", ""),
                "PromoType": promo_type,
                "PromoValue": float(promo.get("promo_value", 0) or 0),
                "PromoLabel": promo.get("promo_label", ""),
            })

        Path(path).write_text(
            json.dumps(rows, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

