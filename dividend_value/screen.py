"""python -m dividend_value.screen --output results/dividend-value

Research only: does not import the bot, read account secrets, or submit orders.
"""
import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from .data import PublicData, DataError, atomic_json, normalize_financial, disclosed, PRICE
from .rules import DEFAULT_CONFIG, evaluate, number

ROOT = Path(__file__).resolve().parents[1]


def reviewed_evidence(path, code, as_of):
    """Manual decisions require dated source documents, not guesses from yield spikes."""
    evidence = json.loads(Path(path).read_text(encoding='utf-8')).get('stocks', {}).get(code)
    if not evidence:
        return {}
    allowed = {'annual', 'valuation', 'latest_balance', 'audit', 'audit_verified',
               'taxable_payment_dps', 'risk_verified', 'risk_flagged'}
    if (evidence.get('as_of') != as_of or not evidence.get('reviewed_by')
            or not evidence.get('reason') or not evidence.get('sources')
            or not evidence.get('published_at') or evidence['published_at'] > as_of
            or not evidence.get('checked_at') or evidence['checked_at'] < evidence['published_at']):
        raise DataError('manual evidence missing dated provenance or outside cutoff')
    for source in evidence['sources']:
        from urllib.parse import urlparse
        parsed = urlparse(source)
        if parsed.scheme != 'https' or parsed.hostname not in {'dart.fss.or.kr', 'opendart.fss.or.kr',
                'kind.krx.co.kr', 'apiportal.koreainvestment.com'}:
            raise DataError('manual evidence must cite official disclosure sources')
    fields = evidence.get('fields', {})
    if set(fields) - allowed:
        raise DataError('manual evidence attempts to replace market identity/price')
    return {'fields': fields, 'provenance': {k: v for k, v in evidence.items() if k != 'fields'}}


def apply_evidence(facts, evidence):
    if not evidence:
        return facts
    fields = evidence['fields']
    by_year = {r['year']: r for r in facts.get('annual', [])}
    for update in fields.get('annual', []):
        year = update['year']
        if year not in by_year:
            raise DataError('manual annual evidence cannot invent a missing financial report')
        allowed = {'year','ordinary_dps','total_taxable_annual_dps','special_dividend_verified',
                   'split_adjustment_verified','dividend_final','share_class',
                   'cash_dividends_parent','payout_scope_verified'}
        if set(update) - allowed:
            raise DataError('manual dividend evidence cannot replace accounting scope or income')
        by_year[year].update(update)
    facts['annual'] = sorted(by_year.values(), key=lambda r: r['year'])
    for key, value in fields.items():
        if key != 'annual':
            facts[key] = value
    facts['manual_evidence'] = evidence['provenance']
    return facts


