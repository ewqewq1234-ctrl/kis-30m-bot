# KIS 30-minute bot

## Trading safeguards

- The code defaults to disabled live execution. The production workflow is
  configured for `KIS_ENV=real`, `DRY_RUN=N`, `ENABLE_LIVE_ORDERS=YES`, and
  `FORCE_BAA=N`. The one-time live-start date guard was `2026-10-20` and has
  elapsed; it is not a current safeguard. Merging changes to the production
  workflow can therefore activate real orders at its next scheduled run.
- Production explicitly sets `BAA_MODE=ATTACK`. This evaluates and selects
  only the existing `ATTACK_UNIVERSE`; Canary signals do not switch it to
  `DEFENSE_UNIVERSE`. `AUTO` is the compatibility default for other invocations.
  Invalid mode values fail closed. If no attack candidate passes its existing
  trend filters, BAA records the skipped selection and reason, sends an alert,
  and does not buy a defensive substitute or automatically liquidate existing
  holdings.
- KODEX 200 (`069500`) uses the separately identified SWING strategy; the
  former DAY intraday entry, fixed profit target, and mandatory intraday
  behavior are disabled. New entries are limited to one share and a 30% NAV
  former-DAY first-entry sleeve cap (30% sleeve × 50% initial weight × 96%
  cash buffer, at most 14.4% of NAV), further bounded by available cash and the
  broker's buyable quantity. BAA's configured 80% allocation is unchanged.
- The four BAA Canary assets are `360750` TIGER 미국S&P500, `251350` KODEX
  MSCI선진국, `195980` PLUS 신흥국MSCI(합성 H), and `437080` KODEX
  미국종합채권ESG액티브(H). KODEX 200 (`069500`) remains excluded from both
  Canary scoring and aggressive BAA ranking.
- `251350` follows a developed-markets index that includes US stocks, so it is
  not an exact equivalent of VEA (developed markets excluding the US).
  `437080` is an ESG-screened US total-bond product and may differ in holdings
  and exclusions from BND. The requested `195980` label uses the current PLUS
  brand. The configured daily-history request uses unadjusted closes
  (`FID_ORG_ADJ_PRC=0`), so corporate actions and distributions may affect
  comparisons. Daily-history parsing and read-only request restrictions remain
  covered by the offline regression suite.
- Defensive BAA ticker codes remain `423160`, `449170`, `453850`, and `411060`.
  `449170` is labeled TIGER KOFR금리액티브(합성), not a USD SOFR fund;
  `453850` is labeled ACE 미국30년국채액티브(H). `423160` and `449170` may
  provide overlapping KOFR-related exposure. Display-name updates do not
  modify historical transactions, holdings, strategy ownership, or state.
- The aggressive BAA candidate set remains three ETFs, with `069500` excluded.
  The 80% target allocation, 13-day rebalance interval, momentum calculation,
  trend filters, defensive allocation, and 96% cash buffer remain unchanged.
- With `BAA_MODE=ATTACK`, only `133690`, `122630`, and `381180` are evaluated
  as BAA targets. Canary data/signals do not trigger defensive selection. No
  eligible attack target or unusable required candidate data blocks new BAA
  purchases; the skip reason is persisted in BAA state and alerted. `FORCE_BAA`
  remains a dry-run diagnostic override and is not the operating-mode switch.
- BAA uses a default target of 80% of total assets in an uptrend and retains
  the existing 50% reduction in a downtrend. Actual post-sale cash is checked
  before a follow-up purchase.
