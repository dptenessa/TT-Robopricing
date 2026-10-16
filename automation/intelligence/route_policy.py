"""Shared commercial exclusions; country membership is a business rule, not a tariff check."""
from pathlib import Path
from functools import lru_cache
import csv
import math

ROUTE_POLICY_VERSION = '2026-10-08-no-domestic-no-intra-eu-v1'
EU_COUNTRIES = frozenset('AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE'.split())
MIN_ORIGIN_MARKET_WEIGHT = 50.0
ACTIVE_INPUT_DIR = None

def configure_input_dir(path):
    global ACTIVE_INPUT_DIR
    ACTIVE_INPUT_DIR = str(path)
    origin_weights.cache_clear()

@lru_cache(maxsize=8)
def origin_weights(input_dir=None):
    input_dir = input_dir or ACTIVE_INPUT_DIR
    folder = Path(input_dir) if input_dir else Path(__file__).resolve().parent/'inputs'/'travel_demand'
    path = folder/'origin_market_weights.csv'
    if not path.exists():
        return {}
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            code = str(row.get('origin_iso2') or row.get('iso2') or '').strip().upper()
            raw = row.get('market_weight') or row.get('weight') or row.get('origin_weight')
            try:
                weight = float(raw)
            except (TypeError, ValueError):
                continue
            if code and math.isfinite(weight):
                result[code] = weight
    if not result:
        raise ValueError(f'No valid origin weights in {path}')
    return result

def origin_allowed(origin, input_dir=None):
    code = str(origin or '').strip().upper()
    weights = origin_weights(input_dir)
    return len(code)==2 and code.isalpha() and (not weights or weights.get(code,0)>=MIN_ORIGIN_MARKET_WEIGHT)

def exclusion_reason(origin, destination, input_dir=None):
    origin = str(origin or '').strip().upper()
    destination = str(destination or '').strip().upper()
    if len(origin)!=2 or len(destination)!=2 or not origin.isalpha() or not destination.isalpha():
        return 'invalid country code'
    if origin == destination:
        return 'domestic route'
    if origin in EU_COUNTRIES and destination in EU_COUNTRIES:
        return 'intra-EU route'
    if not origin_allowed(origin,input_dir):
        return 'origin below market-weight threshold'
    return ''

def route_allowed(origin, destination, input_dir=None):
    return not exclusion_reason(origin,destination,input_dir)
