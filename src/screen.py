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
from src.research_handoff import build_research_metrics, validate_research_metrics, partition_research_metrics, validate_research_index
from src.financials import enrich_sec_facts

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
    # Parse ONLY an explicitly reported provider trade/session date.
    # Do not substitute today's run date or GitHub HTTP retrieval timestamp.
    raw = str(s or '')
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}', raw)
    if m:
        return datetime.strptime(m.group(), '%b %d, %Y').date()
    m = re.search(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', raw)
    return date.fromisoformat(m.group()) if m else None


def corroborate_nasdaq_quote_date(session, rows, required=2):
    """Recover a SOURCE-REPORTED session date when the bulk screener omits asof.

    Requires two independent issuer symbol checks against Nasdaq quote/info.
    Price agreement is a *sample corroboration*, NOT a row-level price timestamp.
    Reject inconsistent or missing provider dates. The existing NYSE-calendar gate
    subsequently rejects dates older than the previous completed session.
    """
    by_symbol = {str(r.get('symbol') or '').upper(): r for r in rows}
    preferred = ('AAPL', 'JPM', 'MSFT', 'XOM', 'NVDA', 'WMT', 'AMZN', 'GOOGL', 'BAC')
    results = []
    errors = []
    for symbol in preferred:
        screener = by_symbol.get(symbol)
        if not screener:
            continue
        reference = number(screener.get('lastsale'))
        if not reference or reference <= 0:
            continue
        url = f'https://api.nasdaq.com/api/quote/{symbol}/info'
        try:
            payload = fetch(session, url, params={'assetclass':'stocks'}, headers=HEADERS, tries=2, timeout=20).json()
            data = payload.get('data') or {}
            observed = data.get('primaryData') or {}
            timestamp = observed.get('lastTradeTimestamp')
            qdate = parse_nasdaq_asof(timestamp)
            quote = number(observed.get('lastSalePrice'))
            if not qdate or not quote or quote <= 0:
                errors.append(f'{symbol}: missing provider trade timestamp or quoted price')
                continue
            deviation = abs(quote-reference)/reference
            if deviation > 0.03:
                errors.append(f'{symbol}: screener/quote disagreement {deviation:.1%} (>3%)')
                continue
            results.append({'ticker':symbol,'trade_timestamp':timestamp,'trade_date':qdate.isoformat(),
                            'price_deviation_pct':round(100*deviation,3),'source':url})
            if len(results)>=required:
                break
        except (FetchFailure,ValueError,KeyError,TypeError) as ex:
            errors.append(f'{symbol}: {str(ex)[:140]}')
    dates={x['trade_date'] for x in results}
    if len(results)<required:
        raise FetchFailure(f'Nasdaq screener missing asof AND only {len(results)}/{required} quote timestamp anchors validated; details: {errors[:5]}')
    if len(dates)!=1:
        raise FetchFailure(f'Nasdaq quote timestamp anchors disagree on session dates: {sorted(dates)}')
    return date.fromisoformat(next(iter(dates))),results

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
    if len(set(source_dates))>1:
        raise FetchFailure('Nasdaq pagination returned inconsistent quote as-of dates')
    provenance = 'bulk screener explicit asof field'
    anchors = []
    if source_dates:
        chosen = source_dates[0]
    else:
        chosen, anchors = corroborate_nasdaq_quote_date(session, records)
        provenance = 'Nasdaq quote/info corroboration — two dated symbols, sampled not per-row timestamp'
    return records,chosen,{'source':STOCKS,'fetched_rows':len(records),
        'reported_total':total,'pages':page+1,'quote_date_provenance':provenance,
        'quote_timestamp_anchors':anchors,'individual_row_timestamps_verified':False}

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
    domestic=[]; midcaps=[]; international=[]; seen_cik_us=set();seen_cik_intl=set()
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
            domestic.append({'ticker':t,'name':name,'segment':'US','sector':gics,'sector_authority':'S&P500 constituents GICS label','country':country or 'US-listed', 'price':p,'market_cap':cap,'cik':cik,'daily_dollar_volume_proxy':round(p*volume,2),'size_class':'LARGE'})
        elif (not country or country=='US') and 2e9<=cap<=2e10 and volume*p>=5_000_000 and not maybe_adr(r):
            cik=secmap.get(t,'')
            sector=vendor_sector(r.get('sector'),r.get('industry'))
            if cik and sector in SECTORS and cik not in seen_cik_us:
                midcaps.append({'ticker':t,'name':name,'segment':'US','sector':sector,
                    'sector_authority':'Nasdaq indicative mapping; verify GICS',
                    'country':'US','price':p,'market_cap':cap,'cik':cik,
                    'daily_dollar_volume_proxy':round(p*volume,2),'size_class':'MID'})
        elif country and country!='US' and (maybe_adr(r) or cap>2e9):
            if volume*p<1_000_000:continue
            cik=secmap.get(t,'')
            # CompanyFacts uses SEC CIK keys. Skip names that cannot be mapped
            # rather than spending SEC requests on CIK0000000000.
            if cik and cik not in seen_cik_intl:
                seen_cik_intl.add(cik)
                international.append({'ticker':t,'name':name,'segment':'INTL','sector':vendor_sector(r.get('sector'),r.get('industry')),'sector_authority':'Nasdaq indicative vendor mapping', 'country':country,'price':p,'market_cap':cap,'cik':cik,'daily_dollar_volume_proxy':round(p*volume,2)})
    domestic.sort(key=lambda x:x['market_cap'],reverse=True)
    international.sort(key=lambda x:x['market_cap'],reverse=True)
    # Preserve country diversification, but do not truncate potentially
    # SEC-verifiable foreign issuers at 150 before the SEC financial gate.
    # Live test #2 recovered 77/100 from that artificially restricted pool.
    intl=[];bycountry=Counter()
    for r in international:
        if bycountry[r['country']]<65:
            intl.append(r);bycountry[r['country']]+=1
        if len(intl)>=420:break
    large_ciks={r['cik'] for r in domestic}
    midcaps.sort(key=lambda r:r['market_cap'],reverse=True)
    mid_selected=[];mid_seen=set();by_sector=Counter()
    for r in midcaps:
        if r['cik'] in large_ciks or r['cik'] in mid_seen or by_sector[r['sector']]>=20:continue
        mid_selected.append(r);mid_seen.add(r['cik']);by_sector[r['sector']]+=1
        if len(mid_selected)>=180:break
    return domestic[:460]+mid_selected,intl

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
         cashflow_period_end=cf['period_end'] if cf else None,
         capital_expenditures_annual=capex['value'] if capex else None,
         fcf_annual=cf['value']-capex['value'] if cf and capex and cf['period_end']==capex['period_end'] else None,
         revenue_annual=rev['value'] if rev else None,
         sec_filed=ni['filed_date'] if ni else (cf['filed_date'] if cf else None),
         sec_period_end=ni['period_end'] if ni else (cf['period_end'] if cf else None),
         sec_url=url,
         fundamental_error='')
        row['fcf_yield']=round(row['fcf_annual']/row['market_cap'],5) if row['fcf_annual'] is not None else None
        return enrich_sec_facts(facts,row,asof)
    except (FetchFailure,requests.RequestException,ValueError) as ex:
        x=dict(row);x.update(annual_pe=None,fcf_yield=None,earnings_annual=None,fcf_annual=None,sec_filed=None,sec_period_end=None,sec_url=url,fundamental_error=str(ex)[:200]);return x

