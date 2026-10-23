import os
import unittest
from datetime import datetime
from unittest.mock import patch
import bot_once as bot

class HourlyRuntimeTests(unittest.TestCase):
    def clock(self, day, hour, minute=12):
        return bot.KST.localize(datetime(2026, 10, day, hour, minute))

    def test_modes_follow_actual_time_and_weekend(self):
        cases = [(23,8,12,"OFF"),(23,9,12,"TRADE"),(23,14,12,"TRADE"),
                 (23,15,12,"TRADE_REPORT"),(23,15,21,"REPORT"),
                 (23,16,12,"REPORT"),(23,17,0,"OFF"),(24,11,12,"OFF")]
        for day,hour,minute,expected in cases:
            self.assertEqual(bot.hourly_runtime_mode(self.clock(day,hour,minute)),expected)

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
