#!/usr/bin/env python3
"""Public-data preclose equity discovery feeder.

No brokerage connectivity, customer positions, trade execution or arbitrary score.
Produces a dated *first-pass* universe and review queue, never v1.4 scores.
Financial source: SEC XBRL; price/date: Nasdaq or Stooq exchange session date.
"""
from __future__ import annotations
import csv, io, json, os, re, sys, time, random, math, hashlib, argparse
from datetime import datetime, date, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from pathlib import Path
import requests

UTC=timezone.utc
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; PortfolioPublicDataResearch/1.0)',
 'Accept':'application/json,text/plain,*/*','Referer':'https://www.nasdaq.com/market-activity/stocks/screener','Origin':'https://www.nasdaq.com'}
STOCKS='https://api.nasdaq.com/api/screener/stocks'
SP500='https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
SEC_MAP='https://www.sec.gov/files/company_tickers.json'
ETF_SYMBOLS='VEA VWO VGK VPL EWJ EWG EWU EWC EWA EEM IEMG VXUS IXUS ACWX EFV EWY EWT EWZ EWW INDA FXI MCHI SPEM SCHE AVDE AVEM EWI EWL EWN EWP EZA'.split()
COUNTRY_CODES={'China':'CN','Taiwan':'TW','Canada':'CA','Japan':'JP','United Kingdom':'GB','India':'IN','Brazil':'BR','South Korea':'KR','Korea':'KR','Germany':'DE','France':'FR','Netherlands':'NL','Switzerland':'CH','Australia':'AU','Mexico':'MX','Sweden':'SE','Israel':'IL','Ireland':'IE','Singapore':'SG','Spain':'ES','Italy':'IT','Hong Kong':'HK','Indonesia':'ID','United States':'US','U.S.':'US','USA':'US'}
SECTORS={'Communication Services','Consumer Discretionary','Consumer Staples','Energy','Financials','Health Care','Industrials','Information Technology','Materials','Real Estate','Utilities'}

class FetchFailure(Exception): pass

def fetch(session,url,params=None,headers=None,tries=3,timeout=40):
    for attempt in range(tries):
        try:
            r=session.get(url,params=params,headers=headers,timeout=timeout)
            if r.status_code in (429,500,502,503,504):
                if attempt<tries-1:
                    time.sleep(min(6,1.5*(attempt+1)));continue
            r.raise_for_status();return r
        except (requests.RequestException, ValueError) as ex:
            if attempt==tries-1:raise FetchFailure(f'{url} {type(ex).__name__}: {str(ex)[:180]}') from ex
            time.sleep(min(6,1.5*(attempt+1)))
    raise FetchFailure(url)

def parse_nasdaq_asof(s):
    # Exact trading-session date, NOT an invented intraday exchange timestamp.
    m=re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}',s or '')
    return datetime.strptime(m.group(),'%b %d, %Y').date() if m else None

def number(s):
    if isinstance(s,(int,float)): return float(s) if math.isfinite(s) else None
    s=str(s or '').replace('$','').replace(',','').replace('%','').strip()
    try:
        z=float(s)
        return z if math.isfinite(z) else None
    except (ValueError,TypeError):return None

def market_rows(session,max_pages=70):
    records=[]; seen=set(); source_dates=[]; total=None; offset=0
    for page in range(max_pages):
        payload=fetch(session,STOCKS,params={'tableonly':'false','download':'true','limit':'500','offset':str(offset)},headers=HEADERS).json()
        d=payload.get('data') or {}; tab=d.get('table') or {}
        rows=tab.get('rows') or d.get('rows') or []
        ddate=parse_nasdaq_asof(tab.get('asof') or d.get('asof') or '')
        if ddate:source_dates.append(ddate)
        if total is None:total=tab.get('totalrecords') or d.get('totalrecords')
        if not rows:break
        for item in rows:
            sym=str(item.get('symbol') or '').strip().upper()
            if sym and sym not in seen:
                seen.add(sym);records.append(item)
        offset+=len(rows)  # Handles provider quietly reducing requested page size.
        if total is not None and offset>=int(total):break
        if len(records)>=6500:break
    if not records:raise FetchFailure('Nasdaq screener returned no rows')
    if not source_dates:raise FetchFailure('Nasdaq did not publish a recognizable trading-session date')
    if len(set(source_dates))>1:raise FetchFailure('Nasdaq pagination returned inconsistent quote as-of dates')
    return records,source_dates[0],{'source':STOCKS,'fetched_rows':len(records),'reported_total':total,'pages':page+1}