def etf_nasdaq(session,ticker,asof):
    """Read dated ETF last trade and session volume from Nasdaq's public info page.

    No inference from HTTP fetch dates or today's run date is allowed. If the
    provider omits required evidence, the security FAILS validation.
    """
    url=f'https://api.nasdaq.com/api/quote/{ticker}/info'
    try:
        payload=fetch(session,url,params={'assetclass':'etf'},headers=HEADERS,tries=2,timeout=22).json()
        data=payload.get('data') or {}
        if data.get('symbol') and data['symbol'].upper()!=ticker.upper():
            raise FetchFailure(f'Nasdaq returned a different symbol for {ticker}: {data["symbol"]}')
        category=str(data.get('assetClass') or '').upper()
        if category and category not in ('ETF','ETFS'):
            raise FetchFailure(f'Not ETF-classified by Nasdaq: {category}')
        raw=data.get('primaryData') or {}
        price=number(raw.get('lastSalePrice'))
        volume=number(raw.get('volume'))
        timestamp=raw.get('lastTradeTimestamp')
        tradeday=parse_nasdaq_asof(timestamp)
        if not timestamp or not tradeday:
            raise FetchFailure('missing explicit provider lastTradeTimestamp')
        if tradeday>asof:
            raise FetchFailure(f'future quote trade date {tradeday}')
        if not price or price<=0 or not volume or volume<=0:
            raise FetchFailure('missing positive lastSalePrice or session volume')
        return {'ticker':ticker,'segment':'INTL_ETF','asset_type':'ETF',
                'quote_asof_date':tradeday.isoformat(),'provider_timestamp':timestamp,
                'price_usd':price,'volume':volume,'source_url':url+'?assetclass=etf',
                'fund_domicile':'US','provider':'Nasdaq quote/info','error':''}
    except (FetchFailure,ValueError,KeyError,TypeError,requests.RequestException) as ex:
        return {'ticker':ticker,'provider':'Nasdaq quote/info','error':str(ex)[:190]}

