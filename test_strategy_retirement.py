"""Linux bot integration: retired SWING must never reach an order API."""
import unittest
from unittest.mock import patch
import test_bot_once
import bot_once as bot
from portfolio_config import BAA_TARGET_RATIO, DIVIDEND_VALUE_TARGET_RATIO


class RetirementTests(unittest.TestCase):
    setUp = test_bot_once.BotSafetyTests.setUp
    write = test_bot_once.BotSafetyTests.write

    def test_allocation_split(self):
        self.assertEqual((BAA_TARGET_RATIO,DIVIDEND_VALUE_TARGET_RATIO),(.6,.4))

    def test_retired_entry_point_and_both_sides_block_before_broker(self):
        with patch.object(bot,'api_request') as api:
            with self.assertRaises(bot.TradeSafetyError): bot.run_swing('fake',{},0,0)
            for strategy in ('SWING','BAA'):
                for side in ('01','02'):
                    with self.subTest(strategy=strategy,side=side), self.assertRaises(bot.TradeSafetyError):
                        bot.execute_order('fake','069500',1,side,strategy=strategy)
            api.assert_not_called()

    def test_legacy_holdings_not_automatically_sold_or_abandoned(self):
        with patch.object(bot,'execute_order') as order:
            with self.assertRaises(bot.TradeSafetyError):
                bot.assert_retired_swing_flat({'069500':{'qty':1}})
            bot.assert_retired_swing_flat({})
            self.write('swing_state.json',{'owned_qty':1})
            with self.assertRaises(bot.TradeSafetyError): bot.assert_retired_swing_flat({})
            order.assert_not_called()