def sp500(session):
    rows=list(csv.DictReader(io.StringIO(fetch(session,SP500).text)))
    out={}
    for r in rows:
        s=(r.get('Symbol') or '').strip().replace('.','/')
        if s:out[s]={'sector':r.get('GICS Sector'),'cik':str(r.get('CIK') or '').strip(), 'name':r.get('Security')}
    return out

def sec_ciks(session,contact):
    if not contact or '@' not in contact:raise FetchFailure('SEC contact email required for SEC mapping request')
    r=fetch(session,SEC_MAP,headers={'User-Agent':f'PortfolioPublicScreen/1.0 ({contact})','Accept':'application/json'}).json()
    return {str(x['ticker']).upper().replace('.','/'):str(x['cik_str']).zfill(10) for x in r.values() if isinstance(x,dict) and x.get('ticker') and x.get('cik_str')}

def maybe_adr(r):
    s=str(r.get('name') or '').lower()
    return any(w in s for w in ['american depositary','american depository','ads','adr','depositary shares','depositary receipts','depositary share'])

def normalize_country(s):
    s=(s or '').strip()
    return COUNTRY_CODES.get(s,s.upper() if len(s)==2 else '')

def vendor_sector(s,industry):
    # Not an official GICS mapping; only used for foreign spread/triage.
    s=str(s or '').lower();i=str(industry or '').lower()
    d={'technology':'Information Technology','health care':'Health Care','finance':'Financials',
       'energy':'Energy','consumer non-durables':'Consumer Staples','consumer durables':'Consumer Discretionary',
       'basic industries':'Materials','basic materials':'Materials','capital goods':'Industrials',
       'transportation':'Industrials','public utilities':'Utilities','real estate':'Real Estate',
       'telecommunications':'Communication Services'}
    if s in d:return d[s]
    if 'bank' in i or 'insurance' in i:return 'Financials'
    if 'reit' in i or 'real estate' in i:return 'Real Estate'
    if 'retail' in i or 'restaurant' in i or 'auto' in i:return 'Consumer Discretionary'
    if 'food' in i or 'beverage' in i or 'tobacco' in i:return 'Consumer Staples'
    if 'entertainment' in i or 'broadcast' in i or 'communication' in i:return 'Communication Services'
    return ''

def select_stocks(rows,sp,secmap):
    domestic=[]; international=[]; seen_cik_us=set();seen_cik_intl=set()
    for r in rows:
        t=str(r.get('symbol') or '').upper().strip()
        p=number(r.get('lastsale'));cap=number(r.get('marketCap'))
        if not t or not p or p<1 or not cap or cap<1e9:continue
        volume=number(r.get('volume'))
        # Approximate CURRENT dollar volume, not 30-day ADV: a conservative liquidity proxy.
        if volume is None or volume<=0:continue
        name=str(r.get('name') or '')
        if re.search(r'(?i)warrant|right to|preferred|depositary shares representing preferred|unit(s)?(\s+each)?\s+consisting|bond|notes due',name):continue
        country=normalize_country(r.get('country'))
        if t in sp:
            if volume*p<5_000_000:continue
            gics=sp[t].get('sector')
            if gics not in SECTORS:continue
            cik=sp[t]['cik'].zfill(10)
            if cik in seen_cik_us:continue
            seen_cik_us.add(cik)
            # US segment follows S&P 500 economic equity universe; separately flag domiciles in portfolio research.
            domestic.append({'ticker':t,'name':name,'segment':'US','sector':gics,'sector_authority':'S&P500 constituents GICS label','country':country or 'US-listed', 'price':p,'market_cap':cap,'cik':cik,'daily_dollar_volume_proxy':round(p*volume,2)})
        elif country and country!='US' and (maybe_adr(r) or cap>2e9):
            if volume*p<1_000_000:continue
            cik=secmap.get(t,'')
            if cik and cik not in seen_cik_intl:
                seen_cik_intl.add(cik)
                international.append({'ticker':t,'name':name,'segment':'INTL','sector':vendor_sector(r.get('sector'),r.get('industry')),'sector_authority':'Nasdaq indicative vendor mapping', 'country':country,'price':p,'market_cap':cap,'cik':cik,'daily_dollar_volume_proxy':round(p*volume,2)})
    domestic.sort(key=lambda x:x['market_cap'],reverse=True)
    international.sort(key=lambda x:x['market_cap'],reverse=True)
    # Domestic S&P 500 has 11 sector breadth. Foreign country cap prevents top-heavy EM representation.
    intl=[];bycountry=Counter()
    for r in international:
        if bycountry[r['country']]<25:
            intl.append(r);bycountry[r['country']]+=1
        if len(intl)>=150:break
    return domestic[:460],intl