- Weekday trade checks run at 09:05, 09:35, and every 30 minutes through
  15:05 KST (13 unique trade slots). A separate 15:35 KST report-only slot
  skips both SWING and BAA trading code. GitHub's documented
  [`timezone: Asia/Seoul` schedule setting](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
  is used directly; each slot has a unique cron and is mapped explicitly to
  its KST time. The existing single
  account concurrency group remains serialized with `cancel-in-progress: false`.
- Each scheduled invocation logs its event, cron, KST slot, run ID, workflow
  creation time, runner start time, and bot start time without logging secrets.
  A trade run is skipped and alerts if it starts more than five minutes after
  its expected slot, its timing metadata is missing/inconsistent, or it crosses
  a KST date boundary. Order submissions also retain the independent market
  hours/holiday guard (`09:00`–`15:20` KST). A report-only run never invokes
  either strategy. A gap over 40 minutes is alerted when a later scheduled run
  arrives; a completely dropped final trigger cannot alert itself and needs an
  independent Actions monitor. GitHub may delay or drop cron events, so these
  schedules do not guarantee punctual execution or real-time stop monitoring.

| KST slot | Cron (`timezone: Asia/Seoul`) | Behavior |
| --- | --- | --- |
| 09:05 | `5 9 * * 1-5` | Trade check |
| 09:35 | `35 9 * * 1-5` | Trade check |
| 10:05, 10:35 | `5 10 * * 1-5`, `35 10 * * 1-5` | Trade checks |
| 11:05, 11:35 | `5 11 * * 1-5`, `35 11 * * 1-5` | Trade checks; BAA window remains 11:00–11:25 |
| 12:05, 12:35 | `5 12 * * 1-5`, `35 12 * * 1-5` | Trade checks |
| 13:05, 13:35 | `5 13 * * 1-5`, `35 13 * * 1-5` | Trade checks |
| 14:05, 14:35 | `5 14 * * 1-5`, `35 14 * * 1-5` | Trade checks |
| 15:05 | `5 15 * * 1-5` | Last trade check; orders remain cut off at 15:20 |
| 15:35 | `35 15 * * 1-5` | Report only; no strategy/order execution |

## KODEX 200 SWING (initial, unvalidated settings)

Signals use 100 or more validated, unadjusted KIS daily OHLCV bars. The latest
bar must be the prior exchange session confirmed by KIS's holiday calendar.
Entry on the next exchange session requires a close above the preceding 20
closes and SMA20 above SMA60. Entry is permitted only 09:10–14:30 KST, at no
more than signal close + ATR14, with one share maximum. The deterministic
100-session initialization window seeds ATR from the arithmetic mean of its
first 14 valid true ranges, then applies Wilder recursion. Initial stop is
verified average fill minus 2 × frozen entry ATR.
The trailing stop is the greater of its previous value, the initial stop, and
the highest eligible completed close since entry minus 3 × frozen entry ATR.
A close below the preceding 10 closes persists a trend-exit intent. Exits are
application-monitored and may execute below a stop after a gap or scheduling
delay; there is no fixed take-profit or holding-period liquidation.

| Rule | Initial setting |
| --- | --- |
| Entry breakout / trend | Prior 20-session closing high / SMA20 > SMA60 |
| Entry volatility cap | Signal close + ATR14 |
| Initial stop | Verified fill − 2 × entry ATR14 |
| Trailing stop | Highest eligible close − 3 × fixed entry ATR14 |
| Trend exit | Close below the prior 10-session closing low |

These are research parameters, not proven or optimized settings. Migration
validates every historical KODEX 200 DAY/BAA order lifecycle and permits
completed legacy activity only when its strategy-specific net position is zero,
no conflicting order remains unresolved, BAA has no KODEX 200 ownership, and
the normal runtime's current account snapshot is flat. It then persists an
explicit migration-complete marker without changing the journal. Any missing
fill/cancellation evidence, malformed lifecycle, nonzero legacy position, or
nonzero account holding blocks SWING; legacy holdings are never adopted or
liquidated automatically. Strategy performance and actual schedule
reliability remain to be validated.

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
python -m unittest -v test_bot_once.py test_api_resilience.py test_one_share.py test_swing_strategy.py
```

The Ubuntu regression workflow runs the proposed suite with outbound socket
connections blocked. These tests do not submit KIS order requests.
The `ci/verify_kis_market_data.py` module is retained because the offline
regression suite imports its data-validation and request-allowlist helpers.
Obsolete one-time patch workflows, manual diagnostic workflow and saved test
output have been removed.

The trading workflow is live-capable and must not be manually dispatched for
testing. Review its diff and offline checks, then obtain explicit approval
before merging; merging can enable `BAA_MODE=ATTACK` and the schedule above.
