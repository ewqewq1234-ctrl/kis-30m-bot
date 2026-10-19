# KIS 30-minute bot

## Trading safeguards

- The code defaults to disabled live execution. The production workflow is
  configured for `KIS_ENV=real`, `DRY_RUN=N`, `ENABLE_LIVE_ORDERS=YES`, and
  `FORCE_BAA=N`; the execution-time guard blocks live orders before
  2026-10-20 in Korea Standard Time.
- KODEX 200 (`069500`) is reserved for short-term trading. New buys are limited
  to one share, and the broker balance is checked again immediately before a
  buy intent is written. A balance/ledger mismatch, unresolved order, or
  uncertain API result halts new orders.
- The four BAA Canary assets are `360750` TIGER 미국S&P500, `251350` KODEX
  MSCI선진국, `195980` PLUS 신흥국MSCI(합성 H), and `437080` KODEX
  미국종합채권ESG액티브(H). KODEX 200 (`069500`) remains DAY-only and is
  excluded from both Canary scoring and aggressive BAA ranking.
- `251350` follows a developed-markets index that includes US stocks, so it is
  not an exact equivalent of VEA (developed markets excluding the US).
  `437080` is an ESG-screened US total-bond product and may differ in holdings
  and exclusions from BND. The requested `195980` label uses the current PLUS
  brand. The configured daily-history request uses unadjusted closes
  (`FID_ORG_ADJ_PRC=0`), so corporate actions and distributions may affect
  comparisons. The isolated read-only KIS verifier checks current quotes,
  product identity, 252-session unadjusted histories, and adjusted-versus-
  unadjusted return/score differences for the new Canary assets. The strategy
  continues to use unadjusted closes; the adjusted data is for verification
  only. Do not merge this change into production until the verifier passes.
- Defensive BAA ticker codes remain `423160`, `449170`, `453850`, and `411060`.
  `449170` is labeled TIGER KOFR금리액티브(합성), not a USD SOFR fund;
  `453850` is labeled ACE 미국30년국채액티브(H). `423160` and `449170` may
  provide overlapping KOFR-related exposure. Display-name updates do not
  modify historical transactions, holdings, strategy ownership, or state.
- The aggressive BAA candidate set remains three ETFs, with `069500` excluded.
  The 80% target allocation, 13-day rebalance interval, momentum calculation,
  trend filters, defensive allocation, and 96% cash buffer remain unchanged.
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
The separate `.github/workflows/kis-market-data-verification.yml` workflow is
manual and read-only: its API allowlist permits quote, product-master, and
history GET requests plus OAuth token issuance only, blocks all order
endpoints, uses temporary runner state, and reads only the encrypted KIS token
cache from `bot-state` when available. It never reads or writes the persisted
trading ledger or strategy state and never writes to `bot-state`.
