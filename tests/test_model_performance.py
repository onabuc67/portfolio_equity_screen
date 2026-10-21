import unittest
from src.model_performance import evaluate,validate_model_ledger


def example(action='BUY'):
    return {
        'schema_version':'1.0','policy':{'public_model_only':True},
        'as_of_utc':'2026-10-21T18:00:00Z',
        'signal_events':[{'event_type':'ISSUED','signal_id':'20261021-INGR-001',
            'published_at_utc':'2026-10-21T18:00:00Z','action':action,'ticker':'INGR',
            'benchmark_ticker':'SPY','asset_class':'US equity','model_only':True,
            'trigger':'Limit 96.00, expires 2026-10-28',
            'expiry':'2026-10-28T20:00:00Z',
            'reference_quote':{'price':96.82,'asof_utc':'2026-10-21T17:50:00Z',
                'source_url':'https://example.com/quote'}}],
        'assessment_events':[{'signal_id':'20261021-INGR-001','trading_sessions':20,
            'asof_utc':'2026-11-20T21:00:00Z','adjustments_verified':True,
            'idea_total_return_pct':9.5,'benchmark_total_return_pct':3.0,
            'friction_pct':0.2,'idea_source_url':'https://example.com/issuer',
            'benchmark_source_url':'https://example.com/index'}],
    }


class ModelPerformanceTest(unittest.TestCase):
    def test_no_signals(self):
        x=example()
        x['signal_events']=[]
        x['assessment_events']=[]
        result=evaluate(x)
        self.assertEqual(result['total_recommended'],0)
        self.assertIsNone(result['horizons']['20']['win_rate_pct'])

    def test_buy_outperformance(self):
        result=evaluate(example('BUY'))
        self.assertEqual(result['horizons']['20']['win_count'],1)
        self.assertAlmostEqual(result['rows'][0]['outcomes']['20']['excess_return_pct'],6.3)
        self.assertEqual(result['horizons']['5']['evaluated_count'],0)

    def test_sell_benefits_from_underperformance(self):
        result=evaluate(example('SELL'))
        self.assertAlmostEqual(result['rows'][0]['outcomes']['20']['excess_return_pct'],-6.7)
        self.assertEqual(result['horizons']['20']['win_count'],0)

    def test_missing_adjustments_is_unverified(self):
        x=example()
        x['assessment_events'][0]['adjustments_verified']=False
        result=evaluate(x)
        self.assertEqual(result['rows'][0]['outcomes']['20']['state'],'UNVERIFIED')

    def test_private_data_forbidden(self):
        x=example()
        x['signal_events'][0]['shares']=10
        with self.assertRaises(ValueError):
            validate_model_ledger(x)

    def test_duplicate_original_forbidden(self):
        x=example()
        x['signal_events'].append(dict(x['signal_events'][0]))
        with self.assertRaises(ValueError):
            validate_model_ledger(x)

    def test_missing_source_forbidden(self):
        x=example()
        x['signal_events'][0]['reference_quote']['source_url']=''
        with self.assertRaises(ValueError):
            validate_model_ledger(x)


if __name__=='__main__':
    unittest.main()