def etf_stooq(session,ticker,asof,apikey=None):
    """Optional second source: Stooq now requires a separate download API key.

    Never request unauthenticated Stooq bulk CSV and count HTML/quota pages as
    market data. An optional GitHub STOOQ_API_KEY secret enables this fallback.
    """
    if not apikey:
        return {'ticker':ticker,'provider':'Stooq','error':'STOOQ_API_KEY not provided; Stooq CSV fallback disabled'}
    try:
        start=(asof-timedelta(days=12)).strftime('%Y%m%d');end=asof.strftime('%Y%m%d')
        resp=fetch(session,'https://stooq.com/q/d/l/',
                   params={'s':ticker.lower()+'.us','i':'d','d1':start,'d2':end,'apikey':apikey},
                   headers={'User-Agent':'Mozilla/5.0'},tries=2,timeout=22)
        rows=list(csv.DictReader(io.StringIO(resp.text)))
        vals=[]
        for x in rows:
            try:
                d=date.fromisoformat(x['Date']);price=float(x['Close']);vol=float(x['Volume'])
                if d<=asof and price>0 and vol>0:vals.append((d,price,vol))
            except (ValueError,KeyError):pass
        if not vals:raise FetchFailure('no eligible dated OHLCV CSV rows from Stooq')
        d,p,v=max(vals)
        return {'ticker':ticker,'segment':'INTL_ETF','asset_type':'ETF',
                'quote_asof_date':d.isoformat(),'price_usd':p,'volume':v,
                'source_url':'https://stooq.com/q/d/l/ (authenticated)','fund_domicile':'US',
                'provider':'Stooq authenticated CSV','error':''}
    except (FetchFailure,ValueError) as ex:return {'ticker':ticker,'provider':'Stooq','error':str(ex)[:190]}

