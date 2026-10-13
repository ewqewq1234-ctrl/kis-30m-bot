"""Offline safety tests for bot_once.py. No real KIS API or orders."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_once as bot


class BotSafetyTests(unittest.TestCase):
    def test_momentum_requires_252_days(self):
        with self.assertRaises(bot.TradeSafetyError):
            bot.calc_fast_baa([100.0] * 251)

    def test_momentum_flat_is_zero(self):
        self.assertAlmostEqual(bot.calc_fast_baa([100.0] * 252), 0.0)

    def test_momentum_rejects_bad_price(self):
        with self.assertRaises(bot.TradeSafetyError):
            bot.calc_fast_baa([100.0] * 251 + [0.0])

    def test_load_missing_returns_copy(self):
        with tempfile.TemporaryDirectory() as d:
            default = {'orders': []}
            got = bot.load_json(Path(d) / 'missing.json', default)
            got['orders'].append(1)
            self.assertEqual(default['orders'], [])

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            p.write_text('{invalid', encoding='utf-8')
            with self.assertRaises(bot.TradeSafetyError):
                bot.load_json(p, {})

    def test_save_and_restore_state(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'state.json'
            with patch.dict('os.environ', {'GITHUB_STATE_MODE': 'off'}):
                bot.save_json(p, {'orders': [{'id': 'example', 'status': 'UNCERTAIN'}]})
            self.assertEqual(bot.load_json(p, {})['orders'][0]['status'], 'UNCERTAIN')

    def test_uncertain_orders_are_detected(self):
        with patch.object(bot, 'journal_orders', return_value=[
            {'id': 'a', 'status': 'UNCERTAIN'},
            {'id': 'b', 'status': 'PARTIAL'},
            {'id': 'c', 'status': 'FILLED'},
        ]):
            self.assertEqual(len(bot.open_or_uncertain_orders()), 2)

    def test_zero_qty_never_orders(self):
        with patch.object(bot, 'api_request', side_effect=AssertionError('network called')):
            self.assertEqual(bot.execute_order('fake', '069500', 0), (False, 0))

    def test_invalid_order_type_never_orders(self):
        with patch.object(bot, 'api_request', side_effect=AssertionError('network called')):
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '069500', 1, order_type='INVALID')

    def test_invalid_order_division_never_orders(self):
        with patch.object(bot, 'api_request', side_effect=AssertionError('network called')):
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '069500', 1, ord_dvsn='00')

    def test_market_closed_prevents_order(self):
        with patch.object(bot, 'order_window', return_value=False), \
             patch.object(bot, 'api_request', side_effect=AssertionError('network called')):
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '069500', 1)

    def test_dry_run_never_posts_order(self):
        with patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), \
             patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=10000), \
             patch.object(bot, 'get_buyable_qty', return_value=10), \
             patch.object(bot, 'journal_orders', return_value=[]), \
             patch.object(bot, 'api_request', side_effect=AssertionError('network called')), \
             patch.object(bot, 'DRY_RUN', True):
            self.assertEqual(bot.execute_order('fake', '069500', 1, strategy='DAY'), (False, 0))


if __name__ == '__main__':
    unittest.main()
