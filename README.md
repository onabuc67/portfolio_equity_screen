# Portfolio & Opportunity Hub — Public-data collector

## What it is
An optional **unattended public-market-data producer** that runs on an internet-connected GitHub Actions runner. It takes no brokerage/financial-account credentials, never reads a private portfolio and never executes trades. Your existing ChatGPT automation remains the decision and reporting process and retains the four native-Library hard gates.

Architecture: **Nasdaq exchange-session quote listing + S&P 500 GICS constituent data + SEC annual XBRL company facts + Stooq dated international ETF daily closes → market-wide coverage QA → 15+15 issuer research queues → public JSON → existing ChatGPT Hub performs deeper issuer research, v1.4 scoring, vetoes and tax/portfolio checks.**

The collector deliberately does **not** pretend to make v1.4 six-factor scores: issuer qualitative diligence is a separate, required gate. No numeric 'Opportunity Score' exists in the output.

## One-time setup — requires user-authorized GitHub repository

1. Create a new **public GitHub repository with only market-data research code** (no private portfolio information). Upload this package to repository root and enable GitHub Actions; a public repository is used because the existing scheduled ChatGPT task can read public raw JSON without recurring authorization prompts.
2. Configure repository secret `SEC_CONTACT_EMAIL` as a valid research contact email, required by the SEC fair-access policy. No brokerage password, brokerage link or trading permission is needed.
3. Run the **Public equity market snapshot (preclose)** workflow manually under Actions. Inspect `public/status.json`. Only `DATA_READY_RESEARCH_PENDING` is an initial bulk-data PASS; `FAILED`/`FAILED_COVERAGE` is a FAIL. The collector produces `public/latest.json` only when coverage is adequate.
4. Once a real run passes, configure the **existing** Portfolio & Opportunity Hub ChatGPT automation to retrieve the exact public raw `latest.json` and `status.json` URLs at its first noon-Mountain reporting run. Check the manifest's report date, source dates, counts, 11-sector/6-country coverage and provider exceptions. If missing/stale, alert on material source failure rather than issuing a fake full screen.
5. The automation must **independently** verify 6+6 issuer finalists using current filings and contemporary timestamped prices, then apply Opportunity Reserve v1.4 six-factor scores, vetoes, stress, account-location, existing holdings, reserve and trade-funding gates. If that phase fails, report INCOMPLETE even if the data producer passes.
6. Commission only after two consecutive **real NYSE trading days** meet all source, 200 US + 100 foreign + 20 ETF, 15+15 investigation, 6+6 validation, quality-control and actual pre-close user delivery requirements. Preserve duplicate suppression. No extra ChatGPT automation.

### Source caveats
* Nasdaq screener is publicly reachable but its web-site endpoint is **not** a formally contracted bulk market-data API. Site format, rate limits and terms can change. It may block automation. All failures are explicit, not quietly substituted with stale data. For higher operational reliability, a licensed bulk-data provider with a contractual API/SLA can be plugged into the same output schema.
* The source's `asof` is a **trading-session date**, **not** a per-security live exchange timestamp. The initial screen uses dated prices and annual historical GAAP metrics; it must not be advertised as intraday trading signals. Finalists require independent intraday quote verification. The prior system's unjustified practice of attaching retrieval times to prices is forbidden.
* Annual GAAP net income can be inflated by investment/nonrecurring gains. The research process must normalize it. SEC companyfacts for ADR issuers can be absent/inconsistent; such rows fail closed. Market caps and financials can be measured at different dates. Results are preliminary triage, not certified expected returns.
* GitHub's schedule is best-effort and sometimes delayed or dropped. This workflow makes repeated scheduled attempts, but the existing ChatGPT automation must independently check the latest snapshot's session date, completion status and report-delivery date. An external SLA-based scheduler is needed if strict on-time completion becomes critical.
* The published JSON includes only selected public market symbols, SEC reference URLs and dates, not raw third-party price/market-cap feeds — **never** the user's portfolio, transactions, tax lots, reserve or holdings.
* Check public data-source terms/licensing before sustained or redistributed use. A commercial license may be required for some automated redistribution activities; the program is intended for personal private research even when technically housed in a public repo. An access-controlled data store may be preferable if licensing prohibits public republication.

## Independent local tests

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
SEC_CONTACT_EMAIL='validcontact@example.org' python -m src.screen --out public
```

The first command set validates logic using **synthetic and mocked** inputs (it does NOT validate market data access). The last command needs an outbound internet-enabled host and a real contact email. `public/status.json` is written even when fetching fails. Never interpret a mocked green test as live production success.

## Existing automation handoff contract

At report time: verify today's or previous complete NYSE session date, source publication, snapshot generated datetime, accepted universe counts, sector/country coverage, 15+15 queues. Do not assign v1.4 Opportunity Scores based only on mechanical PE/FCF screening. For final candidates retrieve issuer filings, normalized annual/TTM cashflow, adjusted earnings, leverage, revisions, competitive thesis, catalysts, and risks and verified quote time. Require 6+6 evidence-backed finalists. Compare against cash/SGOV, taxable credit, duration/TLT and muni constraints, account and tax, concentration. If any gate fails, say exactly which; no trades. Confirm no brokerage login is required.