def financial_facts(api, facts, corp, as_of, cfg):
    company = api.dart('company.json', {'corp_code': corp})
    industry = str(company.get('induty_code', ''))
    facts['industry'] = industry or None
    if industry[:2] in {'64','65','66'}:
        facts['exclusion'] = 'financial_company'
        return facts
    if not industry:
        facts['review_reasons'].append('UNVERIFIED:industry')
    if company.get('acc_mt') != '12':
        facts['review_reasons'].append('UNVERIFIED:non_December_fiscal_calendar')
        return facts
    year = date.fromisoformat(as_of).year - 1
    facts['expected_fiscal_year'] = year
    # Uncached list calls detect corrections; version fingerprint invalidates only
    # this company's cached annual/interim data. Pagination is mandatory.
    disclosures = []
    for page in range(1, 101):
        data = api.dart('list.json', {'corp_code': corp, 'bgn_de': f'{year-5}0101',
            'end_de': as_of.replace('-', ''), 'pblntf_ty': 'A', 'page_count': '100',
            'page_no': str(page), 'last_reprt_at': 'Y'})
        disclosures.extend(data.get('list', []))
        if page >= int(data.get('total_page') or 0):
            break
    else:
        raise DataError('disclosure list exceeded bounded pagination')
    version = hashlib.sha256(json.dumps(disclosures, sort_keys=True).encode()).hexdigest()
    facts['disclosure_fingerprint'] = version
    facts['annual'], facts['raw_dividend_reports'] = [], []
    # CFS is preferred. Only an explicit no-data response permits OFS fallback.
    latest_scope = None
    annual_count = max(cfg['dividend_years'], cfg['positive_income_years'],
                       cfg['roe_years'], cfg['positive_ocf_years'], 3)
    for fy in range(year - annual_count + 1, year + 1):
        params = {'corp_code': corp, 'bsns_year': str(fy), 'reprt_code': '11011'}
        financial = api.dart('fnlttSinglAcntAll.json', {**params, 'fs_div': 'CFS'}, version=version)
        scope = 'CFS'
        if financial.get('status') == '013':
            scope = 'OFS'
            financial = api.dart('fnlttSinglAcntAll.json', {**params, 'fs_div': scope}, version=version)
        rows = financial.get('list', [])
        normalized = normalize_financial(rows, scope, fy, '11011', as_of)
        if normalized:
            facts['annual'].append(normalized)
            if fy == year:
                facts['latest_balance'] = dict(normalized, latest_disclosure_verified=False)
                latest_scope = scope
            for r in normalized['rcept_nos']:
                facts['sources'].append('https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + r)
        dividend = api.dart('alotMatter.json', params, version=version)
        # The endpoint does not identify ordinary vs special DPS or split
        # adjustments. Preserve raw rows; require documented human verification.
        facts['raw_dividend_reports'].append({'year': fy, 'data': dividend.get('list', [])})
    # Fetch the latest disclosed interim balance sheet without annualizing income.
    quarter_reports = [('11013', '분기보고서', '03'), ('11012', '반기보고서', '06'),
                       ('11014', '분기보고서', '09')]
    eligible = []
    for r in disclosures:
        name = r.get('report_nm', '')
        for report, label, month in quarter_reports:
            if label in name and f'({year+1}.{month})' in name:
                eligible.append((month, report))
    if eligible and latest_scope:
        _, report = max(eligible)
        raw = api.dart('fnlttSinglAcntAll.json', {'corp_code': corp, 'bsns_year': str(year+1),
            'reprt_code': report, 'fs_div': latest_scope}, version=version)
        latest = normalize_financial(raw.get('list', []), latest_scope, year+1, report, as_of)
        if latest:
            facts['latest_balance'] = dict(latest, latest_disclosure_verified=True)
    elif latest_scope and any('사업보고서' in r.get('report_nm','') and f'({year}.12)' in r.get('report_nm','') for r in disclosures):
        facts['latest_balance']['latest_disclosure_verified'] = True
    audit_rows = api.dart('accnutAdtorNmNdAdtOpinion.json', {'corp_code':corp,
        'bsns_year':str(year), 'reprt_code':'11011'}, version=version).get('list', [])
    facts['raw_audit_reports'] = audit_rows
    current = [r for r in audit_rows if str(r.get('bsns_year')) == str(year)
               or '당기' in str(r.get('bsns_year',''))]
    opinions = {r.get('adt_opinion') for r in current}
    if disclosed(current, as_of) and len(opinions) == 1 and None not in opinions and '' not in opinions:
        opinion = opinions.pop().strip()
        # Do not turn arbitrary explanatory text into an adverse opinion.
        if opinion in {'적정','한정','부적정','의견거절'}:
            facts.update(audit=opinion, audit_verified=True)
    # Special dividends and per-share bases still need evidence. Absence is
    # REVIEW, never an inferred ordinary payout or clean opinion.
    return facts


def screen_stock(api, stock, corp_codes, as_of, evidence_path, cfg):
    f = dict(stock, as_of=as_of, review_reasons=list(stock.get('review_reasons', [])),
             sources=list(stock.get('sources', [])))
    f['corp_code'] = corp_codes.get(f['code'])
    if f.get('exclusion'):
        return f
    bars = api.daily(f['code'], date.fromisoformat(as_of))
    if len(bars) < cfg['turnover_sessions'] or bars[-1]['date'] != as_of:
        raise DataError('insufficient daily history or stale last trading session')
    f['close'] = bars[-1]['close']
    f['avg_turnover_20d'] = sum(b['turnover'] for b in bars[-cfg['turnover_sessions']:]) / cfg['turnover_sessions']
    f['sources'].append('KIS inquire-daily-itemchartprice: ' + as_of)
    quote = api.kis(PRICE, {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': f['code']}).get('output', {})
    f['kis_indicative_valuation'] = {key: quote.get(key) for key in ('per', 'pbr', 'eps', 'bps', 'stac_month')}
    f['valuation'] = {'eps': number(quote.get('eps')), 'bps': number(quote.get('bps')),
                      'basis': 'unknown_KIS_period', 'basis_verified': False,
                      'share_class': f['share_class'], 'price_date': as_of}
    if not f.get('corp_code'):
        f['review_reasons'].append('UNVERIFIED:DART_corporate_mapping')
        return f
    # Industry is always retrieved even for low-liquidity financial companies.
    profile = api.dart('company.json', {'corp_code': f['corp_code']})
    f['industry'] = profile.get('induty_code')
    if str(f['industry'])[:2] in {'64','65','66'}:
        f['exclusion'] = 'financial_company'
        return f
    indicative = [number(quote.get('per')), number(quote.get('pbr'))]
    if f['avg_turnover_20d'] < cfg['min_daily_turnover_krw']:
        return f  # Proven liquidity failure; avoid all historical requests.
    if ((indicative[0] is not None and indicative[0] > cfg['max_per']) or
            (indicative[1] is not None and indicative[1] > cfg['max_pbr'])):
        f['review_reasons'].append('UNVERIFIED:indicative_valuation_prefilter_requires_period_confirmation')
        # Unknown metric periods cannot prove FAIL. Explicitly retain in REVIEW.
        return f
    financial_facts(api, f, f['corp_code'], as_of, cfg)
    return apply_evidence(f, reviewed_evidence(evidence_path, f['code'], as_of))


def export(output, rows, meta):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'screening_details.json', rows)
    counts = Counter(r['status'] for r in rows)
    reasons = Counter(reason for r in rows for reason in r.get('reasons', []))
    meta.update({'counts': {s: counts[s] for s in ('PASS','FAIL','REVIEW','EXCLUDED')},
        'queried_complete': sum(r.get('query_complete', False) for r in rows),
        'unprocessed': meta.get('universe_total', 0) - len(rows),
        'query_failed': sum(r.get('query_failed', False) for r in rows),
        'pass_unique_companies': len({r.get('corp_code') for r in rows if r['status'] == 'PASS'}),
        'pass_tax_one_share': sum(r['status'] == 'PASS' and (r['metrics'].get('tax_quantity') or {}).get('additional_qty', 0) >= 1 for r in rows),
        'failure_reasons': dict(reasons)})
    meta['partial'] = bool(meta.get('blocker') or meta['unprocessed'] or meta['query_failed'] or not meta.get('universe_verified'))
    meta['validation_incomplete'] = bool(counts['REVIEW'])
    fields = ['name','code','corp_code','market','share_class','industry','status','close','history_method',
        'base_dividend','base_yield','per','pbr','roe_3y_average','debt_ratio','payout_ratio','avg_turnover_20d',
        'buy_ceiling','tax_quantity','tax_assumption','initial_budget','estimated_buy_qty','as_of','reasons','sources']
    groups = {'screening_all.csv': rows, 'candidates.csv': [r for r in rows if r['status']=='PASS'],
        'review_required.csv': [r for r in rows if r['status']=='REVIEW'],
        'financial_excluded.csv': [r for r in rows if r['status']=='EXCLUDED']}
    for name, subset in groups.items():
        with (output / name).open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fields, extrasaction='ignore')
            writer.writeheader()
            for row in subset:
                flat = {**row, **row.get('metrics', {})}
                for k in fields:
                    value = flat.get(k)
                    if isinstance(value, (list,dict)):
                        flat[k] = json.dumps(value, ensure_ascii=False)
                    elif isinstance(value, str) and value.startswith(('=','+','-','@')):
                        flat[k] = "'" + value
                writer.writerow(flat)
    atomic_json(output / 'run_metadata.json', meta)
    status = '부분 검색' if meta['partial'] else '전체 조회 완료'
    esc = lambda x: html.escape(str(x))
    items = ''.join(f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k,v in meta.items())
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>배당·가치 조건검색</title>
<style>body{{font:16px sans-serif;max-width:1100px;margin:40px auto;padding:0 24px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:9px;text-align:left;overflow-wrap:anywhere}}th{{min-width:180px}}</style>
<h1>배당·가치 조건검색 — {status}</h1><p>공격형 BAA 60% / 배당·가치 40% 예약. 배당·가치 주문 비활성.</p>
<p>미확인 자료는 REVIEW입니다. PASS 0은 모든 기업의 부적격을 뜻하지 않습니다. 미조회·검증 미완료 수를 함께 확인하세요.</p>
<p>금액 KRW, 수익률·비율은 소수(0.04=4%). 빈 값은 누락, 0은 확인된 0. 비적용은 not_applicable.</p>
<table>{items}</table><h2>결과 파일</h2><ul>'''
    body += ''.join(f'<li><a href="{name}">{name}</a></li>' for name in [*groups, 'screening_details.json', 'run_metadata.json', 'execution.log'])
    body += '</ul><p>ROE=순이익/평균 귀속자본; 부채비율=부채/총자본; 배당성향=3년 현금배당합/동일귀속 순이익합. 배당 D=min(최근 보통배당,3년 중앙값). 연간 주당지표는 TTM으로 표시하지 않습니다. 세액은 지급분×수량×14%의 절사 전 추정치이며 증권사 합산·절사 처리는 미확인입니다.</p></html>'
    (output / 'summary.html').write_text(body, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='results/dividend-value')
    parser.add_argument('--cache', default='.screen-cache')
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--evidence', default=str(Path(__file__).with_name('evidence.json')))
    parser.add_argument('--as-of', help='latest completed exchange session only; historical scans unsupported')
    parser.add_argument('--max-symbols', type=int, default=0, help='0=all; resume same cutoff/config on next run')
    parser.add_argument('--account-nav', type=float, help='total NAV; dividend strategy receives 40%%')
    parser.add_argument('--holdings', type=Path, help='optional local JSON code: existing quantity (never uploaded)')
    args = parser.parse_args(argv)
    if args.max_symbols < 0 or args.account_nav is not None and args.account_nav <= 0:
        parser.error('max-symbols must be >=0; account NAV must be positive')
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
    cfg_hash = hashlib.sha256(Path(args.config).read_bytes()+Path(args.evidence).read_bytes()).hexdigest()
    portfolio = json.loads((ROOT / 'portfolio.json').read_text())
    if cfg['future_execution']['enabled'] or portfolio['dividend_value_orders_enabled']:
        raise DataError('research implementation cannot enable orders')
    holdings = json.loads(args.holdings.read_text()) if args.holdings else {}
    strategy_nav = args.account_nav * portfolio['dividend_value_target_ratio'] if args.account_nav else None
    api = PublicData(args.cache)
    # Rebuildable cache; prune only files directly in this dedicated directory.
    files = sorted(Path(args.cache).glob('*.json'), key=lambda p:p.stat().st_mtime)
    total_cache = sum(p.stat().st_size for p in files)
    for path in files:
        if time.time() - path.stat().st_mtime > 45*86400 or total_cache > 512*1024*1024:
            size = path.stat().st_size
            path.unlink()
            total_cache -= size
    start, rows = time.monotonic(), []
    meta = {'executed_kst': now.isoformat(), 'as_of': None, 'universe_total': 0,
            'universe_verified': False, 'orders_enabled': False,
            'account_nav_provided': args.account_nav is not None,
            'freshness': 'KIS completed daily bars; DART receipts limited to cutoff; manual evidence required for unresolved items',
            'config_sha256': cfg_hash}
    checkpoint = None
    try:
        missing = [n for n in ('KIS_APP_KEY','KIS_APP_SECRET','DART_API_KEY') if not os.getenv(n)]
        if missing:
            raise DataError('missing GitHub Actions Secrets: ' + ', '.join(missing))
        cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
        baseline = api.daily('069500', cutoff)
        if not baseline or (cutoff - date.fromisoformat(baseline[-1]['date'])).days > 10:
            raise DataError('unable to determine recent completed exchange session')
        as_of = baseline[-1]['date']
        if args.as_of and args.as_of != as_of:
            raise DataError('historical scans unsupported: master/quote snapshots are current')
        meta['as_of'] = as_of
        universe = api.market_master()
        meta['universe_total'], meta['universe_verified'] = len(universe), True
        meta['scope_target_total'] = sum(not s.get('exclusion') for s in universe)
        corp_codes = api.corp_codes()
        signature = hashlib.sha256(json.dumps([as_of,cfg_hash,strategy_nav,holdings,universe],sort_keys=True).encode()).hexdigest()
        checkpoint = Path(args.cache) / ('checkpoint-' + signature + '.json')
        if checkpoint.exists():
            rows = json.loads(checkpoint.read_text(encoding='utf-8'))
        # Failed requests are retried on resume; successful records are not duplicated.
        rows = [r for r in rows if not r.get('query_failed')]
        done = {r['code'] for r in rows}
        attempted = 0
        for stock in universe:
            if stock['code'] in done:
                continue
            if args.max_symbols and attempted >= args.max_symbols:
                break
            attempted += 1
            try:
                facts = screen_stock(api, stock, corp_codes, as_of, args.evidence, cfg)
                row = evaluate(facts, cfg, strategy_nav=strategy_nav, existing_qty=holdings.get(stock['code'],0))
                row['query_complete'] = True
            except (DataError, ValueError, KeyError):
                row = {**stock, 'as_of': as_of, 'status':'REVIEW', 'metrics':{}, 'checks':{},
                       'reasons':['UNVERIFIED:official_query_or_evidence_failed'], 'query_failed':True}
            # Existing holding quantities are intentionally not written to research artifacts.
            if row.get('metrics', {}).get('tax_quantity'):
                row['metrics']['tax_quantity'].pop('existing_qty', None)
            rows.append(row)
            atomic_json(checkpoint, rows)
            print(f"processed={len(rows)}/{len(universe)} status={row['status']}", flush=True)
    except (DataError, ValueError, KeyError) as exc:
        meta['blocker'] = str(exc) if isinstance(exc, DataError) else 'invalid official data or configuration'
    finally:
        meta.update({'api_calls':api.calls, 'cache_hits':api.cache_hits,
            'download_bytes':api.bytes, 'runtime_seconds':round(time.monotonic()-start,2),
            'cache_bytes':sum(p.stat().st_size for p in Path(args.cache).glob('*.json'))})
        export(args.output, rows, meta)
        Path(args.output, 'execution.log').write_text(json.dumps(meta, ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(meta, ensure_ascii=False))
    return 2 if meta.get('blocker') else 0


if __name__ == '__main__':
    raise SystemExit(main())