def annual_value(facts,tags,unit='USD',asof=None):
    opts=[]
    asof=asof or date.today()
    for tax in ('us-gaap','ifrs-full'):
        for tag in tags:
            item=(facts.get(tax) or {}).get(tag) or {}
            for raw in ((item.get('units') or {}).get(unit) or []):
                if raw.get('form') not in ('10-K','20-F','40-F'):continue
                try:
                    end=date.fromisoformat(raw['end']);filed=date.fromisoformat(raw['filed']);
                    start=date.fromisoformat(raw['start'])
                    if not 320 <= (end-start).days <= 395:continue
                    if end>asof or filed>asof or (asof-filed).days>610:continue
                    val=float(raw['val'])
                    if math.isfinite(val):opts.append((end,filed,val,tag))
                except (ValueError,KeyError,TypeError):continue
    opts.sort(reverse=True)
    return {'value':opts[0][2],'period_end':opts[0][0].isoformat(),'filed_date':opts[0][1].isoformat(),'tag':opts[0][3]} if opts else None

def facts_for(session,row,contact,asof):
    if not contact or '@' not in contact:raise FetchFailure('SEC contact email (GitHub secret SEC_CONTACT_EMAIL) is required for fair-access User-Agent')
    ua=f'PortfolioPublicScreen/1.0 ({contact})'
    url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{row['cik']}.json"
    try:
        data=fetch(session,url,headers={'User-Agent':ua,'Accept':'application/json'},tries=2,timeout=35).json()
        facts=data.get('facts') or {}
        ni=annual_value(facts,['NetIncomeLoss','ProfitLoss','NetIncomeLossAvailableToCommonStockholdersBasic','ProfitLossAttributableToOwnersOfParent'],asof=asof)
        cf=annual_value(facts,['NetCashProvidedByUsedInOperatingActivities','CashFlowsFromUsedInOperatingActivities'],asof=asof)
        capex=annual_value(facts,['PaymentsToAcquirePropertyPlantAndEquipment','PurchaseOfPropertyPlantAndEquipment','PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities'],asof=asof)
        rev=annual_value(facts,['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet','Revenue','RevenueFromContractsWithCustomers'],asof=asof)
        row=dict(row)
        row.update(earnings_annual=ni['value'] if ni else None,
         annual_pe=round(row['market_cap']/ni['value'],2) if ni and ni['value']>0 else None,
         operating_cashflow_annual=cf['value'] if cf else None,
         capital_expenditures_annual=capex['value'] if capex else None,
         fcf_annual=cf['value']-capex['value'] if cf and capex and cf['period_end']==capex['period_end'] else None,
         revenue_annual=rev['value'] if rev else None,
         sec_filed=ni['filed_date'] if ni else (cf['filed_date'] if cf else None),
         sec_period_end=ni['period_end'] if ni else (cf['period_end'] if cf else None),
         sec_url=url,
         fundamental_error='')
        row['fcf_yield']=round(row['fcf_annual']/row['market_cap'],5) if row['fcf_annual'] is not None else None
        return row
    except (FetchFailure,requests.RequestException,ValueError) as ex:
        x=dict(row);x.update(annual_pe=None,fcf_yield=None,earnings_annual=None,fcf_annual=None,sec_filed=None,sec_period_end=None,sec_url=url,fundamental_error=str(ex)[:200]);return x

