import unittest
from datetime import datetime, timezone
from src.screen import market_reference_date
from src.model_observations import advance
from src.model_performance import evaluate,validate_model_ledger


def fake_signal():
    return {'schema_version':'1.0','as_of_utc':'2026-10-21T18:00:00Z',
            'policy':{'public_model_only':True},
            'signal_events':[{
                'event_type':'ISSUED','signal_id':'20261021-TEST-001',
                'published_at_utc':'2026-10-21T18:00:00Z','action':'BUY',
                'ticker':'TEST','benchmark_ticker':'SPY','asset_class':'US equity',
                'model_only':True,'trigger':'Next regular session close only',
                'entry_rule':{'type':'NEXT_SESSION_CLOSE','friction_pct':0.2},
                'expiry':'2026-10-28T23:00:00Z',
                'reference_quote':{'price':98.0,'asof_utc':'2026-10-21T17:50:00Z',
                                   'source_url':'https://example.com/ticker'}}],
            'assessment_events':[]}


def synthetic_prices(ticker, earliest, now):
    days=['2026-10-22','2026-10-23','2026-10-26','2026-10-27',
          '2026-10-28','2026-10-29','2026-10-30']
    prices=([100,102,103,104,105,110,111] if ticker=='TEST'
            else [100,100.5,101,101.5,102,103,104])
    return {d:{'date':d,'close':p,'adjusted_close':p}
            for d,p in zip(days,prices)},f'https://example.com/{ticker}'


class ProspectiveModelOutcomeTests(unittest.TestCase):
    def test_provisional_dated_5_session_result_and_idempotency(self):
        ledger=fake_signal()
        updated,audit=advance(ledger,'2026-10-30T23:00:00Z',synthetic_prices)
        self.assertEqual(audit['new_paper_entries'],1)
        self.assertEqual(audit['new_provisional_assessments'],1)
        self.assertEqual(len(updated['signal_events']),2)
        self.assertEqual(updated['signal_events'][1]['entry_session'],'2026-10-22')
        self.assertEqual(updated['assessment_events'][0]['exit_session'],'2026-10-29')
        self.assertFalse(updated['assessment_events'][0]['adjustments_verified'])
        result=evaluate(updated)
        self.assertEqual(result['horizons']['5']['evaluated_count'],0)
        self.assertEqual(result['horizons']['5']['provisional_count'],1)
        self.assertEqual(result['horizons']['5']['provisional_positive_count'],1)
        self.assertAlmostEqual(result['rows'][0]['outcomes']['5']['provisional_excess_return_pct'],6.8)
        again,second=advance(updated,'2026-10-30T23:00:00Z',synthetic_prices)
        self.assertEqual(second['new_paper_entries'],0)
        self.assertEqual(second['new_provisional_assessments'],0)
        self.assertEqual(len(again['assessment_events']),1)

    def test_not_yet_mature(self):
        updated,audit=advance(fake_signal(),'2026-10-23T22:00:00Z',synthetic_prices)
        self.assertEqual(audit['new_provisional_assessments'],0)
        self.assertEqual(updated['assessment_events'],[])

    def test_conditional_without_mechanical_entry_never_fake_fills(self):
        ledger=fake_signal()
        ledger['signal_events'][0]['entry_rule']={'type':'LIMIT_PRICE','limit':98.0}
        updated,audit=advance(ledger,'2026-10-30T23:00:00Z',synthetic_prices)
        self.assertEqual(audit['new_paper_entries'],0)
        self.assertEqual(len(updated['signal_events']),1)
        self.assertEqual(len(audit['skipped_nonmechanical_rules']),1)

    def test_source_failure_no_fabricated_result(self):
        def failed(*args):
            raise ValueError('Provider blocked request')
        updated,audit=advance(fake_signal(),'2026-10-30T23:00:00Z',failed)
        self.assertEqual(len(updated['assessment_events']),0)
        self.assertEqual(len(audit['source_errors']),1)

    def test_utc_rollover_does_not_make_tomorrow_nyse_today(self):
        # 02:19 UTC Oct 22 is still Oct 21 on Wall Street.
        d=datetime(2026,10,22,2,19,tzinfo=timezone.utc)
        self.assertEqual(market_reference_date(d).isoformat(),'2026-10-21')

    def test_future_reference_quote_rejected(self):
        ledger=fake_signal()
        ledger['signal_events'][0]['reference_quote']['asof_utc']='2026-10-21T19:00:00Z'
        with self.assertRaises(ValueError):
            validate_model_ledger(ledger)

    def test_weekly_outcome_is_not_certified_from_single_vendor(self):
        updated,_=advance(fake_signal(),'2026-10-30T23:00:00Z',synthetic_prices)
        summary=evaluate(updated)['horizons']['5']
        self.assertIsNone(summary['win_rate_pct'])
        self.assertEqual(summary['evaluated_count'],0)


if __name__=='__main__':
    unittest.main()
