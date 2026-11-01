"""Tuesday entries and sampled trailing exits, using the bot's durable journal.

No broker transport here: all orders go through execute_order/reconciliation.
"""
from datetime import date, timedelta
import math
import os

from .rules import DEFAULT_CONFIG

STRATEGY = 'DIVIDEND'


def path(bot):
    return bot.BASE_DIR / 'dividend_state.json'


def rebuild(bot):
    state = bot.load_json(path(bot), {})
    previous = state.get('positions', {})
    positions, exits, ids = {}, {}, set()
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
            if old.get('qty') and old.get('entry_id') != oid:
                bot.fail('DIVIDEND position identity mismatch')
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
                del positions[code]
                exits[code] = order['created_at'][:10]
        else:
            bot.fail('DIVIDEND invalid journal side')
    if any(p.get('qty') and code not in positions and code not in exits for code,p in previous.items()):
        bot.fail('DIVIDEND position has no supporting journal')
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
        if (bot.now_kst().weekday() != 1 or permit.get('date') != bot.now_kst().date().isoformat()
                or qty != permit.get('qty') or metadata != permit.get('metadata')):
            bot.fail('DIVIDEND buy lacks Tuesday durable permit')


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
            bot.assert_no_pending(token)
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
    if now.weekday() != 1 or not ('09:10' <= now.strftime('%H:%M') <= '14:30'):
        return
    state = reconcile_ownership(bot, holdings)
    week = f'{now.isocalendar().year}-{now.isocalendar().week}'
    if state.get('buy_week') == week:
        return
    snapshot = bot.load_json(bot.BASE_DIR / 'dividend_signals.json', {})
    prev = bot.previous_exchange_session(token, now.date()).isoformat()
    expected = os.getenv('GITHUB_SHA')
    if (not snapshot or snapshot.get('as_of') != prev or snapshot.get('partial')
            or not expected or snapshot.get('source_sha') != expected
            or snapshot.get('scan_date') != now.date().isoformat()):
        print('>> DIVIDEND: waiting for a complete, current Tuesday scan')
        return
    rows = snapshot.get('candidates', [])
    if not isinstance(rows, list):
        bot.fail('DIVIDEND malformed candidate snapshot')
    # Once per week, including an empty candidate set. No late/restarted retry
    # of the buy batch after an uncertain order outcome.
    state.update(buy_week=week, buy_permits={})
    if not bot.DRY_RUN:
        bot.save_json(path(bot), state)
    rules = DEFAULT_CONFIG['future_execution']
    for candidate in sorted(rows, key=lambda r:r['code']):
        if candidate.get('status') != 'PASS':
            continue
        code = candidate['code']
        if code in holdings or code in bot.ATTACK_UNIVERSE or code in bot.DEFENSE_UNIVERSE or code == bot.CODE_BULL:
            continue
        if not cooldown_complete(bot, token, state.get('last_exits', {}).get(code), now.date()):
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
        price = bot.get_current_price(token, code)
        ceiling = metrics.get('buy_ceiling')
        tax_qty = (metrics.get('tax_quantity') or {}).get('additional_qty')
        turnover = candidate.get('avg_turnover_20d')
        if not ceiling or tax_qty is None or not turnover or price > ceiling:
            continue
        limit = bot.limit_buy_tick(min(price, ceiling))
        budget = min(sleeve*.03, sleeve*.04, sleeve*.20-sector_value,
                     sleeve*.90-value, deposit*.96, turnover*.01)
        qty = min(max(0, int(budget//limit)), int(tax_qty)) if limit > 0 else 0
        if qty <= 0:
            continue
        metadata = {'corp_code':corp,'industry':sector,'signal_as_of':prev,
                    'source_sha':snapshot['source_sha']}
        state['buy_permits'][code] = {'date':now.date().isoformat(),'qty':qty,'metadata':metadata}
        if not bot.DRY_RUN:
            bot.save_json(path(bot), state)
            bot.execute_order(token,code,qty,'02',ord_dvsn='00',price=limit,
                strategy=STRATEGY,action_name='DIV entry',strategy_metadata=metadata)
            bot.assert_no_pending(token)
            state = rebuild(bot)
            holdings, deposit, nav = bot.get_account_balance(token)
        else:
            print(f'>> [DIVIDEND DRY_RUN] eligible {code} qty={qty}; no order')
