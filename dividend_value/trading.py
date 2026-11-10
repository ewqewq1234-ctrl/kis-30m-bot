"""Tuesday entries and sampled trailing exits, using the bot's durable journal.

No broker transport here: all orders go through execute_order/reconciliation.
"""
from datetime import date, timedelta
import math
import os

from .rules import DEFAULT_CONFIG

STRATEGY = 'DIVIDEND'
ONE_DAY_ENTRY_DATE = date(2026, 11, 2)
ONE_OFF_PARTIAL_DATE = date(2026, 11, 10)


def is_one_off_partial_snapshot(snapshot, today):
    """Permit only the verified-PASS subset of the Nov 10 completed-universe scan."""
    return (today == ONE_OFF_PARTIAL_DATE
            and snapshot.get('one_off_partial') is True
            and snapshot.get('partial') is True
            and snapshot.get('scan_date') == today.isoformat()
            and snapshot.get('universe_verified') is True
            and snapshot.get('unprocessed') == 0
            and isinstance(snapshot.get('query_failed'), int)
            and snapshot.get('query_failed') > 0
            and not snapshot.get('blocker'))


def _months_ago(iso_date, months):
    import calendar
    as_of = date.fromisoformat(iso_date)
    month_idx = as_of.year * 12 + as_of.month - 1 - months
    year, zero_month = divmod(month_idx, 12)
    month = zero_month + 1
    return date(year, month, min(as_of.day, calendar.monthrange(year, month)[1]))


def entry_day(now):
    return now.weekday() == 1 or now.date() == ONE_DAY_ENTRY_DATE


def path(bot):
    return bot.BASE_DIR / 'dividend_state.json'


def rebuild(bot):
    state = bot.load_json(path(bot), {})
    previous = state.get('positions', {})
    positions, exits, ids, closed_ids = {}, {}, set(), set()
    for order in bot.journal_orders():
        if order.get('strategy') != STRATEGY:
            continue
        oid = order.get('id')
        if not oid or oid in ids:
            bot.fail('DIVIDEND journal identity missing/duplicate')
        ids.add(oid)
        qty = bot.share_quantity(order.get('confirmed_fill_qty', 0))
        if qty > bot.share_quantity(order.get('requested_qty')):
            bot.fail('DIVIDEND invalid fill quantity')
        if not qty:
            continue
        code = order.get('code')
        amount = float(order.get('confirmed_fill_amt') or 0)
        if not code or not math.isfinite(amount) or amount <= 0:
            bot.fail('DIVIDEND fill evidence missing')
        if order.get('side') == 'BUY':
            if code in positions:
                bot.fail('DIVIDEND additional buys prohibited')
            meta = order.get('strategy_metadata', {})
            if not meta.get('corp_code') or not meta.get('industry'):
                bot.fail('DIVIDEND ownership metadata missing')
            avg = amount / qty
            old = previous.get(code, {})
            if old and (not math.isfinite(float(old.get('high', avg))) or float(old.get('high', avg)) <= 0):
                bot.fail('DIVIDEND invalid persisted trailing peak')
            same = old.get('entry_id') == oid
            positions[code] = dict(meta, qty=qty, avg_price=avg, entry_id=oid,
                high=max(avg, float(old.get('high', avg))) if same else avg,
                armed=bool(old.get('armed')) if same else False,
                exit_intent=old.get('exit_intent') if same else None)
        elif order.get('side') == 'SELL':
            if code not in positions or qty > positions[code]['qty']:
                bot.fail('DIVIDEND sale exceeds verified ownership')
            positions[code]['qty'] -= qty
            if not positions[code]['qty']:
                closed_ids.add(positions[code]['entry_id'])
                del positions[code]
                exits[code] = order['created_at'][:10]
        else:
            bot.fail('DIVIDEND invalid journal side')
    if any(p.get('qty') and code not in positions and code not in exits for code,p in previous.items()):
        bot.fail('DIVIDEND position has no supporting journal')
    if any(p.get('qty') and code in positions and p.get('entry_id') != positions[code]['entry_id']
           and p.get('entry_id') not in closed_ids for code,p in previous.items()):
        bot.fail('DIVIDEND position identity mismatch')
    state.update(positions=positions, last_exits=exits)
    if ids or path(bot).exists():
        bot.save_json(path(bot), state)
    return state


