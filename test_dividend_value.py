"""Synthetic rule tests; these fixtures are NOT market screening results."""
import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dividend_value.data import (PublicData, DataError, normalize_financial,
                                 parse_master, WIDTHS, atomic_json)
from dividend_value.rules import evaluate, tax_quantity, ttm_from_ytd
from dividend_value.screen import export, main, reviewed_evidence, apply_evidence


def fixture():
    return {'code':'000001', 'corp_code':'00000001', 'name':'synthetic',
        'share_class':'common', 'as_of':'2026-10-30', 'expected_fiscal_year':2025,
        'close':10000, 'risk_verified':True, 'risk_flagged':False,
        'annual':[{'year':y, 'scope':'CFS_PARENT', 'unit':'KRW', 'income':100,
            'equity_open':1000, 'equity':1000, 'ocf':120, 'ordinary_dps':500,
            'special_dividend_verified':True, 'split_adjustment_verified':True,
            'dividend_final':True, 'share_class':'common', 'cash_dividends_parent':50,
            'payout_scope_verified':True, 'total_taxable_annual_dps':500} for y in range(2021,2026)],
        'latest_balance':{'equity_total':1000,'liabilities':500,'capital':100,
                          'unit':'KRW','latest_disclosure_verified':True},
        'valuation':{'eps':1500,'bps':12000,'basis':'annual','basis_verified':True,
                     'share_class':'common','price_date':'2026-10-30'},
        'audit':'적정','audit_verified':True,'avg_turnover_20d':100000000}


class RulesTests(unittest.TestCase):
    def test_all_rules_pass_with_verified_synthetic_evidence(self):
        r = evaluate(fixture(), strategy_nav=4000000)
        self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['metrics']['initial_budget'],120000)
        self.assertEqual(r['metrics']['estimated_buy_qty'],12)
        self.assertEqual(r['metrics']['history_method'],'five_years')

    def test_history_or_high_yield_does_not_waive_other_rules(self):
        f=fixture()
        f['annual'][0]['ordinary_dps']=0
        for a in f['annual'][-3:]: a['ordinary_dps']=800
        r=evaluate(f)
        self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['metrics']['history_method'],'high_yield')
        f['annual'][0]['income']=-1
        self.assertEqual(evaluate(f)['status'],'FAIL')

    def test_special_dividend_excluded_from_yield_but_taxable(self):
        f=fixture()
        for a in f['annual']:
            a['ordinary_dps']=300
            a['total_taxable_annual_dps']=1000
        r=evaluate(f)
        self.assertFalse(r['checks']['dividend_yield'])
        self.assertEqual(r['metrics']['tax_quantity']['max_total_qty'],7)
        f['annual'][-1]['special_dividend_verified']=False
        self.assertIsNone(evaluate(f)['metrics']['base_dividend'])

    def test_exact_30_percent_cut_is_rejected(self):
        f=fixture()
        f['annual'][-2]['ordinary_dps']=1000
        f['annual'][-1]['ordinary_dps']=700
        self.assertFalse(evaluate(f)['checks']['no_30pct_cut'])
        f['annual'][-1]['ordinary_dps']=700.01
        self.assertTrue(evaluate(f)['checks']['no_30pct_cut'])

    def test_negative_eps_fails_but_missing_is_review(self):
        f=fixture(); f['valuation']['eps']=-1
        self.assertEqual(evaluate(f)['status'],'FAIL')
        f['valuation']['eps']=None
        self.assertEqual(evaluate(f)['status'],'REVIEW')

    def test_preferred_share_mismatch_and_unverified_basis_review(self):
        f=fixture(); f['share_class']='preferred'
        self.assertEqual(evaluate(f)['status'],'REVIEW')
        f=fixture(); f['valuation']['basis_verified']=False
        self.assertEqual(evaluate(f)['status'],'REVIEW')

    def test_missing_risk_or_audit_cannot_pass(self):
        for key in ('risk_verified','audit_verified'):
            f=fixture(); f.pop(key)
            self.assertEqual(evaluate(f)['status'],'REVIEW')

    def test_mixed_accounting_scope_cannot_pass(self):
        f=fixture(); f['annual'][0]['scope']='OFS'
        self.assertEqual(evaluate(f)['status'],'REVIEW')

    def test_strict_tax_boundary_existing_holdings_and_no_float_rounding(self):
        self.assertEqual(tax_quantity(1000)['max_total_qty'],7)
        self.assertEqual(tax_quantity(500,14)['additional_qty'],0)
        self.assertEqual(tax_quantity(500)['max_total_qty'],14)
        self.assertEqual(tax_quantity(100)['max_total_qty'],71)
        self.assertIsNone(tax_quantity(None))
        with self.assertRaises(ValueError): tax_quantity(100,-1)

    def test_ttm_uses_comparable_cumulative_not_quarter_annualization(self):
        self.assertEqual(ttm_from_ytd(100,90,60,current_months=9,prior_months=9,
                         same_scope=True,same_unit=True),130)
        self.assertIsNone(ttm_from_ytd(100,30,60,current_months=3,prior_months=9,
                         same_scope=True,same_unit=True))

    def test_known_failure_remains_fail_when_other_data_missing(self):
        f=fixture(); f['annual']=[]; f['avg_turnover_20d']=0
        self.assertEqual(evaluate(f)['status'],'FAIL')


