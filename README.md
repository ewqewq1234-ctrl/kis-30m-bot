# KIS 30-minute bot

## Trading safeguards

- Live-order execution is disabled by default. Keep `DRY_RUN=Y`,
  `ENABLE_LIVE_ORDERS=NO`, and `FORCE_BAA=N` until account/ledger reconciliation,
  open-order review, and final operator approval are complete.
- KODEX 200 (`069500`) is reserved for short-term trading. New buys are limited
  to one share, and the broker balance is checked again immediately before a
  buy intent is written. A balance/ledger mismatch, unresolved order, or
  uncertain API result halts new orders.
- `069500` remains in the Canary set but is excluded from aggressive BAA
  candidates and momentum ranking. The aggressive BAA candidate count is
  temporarily three until a fourth ETF is verified against the KIS market-data
  API. The 13-day rebalance interval, momentum calculation, trend filters,
  defensive allocation, and 96% cash buffer remain unchanged.
- BAA uses a default target of 80% of total assets in an uptrend and retains
  the existing 50% reduction in a downtrend. Actual post-sale cash is checked
  before a follow-up purchase.
- Weekday GitHub Actions schedules use UTC cron expressions. The afternoon
  schedule covers 14:00–15:20 KST in five-minute increments and retains the
  separate 14:53 settlement event. A later run can recover a missed settlement.
  GitHub Actions does not guarantee exact execution times; scheduled checks
  cannot provide real-time stop-loss monitoring.

## Aggressive BAA universe review (2026-10-19 KST)

The requested fourth candidate is **not enabled**. Three possible replacements
were screened using public Yahoo Finance chart history (`range=5y`, daily
interval). For each, the retrieved response contained 1,200 non-null daily
closing prices, exceeding 252 observations. Daily volumes below are share
counts; the 20-day average is calculated from that response. This verifies
public ticker/history data, **not** ticker or history access through the KIS
API. No KIS API requests were made for this review.

| Candidate | Underlying/objective | First-trade date | 20-session average volume | 2026-10-19 volume | Overlap / key risks |
|---|---|---:|---:|---:|---|
| 241180 TIGER Nikkei225 | Nikkei 225 Japanese large-cap equities | 2016-03-31 | 42,980 | 41,307 | Best geographic diversification from the current US growth/semi and Korean index exposure; Japan concentration and JPY currency risk. |
| 132030 KODEX Gold Futures(H) | Gold exposure via futures, currency-hedged product | 2010-10-01 | 87,635 | 31,558 | Low equity overlap and adequate history; futures roll, hedge, and commodity risks; less aligned with an equity-growth aggressive sleeve. |
| 091160 KODEX Semicon | Korean semiconductor-sector equities (KRX Semiconductor index) | 2007-01-29 | 2,144,297 | 2,306,992 | Highest observed liquidity; substantial sector overlap with 381180 TIGER US PHLX Semiconductor Nasdaq and high industry concentration. |

241180 is the **research shortlist**, not an approved or implemented replacement.
Its public daily history and reported volumes satisfy the screening data
thresholds and it offers the clearest geographic diversification. Its
availability and sufficient historical data via the KIS API remain unverified;
therefore the safe interim choice is to use the existing three aggressive
candidates:

- 133690 TIGER US Nasdaq 100
- 122630 KODEX Leverage
- 381180 TIGER US PHLX Semiconductor Nasdaq

The explicit request only removes 069500. **122630 KODEX Leverage remains in
the aggressive candidate set**; its leveraged KOSPI 200 exposure and amplified
losses remain material risks.

Public chart data sources:

- [241180 daily chart data](https://query1.finance.yahoo.com/v8/finance/chart/241180.KS?range=5y&interval=1d)
- [132030 daily chart data](https://query1.finance.yahoo.com/v8/finance/chart/132030.KS?range=5y&interval=1d)
- [091160 daily chart data](https://query1.finance.yahoo.com/v8/finance/chart/091160.KS?range=5y&interval=1d)

## Verification

Run the offline safety suite with:

```sh
python -m unittest -v test_bot_once.py test_api_resilience.py test_one_share.py
```

The Ubuntu regression workflow runs the proposed suite with outbound socket
connections blocked. These tests do not submit KIS order requests.
