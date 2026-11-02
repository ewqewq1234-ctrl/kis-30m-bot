"""Synthetic rule tests; these fixtures are NOT market screening results."""
import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dividend_value.data import (PublicData, DataError, normalize_dividend,
                                 normalize_financial, parse_master, WIDTHS)
from dividend_value.rules import evaluate, tax_quantity
from dividend_value.screen import export, main


def fixture():
    return {'code':'000001', 'corp_code':'00000001', 'name':'synthetic',
        'share_class':'common', 'as_of':'2026-10-30', 'expected_fiscal_year':2025,
        'close':10000, 'risk_verified':True, 'risk_flagged':False,
        'annual':[{'year':y, 'scope':'CFS_PARENT', 'unit':'KRW', 'income':100,
            'equity_open':1000, 'equity':1000, 'ocf':120, 'cash_dps':500,
            'cash_dividends':50, 'total_taxable_annual_dps':500} for y in range(2021,2026)],
        'latest_balance':{'equity_total':1000,'liabilities':500,'capital':100,
                          'unit':'KRW','scope':'CFS_PARENT','latest_disclosure_verified':True},
        'valuation':{'per':6.67,'pbr':0.83,'eps':1500,'bps':12000},
        'avg_turnover_20d':100000000}


class RulesTests(unittest.TestCase):
    def test_all_rules_pass_with_verified_synthetic_evidence(self):
        r = evaluate(fixture(), strategy_nav=4000000)
        self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['metrics']['initial_budget'],120000)
        self.assertEqual(r['metrics']['estimated_buy_qty'],12)
        self.assertEqual(r['metrics']['history_method'],'five_years')

    def test_history_or_high_yield_does_not_waive_other_rules(self):
        f=fixture()
        f['annual'][0]['cash_dps']=0
        for a in f['annual'][-3:]: a['cash_dps']=800
        r=evaluate(f)
        self.assertEqual(r['status'],'PASS')
        self.assertEqual(r['metrics']['history_method'],'high_yield')
        f['annual'][0]['income']=-1
        self.assertEqual(evaluate(f)['status'],'FAIL')

    def test_reported_cash_dividend_needs_no_manual_classification(self):
        f=fixture()
        for a in f['annual']:
            a['cash_dps']=300
            a['total_taxable_annual_dps']=1000
        r=evaluate(f)
        self.assertFalse(r['checks']['dividend_yield'])
        self.assertEqual(r['metrics']['tax_quantity']['max_total_qty'],7)
        f['annual'][-1].pop('cash_dps')
        self.assertIsNone(evaluate(f)['metrics']['base_dividend'])

    def test_clearly_corrupt_reported_dps_is_not_used(self):
        f=fixture()
        f['annual'][-1]['cash_dps']=1000000000
        f['annual'][-1]['total_taxable_annual_dps']=1000000000
        r=evaluate(f)
        self.assertEqual(r['status'],'REVIEW')
        self.assertIsNone(r['metrics']['base_dividend'])
        self.assertIsNone(r['metrics']['tax_quantity'])

    def test_exact_30_percent_cut_is_rejected(self):
        f=fixture()
        f['annual'][-2]['cash_dps']=1000
        f['annual'][-1]['cash_dps']=700
        self.assertFalse(evaluate(f)['checks']['no_30pct_cut'])
        f['annual'][-1]['cash_dps']=700.01
        self.assertTrue(evaluate(f)['checks']['no_30pct_cut'])

    def test_kis_valuation_threshold_fails_and_missing_is_review(self):
        f=fixture(); f['valuation']['per']=11
        self.assertEqual(evaluate(f)['status'],'FAIL')
        f=fixture(); f['valuation']['per']=None; f['valuation']['eps']=None
        self.assertEqual(evaluate(f)['status'],'REVIEW')

    def test_share_class_and_valuation_period_metadata_are_not_gates(self):
        f=fixture(); f['share_class']='preferred'
        f['valuation'].update(basis_verified=False, basis='unknown',
                              price_date='2099-01-01', share_class='common')
        self.assertEqual(evaluate(f)['status'],'PASS')

    def test_missing_risk_cannot_pass_but_audit_is_not_evaluated(self):
        f=fixture(); f.pop('risk_verified')
        self.assertEqual(evaluate(f)['status'],'REVIEW')
        r=evaluate(fixture())
        self.assertNotIn('audit',r['checks'])

    def test_mixed_accounting_scope_is_not_a_gate(self):
        f=fixture(); f['annual'][0]['scope']='OFS'
        self.assertEqual(evaluate(f)['status'],'PASS')

    def test_strict_tax_boundary_existing_holdings_and_no_float_rounding(self):
        self.assertEqual(tax_quantity(1000)['max_total_qty'],7)
        self.assertEqual(tax_quantity(500,14)['additional_qty'],0)
        self.assertEqual(tax_quantity(500)['max_total_qty'],14)
        self.assertEqual(tax_quantity(100)['max_total_qty'],71)
        self.assertIsNone(tax_quantity(None))
        with self.assertRaises(ValueError): tax_quantity(100,-1)

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
             'sj_div':'IS','thstrm_amount':'30','thstrm_add_amount':'90',
             'bsns_year':'2026','reprt_code':'11014'}
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

    def test_dividend_rows_are_automatic_and_class_agnostic(self):
        base={'rcept_no':'20260312000001','stlm_dt':'2025-12-31'}
        rows=[{**base,'se':'주당 현금배당금(원)','stock_knd':'보통주','thstrm':'500'},
              {**base,'se':'주당 현금배당금(원)','stock_knd':'우선주','thstrm':'550'},
              {**base,'se':'현금배당금총액(백만원)','stock_knd':'-','thstrm':'10'}]
        result=normalize_dividend(rows,2025,'2026-10-30')
        self.assertEqual(result['cash_dps'],500)
        self.assertEqual(result['total_taxable_annual_dps'],500)
        self.assertEqual(result['cash_dividends'],10000000)
        self.assertEqual(normalize_dividend(rows,2025,'2026-01-01'),{})

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

    def test_batch_resume_retries_failure_without_duplicate_successes(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{
                'KIS_APP_KEY':'fake','KIS_APP_SECRET':'fake','DART_API_KEY':'fake'}):
            client=Mock()
            client.calls=client.cache_hits=client.bytes=0
            client.daily.return_value=[{'date':'2026-10-30','close':100,'turnover':100}]
            client.market_master.return_value=[{'code':'000001'},{'code':'000002'}]
            client.corp_codes.return_value={}
            args=['--output',d,'--cache',str(Path(d,'cache')),'--max-symbols','1']
            with patch('dividend_value.screen.PublicData',return_value=client), \
                 patch('dividend_value.screen.screen_stock',return_value=fixture()) as screen:
                main(args)
                self.assertEqual(screen.call_count,1)
                # Second stock fails, then the next resume retries exactly it.
                screen.side_effect=DataError('failure')
                main(args)
                screen.side_effect=None
                screen.return_value=dict(fixture(),code='000002')
                main(args)
                rows=json.loads(Path(d,'screening_details.json').read_text(encoding='utf-8'))
                self.assertEqual([r['code'] for r in rows],['000001','000002'])
                self.assertFalse(any(r.get('query_failed') for r in rows))
                self.assertEqual(screen.call_count,3)

    def test_unverified_risk_is_review(self):
        f=fixture(); f['risk_verified']=False
        self.assertEqual(evaluate(f)['status'],'REVIEW')


if __name__=='__main__':
    unittest.main()
