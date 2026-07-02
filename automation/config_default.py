import numpy as np
from pathlib import Path

HT_REV_SHARE = 0.03
VAT = 0.215

# ---------------------------------
# Currency management
# ---------------------------------
DEFAULT_EUR_TO_USD = 1.10
EDITOR_DUAL_CURRENCY_DEFAULT = False
BATCH_CURRENCIES = ("USD", "EUR")

# ---------------------------------
# Configuration for outlier removal
# ---------------------------------
K_NEIGHBORS = 3
MIN_NEIGHBORS_REQUIRED = 2

# Distance weights in log-space
GB_WEIGHT = 1.0
DAYS_WEIGHT = 1.0

# Optional: prevent comparisons that are too far away
MAX_DISTANCE = 1.25

# Row-level flag
ROW_RATIO_THRESHOLD = 3.0
# Example: if local market says ~30 USD and row is 100 USD, ratio = 3.33 -> flagged

# Provider-level removal
MIN_MATCHED_OFFERS = 2
PROVIDER_RATIO_THRESHOLD = 3.0
# Remove provider if median ratio across matched offers is above this

USE_LOG_PRICE = True


# ---------------------------------
# Configuration for pricing model
# --------------------------------- 

UTILIZATION_OF_GB_IN_PRACTICE = 0.8
K = 1.28
DAYS_RANGE = np.arange(1, 31)

PACKAGE_CONFIG = {
    "Basic": {"avg_daily": 0.3 / UTILIZATION_OF_GB_IN_PRACTICE, "daily_std": 0.05},
    "Moderate": {"avg_daily": 1.0 / UTILIZATION_OF_GB_IN_PRACTICE, "daily_std": 0.15},
    "Large": {"avg_daily": 2.0 / UTILIZATION_OF_GB_IN_PRACTICE, "daily_std": 0.15},
    "Unlimited": {"avg_daily": 3.0, "daily_std": 0.5},
}

STRATEGY_MAP = {
    "profit_max": {
        "overall": 1.10,
        "plan": {"Basic": 1.05, "Moderate": 1.10, "Large": 1.12, "Unlimited": 1.15},
    },
        "balanced": {
        "overall": 1.00,
        "plan": {"Basic": 1.00, "Moderate": 1.00, "Large": 1.00, "Unlimited": 1.00},
    },
    "market_share_aggressive": {
        "overall": 0.95,
        "plan": {"Basic": 0.95, "Moderate": 0.90, "Large": 0.87, "Unlimited": 0.85},
    },
}

CHOSEN_STRATEGY  = "balanced"

VAT = .215 # Pondered VAT for Europe

COUNTRY_SURFACE_MIN_ROWS = 10
BLEND_SURFACE_MIN_ROWS = 4

DAYS_LOG_OFFSET = 2.0
GB_LOG_OFFSET = 4.0

MIN_REL_STEP_GROWTH = 0.005
MIN_ABS_STEP_GROWTH = 0.02

CONCAVITY_DECAY_FACTOR = 1.0

PROMO_CHECK_DAYS = {7, 15, 30}
PROMO_EPSILON = 1e-9

PROMOS_PATH_DEFAULT = "inputs/promos.json"

#  PROMO MANAGEMENT DIALERS:
GB_TOLERANCE_RATIO = 0.25
PROMO_LOW_PPG_MAX_LOWER_PCT = 0.20

# PROMOS TARGETS TWEAKS
# Promo targeting behavior
# 1 = cheapest competitor, 2 = second cheapest, etc.
PROMO_TARGET_COMPETITOR_RANK = 2

# "below" or "above"
PROMO_TARGET_POSITION = "below"

# Example:
# below + 0.0 => just below chosen competitor
# below + 2.0 => 2% below chosen competitor
# above + 2.0 => 2% above chosen competitor
PROMO_TARGET_MARGIN_PCT = 0.0


# ---------------------------------
# Configuration for region price definition
# ---------------------------------
BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "automation":
    BASE_DIR = BASE_DIR.parent
INPUT_REGIONS = BASE_DIR / "inputs" / "regions.yaml"
OUTPUT_NAME = "region_prices_current.csv"