def etf_quote(session,ticker,asof):
    """Use date-verifiable Nasdaq quotes; optional keyed Stooq fallback.

    The caller decides freshness based on the NYSE prior completed session.
    """
    nasdaq=etf_nasdaq(session,ticker,asof)
    if nasdaq.get('price_usd'):
        return nasdaq
    stooq=etf_stooq(session,ticker,asof,os.getenv('STOOQ_API_KEY'))
    if stooq.get('price_usd'):
        return stooq
    return {'ticker':ticker,'error':('Nasdaq: '+nasdaq.get('error','unknown')+
                                    ' | Stooq: '+stooq.get('error','unknown'))[:320],
            'provider':'both sources failed'}

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
    report={'pipeline_version':'0.4','producer_source_commit':os.getenv('GITHUB_SHA',''),'run_utc':now.isoformat(),'report_date':today.isoformat(),
            'provider_status':{},'coverage':{},'status':'FAILED','failures':[],
            'notes':['Market-wide quote source supplies trading-session date, NOT per-security intraday timestamps.',
                     'Triage results are NOT Opportunity Reserve v1.4 scores or buy recommendations.'],
            'sources':{'us_sector_universe':SP500,'nasdaq':STOCKS,'issuer_fundamentals':'https://data.sec.gov/api/xbrl/companyfacts/',
                       'etf_quote_primary':'https://api.nasdaq.com/api/quote/{symbol}/info?assetclass=etf',
                       'etf_history_optional':'https://stooq.com/q/d/l/ (API key required)'}}
    try:
        stocks,quotedate,meta=market_rows(session)
        report['provider_status']['nasdaq']=meta
        prior=nyse_previous_session(today)
        if quotedate < prior:
            raise FetchFailure(f'Nasdaq quote session {quotedate} is older than previous completed NYSE session {prior}')
        if quotedate>today:raise FetchFailure(f'Nasdaq future-date quote {quotedate}')
        from zoneinfo import ZoneInfo
        market_now=now.astimezone(ZoneInfo('America/New_York'))
        if asof is None and market_now.weekday()<5 and market_now.hour>=10 and quotedate!=today:
            raise FetchFailure(f'Nasdaq bulk quote session stale after 10 ET: {quotedate} vs {today}')
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
        def etf_job(t):
            time.sleep(0.12+random.random()*0.15)
            with requests.Session() as es:
                return etf_quote(es,t,today)
        with ThreadPoolExecutor(max_workers=3) as pool:
            etfs=list(pool.map(etf_job,ETF_SYMBOLS))
        verified_etfs=[e for e in etfs if e and e.get('price_usd') and e['price_usd']*e.get('volume',0)>=300_000 and date.fromisoformat(e['quote_asof_date'])>=prior]
        countries={r['country'] for r in ok_foreign if r['country']}
        sectors={r['sector'] for r in ok_us}
        report['coverage']={'US':len(ok_us),'INTL':len(ok_foreign),'INTL_ETF':len(verified_etfs),
             'US_SECTORS':len(sectors),'INTL_COUNTRIES':len(countries),
             'issuer_facts_attempted':len(enriched),'issuer_facts_with_valuation':len(valid),
             'etf_quotes_attempted':len(etfs),'nasdaq_quote_asof_date':quotedate.isoformat(),
             'etf_quote_asof_range':[min((e['quote_asof_date'] for e in verified_etfs),default=None),max((e['quote_asof_date'] for e in verified_etfs),default=None)]}
        report['provider_status']['issuer_errors']=sum(bool(r.get('fundamental_error')) for r in enriched)
        report['provider_status']['foreign_candidate_pool']=len(foreign)
        report['provider_status']['foreign_sec_facts_errors']=sum(bool(r.get('fundamental_error')) for r in enriched if r['segment']=='INTL')
        report['provider_status']['foreign_missing_valuation']=sum((r.get('annual_pe') is None and r.get('fcf_yield') is None) for r in enriched if r['segment']=='INTL')
        report['provider_status']['etf_sources']=dict(Counter(e.get('provider','unknown') for e in etfs if e))
        report['provider_status']['etf_failed_samples']=[{'ticker':e.get('ticker'),'error':e.get('error')} for e in etfs if e and not e.get('price_usd')][:12]
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
            report['coverage']['US_MIDCAP']=sum(r.get('size_class')=='MID' for r in ok_us)
            report['coverage']['issuer_metrics_published']=len(valid)
            report['next_gate']='ChatGPT must complete six issuer-grade reviews PER segment and six-factor scoring, vetoes, alternatives, plus finalist intraday prices.'
            # Public handoff carries identifiers, issuer-source references and preliminary SEC-derived ratios.
            # It intentionally does NOT redistribute Nasdaq/Stooq raw quotes or vendor market caps.
            def publish_issuer(r):
                keep=('ticker','name','segment','country','sector','sector_authority','cik','sec_filed','sec_period_end','sec_url','annual_pe','fcf_yield')
                return {k:r.get(k) for k in keep}
            public={'meta':report,'us_review_queue':[publish_issuer(r) for r in us_top],
                   'foreign_review_queue':[publish_issuer(r) for r in foreign_top],
                   'international_etfs':[{'ticker':e['ticker'],'quote_asof_date':e['quote_asof_date'],'source_url':e['source_url']} for e in verified_etfs],
                   'warnings':['Issuer queue is NOT validated investment recommendations. Researcher must fetch current quote and reconcile nonrecurring GAAP earnings.','Raw Nasdaq/Stooq price data are not redistributed in this public research-queue artifact.']}
            metrics=build_research_metrics(report,enriched,us_top,foreign_top,verified_etfs)
            validate_research_metrics(metrics,report)
            index,shards=partition_research_metrics(metrics)
            write_json(out/'research_metrics.json',index)
            for name,blob in shards:
                (out/name).write_text(blob,encoding='utf-8')
            validate_research_index(index,report,lambda name:(out/name).read_bytes())
            write_json(out/'latest.json',public)
            # Never persist user portfolio, reserve, tax lots or positions on a public website.
        else:report['status']='FAILED_COVERAGE'
    except Exception as ex:
        report['status']='FAILED'
        report['failures'].append(f'{type(ex).__name__}: {str(ex)[:280]}')
    # Do not leave an old successful public review queue beside a failed
    # current status: downstream readers must never confuse it for fresh data.
    if report['status']!='DATA_READY_RESEARCH_PENDING':
        (out/'latest.json').unlink(missing_ok=True)
        for old in out.glob('research_metrics*.json'):
            old.unlink(missing_ok=True)
    write_json(out/'status.json',report)
    print(json.dumps({'status':report['status'],'coverage':report['coverage'],'failures':report['failures']},indent=2))
    return 0 if report['status']=='DATA_READY_RESEARCH_PENDING' else 2

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',default='public');p.add_argument('--date',default='');args=p.parse_args()
    sys.exit(main(Path(args.out),date.fromisoformat(args.date) if args.date else None,os.getenv('SEC_CONTACT_EMAIL')))
