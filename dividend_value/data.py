"""Official REST adapters with an explicit read-only request allowlist."""
import hashlib
import io
import json
import os
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import requests

from .rules import number

KIS = 'https://openapi.koreainvestment.com:9443'
DART = 'https://opendart.fss.or.kr/api'
MASTER = 'https://new.real.download.dws.co.kr/common/master/'
PRICE = '/uapi/domestic-stock/v1/quotations/inquire-price'
DAILY = '/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice'
ALLOWED_GET = {KIS + PRICE, KIS + DAILY} | {
    DART + '/' + name for name in ('corpCode.xml', 'company.json', 'list.json',
        'fnlttSinglAcntAll.json', 'alotMatter.json')
} | {MASTER + market + '_code.mst.zip' for market in ('kospi', 'kosdaq')}


class DataError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


class PublicData:
    def __init__(self, cache_dir, *, session=None, interval=0.6):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.interval = interval
        self.last_request = 0.0
        self.calls = 0
        self.cache_hits = 0
        self.bytes = 0
        self.token = None
        self.key = os.getenv('KIS_APP_KEY', '')
        self.secret = os.getenv('KIS_APP_SECRET', '')
        self.dart_key = os.getenv('DART_API_KEY', '')

    def request(self, method, url, **kwargs):
        if not ((method == 'GET' and url in ALLOWED_GET)
                or (method == 'POST' and url == KIS + '/oauth2/tokenP')):
            raise DataError('read-only allowlist blocked request')
        # Authentication may be issued once, never automatically replayed.
        attempts = 1 if method == 'POST' else 3
        for attempt in range(attempts):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.calls += 1
            try:
                response = self.session.request(method, url, timeout=(10, 40), **kwargs)
                self.bytes += len(response.content)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(2 ** attempt)
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt + 1 == attempts:
                    # Never interpolate request exceptions: DART key is in query string.
                    raise DataError('official API transport failure after bounded retries') from None
                time.sleep(2 ** attempt)
        raise DataError('official API unavailable')

    def kis(self, path, params):
        if not self.key or not self.secret:
            raise DataError('missing KIS_APP_KEY / KIS_APP_SECRET')
        if self.token is None:
            payload = self.request('POST', KIS + '/oauth2/tokenP', json={
                'grant_type': 'client_credentials', 'appkey': self.key,
                'appsecret': self.secret}).json()
            self.token = payload.get('access_token')
            if not self.token:
                raise DataError('KIS authentication unsuccessful')
        tr_id = {PRICE: 'FHKST01010100', DAILY: 'FHKST03010100'}.get(path)
        if tr_id is None:
            raise DataError('unknown KIS read endpoint')
        data = self.request('GET', KIS + path, params=params, headers={
            'authorization': 'Bearer ' + self.token, 'appkey': self.key,
            'appsecret': self.secret, 'tr_id': tr_id, 'custtype': 'P'}).json()
        if data.get('rt_cd') != '0':
            raise DataError('KIS read rejected (details suppressed to protect credentials)')
        return data

    def dart(self, name, params, *, version=None):
        if not self.dart_key:
            raise DataError('missing DART_API_KEY')
        # version is the current official disclosure-list fingerprint: corrections
        # invalidate all financial cache entries for the affected company.
        safe_key = json.dumps([name, params, version], sort_keys=True)
        path = self.cache / (hashlib.sha256(safe_key.encode()).hexdigest() + '.json')
        if version is not None and path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text(encoding='utf-8'))
        data = self.request('GET', DART + '/' + name,
                            params={**params, 'crtfc_key': self.dart_key}).json()
        if data.get('status') not in {'000', '013'}:
            raise DataError('OpenDART read rejected; inspect key permissions/quota')
        if version is not None:
            atomic_json(path, data)
        return data

    def corp_codes(self):
        if not self.dart_key:
            raise DataError('missing DART_API_KEY')
        raw = self.request('GET', DART + '/corpCode.xml', params={'crtfc_key': self.dart_key}).content
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = ElementTree.fromstring(z.read('CORPCODE.xml'))
            return {r.findtext('stock_code', '').strip(): r.findtext('corp_code')
                    for r in xml.findall('list') if r.findtext('stock_code', '').strip()}
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            raise DataError('OpenDART corporate-code ZIP unavailable') from None

    def daily(self, code, end):
        data = self.kis(DAILY, {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code,
            'FID_INPUT_DATE_1': (end - timedelta(days=100)).strftime('%Y%m%d'),
            'FID_INPUT_DATE_2': end.strftime('%Y%m%d'), 'FID_PERIOD_DIV_CODE': 'D',
            'FID_ORG_ADJ_PRC': '1'})  # Official current example: 1 = unadjusted.
        rows = data.get('output2', [])
        result = {}
        for r in rows:
            stamp = r.get('stck_bsop_date', '')
            if not stamp:
                continue
            try:
                session = date.fromisoformat(f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}')
            except ValueError:
                raise DataError('invalid daily session date') from None
            if session > end:
                continue
            close, turnover = number(r.get('stck_clpr')), number(r.get('acml_tr_pbmn'))
            if close is None or close <= 0 or turnover is None or turnover < 0:
                raise DataError('invalid daily close/turnover')
            normalized = {'date': session.isoformat(), 'close': close, 'turnover': turnover}
            if stamp in result and result[stamp] != normalized:
                raise DataError('conflicting duplicate daily session')
            result[stamp] = normalized
        return sorted(result.values(), key=lambda r: r['date'])

    def market_master(self):
        result = []
        for market in ('kospi', 'kosdaq'):
            raw = self.request('GET', MASTER + market + '_code.mst.zip').content
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    text = z.read(market + '_code.mst').decode('cp949')
                result.extend(parse_master(text, market))
            except (zipfile.BadZipFile, KeyError, UnicodeError):
                raise DataError('KIS market master format unavailable') from None
        if len({r['code'] for r in result}) != len(result):
            raise DataError('duplicate master stock codes')
        return result