def etf_stooq(session,ticker,asof):
    # Stooq daily OHLCV contains an ACTUAL dated trading session, not a fabricated time.
    try:
        start=(asof-timedelta(days=12)).strftime('%Y%m%d');end=asof.strftime('%Y%m%d')
        resp=fetch(session,'https://stooq.com/q/d/l/',params={'s':ticker.lower()+'.us','i':'d','d1':start,'d2':end},headers={'User-Agent':'Mozilla/5.0'},tries=2,timeout=22)
        rows=list(csv.DictReader(io.StringIO(resp.text)))
        vals=[]
        for x in rows:
            try:
                d=date.fromisoformat(x['Date']);price=float(x['Close']);vol=float(x['Volume']);
                if d<=asof and price>0 and vol>0:vals.append((d,price,vol))
            except (ValueError,KeyError):pass
        if not vals:return None
        d,p,v=max(vals)
        return {'ticker':ticker,'segment':'INTL_ETF','asset_type':'ETF','quote_asof_date':d.isoformat(), 'price_usd':p,'volume':v,'source_url':resp.url,'fund_domicile':'US','error':''}
    except FetchFailure as ex:return {'ticker':ticker,'error':str(ex)}

def triage(rows,segment,n=15):
    # NOT the canonical Opportunity Score. Cheapness/free cash flow is a RESEARCH queue only.
    candidates=[r for r in rows if r['segment']==segment and (r.get('annual_pe') or r.get('fcf_yield'))]
    def key(r):
        pe=r.get('annual_pe');fcf=r.get('fcf_yield')
        cheap=(min(30,max(0,30-pe))/30 if pe and pe>0 else 0)
        cash=min(.20,max(-.05,fcf))/0.20 if fcf is not None else 0
        return 0.55*cheap + 0.45*cash
    candidates.sort(key=key,reverse=True)
    chosen=[];cnt=Counter()
    for r in candidates:
        bucket=r.get('sector') if segment=='US' else r.get('country')
        if cnt[bucket]>=4:continue
        chosen.append(r);cnt[bucket]+=1
        if len(chosen)>=n:break
    return chosen

def nyse_previous_session(today):
    # dependency available in production GitHub runner; time zone insensitive for DATE only.
    import pandas_market_calendars as mcal
    cal=mcal.get_calendar('NYSE')
    days=cal.valid_days(start_date=today-timedelta(days=12),end_date=today-timedelta(days=1))
    return days[-1].date() if len(days)>0 else None

