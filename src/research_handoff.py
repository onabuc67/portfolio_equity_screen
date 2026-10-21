"""Public SEC-derived issuer research handoff; not investment advice or canonical scoring.

No raw third-party quotes, market capitalizations, volumes, holdings, tax lots,
reserve balances, account data, or personal identifiers are published.
"""
from __future__ import annotations
from datetime import date
import math


METRICS = (
    'annual_pe', 'fcf_yield', 'earnings_annual', 'operating_cashflow_annual',
    'capital_expenditures_annual', 'fcf_annual', 'revenue_annual',
    'revenue_growth_yoy', 'share_based_compensation_annual',
    'owner_cash_proxy_annual', 'owner_cash_yield_proxy', 'cash_balance',
    'debt_balance', 'net_debt_proxy', 'interest_expense_annual',
    'interest_coverage_proxy',
)
IDENTITY = (
    'ticker', 'name', 'segment', 'country', 'sector', 'sector_authority',
    'size_class', 'cik', 'sec_filed', 'sec_period_end', 'cashflow_period_end', 'sec_url',
    'balance_asof',
)


def _number(value):
    try:
        f = float(value)
        return round(f, 6) if math.isfinite(f) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _public_row(row, report_date, quote_session_date):
    item = {k: row.get(k) for k in IDENTITY}
    item['metrics'] = {k: _number(row.get(k)) for k in METRICS}
    warnings = []
    if row.get('sector') == 'Financials':
        warnings.append('FCF and owner-cash multiples may not be economically comparable for financials')
    if item['metrics']['annual_pe'] is None:
        warnings.append('Positive-earnings annual P/E not established')
    if item['metrics']['owner_cash_proxy_annual'] is None:
        warnings.append('CFO/capex/SBC not available for a matching fiscal period')
    if not row.get('sec_period_end'):
        warnings.append('Issuer fiscal period missing')
    else:
        try:
            lag = (date.fromisoformat(report_date) - date.fromisoformat(row['sec_period_end'])).days
            if lag > 420:
                warnings.append('Annual fiscal period over 420 days old')
        except ValueError:
            warnings.append('Issuer fiscal period could not be parsed')
    item['data_quality'] = {
        'sec_annual_period_end': row.get('sec_period_end'),
        'sec_filed': row.get('sec_filed'),
        'bulk_market_session_date': quote_session_date,
        'individual_quote_timestamp_verified': False,
        'current_quarter_validated': False,
        'normalized_earnings_validated': False,
        'issuer_grade_finalist_validated': False,
        'canonical_six_factor_score': None,
        'warnings': warnings,
    }
    return item


def build_research_metrics(report, enriched, us_queue, foreign_queue, etfs):
    """Create dated, reproducible preliminary public research measurements."""
    valid = [r for r in enriched if r.get('annual_pe') is not None or r.get('fcf_yield') is not None]
    valid.sort(key=lambda r: (r['segment'], r['ticker']))
    report_date = report['report_date']
    session_date = report['coverage']['nasdaq_quote_asof_date']
    rows = [_public_row(r, report_date, session_date) for r in valid]
    by_key = {(r['segment'], r['ticker']): r for r in rows}
    us = [by_key[('US', r['ticker'])] for r in us_queue if ('US', r['ticker']) in by_key]
    foreign = [by_key[('INTL', r['ticker'])] for r in foreign_queue if ('INTL', r['ticker']) in by_key]
    audit = {
        'issuer_metrics_published': len(rows),
        'us_metrics_published': sum(r['segment'] == 'US' for r in rows),
        'us_midcap_metrics_published': sum(r['segment'] == 'US' and r.get('size_class') == 'MID' for r in rows),
        'foreign_metrics_published': sum(r['segment'] == 'INTL' for r in rows),
        'owner_cash_proxy_computable': sum(r['metrics']['owner_cash_proxy_annual'] is not None for r in rows),
        'us_preliminary_queue': len(us),
        'foreign_preliminary_queue': len(foreign),
        'issuer_grade_finalists_validated': 0,
        'canonical_six_factor_scores': 0,
    }
    return {
        'schema_version': '1.0',
        'meta': {
            'run_utc': report['run_utc'],
            'report_date': report_date,
            'status': report['status'],
            'coverage': report['coverage'],
        },
        'provenance': {
            'sec_annual_facts': 'https://data.sec.gov/api/xbrl/companyfacts/',
            'nasdaq_bulk_market_source': report['sources']['nasdaq'],
            'nasdaq_bulk_quote_date': session_date,
            'individual_row_timestamps_verified': False,
            'raw_vendor_quotes_published': False,
            'vendor_cap_sec_ratio_caveat': 'Preliminary vendor-market-cap divided by SEC annual earnings/cash flow; periods differ; independently validate finalist quotes',
            'normalized_earnings_verified': False,
            'current_quarter_verified': False,
            'no_private_portfolio_data': True,
            'no_canonical_scores': True,
        },
        'audit': audit,
        'preliminary_us_queue': us,
        'preliminary_foreign_queue': foreign,
        'issuer_metrics': rows,
        'international_etfs': [
            {'ticker': e['ticker'], 'quote_asof_date': e['quote_asof_date'], 'source_url': e['source_url']}
            for e in etfs
        ],
        'next_steps': [
            'Independently verify dated individual quotes for finalist securities',
            'Validate latest issuer quarterly results and normalize GAAP earnings and SBC',
            'Reconcile leverage, funding, peers, forward return, catalysts and risks',
            'Apply frozen six-factor scoring and vetoes only with complete evidence',
        ],
    }


def validate_research_metrics(payload, report, minimum_us=200, minimum_foreign=100):
    """Fail closed if data artifact mismatches its producer or claims false validation."""
    meta = payload['meta']
    for field in ('run_utc', 'report_date', 'status', 'coverage'):
        if meta[field] != report[field]:
            raise ValueError(f'research handoff {field} mismatch')
    if meta['status'] != 'DATA_READY_RESEARCH_PENDING':
        raise ValueError('producer not ready')
    audit = payload['audit']
    rows = payload['issuer_metrics']
    if audit['issuer_metrics_published'] != len(rows):
        raise ValueError('issuer count mismatch')
    if audit['us_metrics_published'] < minimum_us or audit['foreign_metrics_published'] < minimum_foreign:
        raise ValueError('issuer research metrics below coverage minimum')
    if audit['us_preliminary_queue'] < 15 or audit['foreign_preliminary_queue'] < 15:
        raise ValueError('preliminary research queues below minimum')
    if audit['issuer_grade_finalists_validated'] or audit['canonical_six_factor_scores']:
        raise ValueError('producer may not claim issuer validation or canonical scores')
    if payload['provenance']['individual_row_timestamps_verified'] is not False:
        raise ValueError('bulk market rows cannot claim independent timestamps')
    if payload['provenance']['raw_vendor_quotes_published'] is not False:
        raise ValueError('public redistribution of raw market quotes is forbidden')
    for r in rows:
        if r['data_quality']['canonical_six_factor_score'] is not None:
            raise ValueError('producer must not assign opportunity scores')
        if r['data_quality']['individual_quote_timestamp_verified'] is not False:
            raise ValueError('issuer quotes not independently timestamped')
    return True
