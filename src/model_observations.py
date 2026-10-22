"""Prospective, model-only paper signal follow-up.

This job never reads or publishes private owner portfolio data, never trades,
and cannot turn WATCH/REVIEW REQUIRED into a hypothetical recommendation.
Yahoo daily chart adjusted closes provide PROVISIONAL comparison observations:
corporate actions and tax classification have not been independently audited.
"""
from __future__ import annotations

from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import copy
import math
import requests

from src.model_performance import HORIZONS, validate_model_ledger

NEW_YORK = ZoneInfo("America/New_York")


def _when(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("Offset-aware event timestamp required")
    return d.astimezone(timezone.utc)


def _market_close(day):
    return datetime.combine(date.fromisoformat(day), time(16, 0), NEW_YORK).astimezone(timezone.utc)


def _num(n):
    return type(n) in (float, int) and math.isfinite(n) and n > 0


def yahoo_chart(ticker, earliest, now, http=None):
    """Fetch *dated* adjusted daily closes; fail rather than make up data."""
    client = http or requests.Session()
    link = ("https://query1.finance.yahoo.com/v8/finance/chart/" +
            quote(ticker, safe=""))
    response = client.get(link,
        params={"period1": int((earliest - timedelta(days=7)).timestamp()),
                "period2": int((now + timedelta(days=1)).timestamp()),
                "interval": "1d", "events": "div,splits",
                "includeAdjustedClose": "true"},
        headers={"User-Agent": "Mozilla/5.0 (compatible; PublicModelFollowup/1.0)"},
        timeout=25)
    response.raise_for_status()
    body = response.json()
    if body.get("chart", {}).get("error"):
        raise ValueError(str(body["chart"]["error"]))
    result = (body.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo chart response empty")
    ind = result["indicators"]
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose")
    close = (ind.get("quote") or [{}])[0].get("close")
    times = result.get("timestamp") or []
    if not adj or not close or len(adj) != len(times) or len(close) != len(times):
        raise ValueError("Complete adjusted daily quotes unavailable")
    prices = {}
    for stamp, raw, adjusted in zip(times, close, adj):
        if not _num(raw) or not _num(adjusted):
            continue
        day = datetime.fromtimestamp(stamp, NEW_YORK).date().isoformat()
        if day in prices:
            raise ValueError("Duplicate dated market session")
        prices[day] = {"date": day, "close": float(raw),
                       "adjusted_close": float(adjusted)}
    if not prices:
        raise ValueError("No dated adjusted market quotes")
    return prices, link


def _completed_session_date(asof):
    ny = asof.astimezone(NEW_YORK)
    if ny.hour < 16 or (ny.hour == 16 and ny.minute < 20):
        return (ny.date() - timedelta(days=1)).isoformat()
    return ny.date().isoformat()


def advance(ledger, asof_utc, provider=yahoo_chart):
    """Append-only paper fills and 5/20/60-session provisional assessments.

    Assumes a recommendation specifically precommitted NEXT_SESSION_CLOSE.
    Other conditional/limit ideas are left NO_FILL/UNVERIFIED, never faked.
    Inputs and evaluation horizon are frozen before the future entry session.
    """
    validate_model_ledger(ledger)
    asof = _when(asof_utc)
    out = copy.deepcopy(ledger)
    issued = [e for e in out.get("signal_events", []) if e["event_type"] == "ISSUED"]
    existing_fills = {e["signal_id"]: e for e in out["signal_events"]
                      if e["event_type"] == "TRIGGERED_PAPER"}
    existing_expiry = {e["signal_id"] for e in out["signal_events"]
                       if e["event_type"] in ("CANCELED", "EXPIRED", "NO_FILL", "SUPERSEDED")}
    assessed = {(e["signal_id"], e["trading_sessions"]) for e in
                out.get("assessment_events", [])}
    report = {"asof_utc": asof.isoformat(), "model_only": True,
              "provider": "Yahoo daily adjusted chart (provisional; not issuer-audited)",
              "issued_count": len(issued), "new_paper_entries": 0,
              "new_provisional_assessments": 0, "unverified": [],
              "skipped_nonmechanical_rules": [], "source_errors": []}
    cutoff = _completed_session_date(asof)
    cache = {}

    def fetch(ticker, issued_at):
        if ticker not in cache:
            cache[ticker] = provider(ticker, issued_at, asof)
        return cache[ticker]

    for signal in issued:
        sid = signal["signal_id"]
        if sid in existing_expiry:
            continue
        entry_rule = signal.get("entry_rule") or {}
        if entry_rule.get("type") != "NEXT_SESSION_CLOSE":
            report["skipped_nonmechanical_rules"].append(sid)
            continue
        published = _when(signal["published_at_utc"])
        original_quote = _when(signal["reference_quote"]["asof_utc"])
        if original_quote > published:
            report["unverified"].append({"signal_id": sid,
                                          "reason": "Reference quote postdates recommendation"})
            continue
        if published > asof:
            report["unverified"].append({"signal_id": sid, "reason": "Future issued timestamp"})
            continue
        try:
            security, sec_url = fetch(signal["ticker"], published)
            benchmark, bench_url = fetch(signal["benchmark_ticker"], published)
        except (requests.RequestException, ValueError, KeyError, TypeError) as err:
            report["source_errors"].append({"signal_id": sid,
                                             "reason": str(err)[:200]})
            continue

        original_market_date = published.astimezone(NEW_YORK).date().isoformat()
        common = sorted(day for day in set(security) & set(benchmark)
                        if original_market_date < day <= cutoff)
        if not common:
            report["unverified"].append({"signal_id": sid, "reason": "No completed post-issue session"})
            continue
        first = common[0]
        entry_time = _market_close(first)
        expiry = _when(signal["expiry"])
        if entry_time > expiry:
            if sid not in existing_expiry:
                out["signal_events"].append({"event_type": "EXPIRED", "signal_id": sid,
                    "event_at_utc": asof.isoformat(),
                    "reason": "First independently dated market close later than precommitted expiry"})
            continue

        if sid not in existing_fills:
            fill = {"event_type": "TRIGGERED_PAPER", "signal_id": sid,
                    "event_at_utc": entry_time.isoformat(),
                    "paper_only": True, "entry_rule": "NEXT_SESSION_CLOSE",
                    "entry_session": first, "paper_entry_price": security[first]["close"],
                    "benchmark_entry_price": benchmark[first]["close"],
                    "reference_source": sec_url, "benchmark_source": bench_url}
            out["signal_events"].append(fill)
            existing_fills[sid] = fill
            report["new_paper_entries"] += 1
        else:
            first = existing_fills[sid].get("entry_session")
            if not first or first not in common:
                report["unverified"].append({"signal_id":sid,
                                              "reason":"Original paper-entry session no longer independently matched"})
                continue
            common = [d for d in common if d >= first]

        entry_security = security[first]["adjusted_close"]
        entry_benchmark = benchmark[first]["adjusted_close"]
        for horizon in HORIZONS:
            if (sid, horizon) in assessed:
                continue
            if len(common) < horizon + 1:
                continue
            last = common[horizon]
            sec_ret = 100 * (security[last]["adjusted_close"] / entry_security - 1)
            bench_ret = 100 * (benchmark[last]["adjusted_close"] / entry_benchmark - 1)
            # Vendor-adjusted returns are useful but do NOT claim independent
            # dividend/ROC/corporate-action verification.
            out["assessment_events"].append({
                "signal_id": sid, "trading_sessions": horizon,
                "asof_utc": asof.isoformat(),
                "entry_session": first, "exit_session": last,
                "adjustments_verified": False,
                "evidence_quality": "PROVISIONAL_YAHOO_ADJUSTED_CLOSE",
                "provisional_idea_total_return_pct": round(sec_ret, 6),
                "provisional_benchmark_total_return_pct": round(bench_ret, 6),
                "friction_pct": float(entry_rule.get("friction_pct", 0.2)),
                "idea_source_url": sec_url, "benchmark_source_url": bench_url,
                "assumptions": "Paper next-session close, no actual order; Yahoo adjusted close includes reported cash distributions/splits but not independently reconciled tax/ROC/corporate actions"})
            assessed.add((sid, horizon))
            report["new_provisional_assessments"] += 1

    out["as_of_utc"] = asof.isoformat()
    validate_model_ledger(out)
    return out, report
