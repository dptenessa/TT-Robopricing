from __future__ import annotations

from typing import Any

try:
    from config import DEFAULT_EUR_TO_USD
except Exception:
    DEFAULT_EUR_TO_USD = 1.10

from currency_support import DEFAULT_CURRENCY, normalize_currency


IPG_FIXED_FEE_EUR = (
    0.9 * 0.2
    + 0.25 * 0.1
    + 0.00275 * 0.1
    + 0.05
    + 0.03
    + 20 * 0.01
)
IPG_VARIABLE_RATE = 0.9 * 0.005 + 0.005 + 0.02 * 0.65 + 0.01 * 0.35 + 0.002


def eur_amount_to_currency(amount_eur: Any, currency: str, eur_to_usd: float = DEFAULT_EUR_TO_USD):
    currency = normalize_currency(currency)
    rate = float(eur_to_usd or DEFAULT_EUR_TO_USD)
    if rate <= 0:
        rate = DEFAULT_EUR_TO_USD
    return amount_eur * rate if currency == "USD" else amount_eur


def cost_per_gb_from_eur(cost_per_gb_eur: Any, currency: str, eur_to_usd: float = DEFAULT_EUR_TO_USD):
    return eur_amount_to_currency(cost_per_gb_eur, currency, eur_to_usd)


def ipg_fee(price: Any, currency: str = DEFAULT_CURRENCY, eur_to_usd: float = DEFAULT_EUR_TO_USD):
    fixed_fee = eur_amount_to_currency(IPG_FIXED_FEE_EUR, currency, eur_to_usd)
    return fixed_fee + IPG_VARIABLE_RATE * price
