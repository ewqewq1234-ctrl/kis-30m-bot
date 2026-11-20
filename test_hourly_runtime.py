import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import bot_once as bot

class HourlyRuntimeTests(unittest.TestCase):
    def clock(self, day, hour, minute=12):
        return bot.KST.localize(datetime(2026, 10, day, hour, minute))

    def test_modes_follow_actual_time_and_weekend(self):
        cases = [(23,5,59,"OFF"),(23,6,0,"PRE_MARKET"),(23,6,18,"PRE_MARKET"),
                 (23,8,59,"PRE_MARKET"),(23,9,0,"TRADE"),(23,9,12,"TRADE"),(23,14,12,"TRADE"),
                 (23,15,12,"TRADE_REPORT"),(23,15,21,"REPORT"),
                 (23,16,12,"REPORT"),(23,17,0,"OFF"),(24,11,12,"OFF")]
        for day,hour,minute,expected in cases:
            self.assertEqual(bot.hourly_runtime_mode(self.clock(day,hour,minute)),expected)

    def test_premarket_and_holiday_runs_do_readiness_checks_without_trading(self):
        state = {"buy_week": "2026-44", "buy_batch_status": "IN_PROGRESS",
                 "buy_batch_scan_date": "2026-10-20", "buy_batch_source_sha": "sha"}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base_dir = Path(temporary.name)
        state_file = base_dir / "dividend_state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        with patch.dict(os.environ, {"HOURLY_OPERATION":"Y"}), \
             patch.object(bot, "validate_environment"), \
             patch.object(bot, "BASE_DIR", base_dir), \
             patch.object(bot, "get_access_token", return_value="token"), \
             patch.object(bot, "get_account_balance", return_value=({}, 100000, 100000)) as balance, \
             patch.object(bot, "is_market_open_today", return_value=True), \
             patch.object(bot.dividend_trading, "snapshot_readiness", return_value="ready") as readiness, \
             patch.object(bot, "run_baa") as baa, \
             patch.object(bot.dividend_trading, "entries") as entries, \
             patch.object(bot.dividend_trading, "exits") as exits, \
             patch.object(bot, "LOCK_FILE", base_dir / "bot.lock"):
            output = StringIO()
            with patch.object(bot, "now_kst", return_value=self.clock(23, 6, 18)), redirect_stdout(output):
                bot.main()
            self.assertIn("PRE_MARKET_DEFERRED", output.getvalue())
            balance.assert_called_once()
            readiness.assert_called_once()
            baa.assert_not_called()
            entries.assert_not_called()
            exits.assert_not_called()
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), state)
            balance.reset_mock()
            readiness.reset_mock()
            output = StringIO()
            with patch.object(bot, "now_kst", return_value=self.clock(21, 9, 12)), \
                 patch.object(bot, "is_market_open_today", return_value=False), redirect_stdout(output):
                bot.main()
            self.assertIn("MARKET_CLOSED", output.getvalue())
            balance.assert_called_once()
            readiness.assert_called_once()
            baa.assert_not_called()
            entries.assert_not_called()
            exits.assert_not_called()
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), state)

    def test_offhours_return_before_auth_state_or_alert(self):
        for now in [self.clock(23,3),self.clock(23,18),self.clock(24,11)]:
            with patch.dict(os.environ,{"HOURLY_OPERATION":"Y"}), \
                 patch.object(bot,"now_kst",return_value=now), \
                 patch.object(bot,"get_access_token") as auth, \
                 patch.object(bot,"load_json") as state, \
                 patch.object(bot,"send_telegram") as alert:
                bot.main()
                auth.assert_not_called();state.assert_not_called();alert.assert_not_called()

    def test_closing_route_never_runs_broker_or_strategies(self):
        with patch.dict(os.environ,{"HOURLY_OPERATION":"Y"}), \
             patch.object(bot,"now_kst",return_value=self.clock(23,16)), \
             patch.object(bot,"send_hourly_session_report") as report, \
             patch.object(bot,"get_access_token") as auth, \
             patch.object(bot,"run_baa") as baa,patch.object(bot,"run_swing") as swing:
            bot.main()
            report.assert_called_once();auth.assert_not_called();baa.assert_not_called();swing.assert_not_called()

    def test_report_phase_dedup_and_send_failure_retry(self):
        state={}
        def load(path, default):
            return dict(state) if path==bot.SCHEDULE_STATE_FILE else dict(default)
        def save(path, value):
            if path==bot.SCHEDULE_STATE_FILE: state.update(value)
        with patch.object(bot,"load_json",side_effect=load), \
             patch.object(bot,"save_json",side_effect=save), \
             patch.object(bot,"send_telegram",return_value=True) as send:
            with patch.object(bot,"now_kst",return_value=self.clock(23,15)):
                self.assertTrue(bot.send_hourly_session_report())
                self.assertFalse(bot.send_hourly_session_report())
            with patch.object(bot,"now_kst",return_value=self.clock(23,16)):
                send.return_value=False
                self.assertFalse(bot.send_hourly_session_report())
                self.assertTrue(state['last_hourly_report'].endswith('provisional'))
                send.return_value=True
                self.assertTrue(bot.send_hourly_session_report())
                self.assertTrue(state['last_hourly_report'].endswith('closing'))
