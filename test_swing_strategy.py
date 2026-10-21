"""Offline regressions for KODEX 200 daily-bar SWING behavior."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import bot_once as bot


class SwingStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = {
            "BASE_DIR": root,
            "DAY_STATE_FILE": root / "day_state.json",
            "SWING_STATE_FILE": root / "swing_state.json",
            "BAA_STATE_FILE": root / "baa_state.json",
            "TRADE_HISTORY_FILE": root / "trade_history.json",
            "HOLIDAY_CACHE_FILE": root / "holiday_cache.json",
        }
        for name, value in paths.items():
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"GITHUB_STATE_MODE": "off"})
        env.start()
        self.addCleanup(env.stop)
        telegram = patch.object(bot, "send_telegram")
        telegram.start()
        self.addCleanup(telegram.stop)

    def write(self, name, value):
        (Path(self.tmp.name) / name).write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def bars(closes):
        first = date(2026, 1, 5)
        result = []
        day = first
        for close in closes:
            while day.weekday() >= 5:
                day += timedelta(days=1)
            result.append({
                "date": day, "open": float(close), "high": float(close) + 1,
                "low": float(close) - 1, "close": float(close), "volume": 1000.0,
            })
            day += timedelta(days=1)
        return result

    def ready_entry_state(self, today, bar_date=None):
        state = bot.default_swing_state()
        state.update({
            "signal_date": (bar_date or today - timedelta(days=1)).isoformat(),
            "signal_status": "ENTRY",
            "entry_signal_date": (bar_date or today - timedelta(days=1)).isoformat(),
            "entry_signal_close": 100.0, "signal_atr14": 10.0,
            "entry_opportunity_date": today.isoformat(),
            "entry_opportunity_status": "PENDING",
            "last_processed_bar_date": (bar_date or today - timedelta(days=1)).isoformat(),
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        return state

    def test_signal_excludes_current_bar_and_uses_sma_and_atr(self):
        closes = [float(value) for value in range(1, 100)] + [100.5]
        bars = self.bars(closes)
        result = bot.swing_signal(bars)
        self.assertTrue(result["entry"])
        self.assertGreater(result["sma20"], result["sma60"])
        self.assertGreater(bot.wilder_atr(bars), 0)
        bars[-1]["close"] = max(item["close"] for item in bars[-21:-1])
        bars[-1]["open"] = bars[-1]["close"]
        bars[-1]["high"] = bars[-1]["close"] + 1
        bars[-1]["low"] = bars[-1]["close"] - 1
        self.assertFalse(bot.swing_signal(bars)["entry"])

    def test_wilder_atr_seed_and_recursion(self):
        bars = self.bars([100.0 + index for index in range(16)])
        bars[14]["high"] += 5
        seed_ranges = []
        for prior, current in zip(bars[:15], bars[1:15]):
            seed_ranges.append(max(
                current["high"] - current["low"],
                abs(current["high"] - prior["close"]),
                abs(current["low"] - prior["close"]),
            ))
        atr = sum(seed_ranges[:14]) / 14
        tr15 = max(
            bars[15]["high"] - bars[15]["low"],
            abs(bars[15]["high"] - bars[14]["close"]),
            abs(bars[15]["low"] - bars[14]["close"]),
        )
        expected = (13 * atr + tr15) / 14
        self.assertAlmostEqual(bot.wilder_atr(bars), expected)

    def test_trend_exit_excludes_signal_bar_from_prior_ten_low(self):
        bars = self.bars([100.0 + index for index in range(99)] + [50.0])
        result = bot.swing_signal(bars)
        self.assertTrue(result["trend_exit"])
        self.assertFalse(result["entry"])

    def test_exchange_session_lookup_uses_authoritative_calendar(self):
        session_date = date(2026, 10, 26)
        open_dates = {date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)}
        with patch.object(
            bot, "is_market_open_date", side_effect=lambda token, day: day in open_dates
        ) as calendar:
            self.assertEqual(bot.previous_exchange_session("fake", session_date), date(2026, 10, 23))
            self.assertEqual(bot.next_exchange_session("fake", date(2026, 10, 23)), session_date)
        self.assertEqual(calendar.call_count, 6)

    def test_daily_bars_exclude_incomplete_today_and_reject_stale_history(self):
        session = date(2026, 10, 21)
        previous = date(2026, 10, 20)
        rows = [
            {"stck_bsop_date": "20261020", "stck_oprc": "100", "stck_hgpr": "110",
             "stck_lwpr": "90", "stck_clpr": "105", "acml_vol": "1000"},
            {"stck_bsop_date": "20261021", "stck_oprc": "106", "stck_hgpr": "107",
             "stck_lwpr": "105", "stck_clpr": "106", "acml_vol": "10"},
        ]
        with patch.object(bot, "previous_exchange_session", return_value=previous), \
             patch.object(bot, "api_request", return_value=({"output2": rows}, {})):
            bars = bot.get_validated_daily_bars("fake", session, target_count=1)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["date"], previous)
        stale_responses = [({"output2": [rows[1]]}, {})] + [({"output2": []}, {})] * 6
        with patch.object(bot, "previous_exchange_session", return_value=previous), \
             patch.object(bot, "api_request", side_effect=stale_responses), \
             patch.object(bot.time, "sleep"):
            with self.assertRaisesRegex(bot.TradeSafetyError, "직전 거래일"):
                bot.get_validated_daily_bars("fake", session, target_count=1)

    def test_daily_bars_reject_duplicate_ohlc_and_nonfinite_values(self):
        session = date(2026, 10, 21)
        previous = date(2026, 10, 20)
        valid = {"stck_bsop_date": "20261020", "stck_oprc": "100", "stck_hgpr": "110",
                 "stck_lwpr": "90", "stck_clpr": "105", "acml_vol": "1000"}
        cases = [
            ([valid, valid], "중복"),
            ([{**valid, "stck_hgpr": "99"}], "OHLC"),
            ([{**valid, "stck_clpr": "NaN"}], "close"),
            ([{**valid, "acml_vol": "-1"}], "거래량"),
        ]
        for rows, message in cases:
            with self.subTest(message=message), \
                 patch.object(bot, "previous_exchange_session", return_value=previous), \
                 patch.object(bot, "api_request", return_value=({"output2": rows}, {})):
                with self.assertRaisesRegex(bot.TradeSafetyError, message):
                    bot.get_validated_daily_bars("fake", session, target_count=1)

    def test_safe_flat_migration_preserves_baa_and_legacy_state_blocks(self):
        self.write("baa_state.json", {"target_code": "423160", "owned_qty": 6})
        migrated = bot.migrate_swing_state_if_safe({"423160": {"qty": 6}})
        self.assertEqual(migrated["schema_version"], 1)
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})["owned_qty"], 6)
        bot.SWING_STATE_FILE.unlink()
        self.write("day_state.json", {"owned_qty": 1, "entry_time": "2026-10-20 10:00"})
        with self.assertRaisesRegex(bot.TradeSafetyError, "legacy DAY"):
            bot.migrate_swing_state_if_safe({})
        self.assertFalse(bot.SWING_STATE_FILE.exists())

    def test_rebuild_uses_verified_fill_and_frozen_atr(self):
        self.write("trade_history.json", {"orders": [{
            "id": "buy", "strategy": "SWING", "code": "069500", "side": "BUY",
            "requested_qty": 1, "confirmed_fill_qty": 1,
            "confirmed_fill_amt": 123450, "status": "FILLED",
            "strategy_metadata": {"entry_atr14": 250, "entry_session": "2026-10-20"},
        }]})
        bot.save_json(bot.SWING_STATE_FILE, bot.default_swing_state())
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["owned_qty"], 1)
        self.assertEqual(state["verified_entry_price"], 123450)
        self.assertEqual(state["entry_atr14"], 250)
        self.assertEqual(state["initial_stop"], 122950)

    def test_premarket_keeps_entry_opportunity_until_eligible_run(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        state = self.ready_entry_state(today, bars[-1]["date"])
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 8, 52))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price") as quote:
            bot.run_swing("fake", {}, 10000, 100000)
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["entry_opportunity_status"], "PENDING")
        quote.assert_not_called()
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 9, 10))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=105), \
             patch.object(bot, "get_buyable_qty", return_value=1), \
             patch.object(bot, "execute_order", return_value=(False, 0)) as order, \
             patch.object(bot, "DRY_RUN", True):
            bot.run_swing("fake", {}, 10000, 100000)
        self.assertEqual(order.call_args.args[2], 1)
        self.assertEqual(order.call_args.kwargs["strategy"], "SWING")
        self.assertEqual(order.call_args.kwargs["ord_dvsn"], "00")

    def test_antichasing_skip_is_persisted_without_retry(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        self.ready_entry_state(today, bars[-1]["date"])
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=111), \
             patch.object(bot, "execute_order") as order:
            bot.run_swing("fake", {}, 10000, 100000)
            bot.run_swing("fake", {}, 10000, 100000)
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["entry_opportunity_status"], "SKIPPED")
        self.assertIn("anti-chasing", state["entry_skip_reason"])
        order.assert_not_called()

    def test_non_next_session_signal_is_not_caught_up(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "next_exchange_session", return_value=today + timedelta(days=1)), \
             patch.object(bot, "get_current_price") as quote:
            bot.run_swing("fake", {}, 10000, 100000)
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["signal_status"], "DATA_SESSION_MISMATCH")
        self.assertEqual(state["entry_opportunity_status"], "NONE")
        quote.assert_not_called()

    def test_sleeve_cash_check_and_entry_cutoff(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        self.ready_entry_state(today, bars[-1]["date"])
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 14, 31))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price") as quote:
            bot.run_swing("fake", {}, 1, 100000)
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["entry_opportunity_status"], "PENDING")
        quote.assert_not_called()
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 14, 30))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=100), \
             patch.object(bot, "get_buyable_qty") as buyable, \
             patch.object(bot, "execute_order") as order:
            bot.run_swing("fake", {}, 1, 100000)
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["entry_opportunity_status"], "SKIPPED")
        buyable.assert_not_called()
        order.assert_not_called()

    def test_limit_price_uses_valid_tick_and_is_sent_to_the_order_api(self):
        self.assertEqual(bot.limit_buy_tick(2008), 2005)
        self.assertEqual(bot.limit_buy_tick(4999), 4995)
        self.assertEqual(bot.limit_buy_tick(5009), 5000)
        bot.save_json(bot.SWING_STATE_FILE, bot.default_swing_state())
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "DRY_RUN", False), \
             patch.object(bot, "order_window", return_value=True), \
             patch.object(bot, "is_market_open_today", return_value=True), \
             patch.object(bot, "assert_no_pending"), \
             patch.object(bot, "check_daily_loss"), \
             patch.object(bot, "get_current_price", return_value=2000), \
             patch.object(bot, "get_buyable_qty", return_value=1), \
             patch.object(bot, "get_account_balance", return_value=({}, 10000, 100000)), \
             patch.object(bot, "ORDER_POLL_SECONDS", 0), \
             patch.object(bot, "ACCOUNT_ORDER_ATTEMPTS", 0), \
             patch.object(bot, "api_request", return_value=({"output": {"ODNO": "1"}}, {})) as api:
            with self.assertRaisesRegex(bot.TradeSafetyError, "미체결"):
                bot.execute_order(
                    "fake", "069500", 1, "02", ord_dvsn="00", price=2005,
                    action_name="SWING entry", strategy="SWING",
                    strategy_metadata={"entry_atr14": 10, "entry_session": "2026-10-21"},
                )
        request = api.call_args
        self.assertEqual(request.args[0], "POST")
        self.assertEqual(request.kwargs["body"]["ORD_DVSN"], "00")
        self.assertEqual(request.kwargs["body"]["ORD_UNPR"], "2005")

    def test_trailing_stop_uses_only_post_entry_closes_and_never_decreases(self):
        today = date(2026, 10, 21)
        bars = self.bars([1000.0] * 98 + [110.0, 111.0])
        latest = bars[-1]["date"].isoformat()
        state = bot.default_swing_state()
        state.update({
            "owned_qty": 1, "verified_entry_price": 100.0, "entry_atr14": 10.0,
            "entry_session": bars[-2]["date"].isoformat(), "initial_stop": 90.0,
            "active_stop": 100.0, "highest_completed_close": 100.0,
            "last_processed_bar_date": bars[-2]["date"].isoformat(),
            "signal_date": latest, "signal_status": "NO_ENTRY",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        holdings = {"069500": {"qty": 1, "avg_price": 100.0}}
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=101), \
             patch.object(bot, "execute_order") as order:
            bot.run_swing("fake", holdings, 10000, 100000)
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["highest_completed_close"], 111)
        self.assertEqual(state["active_stop"], 100)
        order.assert_not_called()

    def test_protective_exit_overrides_trend_and_out_of_window_preserves_intent(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 101)])
        latest = bars[-1]["date"].isoformat()
        state = bot.default_swing_state()
        state.update({
            "owned_qty": 1, "verified_entry_price": 100.0, "entry_atr14": 10.0,
            "entry_session": bars[-2]["date"].isoformat(), "initial_stop": 90.0,
            "active_stop": 90.0, "signal_date": latest,
            "last_processed_bar_date": latest, "exit_intent": "TREND",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        holdings = {"069500": {"qty": 1, "avg_price": 100.0}}
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 15, 20))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=80), \
             patch.object(bot, "execute_order", return_value=(False, 0)) as order:
            bot.run_swing("fake", holdings, 10000, 100000)
        self.assertIn("PROTECTIVE_STOP", order.call_args.kwargs["action_name"])
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        state["exit_intent"] = "TREND"
        bot.save_json(bot.SWING_STATE_FILE, state)
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 15, 21))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price") as quote, \
             patch.object(bot, "execute_order") as late_order:
            bot.run_swing("fake", holdings, 10000, 100000)
        quote.assert_not_called()
        late_order.assert_not_called()
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["exit_intent"], "TREND")

    def test_trend_exit_intent_is_not_cleared_without_verified_fill(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 101)])
        latest = bars[-1]["date"].isoformat()
        state = bot.default_swing_state()
        state.update({
            "owned_qty": 1, "verified_entry_price": 100.0, "entry_atr14": 10.0,
            "entry_session": bars[-2]["date"].isoformat(), "initial_stop": 90.0,
            "active_stop": 90.0, "signal_date": latest,
            "last_processed_bar_date": latest, "exit_intent": "TREND",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        holdings = {"069500": {"qty": 1, "avg_price": 100.0}}
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=80), \
             patch.object(bot, "execute_order", return_value=(False, 0)):
            bot.run_swing("fake", holdings, 10000, 100000)
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["exit_intent"], "PROTECTIVE_STOP")
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["owned_qty"], 1)

    def test_same_session_exit_blocks_reentry_and_expired_signal(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        state = self.ready_entry_state(today, bars[-1]["date"])
        state["last_exit_session"] = today.isoformat()
        bot.save_json(bot.SWING_STATE_FILE, state)
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price") as quote:
            bot.run_swing("fake", {}, 10000, 100000)
        quote.assert_not_called()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        state["entry_opportunity_date"] = "2026-10-20"
        bot.save_json(bot.SWING_STATE_FILE, state)
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", side_effect=bot.TradeSafetyError("stale")):
            bot.run_swing("fake", {}, 10000, 100000)
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["entry_opportunity_status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