def reconcile_ownership(bot, holdings):
    state = rebuild(bot)
    baa = bot.load_json(bot.BAA_STATE_FILE, {})
    for code,p in state['positions'].items():
        if (bot.share_quantity(holdings.get(code, {}).get('qty', 0)) != p['qty']
                or code == baa.get('target_code') or code == bot.CODE_BULL):
            bot.fail('DIVIDEND account/strategy ownership conflict')
    return state


def guard_order(bot, code, qty, side, metadata):
    state = bot.load_json(path(bot), {})
    if side == '01':
        p = state.get('positions', {}).get(code, {})
        if qty != p.get('qty') or not p.get('exit_intent'):
            bot.fail('DIVIDEND unverified exit intent/quantity')
    else:
        permit = state.get('buy_permits', {}).get(code, {})
        now = bot.now_kst()
        now_hms = now.strftime('%H:%M:%S')
        scan_date = metadata.get('signal_scan_date') if isinstance(metadata, dict) else None
        try:
            scan_day = date.fromisoformat(scan_date)
        except (TypeError, ValueError):
            bot.fail('DIVIDEND buy signal date is invalid')
        legacy = now.date() == ONE_DAY_ENTRY_DATE and scan_day == ONE_DAY_ENTRY_DATE
        cycle_day = (
            scan_day.weekday() == 1
            and 0 <= (now.date() - scan_day).days <= 3
            and 1 <= now.weekday() <= 4
        )
        exact_batch = (
            state.get('buy_batch_status') == 'IN_PROGRESS'
            and state.get('buy_batch_scan_date') == scan_date
            and state.get('buy_batch_source_sha') == metadata.get('source_sha')
        )
        if (not (legacy or (cycle_day and exact_batch))
                or not ('09:10:00' <= now_hms <= '15:00:00')
                or (legacy and qty != 1)
                or permit.get('date') != now.date().isoformat()
                or qty != permit.get('qty') or metadata != permit.get('metadata')):
            bot.fail('DIVIDEND buy lacks valid dated permit or is outside Tue-Fri 09:10-15:00 cycle')


def exits(bot, token, holdings, deposit, nav):
    state = reconcile_ownership(bot, holdings)
    rules = DEFAULT_CONFIG['future_execution']
    snapshot = bot.load_json(bot.BASE_DIR / 'dividend_signals.json', {})
    stamp = snapshot.get('scan_date', '')
    if stamp and stamp <= bot.now_kst().date().isoformat():
        for code, reasons in snapshot.get('review_alerts', {}).items():
            key = stamp + ':' + code
            if code in state['positions'] and key not in state.get('review_alerted', []):
                bot.send_telegram(f'DIVIDEND {code}: 공시 매도 검토 / 신규매수 중지 {reasons}', is_alert=True)
                state.setdefault('review_alerted', []).append(key)
        bot.save_json(path(bot), state)
    for code in list(state['positions']):
        p = state['positions'][code]
        price = bot.get_current_price(token, code)
        p['high'] = max(p['high'], price)
        p['armed'] = p['armed'] or price >= p['avg_price'] * (1 + rules['trailing_activation_gain'])
        if p['armed'] and price <= p['high'] * (1 - rules['trailing_drawdown']):
            p['exit_intent'] = 'TRAILING_5_PERCENT'
        # Save observed peak/intent before submitting an order; a crash cannot
        # reset an activated trailing stop or forget an unexecuted exit.
        bot.save_json(path(bot), state)
        if p.get('exit_intent'):
            bot.execute_order(token, code, p['qty'], '01', strategy=STRATEGY,
                action_name='DIV exit', cost_basis=p['avg_price'])
            bot.assert_no_pending(token, allow_pending_dividend_buys=True)
            state = rebuild(bot)
            if not bot.DRY_RUN:
                holdings, deposit, nav = bot.get_account_balance(token)
    return holdings, deposit, nav


def cooldown_complete(bot, token, last_exit, today):
    if not last_exit:
        return True
    cursor, count = date.fromisoformat(last_exit), 0
    while cursor < today and count < 20:
        cursor += timedelta(days=1)
        if bot.is_market_open_date(token, cursor):
            count += 1
    return count >= 20


