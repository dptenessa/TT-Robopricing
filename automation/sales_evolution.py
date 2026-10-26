"""Origin-market sales diagnosis, rendered from locally aggregated quantities."""
import json
from pathlib import Path


def render_sales_evolution(payload):
    try:
        from sales_diagnosis_prices import build_price_evidence
    except ImportError:
        from automation.sales_diagnosis_prices import build_price_evidence
    data = dict(payload)
    if 'pricing' not in data:
        data['pricing'] = build_price_evidence(data)
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    template = Path(__file__).with_name('sales_diagnosis.html').read_text(encoding='utf-8')
    return template.replace('__SALES_DATA__', encoded)
