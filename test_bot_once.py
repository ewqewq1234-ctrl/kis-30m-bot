"""Offline safety regression tests for bot_once.py.

Run: python -m unittest -v test_bot_once.py
All external broker and Telegram calls are mocked. No live orders are sent.
"""
import json
import requests
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# Importing the module does not execute main().
import bot_once as bot


class BotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        start_date_patcher = patch.object(
            bot, 'LIVE_TRADING_START_DATE', datetime(2026, 10, 14).date()
        )
        start_date_patcher.start()
        self.addCleanup(start_date_patcher.stop)
        base = Path(self.tmp.name)
        for name in ('BASE_DIR', 'DAY_STATE_FILE', 'BAA_STATE_FILE', 'TRADE_HISTORY_FILE', 'RISK_STATE_FILE'):
            value = base if name == 'BASE_DIR' else base / {
                'DAY_STATE_FILE': 'day_state.json',
                'BAA_STATE_FILE': 'baa_state.json',
                'TRADE_HISTORY_FILE': 'trade_history.json',
                'RISK_STATE_FILE': 'risk_state.json',
            }[name]
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'GITHUB_STATE_MODE': 'off'})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bot, 'send_telegram')
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, data):
        (Path(self.tmp.name) / name).write_text(json.dumps(data), encoding='utf-8')

    def test_default_order_gates_block_live(self):
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'ENABLE_LIVE_ORDERS', False), \
             patch.object(bot, 'KIS_ENV', 'real'), patch.object(bot, 'KIS_APP_KEY', 'fake'), \
             patch.object(bot, 'KIS_APP_SECRET', 'fake'), patch.object(bot, 'KIS_CANO', '12345678'):
            with self.assertRaises(bot.TradeSafetyError):
                bot.validate_environment()

    def test_dry_run_never_posts_order_or_writes_intent(self):
        with patch.object(bot, 'DRY_RUN', True), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', return_value=10), \
             patch.object(bot, 'api_request') as request:
            result = bot.execute_order('fake', '423160', 2, '02', action_name='테스트', strategy='BAA')
        self.assertEqual(result, (False, 0))
        request.assert_not_called()
        self.assertFalse(bot.TRADE_HISTORY_FILE.exists())

    def test_dry_run_still_rejects_oversized_buy(self):
        with patch.object(bot, 'DRY_RUN', True), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', return_value=1), \
             patch.object(bot, 'api_request') as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '423160', 2, '02', action_name='테스트', strategy='BAA')
        request.assert_not_called()

    def test_uncertain_post_outcome_keeps_intent_and_does_not_retry(self):
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', return_value=10), \
             patch.object(bot, 'api_request', side_effect=bot.TradeSafetyError('timeout')) as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '423160', 2, '02', action_name='테스트', strategy='BAA')
        self.assertEqual(request.call_count, 1)
        record = bot.read_journal()['orders'][0]
        self.assertEqual(record['status'], 'UNCERTAIN')
        self.assertEqual(record['requested_qty'], 2)

    def test_uncertain_intent_without_order_number_blocks_followup(self):
        self.write('trade_history.json', {'orders': [{
            'id': 'x', 'strategy': 'BAA', 'code': '423160', 'side': 'BUY',
            'requested_qty': 2, 'confirmed_fill_qty': 0, 'status': 'UNCERTAIN',
            'created_at': bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'), 'odno': None,
        }]})
        with patch.object(bot, 'get_order_records', return_value=[]):
            with self.assertRaisesRegex(bot.TradeSafetyError, '미확정/미체결'):
                bot.assert_no_pending('fake')

    def test_reconcile_confirmed_fill_rebuilds_owned_qty(self):
        self.write('trade_history.json', {'orders': [{
            'id': 'x', 'strategy': 'BAA', 'code': '423160', 'side': 'BUY',
            'requested_qty': 6, 'confirmed_fill_qty': 0, 'status': 'ACCEPTED',
            'created_at': bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'), 'odno': '000123',
        }]})
        rows = [{'odno': '123', 'pdno': '423160', 'ord_qty': '6',
                 'tot_ccld_qty': '6', 'tot_ccld_amt': '664800'}]
        with patch.object(bot, 'get_order_records', return_value=rows):
            bot.reconcile_orders('fake')
            bot.reconcile_orders('fake')
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'], 6)
        self.assertEqual(bot.read_journal()['orders'][0]['status'], 'FILLED')

    def test_legacy_position_is_not_silently_adopted(self):
        self.write('baa_state.json', {'target_code': None, 'owned_qty': 0})
        now = bot.now_kst().replace(hour=11, minute=10, second=0)
        with patch.object(bot, 'now_kst', return_value=now), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=110800), \
             patch.object(bot, 'execute_order') as order:
            with self.assertRaisesRegex(bot.TradeSafetyError, '출처 미확인'):
                bot.run_baa('fake', {'423160': {'qty': 6, 'avg_price': 110785}}, 300159, 964959)
        order.assert_not_called()

    def test_order_outside_market_is_blocked(self):
        with patch.object(bot, 'order_window', return_value=False), \
             patch.object(bot, 'api_request') as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '423160', 1, '02')
        request.assert_not_called()

    def test_duplicate_order_intent_is_blocked(self):
        key = bot.order_key('423160', 'BUY', 'BAA', '테스트')
        self.write('trade_history.json', {'orders': [{'id': key, 'status': 'FILLED'}]})
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', return_value=10), \
             patch.object(bot, 'api_request') as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, '동일 주문 의도'):
                bot.execute_order('fake', '423160', 1, '02', action_name='테스트', strategy='BAA')
        request.assert_not_called()


    def test_approved_legacy_holding_is_adopted_without_order(self):
        holdings = {'423160': {'qty': 6, 'avg_price': 110785}}
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending') as pending, \
             patch.object(bot, 'api_request') as api:
            self.assertTrue(bot.adopt_verified_legacy_baa('fake', holdings))
        pending.assert_called_once()
        api.assert_not_called()
        rows = bot.read_journal()['orders']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], 'LEGACY_POSITION_ADOPTION_NO_ORDER')
        self.assertEqual(rows[0]['confirmed_fill_qty'], 6)
        state = bot.load_json(bot.BAA_STATE_FILE, {})
        self.assertEqual(state['target_code'], '423160')
        self.assertEqual(state['owned_qty'], 6)

    def test_legacy_adoption_is_idempotent_across_restarts(self):
        holdings = {'423160': {'qty': 6, 'avg_price': 110785}}
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending'):
            self.assertTrue(bot.adopt_verified_legacy_baa('fake', holdings))
            self.assertFalse(bot.adopt_verified_legacy_baa('fake', holdings))
            bot.rebuild_strategy_state()
        self.assertEqual(len(bot.read_journal()['orders']), 1)
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'], 6)

    def test_migration_never_on_live_run(self):
        with patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'assert_no_pending') as pending:
            result = bot.adopt_verified_legacy_baa(
                'fake', {'423160': {'qty': 6, 'avg_price': 110785}})
        self.assertFalse(result)
        pending.assert_not_called()
        self.assertFalse(bot.TRADE_HISTORY_FILE.exists())

    def test_migration_rejects_wrong_quantity(self):
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending') as pending:
            with self.assertRaisesRegex(bot.TradeSafetyError, '수량'):
                bot.adopt_verified_legacy_baa(
                    'fake', {'423160': {'qty': 5, 'avg_price': 110785}})
        pending.assert_not_called()
        self.assertFalse(bot.TRADE_HISTORY_FILE.exists())

    def test_migration_rejects_unrelated_holding(self):
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending') as pending:
            self.assertFalse(bot.adopt_verified_legacy_baa('fake', {
                '423160': {'qty': 6, 'avg_price': 110785},
                '069500': {'qty': 1, 'avg_price': 95000},
            }))
        pending.assert_not_called()
        self.assertFalse(bot.TRADE_HISTORY_FILE.exists())

    def test_migration_rejects_preexisting_baa_journal(self):
        self.write('trade_history.json', {'orders': [{
            'id': 'historic-baa', 'strategy': 'BAA', 'code': '423160',
            'side': 'BUY', 'requested_qty': 1, 'confirmed_fill_qty': 1,
            'status': 'FILLED',
        }]})
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending') as pending:
            with self.assertRaisesRegex(bot.TradeSafetyError, '기존 BAA 주문 기록'):
                bot.adopt_verified_legacy_baa(
                    'fake', {'423160': {'qty': 6, 'avg_price': 110785}})
        pending.assert_not_called()
        self.assertEqual(len(bot.read_journal()['orders']), 1)

    def test_migration_rejects_changed_avg_price(self):
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'assert_no_pending') as pending:
            with self.assertRaisesRegex(bot.TradeSafetyError, '매입단가'):
                bot.adopt_verified_legacy_baa(
                    'fake', {'423160': {'qty': 6, 'avg_price': 110000}})
        pending.assert_not_called()
        self.assertFalse(bot.TRADE_HISTORY_FILE.exists())




