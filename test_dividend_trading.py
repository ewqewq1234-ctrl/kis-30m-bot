"""Offline live-policy tests with a fake broker, never market results."""
import copy
from datetime import datetime, date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from dividend_value import trading as t


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.files={}; self.orders=[]
        def fail(message): raise ValueError(message)
        def load(p,default): return copy.deepcopy(self.files.get(str(p).replace('\\','/'),default))
        def save(p,data): self.files[str(p).replace('\\','/')]=copy.deepcopy(data)
        self.bot=SimpleNamespace(BASE_DIR=Path('/fake'),BAA_STATE_FILE=Path('/fake/baa.json'),
            CODE_BULL='069500',ATTACK_UNIVERSE={},DEFENSE_UNIVERSE={},
            share_quantity=lambda v:int(v),fail=fail,load_json=load,save_json=save,
            journal_orders=lambda:self.orders,now_kst=lambda:datetime(2026,11,3,10,12),
            get_current_price=Mock(return_value=100),execute_order=Mock(),assert_no_pending=Mock(),
            DRY_RUN=False,DIVIDEND_VALUE_TARGET_RATIO=.4,limit_buy_tick=lambda p:int(p),
            previous_exchange_session=lambda *a:date(2026,11,2),
            is_market_open_date=lambda tok,d:d.weekday()<5,
            get_account_balance=Mock(return_value=({},100000,100000)))

    def position(self):
        self.orders=[{'id':'buy','strategy':'DIVIDEND','side':'BUY','code':'000001',
            'requested_qty':3,'confirmed_fill_qty':3,'confirmed_fill_amt':300,
            'created_at':'2026-11-03 10:00:00','strategy_metadata':{'corp_code':'corp','industry':'10'}}]
        return t.rebuild(self.bot)

    def snapshot(self, **updates):
        data={'as_of':'2026-11-02','scan_date':'2026-11-03','source_sha':'sha','partial':False,
            'candidates':[{'code':'000002','status':'PASS','corp_code':'other','industry':'20',
                'avg_turnover_20d':100000000,'metrics':{'buy_ceiling':110,'tax_quantity':{'additional_qty':5}}}]}
        data.update(updates);self.files['/fake/dividend_signals.json']=data

    def test_journal_rebuild_tracks_only_verified_fills(self):
        s=self.position(); self.assertEqual(s['positions']['000001']['avg_price'],100)
        self.orders[0]['confirmed_fill_qty']=0
        with self.assertRaises(ValueError): t.rebuild(self.bot)

    def test_15_percent_arms_and_5_percent_exits_with_persisted_high(self):
        self.position(); holdings={'000001':{'qty':3}}
        self.bot.get_current_price.return_value=120
        t.exits(self.bot,'token',holdings,0,100000)
        self.bot.execute_order.assert_not_called()
        self.assertTrue(self.files['/fake/dividend_state.json']['positions']['000001']['armed'])
        self.bot.get_current_price.return_value=114
        t.exits(self.bot,'token',holdings,0,100000)
        self.bot.execute_order.assert_called_once()
        self.assertEqual(self.bot.execute_order.call_args.args[2:4],(3,'01'))

    def test_below_activation_does_not_trigger_a_fixed_stop(self):
        self.position();self.bot.get_current_price.return_value=90
        t.exits(self.bot,'token',{'000001':{'qty':3}},0,1000)
        self.bot.execute_order.assert_not_called()

    def test_ownership_mismatch_blocks_exit(self):
        self.position()
        with self.assertRaises(ValueError): t.exits(self.bot,'token',{'000001':{'qty':4}},0,1000)
        self.bot.execute_order.assert_not_called()

    def test_buy_cycle_attempts_once_daily_and_retries_wednesday(self):
        self.snapshot()
        with patch.dict('os.environ',GITHUB_SHA='sha'):
            t.entries(self.bot,'token',{},100000,100000)
            t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_called_once()
        self.assertEqual(self.bot.execute_order.call_args.args[2],5)
        self.bot.now_kst=lambda:datetime(2026,11,4,10,12)
        t.entries(self.bot,'token',{},100000,100000)
        self.assertEqual(self.bot.execute_order.call_count,2)
        self.assertEqual(self.bot.execute_order.call_args.args[2],5)

    def test_november_2_one_share_entry_and_durable_weekly_lock(self):
        self.bot.now_kst=lambda:datetime(2026,11,2,11,12)
        self.bot.previous_exchange_session=lambda *a:date(2026,10,30)
        self.snapshot(as_of='2026-10-30',scan_date='2026-11-02')
        self.files['/fake/dividend_signals.json']['candidates'].append(
            dict(self.files['/fake/dividend_signals.json']['candidates'][0],code='000003'))
        with patch.dict('os.environ',GITHUB_SHA='sha'):
            t.entries(self.bot,'token',{},100000,100000)
            t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_called_once()
        self.assertEqual(self.bot.execute_order.call_args.args[2],1)
        state=self.files['/fake/dividend_state.json']
        self.assertEqual(state['buy_week'],'2026-45')
        self.assertEqual(state['buy_permits']['000002']['qty'],1)
        t.guard_order(self.bot,'000002',1,'02',state['buy_permits']['000002']['metadata'])
        with self.assertRaises(ValueError):
            t.guard_order(self.bot,'000002',2,'02',state['buy_permits']['000002']['metadata'])

    def test_november_2_no_pass_does_not_consume_tuesday(self):
        self.bot.now_kst=lambda:datetime(2026,11,2,11,12)
        self.bot.previous_exchange_session=lambda *a:date(2026,10,30)
        self.snapshot(as_of='2026-10-30',scan_date='2026-11-02')
        self.files['/fake/dividend_signals.json']['candidates']=[]
        with patch.dict('os.environ',GITHUB_SHA='sha'):
            t.entries(self.bot,'token',{},100000,100000)
        self.assertNotIn('buy_week',self.files.get('/fake/dividend_state.json',{}))
        self.bot.execute_order.assert_not_called()

    def test_november_3_still_needs_tuesday_snapshot(self):
        self.bot.now_kst=lambda:datetime(2026,11,3,11,12)
        self.snapshot(as_of='2026-10-30',scan_date='2026-11-02')
        with patch.dict('os.environ',GITHUB_SHA='sha'):
            t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_not_called()

    def test_stale_partial_or_foreign_code_snapshot_blocks_buy(self):
        for update in ({'as_of':'2026-10-30'},{'partial':True},{'source_sha':'old'},{'scan_date':'2026-11-02'}):
            self.snapshot(**update)
            with patch.dict('os.environ',GITHUB_SHA='sha'):
                t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_not_called()

    def test_review_never_bought_and_existing_stock_not_added(self):
        self.snapshot();self.files['/fake/dividend_signals.json']['candidates'][0]['status']='REVIEW'
        with patch.dict('os.environ',GITHUB_SHA='sha'): t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_not_called()

    def test_cooldown_counts_exchange_sessions(self):
        self.assertFalse(t.cooldown_complete(self.bot,'token','2026-11-02',date(2026,11,3)))
        self.assertTrue(t.cooldown_complete(self.bot,'token','2026-09-01',date(2026,11,3)))

    def test_direct_unpermitted_orders_blocked(self):
        with self.assertRaises(ValueError): t.guard_order(self.bot,'000001',1,'02',{})
        with self.assertRaises(ValueError): t.guard_order(self.bot,'000001',1,'01',{})

    def test_dry_run_does_not_consume_week_or_submit(self):
        self.bot.DRY_RUN=True;self.snapshot()
        with patch.dict('os.environ',GITHUB_SHA='sha'): t.entries(self.bot,'token',{},100000,100000)
        self.bot.execute_order.assert_not_called()
        self.assertNotIn('buy_week',self.files.get('/fake/dividend_state.json',{}))

    def test_reentry_replays_closed_journal_without_inheriting_old_peak(self):
        self.position()
        self.orders += [dict(self.orders[0], id='sell', side='SELL',created_at='2026-11-03 11:00:00'),
                        dict(self.orders[0], id='new-buy',created_at='2026-12-08 10:00:00')]
        s=t.rebuild(self.bot)
        self.assertEqual(s['positions']['000001']['entry_id'],'new-buy')
        self.assertEqual(t.rebuild(self.bot)['positions']['000001']['high'],100)

    def test_cash_and_turnover_limits_block_unfundable_share(self):
        for cash,turnover in [(0,100000000),(100000,1)]:
            self.files={};self.snapshot()
            self.files['/fake/dividend_signals.json']['candidates'][0]['avg_turnover_20d']=turnover
            with patch.dict('os.environ',GITHUB_SHA='sha'): t.entries(self.bot,'token',{},cash,100000)
        self.bot.execute_order.assert_not_called()

    def test_sector_at_twenty_percent_blocks_entry(self):
        self.position();self.orders[0].update(requested_qty=80,confirmed_fill_qty=80,confirmed_fill_amt=8000)
        self.snapshot();self.files['/fake/dividend_signals.json']['candidates'][0]['industry']='10'
        with patch.dict('os.environ',GITHUB_SHA='sha'):
            t.entries(self.bot,'token',{'000001':{'qty':80}},100000,100000)
        self.bot.execute_order.assert_not_called()
