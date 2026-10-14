
"""Offline safety regression tests for bot_once.py.

Run: python -m unittest -v test_bot_once.py
All external broker and Telegram calls are mocked. No live orders are sent.
"""
import json
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


if __name__ == '__main__':
    unittest.main()
