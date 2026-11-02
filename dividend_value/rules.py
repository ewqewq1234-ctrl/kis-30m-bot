"""Deterministic rules. Missing, zero and not-applicable are distinct values.

Amounts: KRW, ratios: fractions, periods: fiscal years. No quarterly amount
is multiplied by four. Only fields available from the automated KIS/OpenDART
run are used; no manual evidence gate is applied.
"""
import copy
import json
import math
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import median

DEFAULT_CONFIG = json.loads(Path(__file__).with_name('config.json').read_text())


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(',', '').strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def tax_quantity(taxable_dps, existing=0):
    """Strict unrounded income-tax bound; estimated, NOT a tax exemption promise."""
    if isinstance(existing, bool) or int(existing) != existing or existing < 0:
        raise ValueError('existing quantity must be a nonnegative integer')
    amount = number(taxable_dps)
    if amount is None or amount <= 0:
        return None
    unit_tax = Decimal(str(taxable_dps).replace(',', '')) * Decimal('0.14')
    maximum = int((Decimal('1000') / unit_tax).to_integral_value(rounding=ROUND_CEILING)) - 1
    return {'max_total_qty': maximum, 'additional_qty': max(0, maximum - existing),
            'basis': 'estimated: taxable DPS × total shares × 14% < KRW 1,000; no rounding',
            'existing_qty': existing}


