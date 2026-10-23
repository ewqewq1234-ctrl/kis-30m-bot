"""Offline KODEX 200 cap and BAA allocation safety regressions."""
import os
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

    def attempt(self, actual=0, owned=0, qty=1, strategy="SWING", side="02", baa=False):
        self.write("day_state.json", {"owned_qty": 0})
        swing = bot.default_swing_state()
        swing["owned_qty"] = owned
        self.write("swing_state.json", swing)
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

    def test_069500_day_order_is_rejected_without_post(self):
        with patch.object(bot, "api_request") as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, "잘못된 주문 방향/전략"):
                bot.execute_order("fake", "069500", 1, strategy="DAY")
        request.assert_not_called()

    def test_baa_and_legacy_day_conflicts_block_buys(self):
        for kwargs in [{"strategy": "BAA"}, {"strategy": "DAY"}, {"baa": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(bot.TradeSafetyError):
                self.attempt(**kwargs)

    def test_sell_is_not_blocked_by_one_share_buy_cap(self):
        self.assertEqual(self.attempt(actual=1, owned=1, qty=1, side="01"), (False, 0))

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
                bot.execute_order("fake", "069500", 1, strategy="SWING")
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

    def test_attack_mode_ignores_canary_signal_and_never_reads_defensive_universe(self):
        requested = []

        def daily_closes(token, code, target_count=252):
            requested.append(code)
            if code in bot.CANARY_UNIVERSE:
                return [100.0] * 251 + [1.0]
            slope = {"133690": 1, "122630": 2, "381180": 3}[code]
            return [100.0 + slope * index for index in range(252)]

        with patch.object(bot, "BAA_MODE", "ATTACK"), \
             patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, _, defensive, _, _ = bot.select_baa_target("fake")
        self.assertFalse(defensive)
        self.assertEqual(target, "381180")
        self.assertEqual(set(requested), set(bot.ATTACK_UNIVERSE))
        self.assertTrue(set(bot.ATTACK_UNIVERSE).isdisjoint(bot.DEFENSE_UNIVERSE))
        self.assertNotIn("069500", requested)

    def test_attack_mode_without_eligible_candidate_does_not_fall_back_to_defense(self):
        requested = []

        def daily_closes(token, code, target_count=252):
            requested.append(code)
            return [300.0 - index for index in range(252)]

        with patch.object(bot, "BAA_MODE", "ATTACK"), \
             patch.object(bot, "get_daily_closes", side_effect=daily_closes):
            target, name, defensive, logs, _ = bot.select_baa_target("fake")
        self.assertIsNone(target)
        self.assertIsNone(name)
        self.assertFalse(defensive)
        self.assertEqual(set(requested), set(bot.ATTACK_UNIVERSE))
        self.assertTrue(any("조건을 충족한 종목이 없습니다" in item for item in logs))

    def test_attack_mode_no_candidate_records_alert_and_preserves_existing_position(self):
        self.write("baa_state.json", {
            "target_code": "133690", "owned_qty": 1,
            "last_executed_date": "2026-10-01",
        })
        with patch.object(
            bot, "now_kst",
            return_value=bot.KST.localize(datetime(2026, 10, 21, 11, 5)),
        ), patch.object(bot, "BAA_MODE", "ATTACK"), \
             patch.object(bot, "select_baa_target", return_value=(
                 None, None, False, ["공격 적격 종목 없음"], [],
             )), patch.object(bot, "get_current_price") as quote, \
             patch.object(bot, "execute_order") as order, \
             patch.object(bot, "send_telegram") as telegram:
            bot.run_baa("fake", {"133690": {"qty": 1}}, 100000, 200000)
        state = bot.load_json(bot.BAA_STATE_FILE, {})
        self.assertEqual(state["target_code"], "133690")
        self.assertEqual(state["owned_qty"], 1)
        self.assertEqual(state["last_executed_date"], "2026-10-01")
        self.assertEqual(state["last_skip_date"], "2026-10-21")
        self.assertEqual(state["last_skip_mode"], "ATTACK")
        self.assertIn("공격 적격 종목 없음", state["last_skip_reason"])
        quote.assert_not_called()
        order.assert_not_called()
        telegram.assert_called_once()
        self.assertTrue(telegram.call_args.kwargs["is_alert"])

    def test_attack_mode_invalid_candidate_data_is_recorded_and_alerted(self):
        with patch.object(
            bot, "now_kst",
            return_value=bot.KST.localize(datetime(2026, 10, 21, 11, 5)),
        ), patch.object(bot, "BAA_MODE", "ATTACK"), \
             patch.object(
                 bot, "select_baa_target",
                 side_effect=bot.TradeSafetyError("공격 후보 일봉 데이터 오류"),
             ), patch.object(bot, "execute_order") as order, \
             patch.object(bot, "send_telegram") as telegram:
            with self.assertRaisesRegex(bot.TradeSafetyError, "일봉 데이터 오류"):
                bot.run_baa("fake", {}, 100000, 200000)
        state = bot.load_json(bot.BAA_STATE_FILE, {})
        self.assertEqual(state["last_skip_mode"], "ATTACK")
        self.assertIn("공격 후보 일봉 데이터 오류", state["last_skip_reason"])
        order.assert_not_called()
        telegram.assert_called_once()
        self.assertTrue(telegram.call_args.kwargs["is_alert"])

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
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 16, 15, 40))), \
             patch.object(bot, "RUN_MIDDAY_REPORT", True), \
             patch.object(bot, "RUN_REPORT_ONLY", True), \
             patch.object(bot, "FORCE_BAA", False), \
             patch.object(bot, "select_baa_target", return_value=("423160", "KOFR", True, [], [])), \
             patch.object(bot, "send_telegram") as telegram:
            bot.handle_daily_closing_report("fake", {}, 1_000_000)
        self.assertTrue(telegram.call_args.kwargs["is_alert"])
        self.assertIn("지연 복구", telegram.call_args.args[0])

    def test_missed_afternoon_schedule_gap_alerts(self):
        with patch.object(bot, "now_kst", side_effect=[
            bot.KST.localize(datetime(2026, 10, 16, 14, 5)),
            bot.KST.localize(datetime(2026, 10, 16, 14, 46)),
        ]), patch.object(bot, "send_telegram") as telegram:
            bot.monitor_scheduled_execution()
            bot.monitor_scheduled_execution()
        telegram.assert_called_once()
        self.assertTrue(telegram.call_args.kwargs["is_alert"])

    def test_regular_schedule_spacing_does_not_alert_as_a_missed_run(self):
        with patch.object(bot, "now_kst", side_effect=[
            bot.KST.localize(datetime(2026, 10, 16, 14, 5)),
            bot.KST.localize(datetime(2026, 10, 16, 14, 35)),
        ]), patch.object(bot, "send_telegram") as telegram:
            bot.monitor_scheduled_execution()
            bot.monitor_scheduled_execution()
        telegram.assert_not_called()

    def test_scheduled_trade_guard_accepts_delays_and_missing_timestamps(self):
        env = {
            "GITHUB_EVENT_NAME": "schedule",
            "BOT_SCHEDULE_CRON": "5 11 * * 1-5",
            "BOT_SCHEDULE_SLOT_KST": "11:05",
            "BOT_RUN_CREATED_AT": "2026-10-21T02:12:00Z",
            "BOT_RUN_STARTED_AT": "2026-10-21T04:00:00Z",
        }
        with patch.dict(os.environ, env), patch.object(bot, "RUN_REPORT_ONLY", False):
            for hour, minute in [(11, 12), (13, 5), (14, 15), (15, 20)]:
                now = bot.KST.localize(datetime(2026, 10, 21, hour, minute))
                self.assertIsNone(bot.scheduled_trade_skip_reason(now))
            env["BOT_RUN_CREATED_AT"] = ""
            env["BOT_RUN_STARTED_AT"] = ""
            with patch.dict(os.environ, env):
                self.assertIsNone(bot.scheduled_trade_skip_reason(now))
            for hour, minute in [(8, 59), (15, 21), (19, 0)]:
                now = bot.KST.localize(datetime(2026, 10, 21, hour, minute))
                self.assertIn("시간 밖", bot.scheduled_trade_skip_reason(now))
            saturday = bot.KST.localize(datetime(2026, 10, 24, 11, 0))
            self.assertIn("시간 밖", bot.scheduled_trade_skip_reason(saturday))

    def test_scheduled_trade_guard_rejects_cron_slot_mismatch(self):
        env = {
            "GITHUB_EVENT_NAME": "schedule",
            "BOT_SCHEDULE_CRON": "35 9 * * 1-5",
            "BOT_SCHEDULE_SLOT_KST": "14:15",
        }
        now = bot.KST.localize(datetime(2026, 10, 21, 14, 15))
        with patch.dict(os.environ, env), patch.object(bot, "RUN_REPORT_ONLY", False):
            self.assertIn("일치", bot.scheduled_trade_skip_reason(now))

    def test_baa_is_not_evaluated_for_delayed_1415_execution(self):
        now = bot.KST.localize(datetime(2026, 10, 21, 14, 15))
        with patch.object(bot, "now_kst", return_value=now), \
             patch.object(bot, "select_baa_target") as target:
            bot.run_baa("offline", {}, 100_000, 100_000)
        target.assert_not_called()

    def test_report_only_schedule_bypasses_trading_delay_guard(self):
        now = bot.KST.localize(datetime(2026, 10, 21, 17, 56))
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule",
            "BOT_SCHEDULE_CRON": bot.REPORT_ONLY_CRON,
            "BOT_SCHEDULE_SLOT_KST": "15:35",
        }), patch.object(bot, "RUN_REPORT_ONLY", True):
            self.assertIsNone(bot.scheduled_trade_skip_reason(now))

    def test_five_early_kst_triggers_preserve_logical_slots_and_report(self):
        workflow = Path(".github/workflows/trade.yml").read_text(encoding="utf-8")
        entries = re.findall(r"- cron: '([^']+)'\r?\n\s+timezone: '([^']+)'", workflow)
        expected = {
            "35 5 * * 1-5": ("09:35", "N", "35 9 * * 1-5"),
            "5 7 * * 1-5": ("11:05", "N", "5 11 * * 1-5"),
            "5 9 * * 1-5": ("13:05", "N", "5 13 * * 1-5"),
            "5 11 * * 1-5": ("15:05", "N", "5 15 * * 1-5"),
            "35 11 * * 1-5": ("15:35", "Y", "35 15 * * 1-5"),
        }
        self.assertEqual(len(entries), 5)
        self.assertEqual({zone for _, zone in entries}, {"Asia/Seoul"})
        self.assertEqual({cron for cron, _ in entries}, set(expected))
        cases = re.findall(
            r"'([^']+)'\) slot='([^']+)'; report_only='([NY])'; logical_cron='([^']+)' ;;",
            workflow,
        )
        self.assertEqual({c: (slot, report, logical) for c, slot, report, logical in cases}, expected)
        for cron, (slot, report, logical) in expected.items():
            self.assertEqual(bot.SCHEDULE_CRON_TO_KST[logical], slot)
            minute, hour, *_ = cron.split()
            self.assertEqual(int(hour) * 60 + int(minute) + 240,
                             int(slot[:2]) * 60 + int(slot[3:]))
            self.assertEqual(report == "Y", logical == bot.REPORT_ONLY_CRON)
        self.assertIn("BOT_SCHEDULE_CRON: ${{ steps.schedule.outputs.logical_cron || '' }}", workflow)
        self.assertIn("RUN_REPORT_ONLY: ${{ steps.schedule.outputs.report_only || 'N' }}", workflow)
        self.assertIn("group: kis-bot-single-account", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("workflow_dispatch:", workflow)

    def test_live_order_is_blocked_after_market_close(self):
        with patch.object(
            bot, "now_kst",
            return_value=bot.KST.localize(datetime(2026, 10, 21, 15, 36)),
        ), patch.object(bot, "DRY_RUN", False), \
             patch.object(bot, "api_request") as request:
            with self.assertRaisesRegex(bot.TradeSafetyError, "정규장 시간 외"):
                bot.execute_order("fake", "423160", 1, strategy="BAA")
        request.assert_not_called()

    def test_broker_quantity_must_be_explicit_and_nonnegative_integer(self):
        for value in [None, "", "unknown", "-1", "1.5", "nan", True, "999999999999999999999999999999999999"]:
            with self.subTest(value=value), self.assertRaises(bot.TradeSafetyError):
                bot.share_quantity(value)
        self.assertEqual(bot.share_quantity("1"), 1)