# Widths and indices from the official KIS stocks_info parsers, retrieved
# 2026-11-01. Preserve CP949 character slicing after decoding, as in upstream.
WIDTHS = {
    'kospi': [2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1],
    'kosdaq': [2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1]
}


def parse_master(text, market):
    widths = WIDTHS[market]
    tail_len = sum(widths)
    indices = ({'etp':12, 'spac':19, 'halt':34, 'liquidation':35, 'managed':36,
                'preferred':54, 'month':52, 'bank':18, 'securities':26, 'insurance':28}
               if market == 'kospi' else
               {'etp':8, 'spac':14, 'halt':29, 'liquidation':30, 'managed':31,
                'preferred':49, 'month':47, 'bank':13, 'securities':21, 'insurance':23})
    stocks = []
    for row in text.splitlines():
        if len(row) < tail_len + 22:
            raise DataError('master schema length mismatch')
        head, tail = row[:-tail_len], row[-tail_len:]
        parts, pos = [], 0
        for width in widths:
            parts.append(tail[pos:pos+width].strip())
            pos += width
        flags = {key: parts[index] for key, index in indices.items()}
        # KIS official KOSPI/KOSDAQ master "주식 상장 일자" fields (YYYYMMDD).
        # KOSPI index 49; KOSDAQ index 44, indexed after the parsed fixed-width tail.
        raw_listing = parts[49 if market == 'kospi' else 44]
        try:
            listing_date = date.fromisoformat(raw_listing).isoformat() if len(raw_listing) == 8 else None
        except ValueError:
            listing_date = None
        code, isin, name = head[:9].strip(), head[9:21].strip(), head[21:].strip()
        if not code or not name:
            raise DataError('master identity missing')
        group = parts[0]
        exclusion = None
        if flags['spac'] == 'Y':
            exclusion = 'SPAC'
        elif flags['etp'] in {'1', '2', '3', '4'} or group in {'EF', 'EN', 'ET'}:
            exclusion = 'ETF/ETN'
        elif group == 'RT':
            exclusion = 'REIT'
        elif group in {'BC', 'PF', 'IF', 'MF', 'DR', 'SW', 'SR', 'FS'}:
            exclusion = 'not_common_or_preferred_domestic_stock:' + group
        safe = all(flags[key] in {'N', 'Y'} for key in ('halt', 'liquidation', 'managed'))
        # Unrecognized groups are REVIEW; never assume a fund is a normal share.
        stocks.append({'code': code, 'isin': isin, 'name': name, 'market': market.upper(),
            'share_class': 'preferred' if flags['preferred'] not in {'0', ''} else 'common',
            'risk_verified': safe, 'risk_flagged': any(flags[k] == 'Y' for k in ('halt','liquidation','managed')),
            'listing_date': listing_date, 'listing_date_verified': bool(listing_date),
            'exclusion': exclusion, 'master_group': group, 'master_flags': flags,
            'review_reasons': [] if group == 'ST' or exclusion else ['UNVERIFIED:security_group'],
            'sources': [MASTER + market + '_code.mst.zip']})
    return stocks


