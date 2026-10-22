"""Prospectively evaluate PUBLIC hypothetical model recommendations.

This module uses ONLY public generic idea events (never brokerage data).
The analyst must supply independently verified, split/dividend-aware total
returns from original prospective entry to a matured trading-session horizon.
If any required evidence is missing, return UNVERIFIED rather than guess.
"""
from collections import Counter
from datetime import datetime
import math

HORIZONS = (5, 20, 60)


def _utc(value):
    if not isinstance(value,str):
        raise ValueError('Timestamp missing')
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp must include UTC offset')
    return d


def _number(value):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError('Nonfinite or missing numeric return')
    return float(value)


def validate_model_ledger(ledger):
    if ledger.get('schema_version')!='1.0' or not ledger.get('policy',{}).get('public_model_only'):
        raise ValueError('Unsupported/private model ledger')
    issued={}
    amendments=[]
    for event in ledger.get('signal_events',[]):
        et=event.get('event_type')
        sid=event.get('signal_id')
        if not isinstance(sid,str) or len(sid)<7:
            raise ValueError('Missing stable signal ID')
        if et=='ISSUED':
            if sid in issued:raise ValueError('Duplicate original recommendation ID')
            if event.get('action') not in ('BUY','ADD','REDUCE','SELL','EXIT'):
                raise ValueError('WATCH/HOLD is not an actual trade recommendation')
            if not event.get('model_only') or any(k in event for k in
                    ('account','shares','lot','portfolio_balance','tax_lot','user','actual_fill')):
                raise ValueError('Personal/executed transaction data forbidden')
            if not event.get('ticker') or not event.get('benchmark_ticker'):
                raise ValueError('Instrument and benchmark required')
            if not event.get('trigger') or not event.get('expiry'):
                raise ValueError('Prospective trigger and expiry required')
            q=event.get('reference_quote') or {}
            if not q.get('source_url') or not q.get('asof_utc') or not q.get('price'):
                raise ValueError('Source-dated original price required')
            published=_utc(event['published_at_utc']);quote_time=_utc(q['asof_utc'])
            if quote_time>published:raise ValueError('Post-publication quote is look-ahead bias')
            _utc(event['expiry'])
            if _utc(event['expiry'])<=published:raise ValueError('Expiry must follow recommendation')
            _number(q['price'])
            issued[sid]=event
        elif et in ('TRIGGERED_PAPER','NO_FILL','CANCELED','EXPIRED','SUPERSEDED','CLOSED'):
            if not event.get('event_at_utc'):raise ValueError('Amendment needs dated original')
            _utc(event['event_at_utc'])
            amendments.append(event)
        else:raise ValueError('Unknown event type')
    for event in amendments:
        if event['signal_id'] not in issued:raise ValueError('Amendment without original')
        if _utc(event['event_at_utc'])<_utc(issued[event['signal_id']]['published_at_utc']):
            raise ValueError('Backward-dated amendment')
    asof=ledger.get('as_of_utc')
    if asof:_utc(asof)
    observations=set()
    for e in ledger.get('assessment_events',[]):
        sid=e.get('signal_id');h=e.get('trading_sessions')
        if sid not in issued or h not in HORIZONS:
            raise ValueError('Outcome without original idea or canonical horizon')
        _utc(e.get('asof_utc'))
        if _utc(e['asof_utc'])<_utc(issued[sid]['published_at_utc']):
            raise ValueError('Outcome before idea was public')
        key=(sid,h)
        if key in observations:raise ValueError('Outcome cannot be overwritten; post corrected event separately')
        observations.add(key)
        if e.get('adjustments_verified') is True:
            for keynum in ('idea_total_return_pct','benchmark_total_return_pct','friction_pct'):
                _number(e.get(keynum))
            if not e.get('idea_source_url') or not e.get('benchmark_source_url'):
                raise ValueError('Verified outcome must cite source prices and distributions')
        elif e.get('adjustments_verified') is False:
            if e.get('evidence_quality')=='PROVISIONAL_YAHOO_ADJUSTED_CLOSE':
                for keynum in ('provisional_idea_total_return_pct','provisional_benchmark_total_return_pct','friction_pct'):
                    _number(e.get(keynum))
                if not e.get('idea_source_url') or not e.get('benchmark_source_url'):
                    raise ValueError('Provisional return must cite the source')
                if not e.get('entry_session') or not e.get('exit_session') or e['entry_session']>=e['exit_session']:
                    raise ValueError('Provisional dates/entry invalid')
        else:
            raise ValueError('Return evidence quality must be explicit')
    return issued