def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def main(out,asof=None,contact=None):
    now=datetime.now(UTC);today=asof or now.date();session=requests.Session()
    report={'pipeline_version':'0.2','run_utc':now.isoformat(),'report_date':today.isoformat(),
            'provider_status':{},'coverage':{},'status':'FAILED','failures':[],
            'notes':['Market-wide quote source supplies trading-session date, NOT per-security intraday timestamps.',
                     'Triage results are NOT Opportunity Reserve v1.4 scores or buy recommendations.'],
            'sources':{'us_sector_universe':SP500,'nasdaq':STOCKS,'issuer_fundamentals':'https://data.sec.gov/api/xbrl/companyfacts/',
                       'etf_history':'https://stooq.com/q/d/l/'}}
    try:
        stocks,quotedate,meta=market_rows(session)
        report['provider_status']['nasdaq']=meta
        prior=nyse_previous_session(today)
        if quotedate < prior:
            raise FetchFailure(f'Nasdaq quote session {quotedate} is older than previous completed NYSE session {prior}')
        if quotedate>today:raise FetchFailure(f'Nasdaq future-date quote {quotedate}')
        if not contact or '@' not in contact:raise FetchFailure('SEC_CONTACT_EMAIL not set; SEC fair-access identity gate')
        sp=sp500(session);ciks=sec_ciks(session,contact)
        us,foreign=select_stocks(stocks,sp,ciks)
        report['provider_status']['sp500_constituents']=len(sp)
        report['provider_status']['eligible_pre_sec']={'us':len(us),'foreign':len(foreign)}
        if len(us)<200 or len(foreign)<100:raise FetchFailure('Insufficient domestic/foreign liquid candidates from Nasdaq + S&P universe')
        if not contact or '@' not in contact:raise FetchFailure('SEC_CONTACT_EMAIL not set; SEC companyfacts fetch blocked by fair-access identity gate')
        # Concurrency and sleep jitter: 3 workers, <=3 req/s, below SEC fair access ceiling.
        enriched=[]
        def task(row):
            time.sleep(0.34+random.random()*0.22)
            with requests.Session() as s:return facts_for(s,row,contact,today)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(task,r) for r in (us+foreign)]
            for f in as_completed(futures):enriched.append(f.result())
        valid=[r for r in enriched if r.get('annual_pe') is not None or r.get('fcf_yield') is not None]
        ok_us=[r for r in valid if r['segment']=='US']
        ok_foreign=[r for r in valid if r['segment']=='INTL']
        with ThreadPoolExecutor(max_workers=4) as pool:
            etfs=list(pool.map(lambda t:etf_stooq(requests.Session(),t,today),ETF_SYMBOLS))
        verified_etfs=[e for e in etfs if e and e.get('price_usd') and e['price_usd']*e.get('volume',0)>=300_000 and date.fromisoformat(e['quote_asof_date'])>=prior]
        countries={r['country'] for r in ok_foreign if r['country']}
        sectors={r['sector'] for r in ok_us}
        report['coverage']={'US':len(ok_us),'INTL':len(ok_foreign),'INTL_ETF':len(verified_etfs),
             'US_SECTORS':len(sectors),'INTL_COUNTRIES':len(countries),
             'issuer_facts_attempted':len(enriched),'issuer_facts_with_valuation':len(valid),
             'etf_quotes_attempted':len(etfs),'nasdaq_quote_asof_date':quotedate.isoformat(),
             'etf_quote_asof_range':[min((e['quote_asof_date'] for e in verified_etfs),default=None),max((e['quote_asof_date'] for e in verified_etfs),default=None)]}
        report['provider_status']['issuer_errors']=sum(bool(r.get('fundamental_error')) for r in enriched)
        if len(ok_us)<200:report['failures'].append(f'US issuer-valued: {len(ok_us)}/200')
        if len(ok_foreign)<100:report['failures'].append(f'Foreign issuer-valued: {len(ok_foreign)}/100')
        if len(verified_etfs)<20:report['failures'].append(f'International ETF dated quotes: {len(verified_etfs)}/20')
        if len(sectors)<11:report['failures'].append(f'US 11-sector breadth: {len(sectors)}/11')
        if len(countries)<6:report['failures'].append(f'Foreign country breadth: {len(countries)}/6')
        us_top=triage(ok_us,'US',15);foreign_top=triage(ok_foreign,'INTL',15)
        if len(us_top)<15:report['failures'].append(f'US preliminary candidates: {len(us_top)}/15')
        if len(foreign_top)<15:report['failures'].append(f'Foreign preliminary candidates: {len(foreign_top)}/15')
        if not report['failures']:
            report['status']='DATA_READY_RESEARCH_PENDING'
            report['next_gate']='ChatGPT must complete six issuer-grade reviews PER segment and six-factor scoring, vetoes, alternatives, plus finalist intraday prices.'
            # Public handoff carries identifiers, issuer-source references and dated coverage ONLY.
            # It intentionally does NOT redistribute Nasdaq/Stooq raw quotes or vendor market caps.
            def publish_issuer(r):
                keep=('ticker','name','segment','country','sector','sector_authority','cik','sec_filed','sec_period_end','sec_url')
                return {k:r.get(k) for k in keep}
            public={'meta':report,'us_review_queue':[publish_issuer(r) for r in us_top],
                   'foreign_review_queue':[publish_issuer(r) for r in foreign_top],
                   'international_etfs':[{'ticker':e['ticker'],'quote_asof_date':e['quote_asof_date'],'source_url':e['source_url']} for e in verified_etfs],
                   'warnings':['Issuer queue is NOT validated investment recommendations. Researcher must fetch current quote and reconcile nonrecurring GAAP earnings.','Raw Nasdaq/Stooq price data are not redistributed in this public research-queue artifact.']}
            write_json(out/'latest.json',public)
            # Never persist user portfolio, reserve, tax lots or positions on a public website.
        else:report['status']='FAILED_COVERAGE'
    except Exception as ex:
        report['failures'].append(f'{type(ex).__name__}: {str(ex)[:280]}')
    write_json(out/'status.json',report)
    print(json.dumps({'status':report['status'],'coverage':report['coverage'],'failures':report['failures']},indent=2))
    return 0 if report['status']=='DATA_READY_RESEARCH_PENDING' else 2

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',default='public');p.add_argument('--date',default='');args=p.parse_args()
    sys.exit(main(Path(args.out),date.fromisoformat(args.date) if args.date else None,os.getenv('SEC_CONTACT_EMAIL')))
