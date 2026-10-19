"""Offline KODEX 200 cap and BAA allocation safety regressions."""
import re
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import bot_once as bot
from ci.verify_kis_market_data import (
    EXPECTED_PRODUCT_NAMES,
    momentum_metrics,
    read_only_request,
    verify_product_identity,
)
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

    def test_live_orders_are_blocked_before_kst_start_date(self):
        with patch.object(bot, "LIVE_TRADING_START_DATE", datetime(2026, 10, 20).date()), \
             patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 19, 14, 0))), \
             patch.object(bot, "DRY_RUN", False), \
             patch.object(bot, "order_window", return_value=True), \
             patch.object(bot, "api_request") as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, "시작일"):
                bot.execute_order("fake", "423160", 1, strategy="BAA")
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
        self.assertNotIn(bot.CODE_BULL, requested)
        self.assertTrue(set(bot.CANARY_UNIVERSE).issubset(requested))
        self.assertEqual(len(bot.ATTACK_UNIVERSE), 3)

    def test_exact_canary_and_defensive_universes(self):
        self.assertEqual(bot.CANARY_UNIVERSE, {
            "360750": "TIGER 미국S&P500",
            "251350": "KODEX MSCI선진국",
            "195980": "PLUS 신흥국MSCI(합성 H)",
            "437080": "KODEX 미국종합채권ESG액티브(H)",
        })
        self.assertEqual(bot.DEFENSE_UNIVERSE, {
            "423160": "KODEX KOFR금리액티브(합성)",
            "449170": "TIGER KOFR금리액티브(합성)",
            "453850": "ACE 미국30년국채액티브(H)",
            "411060": "ACE KRX금현물",
        })
        self.assertTrue({"069500", "371460", "453850"}.isdisjoint(bot.CANARY_UNIVERSE))
        self.assertEqual(bot.CODE_BULL, "069500")
        self.assertNotIn("069500", bot.ATTACK_UNIVERSE)
        self.assertIn("453850", bot.DEFENSE_UNIVERSE)
        self.assertEqual(set(bot.ATTACK_UNIVERSE), {"133690", "122630", "381180"})
        self.assertEqual(bot.BAA_TARGET_RATIO, 0.80)

    def test_comparison_metrics_use_existing_fast_baa_formula(self):
        closes = [100.0 + index for index in range(252)]
        returns, score = momentum_metrics(closes)
        self.assertAlmostEqual(returns["1m"], closes[-1] / closes[-21] - 1)
        self.assertAlmostEqual(returns["3m"], closes[-1] / closes[-63] - 1)
        self.assertAlmostEqual(returns["6m"], closes[-1] / closes[-126] - 1)
        self.assertAlmostEqual(returns["12m"], closes[-1] / closes[-252] - 1)
        self.assertEqual(score, bot.calc_fast_baa(closes))

    def test_market_data_verifier_blocks_order_endpoints(self):
        with patch.object(bot, "api_request") as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, "blocked unexpected request"):
                read_only_request(
                    "POST", "/uapi/domestic-stock/v1/trading/order-cash", "fake"
                )
        request.assert_not_called()

    def test_product_identity_matches_kis_master_and_rejects_mismatches(self):
        for code, expected_name in EXPECTED_PRODUCT_NAMES.items():
            with self.subTest(code=code), patch(
                "ci.verify_kis_market_data.read_only_request",
                return_value=({"output": {
                    "pdno": code,
                    "prdt_abrv_name": expected_name,
                }}, {}),
            ):
                verify_product_identity("fake", code)

        with patch(
            "ci.verify_kis_market_data.read_only_request",
            return_value=({"output": {
                "pdno": "455030",
                "prdt_abrv_name": "KODEX USD SOFR",
            }}, {}),
        ):
            with self.assertRaisesRegex(bot.TradeSafetyError, "code mismatch"):
                verify_product_identity("fake", "449170")

    def test_all_positive_canaries_select_aggressive_baa(self):
        requested = []

        def daily_closes(token, code, target_count=252):
            requested.append(code)
            slope = {"133690": 1, "122630": 2, "381180": 3}.get(code, 1)
            return [100.0 + slope * index for index in range(252)]

        with patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, _, defensive, logs, _ = bot.select_baa_target("fake")
        self.assertFalse(defensive)
        self.assertEqual(target, "381180")
        self.assertEqual(len(logs), 4)
        self.assertTrue(set(bot.CANARY_UNIVERSE).issubset(requested))

    def test_one_nonpositive_canary_switches_to_defensive_baa(self):
        def daily_closes(token, code, target_count=252):
            if code == "195980":
                return [110.0] * 251 + [90.0]
            if code in bot.DEFENSE_UNIVERSE:
                slope = {"423160": 3, "449170": 2, "453850": 1, "411060": 0}.get(code, 0)
                return [100.0 + slope * index for index in range(252)]
            return [100.0 + index for index in range(252)]

        with patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, _, defensive, logs, _ = bot.select_baa_target("fake")
        self.assertTrue(defensive)
        self.assertEqual(target, "423160")
        self.assertEqual(len(logs), 4)

    def test_zero_canary_score_switches_to_defensive_baa(self):
        def daily_closes(token, code, target_count=252):
            if code == "437080":
                return [100.0] * 252
            if code in bot.DEFENSE_UNIVERSE:
                slope = {"423160": 3, "449170": 2, "453850": 1, "411060": 0}.get(code, 0)
                return [100.0 + slope * index for index in range(252)]
            return [100.0 + index for index in range(252)]

        with patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, _, defensive, _, _ = bot.select_baa_target("fake")
        self.assertTrue(defensive)
        self.assertEqual(target, "423160")

    def test_kis_daily_history_paginates_sorts_and_returns_252_unique_closes(self):
        as_of = datetime(2026, 10, 19)
        requests = []

        def page_response(method, path, token, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice")
            params = kwargs["params"]
            self.assertEqual(params["FID_INPUT_ISCD"], "251350")
            self.assertEqual(params["FID_ORG_ADJ_PRC"], "0")
            start = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d")
            end = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d")
            requests.append((start, end))
            rows = []
            cursor = start
            while cursor <= end:
                if cursor.weekday() < 5:
                    rows.append({
                        "stck_bsop_date": cursor.strftime("%Y%m%d"),
                        "stck_clpr": str(cursor.toordinal()),
                    })
                cursor += timedelta(days=1)
            return {"output2": list(reversed(rows))}, {}

        with patch.object(bot, "now_kst", return_value=bot.KST.localize(as_of)), \
             patch.object(bot, "api_request", side_effect=page_response), \
             patch.object(bot.time, "sleep"):
            closes = bot.get_daily_closes("fake", "251350")
        self.assertEqual(len(closes), 252)
        self.assertEqual(len(requests), 3)
        self.assertEqual(closes, sorted(closes))
        for prior, following in zip(requests, requests[1:]):
            self.assertEqual(following[1].date() + timedelta(days=1), prior[0].date())

    def test_kis_daily_history_can_verify_adjusted_closes_on_request(self):
        def page_response(method, path, token, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(
                path, "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
            )
            self.assertEqual(kwargs["params"]["FID_ORG_ADJ_PRC"], "1")
            return {"output2": [{
                "stck_bsop_date": "20261019",
                "stck_clpr": "100",
            }]}, {}

        with patch.object(
            bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 19))
        ), patch.object(bot, "api_request", side_effect=page_response), \
             patch.object(bot.time, "sleep"):
            closes = bot.get_daily_closes("fake", "360750", target_count=1, adjusted=True)
        self.assertEqual(closes, [100.0])

    def test_current_price_requests_for_each_new_canary_are_read_only(self):
        for code in ("251350", "195980", "437080"):
            with self.subTest(code=code), patch.object(
                bot, "api_request", return_value=({"output": {"stck_prpr": "12345"}}, {})
            ) as request:
                self.assertEqual(bot.get_current_price("fake", code), 12345)
            request.assert_called_once()
            self.assertEqual(request.call_args.args[:2], (
                "GET", "/uapi/domestic-stock/v1/quotations/inquire-price"
            ))
            self.assertEqual(request.call_args.kwargs["params"]["FID_INPUT_ISCD"], code)

    def test_kis_daily_history_rejects_duplicate_or_invalid_data(self):
        valid = {"stck_bsop_date": "20261016", "stck_clpr": "1000"}
        cases = [
            ([valid, valid], "중복"),
            ([{"stck_bsop_date": "20261016", "stck_clpr": "0"}], "종가"),
            ([{"stck_bsop_date": "20261340", "stck_clpr": "1000"}], "날짜"),
            ([{"stck_bsop_date": "20261016", "stck_clpr": "NaN"}], "종가"),
        ]
        for rows, expected in cases:
            with self.subTest(rows=rows), \
                 patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 19))), \
                 patch.object(bot, "api_request", return_value=({"output2": rows}, {})), \
                 patch.object(bot.time, "sleep"):
                with self.assertRaisesRegex(bot.TradeSafetyError, expected):
                    bot.get_daily_closes("fake", "251350")

    def test_kis_daily_history_rejects_insufficient_data_and_api_failures(self):
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 19))), \
             patch.object(bot, "api_request", return_value=({"output2": []}, {})) as request, \
             patch.object(bot.time, "sleep"):
            with self.assertRaisesRegex(bot.TradeSafetyError, "일봉 부족"):
                bot.get_daily_closes("fake", "437080")
        self.assertEqual(request.call_count, 7)

        with patch.object(bot, "api_request", side_effect=bot.TradeSafetyError("KIS GET failed")):
            with self.assertRaisesRegex(bot.TradeSafetyError, "KIS GET failed"):
                bot.get_daily_closes("fake", "195980")

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
        patch.object(bot, "select_baa_target", return_value=(
            "449170", bot.DEFENSE_UNIVERSE["449170"], True, [], closes
        )), \
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