class DataTests(unittest.TestCase):
    def test_allowlist_blocks_all_order_and_account_requests(self):
        with tempfile.TemporaryDirectory() as d:
            session=Mock(); api=PublicData(d,session=session,interval=0)
            for method,path in [('POST','/uapi/domestic-stock/v1/trading/order-cash'),
                                ('GET','/uapi/domestic-stock/v1/trading/inquire-balance')]:
                with self.assertRaises(DataError):
                    api.request(method,'https://openapi.koreainvestment.com:9443'+path)
            session.request.assert_not_called()

    def test_financial_cutoff_and_interim_not_annualized(self):
        row={'rcept_no':'20260901000001','account_id':'ifrs-full_ProfitLossAttributableToOwnersOfParent',
             'sj_div':'IS','thstrm_amount':'30','thstrm_add_amount':'90'}
        self.assertIsNone(normalize_financial([row],'CFS',2026,'11014','2026-08-31'))
        r=normalize_financial([row],'CFS',2026,'11014','2026-10-30')
        self.assertNotIn('income',r)

    def test_master_uses_share_class_and_official_exclusion_flags(self):
        for market in ('kospi','kosdaq'):
            values=[' ' * w for w in WIDTHS[market]]
            values[0]='ST'
            for i in ((34,35,36) if market=='kospi' else (29,30,31)): values[i]='N'
            values[54 if market=='kospi' else 49]='1'
            row='005935   KR7005931001삼성전자우'+''.join(values)
            stock=parse_master(row,market)[0]
            self.assertEqual(stock['code'],'005935')
            self.assertEqual(stock['share_class'],'preferred')
            self.assertTrue(stock['risk_verified'])
            values[0]='RT'
            self.assertEqual(parse_master('005935   KR7005931001삼성전자우'+''.join(values),market)[0]['exclusion'],'REIT')

    def test_failed_fetch_not_cached_and_success_versioned_without_key(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'DART_API_KEY':'super-secret'}):
            api=PublicData(d, interval=0)
            with patch.object(api,'request',side_effect=DataError('failed')):
                with self.assertRaises(DataError): api.dart('alotMatter.json',{'corp_code':'1'},version='v1')
            self.assertFalse(list(Path(d).glob('*.json')))
            response=Mock(); response.json.return_value={'status':'000','list':[]}
            with patch.object(api,'request',return_value=response) as req:
                api.dart('alotMatter.json',{'corp_code':'1'},version='v1')
                api.dart('alotMatter.json',{'corp_code':'1'},version='v1')
                self.assertEqual(req.call_count,1)
            self.assertNotIn('super-secret',''.join(p.read_text() for p in Path(d).glob('*.json')))

    def test_manual_evidence_requires_provenance_and_cannot_replace_income(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'evidence.json')
            atomic_json(p,{'stocks':{'000001':{'as_of':'2026-10-30','fields':{}}}})
            with self.assertRaises(DataError): reviewed_evidence(p,'000001','2026-10-30')
            with self.assertRaises(DataError):
                apply_evidence(fixture(),{'provenance':{},'fields':{'annual':[{'year':2025,'income':999}]}})

    def test_export_counts_no_thirty_stock_truncation_and_html_escape(self):
        with tempfile.TemporaryDirectory() as d:
            rows=[evaluate(dict(fixture(), code=str(i),name='<script>')) for i in range(31)]
            export(d,rows,{'universe_total':31,'universe_verified':True})
            meta=json.loads(Path(d,'run_metadata.json').read_text())
            self.assertEqual(meta['counts']['PASS'],31)
            self.assertFalse(meta['partial'])
            self.assertEqual(len(Path(d,'candidates.csv').read_text(encoding='utf-8-sig').splitlines()),32)

    def test_missing_secrets_produces_blocked_result_without_network(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{},clear=True), \
             patch.object(PublicData,'request') as request:
            result=main(['--output',d,'--cache',str(Path(d,'cache'))])
            self.assertEqual(result,2)
            request.assert_not_called()
            meta=json.loads(Path(d,'run_metadata.json').read_text())
            self.assertTrue(meta['partial'])
            self.assertIn('DART_API_KEY',meta['blocker'])
            self.assertIsNone(meta['as_of'])


if __name__=='__main__':
    unittest.main()
