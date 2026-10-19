"""Offline KODEX 200 cap and BAA allocation safety regressions."""
import re
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bot_once as bot
import test_bot_once


class OneShareTests(unittest.TestCase):
    write = test_bot_once.BotSafetyTests.write

    def setUp(self):
        test_bot_once.BotSafetyTests.setUp(self)
        schedule_state = Path(self.tmp.name) / "schedule_state.json"
        patcher = patch.object(bot, "SCHEDULE_STATE_FILE", schedule_state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def attempt(self, actual=0, owned=0, qty=1, strategy="DAY", side="02", baa=False):
        self.write("day_state.json", {"owned_qty": owned})
        self.write(
            "baa_state.json",
            {"target_code": "069500" if baa else None, "owned_qty": 1 if baa else 0},
        )
        with ExitStack() as stack:
            for name, value in [
                ("order_window", True),
                ("is_market_open_today", True),
                ("assert_no_pending", None),
                ("check_daily_loss", None),
                ("get_current_price", 1000),
                ("get_buyable_qty", 100),
                ("get_account_balance", ({"069500": {"qty": actual}}, 100000, 200000)),
            ]:
                stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(bot, "DRY_RUN", True))
            request = stack.enter_context(patch.object(bot, "api_request"))
            try:
                return bot.execute_order("fake", "069500", qty, side, strategy=strategy)
            finally:
                request.assert_not_called()

    def test_buy_cap_and_internal_state_mismatch(self):
        for actual, owned, qty in [(0, 0, 2), (1, 1, 1), (2, 2, 1), (1, 0, 1), (0, 1, 1)]:
            with self.subTest(actual=actual, owned=owned, qty=qty):
                with self.assertRaises(bot.TradeSafetyError):
                    self.attempt(actual, owned, qty)
        self.assertEqual(self.attempt(), (False, 0))

    def test_baa_conflicts_and_only_day_strategy_may_buy_ticker(self):
        for kwargs in [{"strategy": "BAA"}, {"baa": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(bot.TradeSafetyError):
                self.attempt(**kwargs)

    def test_sell_is_not_blocked_by_one_share_buy_cap(self):
        self.assertEqual(self.attempt(actual=2, owned=2, qty=2, side="01"), (False, 0))

    def test_fresh_account_verification_failure_blocks_order(self):
        with patch.object(bot, "order_window", return_value=True), \
             patch.object(bot, "is_market_open_today", return_value=True), \
             patch.object(bot, "assert_no_pending"), \
             patch.object(bot, "check_daily_loss"), \
             patch.object(bot, "get_current_price", return_value=1000), \
             patch.object(bot, "get_buyable_qty", return_value=10), \
             patch.object(bot, "get_account_balance", side_effect=bot.TradeSafetyError("unavailable")), \
             patch.object(bot, "api_request") as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order("fake", "069500", 1, strategy="DAY")
        request.assert_not_called()

    def test_pending_order_blocks_additional_order(self):
        with patch.object(bot, "order_window", return_value=True), \
             patch.object(bot, "is_market_open_today", return_value=True), \
             patch.object(bot, "assert_no_pending", side_effect=bot.TradeSafetyError("pending")), \
             patch.object(bot, "api_request") as request:
            with self.assertRaises(bot.TradeSafetyError):
                bot.execute_order("fake", "069500", 1, strategy="DAY")
        request.assert_not_called()

    def test_large_day_budget_still_requests_one_share(self):
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 14, 0))), \
             patch.object(bot, "get_current_price", return_value=1000), \
             patch.object(bot, "check_daytrade_signal", return_value=True), \
             patch.object(bot, "execute_order") as order:
            bot.run_intraday("fake", {}, 10_000_000, 10_000_000)
        self.assertEqual(order.call_args.args[2], 1)

    def test_baa_aggressive_ranking_excludes_short_term_ticker(self):
        requested = []

        def daily_closes(token, code, target_count=252):
            requested.append(code)
            slope = {"069500": 10, "133690": 1, "122630": 2, "381180": 3}.get(code, 1)
            return [100.0 + slope * index for index in range(252)]

        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 11, 10))), \
             patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, _, defensive, _, _ = bot.select_baa_target("fake")
        self.assertFalse(defensive)
        self.assertNotIn(bot.CODE_BULL, bot.ATTACK_UNIVERSE)
        self.assertNotEqual(target, bot.CODE_BULL)
        self.assertEqual(target, "381180")
        self.assertIn(bot.CODE_BULL, requested)  # retained only for Canary assessment
        self.assertEqual(len(bot.ATTACK_UNIVERSE), 3)

    def test_legacy_baa_069500_record_halts_even_with_zero_tracked_shares(self):
        self.write("baa_state.json", {"target_code": bot.CODE_BULL, "owned_qty": 0})
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 11, 10))), \
             patch.object(bot, "select_baa_target", return_value=("423160", "KOFR", True, [], [100.0] * 252)), \
             patch.object(bot, "get_current_price", return_value=1000), \
             patch.object(bot, "execute_order") as order:
            with self.assertRaisesRegex(bot.TradeSafetyError, "수동 계좌 대조"):
                bot.run_baa("fake", {}, 100000, 100000)
        order.assert_not_called()

    def test_baa_budget_uses_eighty_percent_of_total_assets(self):
        closes = [100.0] * 252
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 11, 10))), \
             patch.object(bot, "select_baa_target", return_value=("449170", "SOFR", True, [], closes)), \
             patch.object(bot, "get_current_price", return_value=1000), \
             patch.object(bot, "send_telegram") as telegram, \
             patch.object(bot, "DRY_RUN", True), \
             patch.object(bot, "api_request") as request:
            bot.run_baa("fake", {}, 1_000_000, 1_000_000)
        self.assertIn("768,000원", telegram.call_args.args[0])
        request.assert_not_called()

    def test_insufficient_cash_after_baa_sale_stops_before_purchase(self):
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 11, 10))), \
             patch.object(bot, "select_baa_target", return_value=("423160", "KOFR", True, [], [100.0] * 252)), \
             patch.object(bot, "get_current_price", return_value=1000), \
             patch.object(bot, "load_json", side_effect=[
                 {"target_code": "449170", "owned_qty": 1},
                 {"owned_qty": 0},
                 {"target_code": "449170", "owned_qty": 0},
             ]), \
             patch.object(bot, "execute_order", return_value=(True, 1)) as order, \
             patch.object(bot, "get_account_balance", return_value=({}, 500, 1_000_000)), \
             patch.object(bot, "assert_no_pending"), \
             patch.object(bot, "DRY_RUN", False):
            with self.assertRaisesRegex(bot.TradeSafetyError, "BAA 목표수량 미달"):
                bot.run_baa("fake", {"449170": {"qty": 1}}, 100000, 1_000_000)
        order.assert_called_once()
        self.assertEqual(order.call_args.args[3], "01")

    def test_late_report_recovers_a_missed_1453_settlement(self):
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 15, 0))), \
             patch.object(bot, "RUN_MIDDAY_REPORT", True), \
             patch.object(bot, "FORCE_BAA", False), \
             patch.object(bot, "select_baa_target", return_value=("423160", "KOFR", True, [], [])), \
             patch.object(bot, "send_telegram") as telegram:
            bot.handle_daily_closing_report("fake", {}, 1_000_000)
        self.assertTrue(telegram.call_args.kwargs["is_alert"])
        self.assertIn("지연 복구", telegram.call_args.args[0])

    def test_missed_afternoon_schedule_gap_alerts(self):
        with patch.object(bot, "now_kst", side_effect=[
            bot.KST.localize(datetime(2026, 10, 16, 14, 0)),
            bot.KST.localize(datetime(2026, 10, 16, 14, 12)),
        ]), patch.object(bot, "send_telegram") as telegram:
            bot.monitor_scheduled_execution()
            bot.monitor_scheduled_execution()
        telegram.assert_called_once()
        self.assertTrue(telegram.call_args.kwargs["is_alert"])

    def test_five_minute_schedule_is_unique_and_correctly_converts_to_kst(self):
        workflow = Path(".github/workflows/trade.yml").read_text(encoding="utf-8")
        slots = []
        for minute, hour in re.findall(r"cron: '([^ ]+) ([^ ]+) \* \* 1-5'", workflow):
            minutes = range(0, 60, 5) if minute == "*/5" else map(int, minute.split(","))
            slots.extend(int(hour) * 60 + value for value in minutes)
        self.assertEqual(len(slots), 51)
        self.assertEqual(len(slots), len(set(slots)))
        kst_slots = {((minute // 60 + 9) % 24) * 60 + minute % 60 for minute in slots}
        self.assertEqual(kst_slots & set(range(14 * 60, 15 * 60 + 21)),
                         set(range(14 * 60, 15 * 60 + 21, 5)) | {14 * 60 + 53})

    def test_broker_quantity_must_be_explicit_and_nonnegative_integer(self):
        for value in [None, "", "unknown", "-1", "1.5", "nan", True, "999999999999999999999999999999999999"]:
            with self.subTest(value=value), self.assertRaises(bot.TradeSafetyError):
                bot.share_quantity(value)
        self.assertEqual(bot.share_quantity("1"), 1)