def evaluate(facts, config=None, *, strategy_nav=None, existing_qty=0):
    cfg = config or DEFAULT_CONFIG
    f = copy.deepcopy(facts)
    checks, metrics = {}, {}

    def check(name, value):
        checks[name] = value  # True / False / None, never coerce missing to zero

    def compare(name, value, predicate):
        n = number(value)
        check(name, None if n is None else bool(predicate(n)))

    if f.get('exclusion'):
        return {**f, 'status': 'EXCLUDED', 'reasons': [f['exclusion']], 'checks': {}, 'metrics': {}}
    compare('valid_close', f.get('close'), lambda n: n > 0)
    check('market_status_verified', True if f.get('risk_verified') is True else None)
    if f.get('risk_flagged') is True:
        check('market_status_safe', False)
    else:
        check('market_status_safe', True if f.get('risk_verified') is True else None)

    annual = sorted(f.get('annual', []), key=lambda r: r.get('year', 0))
    expected = f.get('expected_fiscal_year')
    required = max(cfg['dividend_years'], cfg['positive_income_years'],
                   cfg['roe_years'], cfg['positive_ocf_years'], 3)
    by_year = {a.get('year'): a for a in annual}
    years = list(range(expected - required + 1, expected + 1)) if isinstance(expected, int) else []
    a = [by_year.get(y, {}) for y in years]
    check('annual_periods', bool(years) and len(by_year) == len(annual)
          and all(by_year.get(y) for y in years) or None)
    check('annual_krw_units', True if a and all(r.get('unit') == 'KRW' for r in a) else None)
    income = [number(r.get('income')) for r in a[-cfg['positive_income_years']:]]
    check('positive_income', False if any(v is not None and v <= 0 for v in income)
          else (True if len(income) == cfg['positive_income_years'] and None not in income else None))
    ocf = [number(r.get('ocf')) for r in a[-cfg['positive_ocf_years']:]]
    check('positive_ocf', False if any(v is not None and v <= 0 for v in ocf)
          else (True if len(ocf) == cfg['positive_ocf_years'] and None not in ocf else None))
    roes = []
    for r in a[-cfg['roe_years']:]:
        ni, end, start = map(number, (r.get('income'), r.get('equity'), r.get('equity_open')))
        if None not in (ni, end, start) and start > 0 and end > 0:
            roes.append(ni / ((start + end) / 2))
    metrics['roe_3y_average'] = sum(roes) / len(roes) if len(roes) == cfg['roe_years'] else None
    compare('roe', metrics['roe_3y_average'], lambda v: v >= cfg['min_roe'])
    latest = f.get('latest_balance', {})
    equity, debt, capital = map(number, (latest.get('equity_total'), latest.get('liabilities'), latest.get('capital')))
    latest_ok = latest.get('latest_disclosure_verified') is True and latest.get('unit') == 'KRW'
    compare('positive_equity', equity if latest_ok else None, lambda v: v > 0)
    metrics['debt_ratio'] = debt / equity if latest_ok and equity is not None and equity > 0 and debt is not None else None
    compare('debt', metrics['debt_ratio'], lambda v: 0 <= v <= cfg['max_debt_ratio'])
    check('capital_not_impaired', equity >= capital if latest_ok and equity is not None and capital is not None and capital > 0 else None)
    close = number(f.get('close'))
    # Use OpenDART's reported annual cash DPS as-is. Ordinary/special status,
    # split adjustment, finality and share-class matching are not evaluated.
    # Reject only clearly corrupt API values above 1,000% of the current price.
    dps = [number(r.get('cash_dps')) for r in a]
    dps = [None if v is not None and close and v > close * 10 else v for v in dps]
    recent = dps[-3:]
    last = dps[-1] if dps else None
    metrics['base_dividend'] = min(last, median(recent)) if len(recent) == 3 and None not in recent else None
    metrics['base_yield'] = metrics['base_dividend'] / close if metrics['base_dividend'] is not None and close and close > 0 else None
    continuous = (all(v > 0 for v in dps[-cfg['dividend_years']:])
                  if len(dps) >= cfg['dividend_years'] and None not in dps[-cfg['dividend_years']:] else None)
    high_yield = last / close >= cfg['high_yield_alternative'] if last is not None and close and close > 0 else None
    history = True if continuous is True or high_yield is True else (False if continuous is False and high_yield is False else None)
    check('dividend_history_or_high_yield', history)
    metrics['history_method'] = 'five_years' if continuous else ('high_yield' if high_yield else None)
    compare('dividend_yield', metrics['base_yield'], lambda v: v >= cfg['min_yield'])
    if len(dps) >= 2 and None not in dps[-2:] and dps[-2] > 0:
        check('no_30pct_cut', Decimal(str(dps[-1])) > Decimal(str(dps[-2])) * (1 - Decimal(str(cfg['dividend_cut_threshold']))))
    else:
        check('no_30pct_cut', None)
    cash = [number(r.get('cash_dividends')) for r in a[-3:]]
    ni3 = [number(r.get('income')) for r in a[-3:]]
    payout_ok = len(cash) == 3 and None not in cash and None not in ni3 and sum(ni3) > 0
    metrics['payout_ratio'] = sum(cash) / sum(ni3) if payout_ok else None
    compare('payout', metrics['payout_ratio'], lambda v: 0 <= v <= cfg['max_payout_ratio'])
    valuation = f.get('valuation', {})
    eps, bps = map(number, (valuation.get('eps'), valuation.get('bps')))
    metrics['per'] = number(valuation.get('per'))
    metrics['pbr'] = number(valuation.get('pbr'))
    if metrics['per'] is None and close and eps is not None and eps > 0:
        metrics['per'] = close / eps
    if metrics['pbr'] is None and close and bps is not None and bps > 0:
        metrics['pbr'] = close / bps
    compare('per', metrics['per'], lambda v: 0 < v <= cfg['max_per'])
    compare('pbr', metrics['pbr'], lambda v: 0 < v <= cfg['max_pbr'])
    compare('turnover', f.get('avg_turnover_20d'), lambda v: v >= cfg['min_daily_turnover_krw'])
    d = metrics['base_dividend']
    if (eps is None or eps <= 0) and close and metrics['per'] and metrics['per'] > 0:
        eps = close / metrics['per']
    if (bps is None or bps <= 0) and close and metrics['pbr'] and metrics['pbr'] > 0:
        bps = close / metrics['pbr']
    metrics['buy_ceiling'] = min(d / cfg['min_yield'], eps * cfg['max_per'], bps * cfg['max_pbr']) if None not in (d, eps, bps) and eps > 0 and bps > 0 else None
    # The reported annual cash DPS is used for the tax quantity estimate.
    taxable = f.get('taxable_payment_dps')
    tax_basis = 'verified payment' if taxable is not None else 'estimated annual lump sum'
    if taxable is None and a:
        taxable = a[-1].get('total_taxable_annual_dps')
    if number(taxable) is not None and close and number(taxable) > close * 10:
        taxable = None
    tax = tax_quantity(taxable, existing_qty)
    metrics['tax_quantity'] = tax
    metrics['tax_assumption'] = tax_basis
    metrics['tax_limited_investment'] = tax['additional_qty'] * close if tax and close else None
    if strategy_nav is not None and close and close > 0:
        budget = float(strategy_nav) * cfg['future_execution']['initial_weight_of_strategy_nav']
        metrics['initial_budget'] = budget
        metrics['estimated_buy_qty'] = min(int(budget // close), tax['additional_qty']) if tax else None
    failures = [name for name, passed in checks.items() if passed is False]
    missing = [name for name, passed in checks.items() if passed is None]
    status = 'FAIL' if failures else ('REVIEW' if missing or f.get('review_reasons') else 'PASS')
    reasons = ['FAIL:' + s for s in failures] + ['UNVERIFIED:' + s for s in missing] + f.get('review_reasons', [])
    return {**f, 'status': status, 'reasons': reasons, 'checks': checks, 'metrics': metrics}
