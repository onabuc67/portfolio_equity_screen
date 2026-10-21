import json, tempfile, unittest
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch
from src import screen

class ProviderAndQualityTests(unittest.TestCase):
 def test_nasdaq_session_date_not_intraday_fabrication(self):
  self.assertEqual(screen.parse_nasdaq_asof('Last price as of Oct 19, 2026'),date(2026,10,19))
  self.assertIsNone(screen.parse_nasdaq_asof('Last price as of now'))
 def test_number_and_spurious_rows(self):
  self.assertEqual(screen.number('$123,456.25'),123456.25)
  self.assertIsNone(screen.number('N/A'))
  self.assertIsNone(screen.number(float('nan')))
 def test_sec_annual_exact_period(self):
  f={'us-gaap':{'NetIncomeLoss':{'units':{'USD':[{'form':'10-K','filed':'2026-02-04','start':'2025-01-01','end':'2025-12-31','val':100}, {'form':'10-Q','filed':'2026-04-28','start':'2026-01-01','end':'2026-03-31','val':80}]}}}}
  self.assertEqual(screen.annual_value(f,['NetIncomeLoss'],asof=date(2026,10,20))['value'],100)
 def test_duplicate_share_class_not_double_count(self):
  sp={'AAA':{'sector':'Industrials','cik':'123'},'AAA/2':{'sector':'Industrials','cik':'123'},'BBB':{'sector':'Energy','cik':'124'}}
  rows=[{'symbol':'AAA','name':'AAA Common Stock','lastsale':'$12','marketCap':'4000000000','country':'United States','volume':'1000000'},
        {'symbol':'AAA/2','name':'AAA Class B','lastsale':'$12','marketCap':'4000000000','country':'United States','volume':'1000000'},
        {'symbol':'BBB','name':'BBB','lastsale':'$12','marketCap':'4000000000','country':'United States','volume':'1000000'}]
  us,_=screen.select_stocks(rows,sp,{})
  # Provider names/CIKs are recorded so duplicate legal issuers can be collapsed by consumer.
  self.assertEqual(len({r['cik'] for r in us}),2)
 def test_country_diversity_cap(self):
  rows=[]
  for i in range(40):
   rows.append({'symbol':'C'+str(i),'name':'Foreign American Depositary Shares','lastsale':'$10','marketCap':str(1e11-i*1e7),'country':'China','sector':'Technology','volume':'1000000'})
  for i in range(8):
   rows.append({'symbol':'B'+str(i),'name':'Foreign American Depositary Shares','lastsale':'$10','marketCap':str(5e10-i*1e7),'country':'Brazil','sector':'Finance','volume':'1000000'})
  _,foreign=screen.select_stocks(rows,{}, {r['symbol']:str(i+100) for i,r in enumerate(rows)})
  self.assertEqual(sum(x['country']=='CN' for x in foreign),40)
  self.assertEqual(sum(x['country']=='BR' for x in foreign),8)
 def test_fail_closed_no_sec_contact(self):
  class Fake: pass
  with self.assertRaises(screen.FetchFailure):screen.facts_for(Fake(),{'cik':'0000320193'},'',date(2026,10,20))
 def test_no_automatic_canonical_score(self):
  r={'ticker':'AAA','sector':'Energy','segment':'US','annual_pe':6,'fcf_yield':.18}
  for result in screen.triage([r],'US',15):
   self.assertNotIn('opportunity_score',result)
 def test_fail_closed_without_market_feed(self):
  with tempfile.TemporaryDirectory() as d:
   with patch.object(screen,'market_rows',side_effect=screen.FetchFailure('upstream offline')):
    code=screen.main(Path(d),date(2026,10,20),contact='research@example.net')
   r=json.loads((Path(d)/'status.json').read_text());self.assertEqual(code,2)
   self.assertEqual(r['status'],'FAILED');self.assertFalse((Path(d)/'latest.json').exists())
 def test_full_mock_data_ready_with_no_trade_signal(self):
  sectors=sorted(screen.SECTORS)
  us=[{'symbol':f'U{i:03d}','name':f'Public Company {i}', 'lastsale':'$30.00','marketCap':str(100000000000-i*100000000),'country':'United States','volume':'1000000'} for i in range(220)]
  countries=['China','Taiwan','Canada','Japan','Germany','Brazil','India','United Kingdom']
  it=[{'symbol':f'I{i:03d}','name':f'Public Company ADR {i}', 'lastsale':'$20.00','marketCap':str(50000000000-i*10000000), 'country':countries[i%8], 'sector':'Technology','volume':'1000000'} for i in range(120)]
  sp={r['symbol']:{'sector':sectors[i%11], 'cik':str(i+1)} for i,r in enumerate(us)}
  ciks={r['symbol']:str(i+700) for i,r in enumerate(it)}
  def filed(session,row,contact,asof):
   return dict(row,annual_pe=12.0,fcf_yield=.05,sec_filed='2026-05-01',sec_period_end='2025-12-31',sec_url='https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json',fundamental_error='')
  def etf(session,ticker,asof):
   return {'ticker':ticker,'segment':'INTL_ETF','asset_type':'ETF','price_usd':50,'quote_asof_date':'2026-10-19','source_url':'https://stooq.com/q/d/l/?s=vea.us&i=d','volume':500000,'fund_domicile':'US','error':''}
  with tempfile.TemporaryDirectory() as d:
   with patch.object(screen,'market_rows',return_value=(us+it,date(2026,10,19),{'source':screen.STOCKS,'fetched_rows':340})),\
        patch.object(screen,'sp500',return_value=sp),\
        patch.object(screen,'sec_ciks',return_value=ciks),\
        patch.object(screen,'facts_for',side_effect=filed),\
        patch.object(screen,'etf_quote',side_effect=etf),\
        patch.object(screen,'nyse_previous_session',return_value=date(2026,10,19)),\
        patch.object(screen.time,'sleep',return_value=None):
    code=screen.main(Path(d),date(2026,10,20),contact='research@example.net')
   obj=json.loads((Path(d)/'status.json').read_text());self.assertEqual(code,0,obj)
   self.assertEqual(obj['status'],'DATA_READY_RESEARCH_PENDING')
   self.assertGreaterEqual(obj['coverage']['US'],200)
   self.assertGreaterEqual(obj['coverage']['INTL'],100)
   self.assertGreaterEqual(obj['coverage']['INTL_ETF'],20)
   r=json.loads((Path(d)/'latest.json').read_text());self.assertEqual(len(r['us_review_queue']),15)
   self.assertEqual(len(r['foreign_review_queue']),15)
   self.assertNotIn('opportunity_score',r['us_review_queue'][0])
   m=json.loads((Path(d)/'research_metrics.json').read_text())
   self.assertEqual(m['meta']['run_utc'],obj['run_utc'])
   self.assertEqual(m['meta']['coverage'],obj['coverage'])
   self.assertEqual(m['audit']['issuer_metrics_published'],obj['coverage']['issuer_facts_with_valuation'])
   self.assertEqual(m['audit']['canonical_six_factor_scores'],0)
   self.assertIsNone(m['preliminary_us_queue'][0]['data_quality']['canonical_six_factor_score'])

if __name__=='__main__':unittest.main()
