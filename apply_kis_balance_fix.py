#!/usr/bin/env python3
"""Patch kis-30m-bot/bot_once.py against inspected main-branch Git blob.

Does not change trading thresholds, order enablement, portfolio accounting,
state formats, secrets, or GitHub workflow scheduling.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

ORIGINAL_GIT_BLOB = 'df739c4a16de0c523d3dd0880a49e44c71fc5951'


def blob_hash(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def replace_once(source: str, old: str, new: str, label: str) -> str:
    hits = source.count(old)
    if hits != 1:
        raise ValueError(f'{label}: 기존 코드가 예상과 다름 (일치 {hits}건); 안전하게 수정 중단')
    return source.replace(old, new, 1)


def patch(source: str) -> str:
    # Track actual order intention only. Dry-runs do not mutate account.
    source = replace_once(source,
        'SESSION = requests.Session()\nACCESS_TOKEN = None\n',
        'SESSION = requests.Session()\nACCESS_TOKEN = None\n# In-process counter: increments only after durable journal checkpoint, before order POST.\nACCOUNT_ORDER_ATTEMPTS = 0\n',
        'Order activity counter')

    start = source.index('def api_request(method, path, token=None, *')
    end = source.index('\n\ndef _token_keys(', start)
    old = source[start:end]
    new = '''def api_request(method, path, token=None, *, tr_id=None, params=None, body=None,
                timeout=10, retries=1, tr_cont=None):
    """Fail-closed KIS request. Retry only transient GET errors; never POST."""
    global ACCESS_TOKEN
    headers = {"content-type": "application/json; charset=utf-8", "appkey": KIS_APP_KEY,
               "appsecret": KIS_APP_SECRET}
    if token:
        headers["authorization"] = f"Bearer {ACCESS_TOKEN or token}"
    if tr_id:
        headers["tr_id"] = tr_id
    # Official KIS balance example: first page '', subsequent pages 'N'.
    if tr_cont is not None:
        headers["tr_cont"] = tr_cont
    attempts = (retries + 1) if method == "GET" else 1
    for attempt in range(attempts):
        response = None
        try:
            response = SESSION.request(method, KIS_URL + path, headers=headers, params=params,
                                       json=body, timeout=timeout)
            if response.status_code == 401 and method == "GET" and token and attempt + 1 < attempts:
                # Only reauthorize GET; POST order outcome may be uncertain.
                fresh = get_access_token(force=True)
                headers["authorization"] = f"Bearer {fresh}"
                time.sleep(0.5)
                continue
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TradeSafetyError("API 응답 형식 오류")
            if "rt_cd" in result and result["rt_cd"] != "0":
                raise TradeSafetyError(
                    f"KIS API 거부 [{path}]: {result.get('msg_cd')} {result.get('msg1')}")
            return result, response.headers
        except TradeSafetyError:
            # Business errors are not transient; do not resend.
            raise
        except (requests.RequestException, ValueError) as ex:
            status = (response.status_code if response is not None else None)
            retryable = method == "GET" and (
                status in (429, 500, 502, 503, 504)
                or (status is None and isinstance(ex,
                    (requests.Timeout, requests.ConnectionError)))
            )
            if not retryable or attempt + 1 >= attempts:
                # Avoid printing account number from requests' full URL.
                detail = f"HTTP {status}" if status is not None else type(ex).__name__
                raise TradeSafetyError(f"API 호출 실패 [{path}]: {detail}; "
                                       f"요청 시도 {attempt + 1}회") from ex
            wait = min(4.0, 1.0 * (2 ** attempt))
            if status == 429 and response is not None:
                try:
                    wait = min(10.0, max(wait, float(response.headers.get("Retry-After", 0))))
                except (ValueError, TypeError):
                    pass
            print(f">> [KIS API 일시 오류] {path}, "
                  f"{('HTTP ' + str(status)) if status is not None else type(ex).__name__}; "
                  f"{wait:.1f}초 후 GET 재시도 ({attempt + 1}/{attempts - 1})")
            time.sleep(wait)
    raise TradeSafetyError("알 수 없는 API 호출 실패")'''
    source = replace_once(source, old, new, 'api_request')

    source = replace_once(source,
        '''        result, heads = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", token,
                                    tr_id="TTTC8434R" if KIS_ENV == "real" else "VTTC8434R", params=params)''',
        '''        result, heads = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", token,
                                    tr_id="TTTC8434R" if KIS_ENV == "real" else "VTTC8434R",
                                    params=params, retries=2, tr_cont="" if i == 0 else "N")''',
        'Balance retry and continuation header')

    source = replace_once(source,
        '''    try:
        result, _ = api_request("POST", "/uapi/domestic-stock/v1/trading/order-cash", token,
                                tr_id=tr_id, body=body, timeout=10, retries=0)''',
        '''    # As soon as a real order POST is attempted, later balance snapshots must refresh.
    global ACCOUNT_ORDER_ATTEMPTS
    ACCOUNT_ORDER_ATTEMPTS += 1
    try:
        result, _ = api_request("POST", "/uapi/domestic-stock/v1/trading/order-cash", token,
                                tr_id=tr_id, body=body, timeout=10, retries=0)''',
        'Track order submission')

    source = replace_once(source,
        '''        adopt_verified_legacy_baa(token, holdings)
        run_intraday(token, holdings, deposit, total_asset)
        # Avoid stale account snapshot after a daytrade order.
        assert_no_pending(token)
        holdings, deposit, tot_eval = get_account_balance(token)
        run_baa(token, holdings, deposit, tot_eval, force_run=FORCE_BAA)
        assert_no_pending(token)
        holdings, deposit, tot_eval = get_account_balance(token)
        handle_daily_closing_report(token, holdings, tot_eval)''',
        '''        adopt_verified_legacy_baa(token, holdings)
        before_intraday_orders = ACCOUNT_ORDER_ATTEMPTS
        run_intraday(token, holdings, deposit, total_asset)
        # Always reconcile before BAA; no automatic reorders on uncertain fills.
        assert_no_pending(token)
        # Refresh before BAA when the bot placed an order, or when BAA may trade.
        # In other windows, reuse the initial snapshot to avoid needless API calls.
        baa_time = now_kst().strftime("%H:%M")
        if (ACCOUNT_ORDER_ATTEMPTS != before_intraday_orders or FORCE_BAA
                or "11:00" <= baa_time <= "11:25"):
            holdings, deposit, tot_eval = get_account_balance(token)
        before_baa_orders = ACCOUNT_ORDER_ATTEMPTS
        run_baa(token, holdings, deposit, tot_eval, force_run=FORCE_BAA)
        assert_no_pending(token)
        if ACCOUNT_ORDER_ATTEMPTS != before_baa_orders:
            holdings, deposit, tot_eval = get_account_balance(token)
        handle_daily_closing_report(token, holdings, tot_eval)''',
        'Skip redundant balance snapshots')

    return source


def main() -> None:
    parser = argparse.ArgumentParser(description='Apply fail-closed KIS API balance fix')
    parser.add_argument('repo', type=Path, nargs='?', default=Path('.'),
                        help='Path to local clone of kis-30m-bot')
    parser.add_argument('--output', type=Path,
                        help='Optional output file instead of modifying original')
    args = parser.parse_args()
    target = args.repo / 'bot_once.py'
    data = target.read_bytes()
    digest = blob_hash(data)
    if digest != ORIGINAL_GIT_BLOB:
        raise SystemExit(f'Original blob mismatch ({digest}); refuse to patch. '
                         'Use source commit inspected on 2026-10-15, or request rebase.')
    result = patch(data.decode('utf-8'))
    compile(result, str(target), 'exec')
    destination = args.output if args.output is not None else target
    bundled_test = Path(__file__).with_name('test_api_resilience.py')
    if not bundled_test.exists():
        raise SystemExit('Test companion file test_api_resilience.py is missing')
    if args.output is None:
        workflow = args.repo / '.github' / 'workflows' / 'trade.yml'
        workflow_original = workflow.read_text(encoding='utf-8')
        workflow_patched = replace_once(
            workflow_original,
            '- run: python -m unittest -v test_bot_once.py',
            '- run: python -m unittest -v test_bot_once.py test_api_resilience.py',
            'GitHub Actions unit test step',
        )
        test_target = args.repo / 'test_api_resilience.py'
        if test_target.exists() and test_target.read_bytes() != bundled_test.read_bytes():
            raise SystemExit('Existing test_api_resilience.py differs; refuse to overwrite')
        destination.write_text(result, encoding='utf-8')
        test_target.write_bytes(bundled_test.read_bytes())
        workflow.write_text(workflow_patched, encoding='utf-8')
        print('Installed additional tests:', test_target)
        print('Extended workflow unittest command:', workflow)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result, encoding='utf-8')
    print('Patched:', destination)
    print('Original SHA:', digest)
    print('No workflow or live-order settings changed.')


if __name__ == '__main__':
    main()
