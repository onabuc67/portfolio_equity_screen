"""SEC issuer fact enrichments. Preliminary research measurements, not scored advice.

Financial values are USD-tagged, source-filed annual flow or instant facts.
Every owner-cash proxy requires same-year CFO, capex and stock compensation.
"""
from __future__ import annotations
from datetime import date
import math


def _annual(facts, tags, asof, latest_end=None, prior=False):
    opts=[]
    for taxonomy in ('us-gaap','ifrs-full'):
        for tag in tags:
            for raw in (((facts.get(taxonomy) or {}).get(tag) or {}).get('units') or {}).get('USD',[]):
                if raw.get('form') not in ('10-K','20-F','40-F'):continue
                try:
                    end=date.fromisoformat(raw['end'])
                    filed=date.fromisoformat(raw['filed'])
                    start=date.fromisoformat(raw['start'])
                    val=float(raw['val'])
                    if not (320<=(end-start).days<=395 and end<=asof and filed<=asof and math.isfinite(val)):
                        continue
                    if prior and (not latest_end or not 330<=(latest_end-end).days<=400):continue
                    opts.append((end,filed,val))
                except (ValueError,KeyError,TypeError):continue
    opts.sort(reverse=True)
    return {'value':opts[0][2],'period_end':opts[0][0].isoformat()} if opts else None


def _instant(facts,tags,asof):
    opts=[]
    for taxonomy in ('us-gaap','ifrs-full'):
        for tag in tags:
            for raw in (((facts.get(taxonomy) or {}).get(tag) or {}).get('units') or {}).get('USD',[]):
                if raw.get('form') not in ('10-K','10-Q','20-F','40-F') or raw.get('start'):continue
                try:
                    end=date.fromisoformat(raw['end']);filed=date.fromisoformat(raw['filed']);val=float(raw['val'])
                    if end<=asof and filed<=asof and math.isfinite(val):opts.append((end,filed,val))
                except (ValueError,KeyError,TypeError):continue
    opts.sort(reverse=True)
    return {'value':opts[0][2],'period_end':opts[0][0].isoformat()} if opts else None


def enrich_sec_facts(facts,row,asof):
    """Adds comparable public annual financial proxies with explicit null gaps."""
    r=dict(row)
    revenue_tags=['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet','Revenue','RevenueFromContractsWithCustomers']
    revenue=_annual(facts,revenue_tags,asof)
    prior_revenue=_annual(facts,revenue_tags,asof,date.fromisoformat(revenue['period_end']),True) if revenue else None
    sbc=_annual(facts,['ShareBasedCompensation','ShareBasedCompensationArrangementByShareBasedPaymentAwardExpenseRecognized'],asof)
    interest=_annual(facts,['InterestExpenseNonOperating','InterestExpense'],asof)
    cash=_instant(facts,['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],asof)
    current_debt=_instant(facts,['LongTermDebtCurrent','LongTermDebtAndCapitalLeaseObligationsCurrent','ShortTermBorrowings'],asof)
    long_debt=_instant(facts,['LongTermDebtNoncurrent','LongTermDebtAndCapitalLeaseObligationsNoncurrent'],asof)
    total_debt=_instant(facts,['LongTermDebt','LongTermDebtAndFinanceLeaseObligations'],asof)
    debt=None;debt_date=None
    if current_debt and long_debt and current_debt['period_end']==long_debt['period_end']:
        debt=current_debt['value']+long_debt['value'];debt_date=current_debt['period_end']
    elif total_debt:
        debt=total_debt['value'];debt_date=total_debt['period_end']
    fcf=r.get('fcf_annual')
    fiscal_end=r.get('cashflow_period_end') or r.get('sec_period_end')
    # Conservative: CFO and capex already match, but annual SBC must match them too.
    owner_cash=fcf-sbc['value'] if fcf is not None and sbc and sbc['period_end']==fiscal_end else None
    rev_growth=(revenue['value']/prior_revenue['value']-1) if revenue and prior_revenue and prior_revenue['value']>0 else None
    ni=r.get('earnings_annual')
    interest_cov=(ni/interest['value']) if ni is not None and interest and interest['value']>0 and interest['period_end']==fiscal_end and r.get('sector')!='Financials' else None
    cap=r.get('market_cap')
    r.update(
        revenue_growth_yoy=round(rev_growth,5) if rev_growth is not None else None,
        share_based_compensation_annual=sbc['value'] if sbc else None,
        owner_cash_proxy_annual=owner_cash,
        owner_cash_yield_proxy=round(owner_cash/cap,5) if owner_cash is not None and cap and cap>0 else None,
        cash_balance=cash['value'] if cash else None,
        debt_balance=debt,
        balance_asof=debt_date or (cash['period_end'] if cash else None),
        net_debt_proxy=debt-cash['value'] if debt is not None and cash and cash['period_end']==debt_date else None,
        interest_expense_annual=interest['value'] if interest else None,
        interest_coverage_proxy=round(interest_cov,3) if interest_cov is not None else None,
    )
    return r