class FullCoverageTests(unittest.TestCase):
    """Offline scenario tests; all KIS network and order execution mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        start_date_patcher = patch.object(
            bot, 'LIVE_TRADING_START_DATE', datetime(2026, 10, 14).date()
        )
        start_date_patcher.start()
        self.addCleanup(start_date_patcher.stop)
        base = Path(self.tmp.name)
        files = {
            'BASE_DIR': base,
            'DAY_STATE_FILE': base / 'day_state.json',
            'BAA_STATE_FILE': base / 'baa_state.json',
            'TRADE_HISTORY_FILE': base / 'trade_history.json',
            'RISK_STATE_FILE': base / 'risk_state.json',
            'HOLIDAY_CACHE_FILE': base / 'holiday_cache.json',
            'SECURE_TOKEN_FILE': base / 'token_cache.secure',
            'LOCK_FILE': base / '.bot.lock',
        }
        for name, value in files.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.dict(os.environ, {'GITHUB_STATE_MODE': 'off'}),
                  patch.object(bot, 'send_telegram'),
                  patch.object(bot.SESSION, 'request', side_effect=AssertionError('UNMOCKED NETWORK REQUEST'))):
            p.start()
            self.addCleanup(p.stop)

    def write(self, filename, obj):
        (Path(self.tmp.name) / filename).write_text(json.dumps(obj), encoding='utf-8')

    def journal(self, entries):
        self.write('trade_history.json', {'orders': entries})

    def owned(self):
        self.journal([{
            'id': bot.LEGACY_BAA_ID, 'strategy': 'BAA', 'side': 'BUY',
            'code': '423160', 'status': 'FILLED', 'requested_qty': 6,
            'confirmed_fill_qty': 6, 'confirmed_fill_amt': 664707,
            'source': 'LEGACY_POSITION_ADOPTION_NO_ORDER',
        }])
        bot.rebuild_strategy_state()

    def fixed_time(self, hour=11, minute=10):
        return patch.object(bot, 'now_kst', return_value=bot.KST.localize(datetime(2026, 10, 14, hour, minute)))

    def test_baa_dry_run_existing_six_calculates_trim_without_order(self):
        self.owned()
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=110800), \
             patch.object(bot, 'execute_order') as order:
            bot.run_baa('fake', {'423160': {'qty': 6, 'avg_price': 110785}}, 300159, 964929)
        order.assert_not_called()
        state = bot.load_json(bot.BAA_STATE_FILE, {})
        self.assertEqual(state['owned_qty'], 6)
        self.assertFalse(state.get('last_executed_date'))
        self.assertEqual(int(964929 * .8 * .96 // 110800), 6)

    def test_baa_bearish_half_allocation_dry_run_preserves_six(self):
        self.owned()
        prices = [200.] * 192 + [100.] * 60  # SMA20 < SMA60? all last 60 equal: no
        prices = [200.] * 232 + [100.] * 20   # SMA20 below SMA60
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], prices)), \
             patch.object(bot, 'get_current_price', return_value=110800), \
             patch.object(bot, 'execute_order') as order:
            bot.run_baa('fake', {'423160': {'qty': 6, 'avg_price': 110785}}, 300159, 964929)
        order.assert_not_called()
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'], 6)
        self.assertEqual(int(964929 * .8 * .5 * .96 // 110800), 3)

    def test_baa_live_mock_trims_exactly_one_without_real_api(self):
        self.owned()
        calls = []
        def fake_order(token, code, qty, order_type, **kwargs):
            calls.append((code, qty, order_type))
            self.assertEqual((code, qty, order_type), ('423160', 1, '01'))
            data = bot.read_journal()
            data['orders'].append({'id':'sell-1', 'strategy':'BAA','side':'SELL',
                                   'code':'423160','status':'FILLED','requested_qty':qty,
                                   'confirmed_fill_qty':qty,'confirmed_fill_amt':110800})
            bot.save_json(bot.TRADE_HISTORY_FILE, data)
            bot.rebuild_strategy_state()
            return True, qty
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=130000), \
             patch.object(bot, 'execute_order', side_effect=fake_order), \
             patch.object(bot, 'get_account_balance', return_value=({'423160':{'qty':5,'avg_price':110785}}, 410959, 964929)), \
             patch.object(bot, 'assert_no_pending'):
            bot.run_baa('fake', {'423160': {'qty':6,'avg_price':110785}}, 300159, 964929)
        self.assertEqual(calls, [('423160', 1, '01')])
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'], 5)
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['last_executed_date'], '2026-10-14')

    def test_baa_live_mock_old_six_sold_before_buy_new_target(self):
        self.owned()
        calls=[]
        holdings={'423160':{'qty':6,'avg_price':110785}}
        def fake_order(token, code, qty, order_type, **kwargs):
            calls.append((code, qty, order_type))
            side='SELL' if order_type=='01' else 'BUY'
            data=bot.read_journal()
            data['orders'].append({'id':'mock-'+str(len(calls)), 'strategy':'BAA', 'side':side,
                                   'code':code,'status':'FILLED','requested_qty':qty,
                                   'confirmed_fill_qty':qty,'confirmed_fill_amt':qty*100000})
            bot.save_json(bot.TRADE_HISTORY_FILE, data)
            bot.rebuild_strategy_state()
            return True, qty
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=(
                 '449170', bot.DEFENSE_UNIVERSE['449170'], True, [], [100.] * 252
             )), \
             patch.object(bot, 'get_current_price', return_value=100000), \
             patch.object(bot, 'execute_order', side_effect=fake_order), \
             patch.object(bot, 'get_account_balance', return_value=({}, 964929, 964929)), \
             patch.object(bot, 'assert_no_pending'):
            bot.run_baa('fake', holdings, 300159, 964929)
        self.assertEqual(calls, [('423160', 6, '01'), ('449170', 7, '02')])
        state=bot.load_json(bot.BAA_STATE_FILE, {})
        self.assertEqual((state['target_code'], state['owned_qty']), ('449170', 7))

    def test_baa_broker_balance_mismatch_aborts_before_any_sale(self):
        self.owned()
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=110800), \
             patch.object(bot, 'execute_order') as order:
            with self.assertRaisesRegex(bot.TradeSafetyError, '불일치'):
                bot.run_baa('fake', {'423160': {'qty': 5, 'avg_price': 110785}}, 300159, 964929)
        order.assert_not_called()

    def test_baa_extra_untracked_asset_stops_order(self):
        self.owned()
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=110800), \
             patch.object(bot, 'execute_order') as order:
            with self.assertRaisesRegex(bot.TradeSafetyError, '출처 미확인'):
                bot.run_baa('fake', {'423160': {'qty': 6}, '411060': {'qty': 1}}, 300159, 964929)
        order.assert_not_called()

    def test_baa_blocks_intraday_code_even_if_strategy_selects_it(self):
        self.owned()
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('069500', 'KODEX200', False, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=95000), \
             patch.object(bot, 'execute_order') as order:
            with self.assertRaisesRegex(bot.TradeSafetyError, '계좌 내 물량 식별 불가'):
                bot.run_baa('fake', {'423160': {'qty': 6}}, 300159, 964929)
        order.assert_not_called()

    def test_baa_13_day_guard_skips_rebalancing(self):
        self.owned()
        state=bot.load_json(bot.BAA_STATE_FILE, {})
        state['last_executed_date']='2026-10-13'
        bot.save_json(bot.BAA_STATE_FILE, state)
        with self.fixed_time(), patch.object(bot, 'select_baa_target') as choose, \
             patch.object(bot, 'execute_order') as order:
            bot.run_baa('fake', {'423160': {'qty': 6}}, 300159, 964929)
        choose.assert_not_called(); order.assert_not_called()

    def test_baa_window_guard_skips_outside(self):
        self.owned()
        with self.fixed_time(11, 26), patch.object(bot, 'select_baa_target') as choose:
            bot.run_baa('fake', {'423160': {'qty': 6}}, 300159, 964929)
        choose.assert_not_called()

    def test_baa_mock_sale_failure_leaves_execution_date_unset(self):
        self.owned()
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=130000), \
             patch.object(bot, 'execute_order', side_effect=bot.TradeSafetyError('partial fill')):
            with self.assertRaisesRegex(bot.TradeSafetyError, 'partial fill'):
                bot.run_baa('fake', {'423160':{'qty':6}}, 300159, 964929)
        self.assertFalse(bot.load_json(bot.BAA_STATE_FILE, {}).get('last_executed_date'))

    def test_baa_live_mock_target_already_met_does_not_send_order(self):
        self.owned()
        # Choose a target price where 6 shares exactly match the 80%*96% budget.
        price=110800
        self.assertEqual(int(964929*.8*.96//price), 6)
        with self.fixed_time(), patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'select_baa_target', return_value=('423160', 'KOFR', True, [], [100.] * 252)), \
             patch.object(bot, 'get_current_price', return_value=price), \
             patch.object(bot, 'execute_order') as order, \
             patch.object(bot, 'assert_no_pending'):
            bot.run_baa('fake', {'423160':{'qty':6}}, 300159, 964929)
        order.assert_not_called()
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'], 6)

    def test_sell_more_than_broker_holdings_fails_without_post(self):
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), \
             patch.object(bot, 'get_account_balance', return_value=({'423160':{'qty':2}},0,100000)), \
             patch.object(bot, 'api_request') as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, '보유보다 많은 매도'):
                bot.execute_order('fake','423160',3,'01',strategy='BAA')
        request.assert_not_called()

    def test_sell_market_order_request_contains_correct_side_and_quantity(self):
        self.owned()
        captured=[]
        def fake_api(method,path,token=None,**kwargs):
            captured.append((method,path,kwargs))
            return ({'output':{'ODNO':'00001234'}}, {})
        filled=[{'odno':'1234','pdno':'423160','ord_qty':'1',
                 'tot_ccld_qty':'1','tot_ccld_amt':'110800'}]
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), \
             patch.object(bot, 'get_account_balance', return_value=({'423160':{'qty':6}},300159,964929)), \
             patch.object(bot, 'api_request', side_effect=fake_api), \
             patch.object(bot, 'get_order_records', return_value=filled), \
             patch.object(bot.time, 'sleep'):
            self.assertEqual(bot.execute_order('fake','423160',1,'01',strategy='BAA', action_name='trim'),(True,1))
        self.assertEqual(len(captured),1)
        method,path,kwargs=captured[0]
        self.assertEqual(method,'POST')
        self.assertEqual(kwargs['tr_id'],'TTTC0011U')
        self.assertEqual(kwargs['body']['ORD_QTY'],'1')
        self.assertEqual(kwargs['body']['ORD_DVSN'],'01')
        self.assertEqual(kwargs['body']['ORD_UNPR'],'0')
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'],5)

    def test_partial_sell_records_filled_qty_and_blocks_followup(self):
        self.owned()
        row={'odno':'100','pdno':'423160','ord_qty':'2','tot_ccld_qty':'1',
             'tot_ccld_amt':'110800','cncl_cfrm_qty':'0'}
        def fake_api(method,path,token=None,**kwargs):
            return {'output':{'ODNO':'000100'}},{}
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), \
             patch.object(bot, 'get_account_balance', return_value=({'423160':{'qty':6}},300159,964929)), \
             patch.object(bot, 'api_request', side_effect=fake_api) as api, \
             patch.object(bot, 'get_order_records', return_value=[row]), \
             patch.object(bot.time, 'sleep'):
            with self.assertRaisesRegex(bot.TradeSafetyError, '부분체결'):
                bot.execute_order('fake','423160',2,'01',strategy='BAA',action_name='partial')
        rec=bot.read_journal()['orders'][-1]
        self.assertEqual((rec['status'],rec['confirmed_fill_qty']),('PARTIAL',1))
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})['owned_qty'],5)
        self.assertEqual(api.call_count,1)
        with patch.object(bot, 'get_order_records', return_value=[row]):
            with self.assertRaisesRegex(bot.TradeSafetyError, '미확정/미체결'):
                bot.assert_no_pending('fake')

    def test_timeout_does_not_repeat_buy_order(self):
        with patch.object(bot, 'DRY_RUN', False), patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', return_value=10), \
             patch.object(bot, 'api_request', side_effect=bot.TradeSafetyError('timeout')) as api:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake','449170',2,'02',strategy='BAA',action_name='timeout')
            with self.assertRaisesRegex(bot.TradeSafetyError, '동일 주문 의도'):
                bot.execute_order('fake','449170',2,'02',strategy='BAA',action_name='timeout')
        self.assertEqual(api.call_count,1)

    def test_reconcile_partial_then_complete_no_double_count(self):
        self.journal([{'id':'o1','strategy':'BAA','side':'BUY','code':'449170',
                       'requested_qty':3,'status':'ACCEPTED','confirmed_fill_qty':0,
                       'created_at':bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'), 'odno':'001'}])
        def row(filled):
            return [{'odno':'1','pdno':'449170','ord_qty':'3',
                     'tot_ccld_qty':str(filled),'tot_ccld_amt':str(filled*100000)}]
        with patch.object(bot,'get_order_records',side_effect=[row(1),row(3)]):
            bot.reconcile_orders('fake')
            self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],1)
            bot.reconcile_orders('fake')
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],3)
        bot.rebuild_strategy_state()
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],3)

    def test_reconcile_qty_regression_blocks(self):
        self.journal([{'id':'o1','strategy':'BAA','side':'BUY','code':'449170',
                       'requested_qty':3,'status':'PARTIAL','confirmed_fill_qty':2,
                       'created_at':bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'), 'odno':'001'}])
        with patch.object(bot,'get_order_records',return_value=[{'odno':'1','pdno':'449170',
                            'ord_qty':'3','tot_ccld_qty':'1','tot_ccld_amt':'100000'}]):
            with self.assertRaisesRegex(bot.TradeSafetyError,'역행'):
                bot.reconcile_orders('fake')

    def test_overnight_uncertain_blocks_automatic_recovery(self):
        self.journal([{'id':'o1','strategy':'BAA','side':'BUY','code':'449170',
                       'requested_qty':3,'status':'ACCEPTED','confirmed_fill_qty':0,
                       'created_at':'2026-10-13 11:10:00', 'odno':'001'}])
        with self.fixed_time(), patch.object(bot, 'get_order_records') as api:
            with self.assertRaisesRegex(bot.TradeSafetyError,'전일'):
                bot.reconcile_orders('fake')
        api.assert_not_called()

    def test_external_open_broker_order_blocks_new(self):
        with patch.object(bot,'get_order_records',return_value=[{'ord_qty':'3',
                        'tot_ccld_qty':'1','cncl_cfrm_qty':'0'}]):
            with self.assertRaisesRegex(bot.TradeSafetyError,'미체결 주문'):
                bot.assert_no_pending('fake')

    def test_confirmed_broker_cancellation_no_open_remainder(self):
        """BUG DETECTOR: cancellation should not leave a permanent PARTIAL."""
        self.journal([{'id':'canceled-partial','strategy':'BAA','side':'BUY',
            'code':'449170','requested_qty':3,'status':'PARTIAL','confirmed_fill_qty':1,
            'created_at':bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'),'odno':'777'}])
        records=[{'odno':'777','pdno':'449170','ord_qty':'3',
                  'tot_ccld_qty':'1','tot_ccld_amt':'100000','cncl_cfrm_qty':'2'}]
        with patch.object(bot,'get_order_records',return_value=records):
            bot.assert_no_pending('fake')
        self.assertEqual(bot.read_journal()['orders'][0]['status'],'CANCELED')
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],1)

    def test_broker_order_not_covered_by_journal_is_blocked(self):
        with patch.object(bot,'get_order_records',return_value=[{'ord_qty':'1',
                        'tot_ccld_qty':'0','cncl_cfrm_qty':'0'}]):
            with self.assertRaisesRegex(bot.TradeSafetyError,'미체결 주문'):
                bot.assert_no_pending('fake')

    def test_risk_daily_loss_buy_blocks(self):
        self.write('risk_state.json', {'date':bot.now_kst().strftime('%Y-%m-%d'),'opening_nav':1000000})
        with patch.object(bot,'get_account_balance',return_value=({},0,970000)):
            with self.assertRaisesRegex(bot.TradeSafetyError,'손실한도'):
                bot.check_daily_loss('fake')

    def test_risk_daily_loss_sell_allowed(self):
        with patch.object(bot,'get_account_balance') as account:
            bot.check_daily_loss('fake',allow_sell=True)
        account.assert_not_called()

    def test_risk_missing_snapshot_blocks_new_buy(self):
        with patch.object(bot,'get_account_balance',return_value=({},0,1000000)):
            with self.assertRaisesRegex(bot.TradeSafetyError,'스냅샷'):
                bot.check_daily_loss('fake')

    def test_damaged_journal_fails_closed(self):
        (Path(self.tmp.name)/'trade_history.json').write_text('{broken',encoding='utf-8')
        with self.assertRaises(bot.TradeSafetyError):
            bot.rebuild_strategy_state()

    def test_git_push_failure_blocks_write_and_prevents_order(self):
        """Simulate git failure while persisting an INTENT before POST."""
        with patch.dict(os.environ, {'GITHUB_STATE_MODE':'git'}), \
             patch.object(bot.subprocess,'run',side_effect=OSError('git offline')):
            # Current implementation should fail closed, but may throw OSError.
            with self.assertRaises((bot.TradeSafetyError,OSError)):
                bot.save_json(bot.TRADE_HISTORY_FILE,{'orders':[]})

    def test_token_encrypted_roundtrip_and_no_plaintext(self):
        from time import time as epoch_time
        with patch.object(bot,'KIS_APP_KEY','fake-key'), patch.object(bot,'KIS_APP_SECRET','fake-secret'):
            bot._save_secure_token('super-secret-demo-token',int(epoch_time())+3600)
            recovered=bot._read_secure_token()
        self.assertEqual(recovered['token'],'super-secret-demo-token')
        self.assertNotIn(b'super-secret-demo-token',bot.SECURE_TOKEN_FILE.read_bytes())

    def test_tampered_encrypted_token_rejected(self):
        with patch.object(bot,'KIS_APP_KEY','fake-key'), patch.object(bot,'KIS_APP_SECRET','fake-secret'):
            bot._save_secure_token('super-secret-demo-token',1234567890)
            blob=bytearray(bot.SECURE_TOKEN_FILE.read_bytes())
            blob[-33] ^= 0x55
            bot.SECURE_TOKEN_FILE.write_bytes(bytes(blob))
            with self.assertRaisesRegex(bot.TradeSafetyError,'인증 실패'):
                bot._read_secure_token()

    def test_token_cache_reused_without_issuing_again(self):
        with patch.object(bot,'KIS_APP_KEY','fake-key'), patch.object(bot,'KIS_APP_SECRET','fake-secret'), \
             patch.object(bot,'ACCESS_TOKEN',None), patch.object(bot,'api_request') as api:
            bot._save_secure_token('cached-token',int(bot.time.time())+7200)
            value=bot.get_access_token()
        self.assertEqual(value,'cached-token'); api.assert_not_called()

    def test_api_get_retries_but_post_never_retries(self):
        from requests import Response
        good=Response(); good.status_code=200; good._content=b'{"rt_cd":"0"}'
        with patch.object(bot.SESSION,'request',side_effect=[requests.exceptions.Timeout(),good]) as session, \
             patch.object(bot.time,'sleep'):
            self.assertEqual(bot.api_request('GET','/test',retries=1)[0]['rt_cd'],'0')
        self.assertEqual(session.call_count,2)
        with patch.object(bot.SESSION,'request',side_effect=requests.exceptions.Timeout()) as session:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('POST','/test',retries=3)
        self.assertEqual(session.call_count,1)

    def test_baa_zero_history_is_not_silently_accepted(self):
        self.write('baa_state.json',{'target_code':'423160','owned_qty':6})
        with self.assertRaisesRegex(bot.TradeSafetyError,'체결 저널이 없음'):
            bot.rebuild_strategy_state()




class StrategyAndIntegrationTests(unittest.TestCase):
    """Additional strategy and API edge cases; isolated fake account only."""
    setUp = FullCoverageTests.setUp
    write = FullCoverageTests.write
    journal = FullCoverageTests.journal
    owned = FullCoverageTests.owned
    fixed_time = FullCoverageTests.fixed_time

    def test_intraday_entry_signal_creates_one_share_intent_only(self):
        with self.fixed_time(10, 20), patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot,'get_current_price',return_value=95000), \
             patch.object(bot,'check_daytrade_signal',return_value=True), \
             patch.object(bot,'execute_order',return_value=(False,0)) as order:
            bot.run_intraday('fake', {}, 300159, 964929)
        order.assert_called_once()
        args,kw=order.call_args
        self.assertEqual((args[1],args[2],args[3]),('069500',1,'02'))
        self.assertEqual(kw['strategy'],'DAY')

    def test_intraday_exit_profit_threshold_places_sell(self):
        self.write('day_state.json', {'owned_qty':2, 'entry_time':'2026-10-14 09:10'})
        with self.fixed_time(10,20),patch.object(bot,'get_current_price',return_value=101300),\
             patch.object(bot,'execute_order') as order:
            bot.run_intraday('fake', {'069500':{'qty':2,'avg_price':100000}}, 50000, 250000)
        order.assert_called_once()
        args,kwargs=order.call_args
        self.assertEqual((args[1],args[2],args[3]),('069500',2,'01'))
        self.assertEqual(kwargs['action_name'],'목표익절')

    def test_intraday_exit_stop_loss_threshold_places_sell(self):
        self.write('day_state.json', {'owned_qty':2, 'entry_time':'2026-10-14 09:10'})
        with self.fixed_time(10,20),patch.object(bot,'get_current_price',return_value=99000),\
             patch.object(bot,'execute_order') as order:
            bot.run_intraday('fake', {'069500':{'qty':2,'avg_price':100000}}, 50000, 250000)
        order.assert_called_once()
        self.assertEqual(order.call_args.kwargs['action_name'],'칼손절')

    def test_intraday_rejects_untracked_shares_without_order(self):
        with self.fixed_time(10,20),patch.object(bot,'execute_order') as order:
            bot.run_intraday('fake', {'069500':{'qty':1,'avg_price':95000}},300159,964929)
        order.assert_not_called()

    def test_intraday_rejects_owned_shares_missing_at_broker(self):
        self.write('day_state.json', {'owned_qty':1})
        with self.fixed_time(10,20),patch.object(bot,'execute_order') as order:
            with self.assertRaisesRegex(bot.TradeSafetyError,'충돌'):
                bot.run_intraday('fake',{},300159,964929)
        order.assert_not_called()

    def test_intraday_insufficient_minute_candles_no_signal(self):
        with self.fixed_time(10,20),patch.object(bot,'collect_minute_candles',return_value=[
            {'time':'095900','price':95000,'volume':1000}]*59),\
             patch.object(bot,'get_current_price') as current:
            self.assertFalse(bot.check_daytrade_signal('fake'))
        current.assert_not_called()

    def test_intraday_short_volume_signal_stays_false(self):
        bars=[]
        for i in range(60):
            bars.append({'time':f'{9+(i//60):02d}{i%60:02d}00',
                         'price':90000+i*10,'volume':100})
        with self.fixed_time(10,20),patch.object(bot,'collect_minute_candles',return_value=bars),\
             patch.object(bot,'get_current_price',return_value=90700):
            self.assertFalse(bot.check_daytrade_signal('fake'))

    def test_fast_baa_returns_expected_252_day_score(self):
        data=[100.+i*0.1 for i in range(252)]
        ans=bot.calc_fast_baa(data)
        expected=(16*(data[-1]/data[-21]-1)+6*(data[-1]/data[-63]-1)+
                  2*(data[-1]/data[-126]-1)+(data[-1]/data[-252]-1))
        self.assertAlmostEqual(ans,expected)
        with self.assertRaises(bot.TradeSafetyError):
            bot.calc_fast_baa([100.]*251)

    def test_baa_defensive_target_when_canary_negative(self):
        def fake_closes(token,code,target_count=252):
            if code=='251350':return [110.]*251+[90.]
            if code=='423160':return [100.]*251+[110.]
            return [100.+i for i in range(252)]
        with patch.object(bot,'get_daily_closes',side_effect=fake_closes):
            code,name,is_defense,logs,closes=bot.select_baa_target('fake')
        self.assertTrue(is_defense)
        self.assertEqual(code,'423160')
        self.assertEqual(len(logs),4)

    def test_order_http_post_rejected_but_no_retry(self):
        from requests import Response
        fail_resp=Response();fail_resp.status_code=400;fail_resp._content=b'bad request'
        with patch.object(bot.SESSION,'request',return_value=fail_resp) as api:
            with self.assertRaises(bot.TradeSafetyError):
                bot.api_request('POST','/uapi/domestic-stock/v1/trading/order-cash',retries=10)
        self.assertEqual(api.call_count,1)

    def test_broker_cancel_all_qty_zero_fill_terminal(self):
        self.journal([{'id':'o','strategy':'BAA','side':'BUY','code':'449170',
            'requested_qty':2,'confirmed_fill_qty':0,'status':'ACCEPTED',
            'created_at':bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'),'odno':'4'}])
        with patch.object(bot,'get_order_records',return_value=[{
            'odno':'4','pdno':'449170','ord_qty':'2','tot_ccld_qty':'0',
            'tot_ccld_amt':'0','cncl_cfrm_qty':'2'}]):
            bot.assert_no_pending('fake')
        self.assertEqual(bot.read_journal()['orders'][0]['status'],'CANCELED')
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],0)

    def test_broker_invalid_fill_plus_cancel_aborts(self):
        self.journal([{'id':'o','strategy':'BAA','side':'BUY','code':'449170',
            'requested_qty':2,'confirmed_fill_qty':0,'status':'ACCEPTED',
            'created_at':bot.now_kst().strftime('%Y-%m-%d %H:%M:%S'),'odno':'4'}])
        with patch.object(bot,'get_order_records',return_value=[{
            'odno':'4','pdno':'449170','ord_qty':'2','tot_ccld_qty':'2',
            'tot_ccld_amt':'200000','cncl_cfrm_qty':'1'}]):
            with self.assertRaisesRegex(bot.TradeSafetyError,'수량 불일치'):
                bot.reconcile_orders('fake')

    def test_duplicate_legacy_record_is_rejected(self):
        self.owned()
        data=bot.read_journal()
        data['orders'].append(dict(data['orders'][0]))
        bot.save_json(bot.TRADE_HISTORY_FILE,data)
        with patch.object(bot,'DRY_RUN',True):
            with self.assertRaisesRegex(bot.TradeSafetyError,'중복'):
                bot.adopt_verified_legacy_baa('fake',{'423160':{'qty':6,'avg_price':110785}})

    def test_baa_empty_balance_fails_if_owned_six(self):
        self.owned()
        with self.fixed_time(),patch.object(bot,'select_baa_target',return_value=('423160','KOFR',True,[],[100.]*252)),\
             patch.object(bot,'get_current_price',return_value=110800),\
             patch.object(bot,'execute_order') as order:
            with self.assertRaises(bot.TradeSafetyError):
                bot.run_baa('fake',{},300159,964929)
        order.assert_not_called()

    def test_midday_report_requires_final_schedule_flag(self):
        with self.fixed_time(14, 53), patch.object(bot, 'RUN_MIDDAY_REPORT', False), \
             patch.object(bot, 'FORCE_BAA', False), \
             patch.object(bot, 'select_baa_target') as choose:
            bot.handle_daily_closing_report('fake', {}, 964929)
        choose.assert_not_called()
        self.assertFalse(bot.load_json(bot.BAA_STATE_FILE, {}).get('last_report_date'))

    def test_closing_report_only_once_per_day(self):
        with self.fixed_time(14,53),patch.object(bot,'RUN_MIDDAY_REPORT',True),patch.object(bot,'select_baa_target',return_value=('423160','KOFR',True,['test'],[100.]*252)) as choose:
            bot.handle_daily_closing_report('fake',{},964929)
            bot.handle_daily_closing_report('fake',{},964929)
        choose.assert_called_once()
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['last_report_date'],'2026-10-14')

    def test_main_drives_migration_first_then_trade_decisions(self):
        """Scenario with mocked broker data; simulate runner orchestration."""
        timeline=[]
        holdings={'423160':{'qty':6,'avg_price':110785}}
        # Simulate main() calls while leaving real migration/journal intact.
        with self.fixed_time(10,40),patch.object(bot,'KIS_APP_KEY','test'),\
             patch.object(bot,'KIS_APP_SECRET','test'),patch.object(bot,'KIS_CANO','12345678'),\
             patch.object(bot,'KIS_ENV','real'),patch.object(bot,'DRY_RUN',True),\
             patch.object(bot,'get_access_token',return_value='fake'),\
             patch.object(bot,'is_market_open_today',return_value=True),\
             patch.object(bot,'get_account_balance',return_value=(holdings,300159,964929)),\
             patch.object(bot,'get_order_records',return_value=[]),\
             patch.object(bot,'get_current_price',return_value=95000),\
             patch.object(bot,'check_daytrade_signal',return_value=False),\
             patch.object(bot,'run_baa',side_effect=lambda *a,**k:timeline.append('baa')):
            bot.main()
        self.assertEqual(timeline,['baa'])
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE,{})['owned_qty'],6)
        self.assertEqual(len(bot.read_journal()['orders']),1)



class PersistentStateIntegrationTests(unittest.TestCase):
    """Exercise real local git commits/push/fetch with isolated directories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from pathlib import Path
        root = Path(self.tmp.name)
        self.remote = root / 'remote.git'
        self.checkout = root / 'state'
        self.checkout.mkdir()
        self.history = self.checkout / 'trade_history.json'
        self._run('git', 'init', '--bare', str(self.remote))
        self._run('git', 'init', str(self.checkout))
        self._run('git', '-C', str(self.checkout), 'checkout', '-b', 'bot-state')
        self._run('git', '-C', str(self.checkout), 'remote', 'add', 'origin', str(self.remote))
        self._run('git', '-C', str(self.checkout), 'config', 'user.email', 'test@example.com')
        self._run('git', '-C', str(self.checkout), 'config', 'user.name', 'test')
        (self.checkout / '.gitignore').write_text('token_cache.json\n', encoding='utf-8')
        self._run('git', '-C', str(self.checkout), 'add', '.gitignore')
        self._run('git', '-C', str(self.checkout), 'commit', '-m', 'Initialize state')
        self._run('git', '-C', str(self.checkout), 'push', 'origin', 'bot-state')
        for name,value in [('BASE_DIR',self.checkout),('TRADE_HISTORY_FILE',self.history)]:
            p=patch.object(bot,name,value)
            p.start()
            self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'GITHUB_STATE_MODE':'git'})
        p.start(); self.addCleanup(p.stop)

    def _run(self,*args):
        import subprocess
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)

    def test_git_checkpoint_pushed_and_recovered_by_fresh_clone(self):
        bot.save_json(bot.TRADE_HISTORY_FILE, {'orders': [{'id':'mock-intent','status':'INTENT'}]})
        from pathlib import Path
        recovered = Path(self.tmp.name) / 'recover'
        self._run('git','clone','--branch','bot-state',str(self.remote),str(recovered))
        data=json.loads((recovered/'trade_history.json').read_text(encoding='utf-8'))
        self.assertEqual(data['orders'][0]['status'],'INTENT')

    def test_git_checkpoint_push_failure_blocks_before_order(self):
        self._run('git','-C',str(self.checkout),'remote','set-url','origin',str(self.checkout/'missing.git'))
        with self.assertRaises(bot.TradeSafetyError):
            bot.save_json(bot.TRADE_HISTORY_FILE, {'orders': [{'id':'x','status':'INTENT'}]})
        self.assertTrue(self.history.exists())
        # Local write is NOT enough; the git failure must block the next POST.

if __name__ == '__main__':
    unittest.main()
