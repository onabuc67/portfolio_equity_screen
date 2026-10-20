# Commissioning status — 2026-10-20

## Completed in this chat
- Located/reviewed four canonical native Library sources; this code package does not mutate them.
- Verified through public web browsing that Nasdaq's screener JSON publishes market-cap/price rows and an as-of **session date**, and that the S&P 500 open constituent CSV publishes ticker + GICS sector + CIK. Web browsing does not prove the runner can access those sites from GitHub.
- Implemented a scheduled Python collector with fail-closed source/date handling, SEC XBRL annual fundamental enrichment, US/foreign/ETF breadth checks, country diversity, basic liquidity proxy, and 15+15 preliminary issuer research queues.
- Tested local software with 9 unittest checks; the mocked 371-row integration scenario passes 220 US + 120 foreign equities + 31 international ETFs, 11 sectors, 8 foreign countries; the upstream-offline test fails closed.
- The local container cannot resolve external DNS. Direct real-data collection in this conversation is therefore **NOT TESTED**.
- No external GitHub repository or scheduled workflow was deployed. No two-day live commissioning occurred.
- Existing ChatGPT task remains a separate enabled hourly automation, and has NOT yet been pointed to a deployed JSON URL.

## Outstanding gates to claim production readiness
1. User-authorized internet-enabled hosted job, configured with SEC contact email, successfully gets real market + issuer records.
2. At least 200 US individual issuers, 100 non-US issuers from six countries, 20 international ETFs, all source dates/liquidity and eleven US sectors verified.
3. Existing ChatGPT automation can read date-current manifest without recurring prompts; retrieves and verifies 6 US + 6 foreign issuers, full 25/20/20/15/10/10 scoring and vetoes and delivers 3+3 ranked defensible recommendations or explicit gap before close.
4. Two consecutive actual trading days show complete output and no duplicate/late deliverables.

## Observability
- `public/status.json`: current job run, data-source failure reasons, attempted/validated counts, producer status.
- `public/latest.json`: latest first-pass data-ready shortlist, only overwritten when a run meets coverage gates. Consumers must check status.json **and** date/freshness; never trust latest.json alone.
- `DATA_READY_RESEARCH_PENDING`: bulk first-pass success **ONLY**; not a finished daily portfolio recommendation.
- `FAILED` / `FAILED_COVERAGE`: do not claim or fabricate screening completion.