def account(rows, account_id, statement, field='thstrm_amount'):
    values = {number(r.get(field)) for r in rows if r.get('account_id') == account_id and r.get('sj_div') in statement}
    return values.pop() if len(values) == 1 else None


def disclosed(rows, as_of):
    return bool(rows) and all(str(r.get('rcept_no', ''))[:8].isdigit()
                              and str(r['rcept_no'])[:8] <= as_of.replace('-', '') for r in rows)


def normalize_dividend(rows, year, as_of):
    """Extract class-agnostic annual cash dividend values from OpenDART.

    The minimum numeric DPS across reported stock kinds is used so the screen
    does not require a common/preferred mapping and does not overstate yield.
    No ordinary/special or split-adjustment inference is attempted.
    """
    if not disclosed(rows, as_of):
        return {}
    current = [r for r in rows if str(r.get('stlm_dt', '')).startswith(f'{year}-')]
    if not current:
        return {}
    dps = [number(r.get('thstrm')) for r in current
           if str(r.get('se', '')).strip() == '주당 현금배당금(원)']
    dps = [v for v in dps if v is not None and v >= 0]
    totals = {number(r.get('thstrm')) for r in current
              if str(r.get('se', '')).strip() == '현금배당금총액(백만원)'}
    totals.discard(None)
    result = {}
    if dps:
        result['cash_dps'] = min(dps)
        result['total_taxable_annual_dps'] = min(dps)
    if len(totals) == 1:
        result['cash_dividends'] = totals.pop() * 1_000_000
    return result


def normalize_financial(rows, scope, year, report, as_of):
    if (not disclosed(rows, as_of)
            or any(str(r.get('bsns_year')) != str(year) or r.get('reprt_code') != report for r in rows)
            or any(r.get('currency', 'KRW') != 'KRW' for r in rows)):
        return None
    ids = {'income': 'ifrs-full_ProfitLossAttributableToOwnersOfParent' if scope == 'CFS' else 'ifrs-full_ProfitLoss',
           'equity': 'ifrs-full_EquityAttributableToOwnersOfParent' if scope == 'CFS' else 'ifrs-full_Equity'}
    r = {'year': year, 'scope': 'CFS_PARENT' if scope == 'CFS' else 'OFS', 'unit': 'KRW',
         'report_code': report, 'rcept_nos': sorted({x['rcept_no'] for x in rows}),
         'equity': account(rows, ids['equity'], {'BS'}),
         'equity_open': account(rows, ids['equity'], {'BS'}, 'frmtrm_amount'),
         'equity_total': account(rows, 'ifrs-full_Equity', {'BS'}),
         'liabilities': account(rows, 'ifrs-full_Liabilities', {'BS'}),
         'capital': account(rows, 'ifrs-full_IssuedCapital', {'BS'})}
    if report == '11011':
        r['income'] = account(rows, ids['income'], {'IS','CIS'})
        r['ocf'] = account(rows, 'ifrs-full_CashFlowsFromUsedInOperatingActivities', {'CF'})
    # Interim income intentionally not normalized. thstrm_amount may be 3 months,
    # thstrm_add_amount is YTD; TTM EPS requires comparable weighted shares too.
    return r