def entries(bot, token, holdings, deposit, nav):
    now = bot.now_kst()
    today = now.date()
    if not ('09:10:00' <= now.strftime('%H:%M:%S') <= '15:00:00'):
        return
    one_day_test = today == ONE_DAY_ENTRY_DATE
    if not one_day_test and not 1 <= now.weekday() <= 4:
        return
    state = reconcile_ownership(bot, holdings)
    week = f'{now.isocalendar().year}-{now.isocalendar().week}'
    snapshot = bot.load_json(bot.BASE_DIR / 'dividend_signals.json', {})
    scan_date = snapshot.get('scan_date')
    try:
        scan_day = date.fromisoformat(scan_date)
    except (TypeError, ValueError):
        return
    expected = os.getenv('GITHUB_SHA')
    cycle_tuesday = today - timedelta(days=now.weekday() - 1) if 1 <= now.weekday() <= 4 else None
    regular = (entry_day(now) and scan_day == today)
    continuation = (
        cycle_tuesday is not None
        and scan_day == cycle_tuesday
        and state.get('buy_week') == week
        and state.get('buy_batch_status') == 'IN_PROGRESS'
        and state.get('buy_batch_scan_date') == scan_date
        and state.get('buy_batch_source_sha') == snapshot.get('source_sha')
    )
    if not regular and not continuation:
        return
    partial_oneoff = is_one_off_partial_snapshot(snapshot, today)
    if partial_oneoff and state.get('one_off_partial_consumed') == today.isoformat():
        return
    same_cycle = (
        state.get('buy_week') == week
        and state.get('buy_batch_scan_date') == scan_date
        and state.get('buy_batch_source_sha') == snapshot.get('source_sha')
    )
    if regular and same_cycle and state.get('buy_batch_status') == 'COMPLETE':
        return
    prev = bot.previous_exchange_session(token, scan_day).isoformat()
    source_matches = (snapshot.get('source_sha') == expected if regular and not same_cycle
                      else snapshot.get('source_sha') == state.get('buy_batch_source_sha'))
    if (not snapshot or snapshot.get('as_of') != prev
            or (snapshot.get('partial') and not partial_oneoff)
            or not snapshot.get('source_sha') or not source_matches):
        print('>> DIVIDEND: waiting for the complete Tuesday cycle snapshot to validate')
        return
    rows = snapshot.get('candidates', [])
    if not isinstance(rows, list):
        bot.fail('DIVIDEND malformed candidate snapshot')
    if not any(isinstance(row, dict) and row.get('status') == 'PASS' for row in rows):
        return
    # Only a single invocation can consume this incomplete, strictly scoped snapshot.
    if partial_oneoff and not bot.DRY_RUN:
        state['one_off_partial_consumed'] = today.isoformat()
        bot.save_json(path(bot), state)
    # Start one durable cycle on Tuesday. Wednesday-Friday reuse this exact
    # snapshot and re-evaluate only symbols not already filled or pending.
    if regular and not same_cycle and not bot.DRY_RUN:
        state.update(buy_week=week, buy_batch_status='IN_PROGRESS',
                     buy_batch_scan_date=scan_date,
                     buy_batch_source_sha=snapshot['source_sha'],
                     buy_permits={}, buy_attempts={})
        if not one_day_test:
            bot.save_json(path(bot), state)
    state.setdefault('buy_permits', {})
    state.setdefault('buy_attempts', {})
    if (one_day_test
            and any(day == today.isoformat()
                    for day in state.get('buy_attempts', {}).values())):
        return
    rules = DEFAULT_CONFIG['future_execution']
    pending_codes = {str(o.get('code')) for o in bot.journal_orders()
                     if o.get('strategy') == STRATEGY and o.get('side') == 'BUY'
                     and o.get('status') in {'ACCEPTED', 'PARTIAL'}}
    attempted_today = {
        str(code) for code, attempt_day in state.get('buy_attempts', {}).items()
        if attempt_day == today.isoformat()
    }
    attempted_today.update(
        str(o.get('code')) for o in bot.journal_orders()
        if o.get('strategy') == STRATEGY and o.get('side') == 'BUY'
        and str(o.get('created_at', ''))[:10] == today.isoformat()
    )
    for candidate in sorted(rows, key=lambda r:r['code']):
        if candidate.get('status') != 'PASS':
            continue
        if partial_oneoff:
            # The full-universe scan may have failed for other symbols. Never
            # buy an unverified symbol, or one younger than 7 calendar months.
            try:
                listed = date.fromisoformat(candidate['listing_date'])
                if (not candidate.get('query_complete')
                        or not candidate.get('listing_date_verified')
                        or listed > _months_ago(prev, 7)
                        or candidate.get('risk_verified') is not True
                        or candidate.get('risk_flagged') is True
                        or candidate.get('query_failed')
                        or candidate.get('exclusion')):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
        code = candidate['code']
        if code in pending_codes:
            print(f'>> DIVIDEND {code}: prior buy remains pending; skip duplicate and continue other candidates')
            continue
        if code in attempted_today:
            continue
        if code in holdings or code in bot.ATTACK_UNIVERSE or code in bot.DEFENSE_UNIVERSE or code == bot.CODE_BULL:
            continue
        if not cooldown_complete(bot, token, state.get('last_exits', {}).get(code), today):
            continue
        corp, sector = candidate.get('corp_code'), str(candidate.get('industry') or '')[:2]
        if not corp or not sector:
            continue
        positions = state['positions']
        if len(positions) >= rules['max_positions'] or any(p['corp_code'] == corp for p in positions.values()):
            continue
        prices = {c:bot.get_current_price(token,c) for c in positions}
        value = sum(p['qty']*prices[c] for c,p in positions.items())
        sector_value = sum(p['qty']*prices[c] for c,p in positions.items() if p['industry'] == sector)
        sleeve = nav * bot.DIVIDEND_VALUE_TARGET_RATIO
        metrics = candidate.get('metrics', {})
        if hasattr(bot, 'get_current_quote'):
            price, best_ask = bot.get_current_quote(token, code)
        else:
            price, best_ask = bot.get_current_price(token, code), None
        ceiling = metrics.get('buy_ceiling')
        tax_qty = (metrics.get('tax_quantity') or {}).get('additional_qty')
        turnover = candidate.get('avg_turnover_20d')
        if not ceiling or tax_qty is None or not turnover or price > ceiling:
            continue
        # Submit a valuation-capped limit even if the live price moves above it.
        # The order may wait for a pullback; it is reconciled before the next day.
        limit = bot.limit_buy_tick(min(best_ask or price, ceiling))
        # Normal sizing keeps the 3% initial and 4% company targets.
        # For a small account, allow one share when those soft targets alone
        # would round the order to zero. The hard 40% sleeve, 20% sector,
        # available-cash, liquidity, valuation and tax limits still apply.
        hard_capacity = min(
            max(0, sleeve*.20-sector_value),
            max(0, sleeve-value),
            max(0, deposit*.96),
            turnover*.01,
        )
        budget = min(sleeve*.03, sleeve*.04, hard_capacity)
        qty = min(max(0, int(budget//limit)), int(tax_qty)) if limit > 0 else 0
        if (qty <= 0 and limit > 0 and int(tax_qty) >= 1
                and limit <= hard_capacity):
            qty = 1
            print(f'>> DIVIDEND {code}: 소액계좌 최소 1주 예외 적용 '
                  f'(지정가 {limit:,.0f}원, 잔여 한도 {hard_capacity:,.0f}원)')
        if one_day_test:
            qty = min(qty, 1)
        if qty <= 0:
            continue
        metadata = {'corp_code':corp,'industry':sector,'signal_as_of':prev,
                    'source_sha':snapshot['source_sha'],'signal_scan_date':scan_date}
        state['buy_permits'][code] = {'date':today.isoformat(),'qty':qty,'metadata':metadata}
        if not bot.DRY_RUN:
            # Persist the per-day attempt before POST. A same-day rerun will
            # never duplicate it; an expired zero-fill can be recalculated tomorrow.
            state['buy_attempts'][code] = today.isoformat()
            bot.save_json(path(bot), state)
            bot.execute_order(token,code,qty,'02',ord_dvsn='00',price=limit,
                strategy=STRATEGY,action_name='DIV entry',strategy_metadata=metadata)
            bot.assert_no_pending(token, allow_pending_dividend_buys=True)
            state = rebuild(bot)
            state.setdefault('buy_attempts', {})[code] = today.isoformat()
            bot.save_json(path(bot), state)
            pending_codes = {str(o.get('code')) for o in bot.journal_orders()
                             if o.get('strategy') == STRATEGY and o.get('side') == 'BUY'
                             and o.get('status') in {'ACCEPTED', 'PARTIAL'}}
            holdings, deposit, nav = bot.get_account_balance(token)
        else:
            print(f'>> [DIVIDEND DRY_RUN] eligible {code} qty={qty}; no order')
        if one_day_test:
            break
    # Friday's final scheduled entry check closes the weekly decision cycle.
    # Any accepted Friday limit remains a broker order for that session and is
    # reconciled on the next run, but it is not retried outside this cycle.
    if (not bot.DRY_RUN and now.weekday() == 4
            and now.strftime('%H:%M:%S') >= '14:42:00'
            and state.get('buy_batch_status') == 'IN_PROGRESS'):
        state.update(buy_batch_status='COMPLETE',
                     buy_batch_completed_at=now.isoformat())
        bot.save_json(path(bot), state)

