"""Single portfolio allocation source; invalid settings stop execution."""
import json
import math
from pathlib import Path

PORTFOLIO = json.loads(Path(__file__).with_name('portfolio.json').read_text(encoding='utf-8'))
ratios = [PORTFOLIO['baa_target_ratio'], PORTFOLIO['dividend_value_target_ratio']]
if (any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
        or not 0 <= v <= 1 for v in ratios) or not math.isclose(sum(ratios), 1)
        or PORTFOLIO['swing_enabled'] is not False
        or PORTFOLIO['dividend_value_orders_enabled'] is not False):
    raise ValueError('Invalid allocation or unsupported strategy execution enabled')
BAA_TARGET_RATIO, DIVIDEND_VALUE_TARGET_RATIO = ratios
