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

# Provider-level removal
MIN_MATCHED_OFFERS = 2
PROVIDER_RATIO_THRESHOLD = 3.0

USE_LOG_PRICE = True


# ---------------------------------
# Configuration for pricing model
# ---------------------------------

UTILIZATION_OF_GB_IN_PRACTICE = 0.8
K = 1.28
DAYS_RANGE = np.arange(1, 31)

PACKAGE_CONFIG = {'Basic': {'avg_daily': 0.37499999999999994, 'daily_std': 0.05}, 'Moderate': {'avg_daily': 1.25, 'daily_std': 0.15}, 'Large': {'avg_daily': 2.5, 'daily_std': 0.15}, 'Unlimited': {'avg_daily': 3.0, 'daily_std': 0.5}}

STRATEGY_MAP = {'balanced': {'overall': 1.0, 'plan': {'Basic': 1.0, 'Moderate': 1.0, 'Large': 1.0, 'Unlimited': 1.0}}, 'profit_max': {'overall': 1.1, 'plan': {'Basic': 1.05, 'Moderate': 1.1, 'Large': 1.12, 'Unlimited': 1.15}}, 'market_share_aggressive': {'overall': 0.95, 'plan': {'Basic': 0.95, 'Moderate': 0.9, 'Large': 0.87, 'Unlimited': 0.85}}}

CHOSEN_STRATEGY = 'balanced'

COUNTRY_SURFACE_MIN_ROWS = 10
BLEND_SURFACE_MIN_ROWS = 4

DAYS_LOG_OFFSET = 2.0
GB_LOG_OFFSET = 4.0

MIN_REL_STEP_GROWTH = 0.005
MIN_ABS_STEP_GROWTH = 0.02

CONCAVITY_DECAY_FACTOR = 1.0

PROMO_CHECK_DAYS = {15, 30, 7}
PROMO_EPSILON = 1e-09

PROMOS_PATH_DEFAULT = "inputs/promos.json"
 
# PROMO MANAGEMENT DIALERS:
GB_TOLERANCE_RATIO = 0.25
PROMO_LOW_PPG_MAX_LOWER_PCT = 0.2

# PROMOS TARGETS TWEAKS
PROMO_TARGET_COMPETITOR_RANK = 2
PROMO_TARGET_POSITION = 'below'
PROMO_TARGET_MARGIN_PCT = 0.0


# ---------------------------------
# Configuration for region price definition (paths are relative to BASE_DIR)
# ---------------------------------
BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "automation":
    BASE_DIR = BASE_DIR.parent
INPUT_REGIONS = BASE_DIR / "inputs" / "regions.yaml"
OUTPUT_NAME = "Region_prices.csv"
