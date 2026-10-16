"""Offline regressions: all broker calls are mocked."""
import re
import unittest
from datetime import datetime
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
import test_bot_once
import bot_once as bot


class OneShareTests(unittest.TestCase):
    setUp = test_bot_once.BotSafetyTests.setUp
    write = test_bot_once.BotSafetyTests.write
    def attempt(self, actual=0, owned=0, qty=1, strategy='DAY', side='02', baa=False):
        self.write('day_state.json', {'owned_qty': owned})
        self.write('baa_state.json', {'target_code': '069500' if baa else None,
                                      'owned_qty': 1 if baa else 0})
        with ExitStack() as stack:
            for name, value in [('order_window', True), ('is_market_open_today', True),
                                ('assert_no_pending', None), ('check_daily_loss', None),
                                ('get_current_price', 1000), ('get_buyable_qty', 100),
                                ('get_account_balance', ({'069500': {'qty': actual}}, 100000, 200000))]:
                stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(bot, 'DRY_RUN', True))
            request = stack.enter_context(patch.object(bot, 'api_request'))
            try:
                return bot.execute_order('fake', '069500', qty, side, strategy=strategy)
            finally:
                request.assert_not_called()

    def test_cap_cases(self):
        for actual, owned, qty in [(0,0,2), (1,1,1), (2,2,1), (1,0,1), (0,1,1)]:
            with self.subTest(actual=actual, owned=owned, qty=qty):
                with self.assertRaises(bot.TradeSafetyError):
                    self.attempt(actual, owned, qty)

    def test_one_share_and_excess_position_sell(self):
        self.assertEqual(self.attempt(), (False, 0))
        self.assertEqual(self.attempt(2,2,2,side='01'), (False, 0))

    def test_baa_conflicts(self):
        for kwargs in [dict(strategy='BAA'), dict(baa=True)]:
            with self.assertRaises(bot.TradeSafetyError):
                self.attempt(**kwargs)

    def test_baa_keeps_top_069500_target_and_blocks_without_fallback(self):
        def closes(token, code, target_count=252):
            if code == '069500':
                return [100.] * 251 + [110.]
            if code in bot.CANARY_UNIVERSE:
                return [100.] * 251 + [101.]
            return [100.] * 252

        with patch.object(bot, 'get_daily_closes', side_effect=closes), \
             patch.object(bot, 'get_current_price', return_value=110), \
             patch.object(bot, 'execute_order') as order:
            target, _, defense, _, _ = bot.select_baa_target('fake')
            self.assertFalse(defense)
            self.assertEqual(target, '069500')
            with patch.object(bot, 'now_kst',
                              return_value=bot.KST.localize(datetime(2026,10,16,11,10))):
                with self.assertRaisesRegex(bot.TradeSafetyError, '계좌 내 물량 식별 불가'):
                    bot.run_baa('fake', {}, 100000, 100000)
        order.assert_not_called()

    def test_large_budget_still_requests_one(self):
        from datetime import datetime
        with patch.object(bot, 'now_kst', return_value=bot.KST.localize(datetime(2026,10,16,14,0))), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'check_daytrade_signal', return_value=True), \
             patch.object(bot, 'execute_order') as order:
            bot.run_intraday('fake', {}, 10000000, 10000000)
            self.assertEqual(order.call_args.args[2], 1)

    def test_fresh_balance_failure_blocks(self):
        with patch.object(bot, 'get_account_balance', side_effect=bot.TradeSafetyError('unavailable')):
            # Exercise the real entry point with its other prerequisites mocked.
            with patch.object(bot, 'order_window', return_value=True), \
                 patch.object(bot, 'is_market_open_today', return_value=True), \
                 patch.object(bot, 'assert_no_pending'), patch.object(bot, 'api_request') as request:
                with self.assertRaises(bot.TradeSafetyError):
                    bot.execute_order('fake','069500',1,strategy='DAY')
                request.assert_not_called()

    def test_malformed_broker_quantities_fail_closed(self):
        for value in [None, '', 'unknown', '-1', '1.5', 'nan', 'inf', True,
                      '99999999999999999999999999999999999999999999999999']:
            with self.subTest(value=value), patch.object(bot, 'api_request', return_value=(
                {'output1': [{'pdno':'069500', 'hldg_qty':value}],
                 'output2': [{'dnca_tot_amt':'100000', 'tot_evlu_amt':'200000'}]}, {})):
                with self.assertRaises(bot.TradeSafetyError):
                    bot.get_account_balance('fake')

    def test_missing_holdings_list_fails_closed(self):
        for rows in [None, {}, '', [None]]:
            with self.subTest(rows=rows), patch.object(bot, 'api_request', return_value=(
                {'output1':rows, 'output2':[{'dnca_tot_amt':'100000','tot_evlu_amt':'200000'}]}, {})):
                with self.assertRaises(bot.TradeSafetyError):
                    bot.get_account_balance('fake')

    def test_duplicate_holdings_across_pages_fail_closed(self):
        row = {'pdno':'069500', 'hldg_qty':'1', 'pchs_avg_pric':'1000'}
        first = ({'output1':[row], 'output2':[{'dnca_tot_amt':'100000','tot_evlu_amt':'200000'}],
                  'ctx_area_fk100':'next','ctx_area_nk100':'page'}, {'tr_cont':'M'})
        second = ({'output1':[row], 'output2':[]}, {})
        with patch.object(bot, 'api_request', side_effect=[first,second]), patch.object(bot.time, 'sleep'):
            with self.assertRaises(bot.TradeSafetyError):
                bot.get_account_balance('fake')

    def test_valid_empty_account_and_holding(self):
        for rows, expected in [([],0), ([{'pdno':'069500','hldg_qty':'1','pchs_avg_pric':'1000'}],1)]:
            with self.subTest(rows=rows), patch.object(bot, 'api_request', return_value=(
                {'output1':rows,'output2':[{'dnca_tot_amt':'100000','tot_evlu_amt':'200000'}]}, {})):
                holdings, _, _ = bot.get_account_balance('fake')
                self.assertEqual(holdings.get('069500', {}).get('qty',0), expected)

    def test_pending_order_blocks_buy_and_sell(self):
        for side in ['01','02']:
            with self.subTest(side=side), patch.object(bot, 'order_window', return_value=True), \
                 patch.object(bot, 'is_market_open_today', return_value=True), \
                 patch.object(bot, 'assert_no_pending', side_effect=bot.TradeSafetyError('pending')), \
                 patch.object(bot, 'api_request') as request:
                with self.assertRaises(bot.TradeSafetyError):
                    bot.execute_order('fake','069500',1,side,strategy='DAY')
                request.assert_not_called()

    def test_account_refresh_is_after_buying_power_check(self):
        timeline = []
        with patch.object(bot, 'DRY_RUN', True), \
             patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty', side_effect=lambda *a: timeline.append('buyable') or 10), \
             patch.object(bot, 'get_account_balance', side_effect=lambda *a: timeline.append('balance') or ({},100000,200000)), \
             patch.object(bot, 'api_request') as request:
            self.assertEqual(bot.execute_order('fake','069500',1,strategy='DAY'), (False,0))
            request.assert_not_called()
        self.assertEqual(timeline, ['buyable','balance'])

    def test_live_buy_saves_intent_after_final_balance_and_before_post(self):
        timeline = []
        save_json = bot.save_json

        def record_save(path, data):
            save_json(path, data)
            if Path(path).name == 'trade_history.json':
                orders = data.get('orders', [])
                if orders and orders[-1].get('status') == 'INTENT':
                    timeline.append('intent')

        def request(method, *args, **kwargs):
            timeline.append('post')
            return {'output': {'ODNO': '123'}}, {}

        with patch.object(bot, 'DRY_RUN', False), \
             patch.object(bot, 'order_window', return_value=True), \
             patch.object(bot, 'is_market_open_today', return_value=True), \
             patch.object(bot, 'assert_no_pending'), patch.object(bot, 'check_daily_loss'), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'get_buyable_qty',
                          side_effect=lambda *a: timeline.append('buyable') or 10), \
             patch.object(bot, 'get_account_balance',
                          side_effect=lambda *a: timeline.append('balance') or ({},100000,200000)), \
             patch.object(bot, 'save_json', side_effect=record_save), \
             patch.object(bot, 'api_request', side_effect=request), \
             patch.object(bot, 'ORDER_POLL_SECONDS', 0):
            with self.assertRaisesRegex(bot.TradeSafetyError, '미체결'):
                bot.execute_order('fake', '069500', 1, strategy='DAY')
        self.assertEqual(timeline, ['buyable', 'balance', 'intent', 'post'])

    def test_insufficient_entry_budget_does_not_buy(self):
        from datetime import datetime
        with patch.object(bot, 'now_kst', return_value=bot.KST.localize(datetime(2026,10,16,14,0))), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'check_daytrade_signal', return_value=True), \
             patch.object(bot, 'execute_order') as order:
            bot.run_intraday('fake', {}, 500, 1000)
        order.assert_not_called()

    def test_late_entry_and_order_cutoff_use_actual_time(self):
        from datetime import datetime
        with patch.object(bot, 'now_kst', return_value=bot.KST.localize(datetime(2026,10,16,14,31))), \
             patch.object(bot, 'get_current_price', return_value=1000), \
             patch.object(bot, 'check_daytrade_signal') as signal, \
             patch.object(bot, 'execute_order') as order:
            bot.run_intraday('fake', {}, 100000, 100000)
        signal.assert_not_called()
        order.assert_not_called()

        with patch.object(bot, 'now_kst', return_value=bot.KST.localize(datetime(2026,10,16,15,21))), \
             patch.object(bot, 'is_market_open_today') as market, \
             patch.object(bot, 'api_request') as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order('fake', '069500', 1, '01', strategy='DAY')
        market.assert_not_called()
        request.assert_not_called()

    def test_schedule_unique_and_kst(self):
        text = Path('.github/workflows/trade.yml').read_text(encoding='utf-8')
        slots = []
        for minute, hour in re.findall(r"cron: '([^ ]+) ([^ ]+) \* \* 1-5'", text):
            minutes = range(0,60,5) if minute == '*/5' else map(int, minute.split(','))
            slots.extend(int(hour)*60+m for m in minutes)
        self.assertEqual(len(slots), 51)
        self.assertEqual(len(slots), len(set(slots)))
        kst_slots = {((minute // 60 + 9) % 24) * 60 + minute % 60 for minute in slots}
        self.assertEqual(kst_slots & set(range(840,921)), set(range(840,921,5)))
        self.assertIn(14 * 60 + 53, kst_slots)
        self.assertEqual(text.count("cron: '*/5 5 * * 1-5'"), 1)
        self.assertEqual(text.count("cron: '53 5 * * 1-5'"), 1)
        self.assertEqual(text.count("cron: '0,5,10,15,20 6 * * 1-5'"), 1)
        self.assertIn("github.event.schedule == '53 5 * * 1-5'", text)
        self.assertIn("DRY_RUN: 'Y'", text)
        self.assertIn("ENABLE_LIVE_ORDERS: 'NO'", text)
        self.assertNotIn('최종 예약', text)
