"""Additional tests for safe KIS balance/API resilience. No broker orders are sent.

Run in the kis-30m-bot repository AFTER applying apply_kis_balance_fix.py:
    python -m unittest -v test_bot_once.py test_api_resilience.py
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

import bot_once as bot


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else {"rt_cd": "0"}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._data


class APIResilienceTests(unittest.TestCase):
    def setUp(self):
        self.old_token = bot.ACCESS_TOKEN
        bot.ACCESS_TOKEN = None
        self.old_attempts = bot.ACCOUNT_ORDER_ATTEMPTS
        bot.ACCOUNT_ORDER_ATTEMPTS = 0

    def tearDown(self):
        bot.ACCESS_TOKEN = self.old_token
        bot.ACCOUNT_ORDER_ATTEMPTS = self.old_attempts

    def test_get_recovers_from_transient_500(self):
        responses = [FakeResponse(500), FakeResponse(200, {"rt_cd": "0", "value": "ok"})]
        with patch.object(bot.SESSION, 'request', side_effect=responses) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            payload, _ = bot.api_request('GET', '/read', 'token')
        self.assertEqual(payload['value'], 'ok')
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_balance_get_retries_twice_on_503_then_succeeds(self):
        success = FakeResponse(200, {
            "rt_cd": "0", "output1": [],
            "output2": [{"dnca_tot_amt": "300159", "tot_evlu_amt": "964989"}],
        })
        with patch.object(bot.SESSION, 'request', side_effect=[
                FakeResponse(503), FakeResponse(503), success]) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            _, cash, nav = bot.get_account_balance('token')
        self.assertEqual((cash, nav), (300159, 964989))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_get_401_refreshes_token_once(self):
        responses = [FakeResponse(401), FakeResponse(200)]
        with patch.object(bot.SESSION, 'request', side_effect=responses) as request, \
             patch.object(bot, 'get_access_token', return_value='NEW-TOKEN') as token, \
             patch.object(bot.time, 'sleep'):
            bot.api_request('GET', '/read', 'OLD-TOKEN')
        self.assertEqual(request.call_count, 2)
        token.assert_called_once_with(force=True)
        self.assertEqual(request.call_args_list[1].kwargs['headers']['authorization'], 'Bearer NEW-TOKEN')

    def test_balance_persistent_503_halts_after_three_requests(self):
        with patch.object(bot.SESSION, 'request', return_value=FakeResponse(503)) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            with self.assertRaisesRegex(bot.TradeSafetyError, 'HTTP 503'):
                bot.get_account_balance('token')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_explicit_continuation_header_forwarded(self):
        with patch.object(bot.SESSION, 'request', return_value=FakeResponse()) as request:
            bot.api_request('GET', '/read', 'token', tr_cont='N')
        self.assertEqual(request.call_args.kwargs['headers']['tr_cont'], 'N')

    def test_get_no_retry_on_403(self):
        with patch.object(bot.SESSION, 'request', side_effect=[FakeResponse(403), FakeResponse(200)]) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('GET', '/read', 'token', retries=2)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_get_no_retry_on_business_rejection(self):
        response = FakeResponse(200, {"rt_cd": "1", "msg_cd": "ERR", "msg1": "rejected"})
        with patch.object(bot.SESSION, 'request', return_value=response) as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('GET', '/read', 'token', retries=2)
        self.assertEqual(request.call_count, 1)

    def test_post_never_retried_even_on_500(self):
        with patch.object(bot.SESSION, 'request', side_effect=[FakeResponse(500), FakeResponse(200)]) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('POST', '/uapi/domestic-stock/v1/trading/order-cash',
                                'token', retries=5)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_429_respects_short_retry_after(self):
        responses = [FakeResponse(429, headers={"Retry-After": "2"}), FakeResponse(200)]
        with patch.object(bot.SESSION, 'request', side_effect=responses) as request, \
             patch.object(bot.time, 'sleep') as sleep:
            bot.api_request('GET', '/read', 'token')
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_timeout_get_retry_but_post_not_retry(self):
        with patch.object(bot.SESSION, 'request', side_effect=[requests.Timeout(), FakeResponse(200)]) as req, \
             patch.object(bot.time, 'sleep'):
            bot.api_request('GET', '/read', 'token')
        self.assertEqual(req.call_count, 2)
        with patch.object(bot.SESSION, 'request', side_effect=requests.Timeout()) as req:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('POST', '/order', 'token')
        self.assertEqual(req.call_count, 1)

    def test_retry_error_does_not_leak_account_number(self):
        # requests itself often adds query strings to the HTTPError message.
        account = '12345678'
        response = FakeResponse(500)
        with patch.object(bot.SESSION, 'request', return_value=response), \
             patch.object(bot.time, 'sleep'):
            with self.assertRaises(bot.TradeSafetyError) as ctx:
                bot.api_request('GET', '/balance', 'token', params={'CANO': account})
        self.assertNotIn(account, str(ctx.exception))

    def test_balance_pagination_uses_official_tr_cont(self):
        first = ({"output1": [{"pdno": "423160", "hldg_qty": "6", "pchs_avg_pric": "109000"}],
                  "output2": [{"dnca_tot_amt": "300159", "tot_evlu_amt": "964989"}],
                  "ctx_area_fk100": "FK", "ctx_area_nk100": "NK"}, {"tr_cont": "M"})
        second = ({"output1": [], "output2": [],
                   "ctx_area_fk100": "", "ctx_area_nk100": ""}, {"tr_cont": "D"})
        with patch.object(bot, 'api_request', side_effect=[first, second]) as api, \
             patch.object(bot.time, 'sleep'):
            holdings, cash, nav = bot.get_account_balance('token')
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0].kwargs['tr_cont'], '')
        self.assertEqual(api.call_args_list[1].kwargs['tr_cont'], 'N')
        self.assertEqual(api.call_args_list[1].kwargs['params']['CTX_AREA_NK100'], 'NK')
        self.assertEqual(holdings['423160']['qty'], 6)
        self.assertEqual((cash, nav), (300159, 964989))

    def _run_main(self, hhmm, *, swing_order=False, baa_order=False):
        date_obj = bot.KST.localize(datetime(2026, 10, 15, int(hhmm[:2]), int(hhmm[3:])))
        def run_swing(*args, **kwargs):
            if swing_order:
                bot.ACCOUNT_ORDER_ATTEMPTS += 1
        def run_baa(*args, **kwargs):
            if baa_order:
                bot.ACCOUNT_ORDER_ATTEMPTS += 1
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(bot, 'LOCK_FILE', Path(folder) / '.test.lock'), \
             patch.object(bot, 'validate_environment'), \
             patch.object(bot, 'now_kst', return_value=date_obj), \
             patch.object(bot, 'get_access_token', return_value='TOKEN'), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending') as pending, \
             patch.object(bot, 'get_account_balance', return_value=({}, 300159, 964989)) as balance, \
             patch.object(bot, 'load_json', return_value={}), \
             patch.object(bot, 'save_json'), \
             patch.object(bot, 'adopt_verified_legacy_baa'), \
             patch.object(bot, 'run_swing', side_effect=run_swing), \
             patch.object(bot, 'run_baa', side_effect=run_baa), \
             patch.object(bot, 'handle_daily_closing_report'):
            bot.main()
            return balance.call_count, pending.call_count

    def test_no_orders_outside_baa_window_one_balance_lookup(self):
        balance_count, pending_count = self._run_main('11:38')
        self.assertEqual(balance_count, 1)
        self.assertEqual(pending_count, 3)

    def test_baa_trading_window_has_fresh_balance(self):
        balance_count, pending_count = self._run_main('11:10')
        self.assertEqual(balance_count, 2)
        self.assertEqual(pending_count, 3)

    def test_swing_order_attempt_forces_balance_refresh(self):
        balance_count, _ = self._run_main('10:30', swing_order=True)
        self.assertEqual(balance_count, 2)

    def test_baa_order_attempt_forces_post_order_balance_refresh(self):
        balance_count, _ = self._run_main('11:10', baa_order=True)
        self.assertEqual(balance_count, 3)

    def test_closing_report_without_orders_uses_initial_balance(self):
        balance_count, _ = self._run_main('16:10')
        self.assertEqual(balance_count, 1)


if __name__ == '__main__':
    unittest.main()