def evaluate(ledger):
    issued=validate_model_ledger(ledger)
    amended={sid:[] for sid in issued}
    for e in ledger['signal_events']:
        if e['event_type']!='ISSUED':amended[e['signal_id']].append(e)
    obs={(e['signal_id'],e['trading_sessions']):e for e in ledger['assessment_events']}
    rows=[]
    for sid,initial in sorted(issued.items(),key=lambda kv:kv[1]['published_at_utc']):
        actions=amended[sid]
        state=actions[-1]['event_type'] if actions else 'RECOMMENDED_NOT_EXECUTED'
        measurements={}
        for horizon in HORIZONS:
            e=obs.get((sid,horizon))
            if not e:
                measurements[str(horizon)]={'state':'NOT_EVALUATED','excess_return_pct':None}
                continue
            if not e['adjustments_verified']:
                if e.get('evidence_quality')=='PROVISIONAL_YAHOO_ADJUSTED_CLOSE':
                    stock=_number(e['provisional_idea_total_return_pct'])
                    bench=_number(e['provisional_benchmark_total_return_pct'])
                    friction=_number(e['friction_pct'])
                    gross=stock-bench if initial['action'] in ('BUY','ADD') else bench-stock
                    measurements[str(horizon)]={'state':'PROVISIONAL',
                        'excess_return_pct':None,
                        'provisional_excess_return_pct':round(gross-friction,5),
                        'provisional_security_return_pct':stock,
                        'provisional_benchmark_return_pct':bench,
                        'entry_session':e['entry_session'],'exit_session':e['exit_session'],
                        'win_unverified':gross-friction>0}
                else:
                    measurements[str(horizon)]={'state':'UNVERIFIED','excess_return_pct':None}
                continue
            stock=_number(e['idea_total_return_pct'])
            bench=_number(e['benchmark_total_return_pct'])
            friction=_number(e['friction_pct'])
            # For SELL/REDUCE, switching to benchmark beats continued hold when stock underperforms.
            gross=stock-bench if initial['action'] in ('BUY','ADD') else bench-stock
            net=gross-friction
            measurements[str(horizon)]={'state':'EVALUATED',
                'excess_return_pct':round(net,5),
                'idea_total_return_pct':stock,
                'benchmark_total_return_pct':bench,
                'friction_pct':friction,
                'win':net>0}
        rows.append({'signal_id':sid,'ticker':initial['ticker'],'action':initial['action'],
                     'published_at_utc':initial['published_at_utc'],
                     'benchmark_ticker':initial['benchmark_ticker'],
                     'paper_state':state,'outcomes':measurements})
    summary={}
    for h in HORIZONS:
        vals=[r['outcomes'][str(h)]['excess_return_pct'] for r in rows
              if r['outcomes'][str(h)]['state']=='EVALUATED']
        vals.sort()
        n=len(vals)
        provisional=[r['outcomes'][str(h)].get('provisional_excess_return_pct') for r in rows
                     if r['outcomes'][str(h)]['state']=='PROVISIONAL']
        summary[str(h)]={'evaluated_count':n,
            'provisional_count':len(provisional),
            'provisional_positive_count':sum(x>0 for x in provisional),
            'provisional_mean_excess_pct':round(sum(provisional)/len(provisional),5) if provisional else None,
            'total_signal_count':len(rows),
            'win_count':sum(x>0 for x in vals),'mean_excess_return_pct':round(sum(vals)/n,5) if n else None,
            'median_excess_return_pct':((vals[(n-1)//2]+vals[n//2])/2) if n else None,
            'win_rate_pct':round(100*sum(x>0 for x in vals)/n,2) if n else None,
            'unverified_or_immature_count':len(rows)-n}
    states=Counter(r['paper_state'] for r in rows)
    return {'model_only':True,'total_recommended':len(rows),
            'paper_state_counts':dict(states),'horizons':summary,'rows':rows,
            'limitations':['No real order fills inferred','Tax-specific outcomes require private after-tax analysis',
                'Verified scores exclude provisional adjusted-close provider data until independently audited',
                'PROVISIONAL is source-dated Yahoo adjusted-close; corporate actions, withholding, ROC and quote accuracy not independently audited',
                'A small number of signals cannot establish persistent forecasting skill',
                'Outcome data missing or immature are not counted as wins or losses']}
