import unittest
from datetime import date
from src.financials import enrich_sec_facts
from src.research_handoff import build_research_metrics, validate_research_metrics
from src.screen import select_stocks


def annual(val, start, end, filed='2026-02-20'):
    return {'form':'10-K','start':start,'end':end,'filed':filed,'val':val}


def instant(val, end='2026-06-30', filed='2026-08-01'):
    return {'form':'10-Q','end':end,'filed':filed,'val':val}


class ProductionContractTests(unittest.TestCase):
    def test_midcap_breadth_not_sp500_only(self):
        rows=[
            {'symbol':'AAA','name':'Large US common','lastsale':'$25','marketCap':'50000000000','country':'United States','volume':'1000000'},
            {'symbol':'MID','name':'Midcap US common','lastsale':'$25','marketCap':'7000000000','country':'United States','volume':'1000000','sector':'Technology'},
            {'symbol':'ADR','name':'Foreign ADR common','lastsale':'$25','marketCap':'7000000000','country':'China','volume':'1000000','sector':'Technology'},
        ]
        us,foreign=select_stocks(rows,{'AAA':{'sector':'Industrials','cik':'10'}},{'MID':'20','ADR':'30'})
        self.assertEqual({x['ticker'] for x in us},{'AAA','MID'})
        self.assertEqual({x['ticker'] for x in foreign},{'ADR'})
        self.assertEqual(next(x for x in us if x['ticker']=='MID')['size_class'],'MID')
        self.assertIn('indicative',next(x for x in us if x['ticker']=='MID')['sector_authority'])

    def test_filed_sec_annual_cashflow_and_balance_dates(self):
        facts={'us-gaap':{
            'RevenueFromContractWithCustomerExcludingAssessedTax':{'units':{'USD':[
                annual(1000,'2024-01-01','2024-12-31','2025-02-20'),
                annual(1200,'2025-01-01','2025-12-31')]}},
            'ShareBasedCompensation':{'units':{'USD':[annual(30,'2025-01-01','2025-12-31')]}},
            'InterestExpense':{'units':{'USD':[annual(20,'2025-01-01','2025-12-31')]}},
            'CashAndCashEquivalentsAtCarryingValue':{'units':{'USD':[instant(500)]}},
            'LongTermDebtCurrent':{'units':{'USD':[instant(50)]}},
            'LongTermDebtNoncurrent':{'units':{'USD':[instant(300)]}},
        }}
        row={'ticker':'AAA','sector':'Industrials','market_cap':10000,'fcf_annual':300,
             'cashflow_period_end':'2025-12-31','sec_period_end':'2025-12-31',
             'earnings_annual':200}
        result=enrich_sec_facts(facts,row,date(2026,10,21))
        self.assertEqual(result['revenue_growth_yoy'],0.2)
        self.assertEqual(result['share_based_compensation_annual'],30)
        self.assertEqual(result['owner_cash_proxy_annual'],270)
        self.assertEqual(result['owner_cash_yield_proxy'],0.027)
        self.assertEqual(result['cash_balance'],500)
        self.assertEqual(result['debt_balance'],350)
        self.assertEqual(result['net_debt_proxy'],-150)
        self.assertEqual(result['interest_coverage_proxy'],10)
        self.assertEqual(result['balance_asof'],'2026-06-30')

    def test_mismatched_sbc_fiscal_year_fails_closed(self):
        facts={'us-gaap':{'ShareBasedCompensation':{'units':{'USD':[
            annual(30,'2024-01-01','2024-12-31','2025-02-20')]}}}}
        row={'ticker':'AAA','sector':'Industrials','market_cap':10000,'fcf_annual':300,
             'cashflow_period_end':'2025-12-31','sec_period_end':'2025-12-31',
             'earnings_annual':200}
        result=enrich_sec_facts(facts,row,date(2026,10,21))
        self.assertIsNone(result['owner_cash_proxy_annual'])
        self.assertIsNone(result['owner_cash_yield_proxy'])

    def test_metrics_publish_no_vendor_quotes_or_scores(self):
        coverage={'US':200,'INTL':100,'nasdaq_quote_asof_date':'2026-10-21'}
        report={'run_utc':'2026-10-21T19:18:00+00:00','report_date':'2026-10-21',
                'status':'DATA_READY_RESEARCH_PENDING','coverage':coverage,
                'sources':{'nasdaq':'https://api.nasdaq.com/api/screener/stocks'}}
        def issuer(i,segment):
            return {'ticker':f'{segment}{i:03d}','name':'Public issuer','segment':segment,
                'country':'US' if segment=='US' else 'JP','sector':'Industrials','size_class':'MID',
                'sec_period_end':'2025-12-31','sec_filed':'2026-02-20',
                'annual_pe':10,'fcf_yield':.05,'market_cap':1e10,'price':50,
                'earnings_annual':1e9,'fcf_annual':5e8,'sec_url':'https://data.sec.gov'}
        rows=[issuer(i,'US') for i in range(200)]+[issuer(i,'INTL') for i in range(100)]
        p=build_research_metrics(report,rows,rows[:15],rows[200:215],[])
        self.assertTrue(validate_research_metrics(p,report))
        self.assertEqual(p['audit']['us_midcap_metrics_published'],200)
        self.assertEqual(p['audit']['canonical_six_factor_scores'],0)
        self.assertNotIn('price',p['issuer_metrics'][0])
        self.assertNotIn('market_cap',p['issuer_metrics'][0])
        self.assertIsNone(p['issuer_metrics'][0]['data_quality']['canonical_six_factor_score'])
        changed=dict(report,run_utc='2026-10-21T19:20:00+00:00')
        with self.assertRaises(ValueError):
            validate_research_metrics(p,changed)


if __name__=='__main__':
    unittest.main()
