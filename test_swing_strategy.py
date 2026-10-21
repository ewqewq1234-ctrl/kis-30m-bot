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

    def day_order(self, order_id, side, timestamp, *, requested=1, filled=1,
                  amount=100000, status="FILLED", strategy="DAY", **extra):
        order = {
            "id": order_id, "strategy": strategy, "code": "069500", "side": side,
            "requested_qty": requested, "confirmed_fill_qty": filled,
            "confirmed_fill_amt": amount if filled else 0,
            "status": status, "created_at": timestamp,
        }
        order.update(extra)
        return order

    def swing_order(self, order_id, side, timestamp, *, price=123450, atr=250,
                    status="FILLED", filled=1, **extra):
        order = {
            "id": order_id, "strategy": "SWING", "code": "069500", "side": side,
            "requested_qty": 1, "confirmed_fill_qty": filled,
            "confirmed_fill_amt": price * filled if filled else 0,
            "status": status, "created_at": timestamp,
        }
        if side == "BUY":
            session = timestamp[:10]
            entry_date = datetime.strptime(session, "%Y-%m-%d").date()
            order["strategy_metadata"] = {
                "entry_atr14": atr, "entry_session": session,
                "signal_date": (entry_date - timedelta(days=1)).isoformat(),
                "signal_close": max(1, price - 100),
            }
        order.update(extra)
        return order

    def save_journal(self, orders):
        self.write("trade_history.json", {"orders": orders})

    def complete_migration(self, holdings=None):
        return bot.migrate_swing_state_if_safe(holdings or {})

    def set_open_position(self, order_id, session, *, price=100, atr=10,
                          high=None, active_stop=None, **updates):
        self.save_journal([self.swing_order(
            order_id, "BUY", f"{session} 10:00:00", price=price, atr=atr,
        )])
        initial = price - 2 * atr
        state = bot.default_swing_state()
        state.update({
            "migration_status": "COMPLETE",
            "migration_completed_at": "2026-10-20 08:52:00",
            "position_id": order_id, "entry_order_intent_id": order_id,
            "owned_qty": 1, "verified_entry_price": price, "entry_atr14": atr,
            "entry_session": session, "initial_stop": initial,
            "active_stop": initial if active_stop is None else active_stop,
            "highest_completed_close": high,
            "entry_opportunity_status": "FILLED",
            "entry_opportunity_date": session,
            "last_processed_bar_date": session,
        })
        state.update(updates)
        bot.save_json(bot.SWING_STATE_FILE, state)
        return state

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

    def test_empty_history_migration_is_complete_and_idempotent(self):
        self.save_journal([])
        self.write("baa_state.json", {"target_code": "423160", "owned_qty": 6})
        before = (Path(self.tmp.name) / "trade_history.json").read_bytes()
        migrated = self.complete_migration({"423160": {"qty": 6}})
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["migration_status"], "COMPLETE")
        self.assertTrue(migrated["migration_completed_at"])
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})["owned_qty"], 6)
        self.assertEqual(self.complete_migration({"423160": {"qty": 6}}), migrated)
        self.assertEqual((Path(self.tmp.name) / "trade_history.json").read_bytes(), before)

    def test_filled_historical_day_cycle_migrates_without_rewriting_history(self):
        orders = [
            self.day_order("legacy-buy", "BUY", "2026-09-01 09:10:00"),
            self.day_order("legacy-sell", "SELL", "2026-09-01 10:20:00", amount=101000),
        ]
        self.save_journal(orders)
        before = (Path(self.tmp.name) / "trade_history.json").read_bytes()
        migrated = self.complete_migration()
        self.assertEqual(migrated["migration_status"], "COMPLETE")
        self.assertEqual((Path(self.tmp.name) / "trade_history.json").read_bytes(), before)

    def test_unrelated_strategy_orders_do_not_block_flat_kodex_migration(self):
        unrelated_day = self.day_order("other-day", "BUY", "2026-09-01 09:10:00")
        unrelated_day["code"] = "005930"
        unrelated_baa = self.day_order(
            "other-baa", "BUY", "2026-09-01 09:11:00", strategy="BAA",
        )
        unrelated_baa["code"] = "423160"
        self.save_journal([unrelated_day, unrelated_baa])
        self.assertEqual(self.complete_migration()["migration_status"], "COMPLETE")

    def test_nonzero_day_journal_or_account_position_blocks_migration(self):
        self.save_journal([self.day_order("legacy-buy", "BUY", "2026-09-01 09:10:00")])
        with self.assertRaisesRegex(bot.TradeSafetyError, "순보유량"):
            self.complete_migration()
        self.save_journal([])
        with self.assertRaisesRegex(bot.TradeSafetyError, "실제 계좌 잔고"):
            self.complete_migration({"069500": {"qty": 1}})

    def test_pending_day_or_conflicting_kodex_order_blocks_migration(self):
        for status, strategy in (("INTENT", "DAY"), ("UNCERTAIN", "DAY"),
                                 ("ACCEPTED", "BAA"), ("PARTIAL", "SWING")):
            with self.subTest(status=status, strategy=strategy):
                self.save_journal([self.day_order(
                    f"{status}-{strategy}", "BUY", "2026-09-01 09:10:00",
                    filled=0, amount=0, status=status, strategy=strategy,
                )])
                with self.assertRaises(bot.TradeSafetyError):
                    self.complete_migration()

    def test_canceled_order_confirmed_fills_are_accounted_for(self):
        orders = [
            self.day_order(
                "legacy-partial", "BUY", "2026-09-01 09:10:00", requested=2,
                filled=1, amount=100000, status="CANCELED", confirmed_cancel_qty=1,
            ),
            self.day_order("legacy-sell", "SELL", "2026-09-01 10:20:00", amount=101000),
            self.day_order(
                "rejected", "BUY", "2026-09-02 09:10:00", filled=0, amount=0,
                status="REJECTED",
            ),
            self.day_order(
                "partial-rejected", "BUY", "2026-09-03 09:10:00", requested=2,
                filled=1, amount=100000, status="REJECTED",
                confirmed_rejected_qty=1,
            ),
            self.day_order("partial-rejected-sell", "SELL", "2026-09-03 10:20:00",
                           amount=101000),
        ]
        self.save_journal(orders)
        self.assertEqual(self.complete_migration()["migration_status"], "COMPLETE")

    def test_malformed_or_inconsistent_day_lifecycle_is_rejected(self):
        cases = [
            [self.day_order("bad", "BUY", "2026-09-01 09:10:00", requested=1, filled=2)],
            [self.day_order("oversell", "SELL", "2026-09-01 09:10:00")],
            [
                self.day_order("partial-reject", "BUY", "2026-09-01 09:10:00",
                               requested=2, filled=1, status="REJECTED"),
            ],
            [
                self.day_order("bad-cancel", "BUY", "2026-09-01 09:10:00",
                               requested=2, filled=1, status="CANCELED",
                               confirmed_cancel_qty=0),
                self.day_order("bad-cancel-sell", "SELL", "2026-09-01 10:20:00"),
            ],
            [
                self.day_order("first", "BUY", "2026-09-01 09:10:00"),
                self.day_order("first", "SELL", "2026-09-01 10:20:00"),
            ],
        ]
        for orders in cases:
            with self.subTest(orders=orders):
                self.save_journal(orders)
                with self.assertRaises(bot.TradeSafetyError):
                    self.complete_migration()

    def test_conflicting_baa_state_blocks_migration(self):
        self.save_journal([])
        self.write("baa_state.json", {"target_code": "069500", "owned_qty": 0})
        with self.assertRaisesRegex(bot.TradeSafetyError, "BAA 상태"):
            self.complete_migration()
        self.write("baa_state.json", {"target_code": "423160", "owned_qty": 0})
        self.save_journal([self.day_order(
            "baa-buy", "BUY", "2026-09-01 09:10:00", strategy="BAA",
        )])
        with self.assertRaisesRegex(bot.TradeSafetyError, "BAA 069500"):
            self.complete_migration()

    def test_safe_flat_migration_preserves_baa_and_legacy_state_blocks(self):
        self.save_journal([])
        self.write("baa_state.json", {"target_code": "423160", "owned_qty": 6})
        migrated = self.complete_migration({"423160": {"qty": 6}})
        self.assertEqual(migrated["migration_status"], "COMPLETE")
        self.assertEqual(bot.load_json(bot.BAA_STATE_FILE, {})["owned_qty"], 6)
        bot.SWING_STATE_FILE.unlink()
        self.write("day_state.json", {"owned_qty": 1, "entry_time": "2026-10-20 10:00"})
        with self.assertRaisesRegex(bot.TradeSafetyError, "DAY"):
            self.complete_migration()
        self.assertFalse(bot.SWING_STATE_FILE.exists())

    def test_rebuild_uses_verified_fill_and_frozen_atr(self):
        self.save_journal([self.swing_order("buy", "BUY", "2026-10-20 10:00:00")])
        bot.save_json(bot.SWING_STATE_FILE, bot.default_swing_state())
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["owned_qty"], 1)
        self.assertEqual(state["verified_entry_price"], 123450)
        self.assertEqual(state["entry_atr14"], 250)
        self.assertEqual(state["initial_stop"], 122950)
        self.assertEqual(state["active_stop"], 122950)
        self.assertIsNone(state["highest_completed_close"])
        self.assertEqual(state["position_id"], "buy")

    def test_reconciliation_preserves_monotonic_trailing_state_for_same_position(self):
        self.set_open_position(
            "buy-1", "2026-10-20", price=123450, atr=250,
            high=124500, active_stop=123750,
        )
        bot.rebuild_strategy_state()
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["position_id"], "buy-1")
        self.assertEqual(state["highest_completed_close"], 124500)
        self.assertEqual(state["active_stop"], 123750)

    def test_verified_exit_clears_position_state_and_keeps_same_session_lock(self):
        self.set_open_position(
            "buy-1", "2026-10-20", high=150, active_stop=120,
            exit_intent="TREND", exit_order_intent_id="sell-1",
        )
        journal = bot.read_journal()
        journal["orders"].append(self.swing_order(
            "sell-1", "SELL", "2026-10-21 10:00:00", status="FILLED",
        ))
        bot.save_json(bot.TRADE_HISTORY_FILE, journal)
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["owned_qty"], 0)
        self.assertIsNone(state["position_id"])
        self.assertIsNone(state["highest_completed_close"])
        self.assertIsNone(state["active_stop"])
        self.assertIsNone(state["exit_intent"])
        self.assertIsNone(state["exit_order_intent_id"])
        self.assertEqual(state["entry_opportunity_status"], "CLOSED")
        self.assertEqual(state["last_exit_session"], "2026-10-21")
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 100)] + [100.5])
        pending_entry = self.ready_entry_state(today, bars[-1]["date"])
        pending_entry["last_exit_session"] = today.isoformat()
        bot.save_json(bot.SWING_STATE_FILE, pending_entry)
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price") as quote:
            bot.run_swing("offline", {}, 10000, 100000)
        quote.assert_not_called()

    def test_reentry_gets_new_stops_and_cannot_inherit_previous_high(self):
        self.set_open_position(
            "buy-1", "2026-10-19", price=20000, atr=1000,
            high=50000, active_stop=47000, exit_intent="TREND",
        )
        journal = bot.read_journal()
        journal["orders"].append(self.swing_order(
            "sell-1", "SELL", "2026-10-20 15:00:00", price=20000,
        ))
        journal["orders"].append(self.swing_order(
            "buy-2", "BUY", "2026-10-21 10:00:00", price=10000, atr=500,
        ))
        bot.save_json(bot.TRADE_HISTORY_FILE, journal)
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["position_id"], "buy-2")
        self.assertEqual(state["initial_stop"], 9000)
        self.assertEqual(state["active_stop"], 9000)
        self.assertIsNone(state["highest_completed_close"])
        self.assertIsNone(state["exit_intent"])
        self.assertEqual(state["last_exit_session"], "2026-10-20")
        bars = self.bars([1000.0] * 98 + [10100.0, 10200.0])
        state["last_processed_bar_date"] = bars[-1]["date"].isoformat()
        bot.save_json(bot.SWING_STATE_FILE, state)
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=9500), \
             patch.object(bot, "execute_order") as order:
            bot.run_swing(
                "offline", {"069500": {"qty": 1, "avg_price": 10000}}, 10000, 100000,
            )
        order.assert_not_called()
        self.assertEqual(bot.load_json(bot.SWING_STATE_FILE, {})["position_id"], "buy-2")

    def test_crash_recovery_uses_verified_fill_without_inheriting_partial_state(self):
        self.save_journal([self.swing_order("buy-crash", "BUY", "2026-10-20 10:00:00")])
        state = bot.default_swing_state()
        state.update({
            "migration_status": "COMPLETE",
            "migration_completed_at": "2026-10-20 08:52:00",
            "entry_order_intent_id": "buy-crash",
            "entry_opportunity_status": "SUBMITTING",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        bot.rebuild_strategy_state()
        recovered = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(recovered["position_id"], "buy-crash")
        self.assertEqual(recovered["owned_qty"], 1)
        self.assertEqual(recovered["initial_stop"], 122950)
        self.assertEqual(recovered["active_stop"], 122950)
        self.assertIsNone(recovered["highest_completed_close"])
        self.assertEqual(recovered["entry_opportunity_status"], "FILLED")

    def test_pending_orders_do_not_create_or_reset_positions(self):
        self.save_journal([self.swing_order(
            "pending-buy", "BUY", "2026-10-20 10:00:00", status="INTENT",
            filled=0,
        )])
        state = bot.default_swing_state()
        state.update({
            "entry_order_intent_id": "pending-buy",
            "entry_opportunity_status": "SUBMITTING",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        bot.rebuild_strategy_state()
        pending_state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(pending_state["owned_qty"], 0)
        self.assertIsNone(pending_state["position_id"])
        self.assertEqual(pending_state["entry_order_intent_id"], "pending-buy")
        self.assertEqual(pending_state["entry_opportunity_status"], "SUBMITTING")

        self.set_open_position(
            "buy-live", "2026-10-20", high=140, active_stop=110,
            exit_intent="TREND", exit_order_intent_id="pending-sell",
        )
        journal = bot.read_journal()
        journal["orders"].append(self.swing_order(
            "pending-sell", "SELL", "2026-10-21 10:00:00", status="ACCEPTED",
            filled=0,
        ))
        bot.save_json(bot.TRADE_HISTORY_FILE, journal)
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["position_id"], "buy-live")
        self.assertEqual(state["active_stop"], 110)
        self.assertEqual(state["highest_completed_close"], 140)
        self.assertEqual(state["exit_intent"], "TREND")

    def test_staged_but_unjournaled_entry_is_safely_reopened(self):
        state = bot.default_swing_state()
        state.update({
            "migration_status": "COMPLETE",
            "migration_completed_at": "2026-10-20 08:52:00",
            "entry_order_intent_id": "not-yet-journaled",
            "entry_opportunity_status": "SUBMITTING",
        })
        bot.save_json(bot.SWING_STATE_FILE, state)
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["owned_qty"], 0)
        self.assertIsNone(state["position_id"])
        self.assertEqual(state["entry_opportunity_status"], "PENDING")
        self.assertIsNone(state["entry_order_intent_id"])

    def test_trailing_stop_is_monotonic_across_repeated_reconciliation(self):
        self.set_open_position("buy-1", "2026-10-20", high=150, active_stop=120)
        bot.rebuild_strategy_state()
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        state["highest_completed_close"] = 170
        state["active_stop"] = 140
        bot.save_json(bot.SWING_STATE_FILE, state)
        bot.rebuild_strategy_state()
        first_stop = bot.load_json(bot.SWING_STATE_FILE, {})["active_stop"]
        bot.rebuild_strategy_state()
        second_stop = bot.load_json(bot.SWING_STATE_FILE, {})["active_stop"]
        self.assertEqual(first_stop, 140)
        self.assertEqual(second_stop, first_stop)

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
        state = self.set_open_position(
            "buy-1", bars[-2]["date"].isoformat(), price=100, atr=10,
            high=100, active_stop=80,
            last_processed_bar_date=bars[-2]["date"].isoformat(),
            signal_date=latest, signal_status="NO_ENTRY",
        )
        bot.save_json(bot.SWING_STATE_FILE, state)
        holdings = {"069500": {"qty": 1, "avg_price": 100.0}}
        with patch.object(bot, "now_kst", return_value=bot.KST.localize(datetime(2026, 10, 21, 10, 0))), \
             patch.object(bot, "get_validated_daily_bars", return_value=bars), \
             patch.object(bot, "get_current_price", return_value=101), \
             patch.object(bot, "execute_order") as order:
            bot.run_swing("fake", holdings, 10000, 100000)
        state = bot.load_json(bot.SWING_STATE_FILE, {})
        self.assertEqual(state["highest_completed_close"], 111)
        self.assertEqual(state["active_stop"], 81)
        order.assert_not_called()

    def test_protective_exit_overrides_trend_and_out_of_window_preserves_intent(self):
        today = date(2026, 10, 21)
        bars = self.bars([float(value) for value in range(1, 101)])
        latest = bars[-1]["date"].isoformat()
        state = self.set_open_position(
            "buy-1", bars[-2]["date"].isoformat(), price=100, atr=10,
            signal_date=latest, last_processed_bar_date=latest,
            exit_intent="TREND",
        )
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
        state = self.set_open_position(
            "buy-1", bars[-2]["date"].isoformat(), price=100, atr=10,
            signal_date=latest, last_processed_bar_date=latest,
            exit_intent="TREND",
        )
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
