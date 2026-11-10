"""No-network regression tests for dividend/value Tuesday Telegram reporting."""
from datetime import datetime
import json
import os
import unittest
from unittest.mock import Mock, patch

from dividend_value import weekly_telegram as w


class WeeklyTelegramTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 16, 40, tzinfo=w.KST)
        self.buy = {"id": "buy-1", "strategy": "DIVIDEND", "code": "000001",
                    "side": "BUY", "created_at": "2026-11-10 14:43:00",
                    "status": "FILLED", "confirmed_fill_qty": 5,
                    "confirmed_fill_amt": 50000}
        self.sale = {"id": "sale-1", "strategy": "DIVIDEND", "code": "000001",
                     "side": "SELL", "created_at": "2026-11-10 15:00:00",
                     "status": "FILLED", "confirmed_fill_qty": 2,
                     "confirmed_fill_amt": 22000, "sell_cost_basis": 10000}
        self.history = {"orders": [self.buy, self.sale]}
        self.state = {"positions": {"000001": {"qty": 3, "avg_price": 10000}}}
        self.metadata = {"executed_kst": "2026-11-10T12:02:00+09:00",
                         "as_of": "2026-11-09", "universe_total": 4408,
                         "counts": {"PASS": 23, "REVIEW": 41, "FAIL": 2448,
                                    "EXCLUDED": 1896},
                         "query_failed": 12, "unprocessed": 0, "partial": True}
        self.rows = [{"code": "000001", "name": "테스트주", "status": "PASS"}]

    def test_confirmed_fills_only_and_profits(self):
        result = w.summarize_journal(self.history, self.state["positions"], self.now.date())
        self.assertEqual(result["inventory"]["000001"]["qty"], 3)
        self.assertEqual(result["inventory"]["000001"]["cost"], 30000)
        self.assertEqual(result["week_buys"][0]["amount"], 50000)
        self.assertEqual(result["week_sells"][0]["profit"], 2000)
        msgs = w.build_messages(self.metadata, self.rows, self.history, self.state,
                                {"000001": 11500}, now=self.now)
        self.assertIn("조회 미검증 12종목", msgs[0])
        self.assertIn("+2,000원", msgs[0])
        self.assertIn("+15.00%", msgs[0])
        self.assertIn("보유 1종목 · 3주", msgs[0])
        self.assertIn("원장 기준", msgs[0])
        self.assertTrue(all(len(s) < w.MAX_MESSAGE_SIZE for s in msgs))

    def test_incomplete_quote_does_not_fake_returns(self):
        msgs = w.build_messages(self.metadata, self.rows, self.history, self.state, {}, now=self.now)
        self.assertIn("산정불가", msgs[0])
        self.assertIn("시세 미확인", msgs[-1])

    def test_unconfirmed_order_not_counted_as_fill(self):
        self.history["orders"].append({
            "id": "pending", "strategy": "DIVIDEND", "code": "000002",
            "side": "BUY", "created_at": "2026-11-10 14:50:00",
            "status": "UNCERTAIN", "confirmed_fill_qty": 0})
        result = w.summarize_journal(self.history, self.state["positions"], self.now.date())
        self.assertEqual(len(result["week_buys"]), 1)
        self.assertEqual(result["unresolved"], 1)

    def test_broker_journal_mismatch_blocks_valuation(self):
        bad = {"positions": {"000001": {"qty": 4, "avg_price": 10000}}}
        with self.assertRaises(w.ReportError):
            w.build_messages(self.metadata, self.rows, self.history, bad,
                             {"000001": 10000}, now=self.now)
        self.sale["sell_cost_basis"] = 9000
        with self.assertRaises(w.ReportError):
            w.summarize_journal(self.history, self.state["positions"], self.now.date())

    def test_kis_price_adapter_calls_only_quote_endpoint(self):
        api = Mock()
        api.kis.return_value = {"output": {"stck_prpr": "12500"}}
        quotes = w.fetch_quotes(["000001"], api=api)
        self.assertEqual(quotes["000001"], 12500)
        self.assertEqual(api.kis.call_args.args[0], w.PRICE)
        api.kis.side_effect = w.DataError("network error")
        self.assertEqual(w.fetch_quotes(["000002"], api=api), {})

    def test_telegram_failure_does_not_echo_secret_url(self):
        session = Mock()
        session.post.side_effect = w.requests.ConnectTimeout(
            "https://api.telegram.org/botSECRET-API-KEY/sendMessage")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "SECRET-API-KEY",
                                     "TELEGRAM_CHAT_ID": "fake"}):
            with self.assertRaises(w.ReportError) as caught:
                w.send_messages(["hello"], session=session)
        self.assertNotIn("SECRET-API-KEY", str(caught.exception))

    def test_non_tuesday_report_blocked(self):
        with self.assertRaises(w.ReportError):
            w.build_messages(self.metadata, self.rows, self.history,
                             self.state, {}, now=datetime(2026, 11, 11, 16, 40,
                                                          tzinfo=w.KST))

    def test_duplicate_and_inconsistent_fills_rejected(self):
        self.history["orders"].append(dict(self.buy))
        with self.assertRaises(w.ReportError):
            w.summarize_journal(self.history, self.state["positions"], self.now.date())


if __name__ == "__main__":
    unittest.main()
