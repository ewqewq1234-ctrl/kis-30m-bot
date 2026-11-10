"""Tuesday dividend/value Telegram report. Read-only KIS quotes + confirmed bot journal.

Does not import bot_once or call any account, balance, order or trade endpoint.
The report concerns confirmed fills in the bot-state journal, NOT an independent
real-time reconciliation with the brokerage account.
"""
import argparse
from datetime import date, datetime, timedelta
import html
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from .data import PublicData, PRICE, DataError

KST = ZoneInfo("Asia/Seoul")
OPEN_STATUSES = {"INTENT", "SUBMITTING", "ACCEPTED", "PARTIAL", "UNCERTAIN"}
MAX_MESSAGE_SIZE = 3500


class ReportError(RuntimeError):
    pass


def money(value):
    return f"{round(value):+,}원"


def krw(value):
    return f"{round(value):,}원"


def pct(gain, basis):
    return f"{gain / basis * 100:+.2f}%" if basis > 0 else "산정불가"


def qty_fill(order):
    value = order.get("confirmed_fill_qty", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ReportError("확정 체결수량 자료가 비정상입니다.")
    try:
        qty = float(value)
    except ValueError:
        raise ReportError("확정 체결수량 자료가 비정상입니다.") from None
    if not math.isfinite(qty) or qty < 0 or not qty.is_integer():
        raise ReportError("확정 체결수량 자료가 비정상입니다.")
    return int(qty)


def positive_amount(order):
    try:
        amount = float(order.get("confirmed_fill_amt", 0))
    except (ValueError, TypeError):
        raise ReportError("확정 체결금액 자료가 비정상입니다.") from None
    if not math.isfinite(amount) or amount <= 0:
        raise ReportError("확정 체결금액 자료가 누락되었습니다.")
    return amount


def order_date(order):
    try:
        return date.fromisoformat(str(order["created_at"])[:10])
    except (ValueError, TypeError, KeyError):
        raise ReportError("체결 원장 일시가 유효하지 않습니다.") from None


def summarize_journal(history, positions, report_day):
    """Aggregate *confirmed* fills only and reconcile derived holdings to bot state."""
    orders = history.get("orders")
    if not isinstance(orders, list) or not isinstance(positions, dict):
        raise ReportError("매매 원장/포지션 형식이 유효하지 않습니다.")
    inventory = {}
    trades = []
    unresolved = 0
    ids = set()
    seen_dates = []
    for order in sorted((o for o in orders if o.get("strategy") == "DIVIDEND"),
                        key=lambda o: (str(o.get("created_at", "")), str(o.get("id", "")))):
        key = order.get("id")
        if not key or key in ids:
            raise ReportError("배당 주문 원장 ID가 누락되거나 중복되었습니다.")
        ids.add(key)
        qty = qty_fill(order)
        created = order_date(order)
        seen_dates.append(created)
        if order.get("status") in OPEN_STATUSES:
            unresolved += 1
        if qty == 0:
            continue
        code = str(order.get("code", "")).strip()
        side = order.get("side")
        if not code or side not in {"BUY", "SELL"}:
            raise ReportError("원장 종목코드/주문 방향이 유효하지 않습니다.")
        amt = positive_amount(order)
        item = inventory.setdefault(code, {"qty": 0, "cost": 0.0})
        if side == "BUY":
            item["qty"] += qty
            item["cost"] += amt
            basis = amt
            profit = None
        else:
            if item["qty"] < qty or item["cost"] <= 0:
                raise ReportError("원장의 매도량이 확인된 매수 잔고보다 많습니다.")
            avg = item["cost"] / item["qty"]
            try:
                recorded_basis = float(order.get("sell_cost_basis") or 0)
            except (ValueError, TypeError):
                raise ReportError("매도 원가 자료가 비정상입니다.") from None
            if not math.isfinite(recorded_basis) or recorded_basis < 0:
                raise ReportError("매도 원가 자료가 비정상입니다.")
            # Journal sells carry the entry's per-share cost; validate against
            # the cost reconstructed from *confirmed* buys before using it.
            if recorded_basis and abs(recorded_basis - avg) > max(1.0, avg * .0005):
                raise ReportError("매도 원가와 확정 매수 원장이 일치하지 않습니다.")
            basis = avg * qty
            profit = amt - basis
            item["qty"] -= qty
            item["cost"] -= basis
            if item["qty"] == 0:
                item["cost"] = 0.0
        trades.append({"code": code, "side": side, "qty": qty, "amount": amt,
                       "basis": basis, "profit": profit, "date": created})

    journal_pos = {code: p for code, p in inventory.items() if p["qty"] > 0}
    state_qty = {str(code): int(p.get("qty", 0)) for code, p in positions.items()
                 if isinstance(p, dict) and p.get("qty")}
    if {code: p["qty"] for code, p in journal_pos.items()} != state_qty:
        raise ReportError("확정 체결원장과 저장된 배당 보유수량이 다릅니다.")
    for code, p in journal_pos.items():
        state_avg = float(positions[code].get("avg_price", 0))
        avg = p["cost"] / p["qty"]
        if not math.isfinite(state_avg) or state_avg <= 0 or abs(state_avg-avg) > max(1.0, avg * .0005):
            raise ReportError("확정 체결원장과 저장된 배당 평균단가가 다릅니다.")
    week_start = report_day - timedelta(days=report_day.weekday())
    week_trades = [t for t in trades if week_start <= t["date"] <= report_day]
    buys = [t for t in week_trades if t["side"] == "BUY"]
    sells = [t for t in week_trades if t["side"] == "SELL"]
    all_sales = [t for t in trades if t["side"] == "SELL"]
    return {"inventory": journal_pos, "week_buys": buys, "week_sells": sells,
            "all_sales": all_sales, "unresolved": unresolved,
            "all_buy_fills": sum(t["side"] == "BUY" for t in trades),
            "all_sell_fills": len(all_sales)}


def fetch_quotes(codes, *, api=None):
    """Only KIS /quotations/inquire-price; unknown quotes remain unknown."""
    if not codes:
        return {}
    if api is None:
        if not os.getenv("KIS_APP_KEY") or not os.getenv("KIS_APP_SECRET"):
            return {}
        api = PublicData(".weekly-quote-cache")
    quotes = {}
    for code in codes:
        try:
            output = api.kis(PRICE, {"FID_COND_MRKT_DIV_CODE": "J",
                                     "FID_INPUT_ISCD": code}).get("output", {})
            price = float(output.get("stck_prpr") or 0)
            if math.isfinite(price) and price > 0:
                quotes[code] = price
        except (DataError, ValueError, TypeError, KeyError):
            # Do not substitute outdated prices or zero.
            pass
    return quotes


def build_messages(metadata, screening_rows, history, position_state, quotes, *, now):
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(screening_rows, list):
        screening_rows = []
    report_date = now.date()
    if report_date.weekday() != 1:
        raise ReportError("화요일이 아닌 날에는 주간 보고서를 작성하지 않습니다.")
    positions = position_state.get("positions", {})
    if not isinstance(positions, dict):
        raise ReportError("보유상태 파일 형식이 유효하지 않습니다.")
    summary = summarize_journal(history, positions, report_date)
    inventory = summary["inventory"]
    names = {str(r.get("code")): str(r.get("name") or r.get("code"))
             for r in screening_rows if isinstance(r, dict)}
    counts = metadata.get("counts") or {}
    partial = (metadata.get("partial") is True or
               metadata.get("query_failed", 0) != 0 or
               metadata.get("blocker") is not None or
               not metadata)
    valid_scan = (str(metadata.get("executed_kst", ""))[:10] == report_date.isoformat())
    if not valid_scan:
        scan_info = "⚠️ 오늘 스크리닝 원본 미확인 (이전 결과 사용 금지)"
    else:
        tag = "⚠️ 부분 검증" if partial else "✅ 검증 완료"
        scan_info = (f"{tag} · 기준 거래일 {html.escape(str(metadata.get('as_of') or '미확인'))}\n"
                     f"전체 {metadata.get('universe_total', '?'):,} / "
                     f"PASS {counts.get('PASS', '?')} · REVIEW {counts.get('REVIEW', '?')} · "
                     f"FAIL {counts.get('FAIL', '?')} · 제외 {counts.get('EXCLUDED', '?')}\n"
                     f"조회 미검증 {metadata.get('query_failed', '?')}종목 · "
                     f"미처리 {metadata.get('unprocessed', '?')}종목")
    buy = summary["week_buys"]
    sell = summary["week_sells"]
    realized_week = sum(t["profit"] for t in sell)
    realized_all = sum(t["profit"] for t in summary["all_sales"])
    sale_basis_week = sum(t["basis"] for t in sell)
    sale_basis_all = sum(t["basis"] for t in summary["all_sales"])
    total_cost = sum(p["cost"] for p in inventory.values())
    total_value = sum(p["qty"] * quotes[code] for code, p in inventory.items() if code in quotes)
    priced_cost = sum(p["cost"] for code, p in inventory.items() if code in quotes)
    unavailable = [c for c in inventory if c not in quotes]
    valuation = ("시세 미확인 종목 없음" if not inventory else
                 f"{krw(total_value)} / 평가손익 {money(total_value-total_cost)} ({pct(total_value-total_cost,total_cost)})"
                 if not unavailable else
                 f"산정불가 (시세 미확인 {len(unavailable)}/{len(inventory)}종목; 확인분 평가금액 {krw(total_value)}, 확인분 손익 {money(total_value-priced_cost)})")
    header = (
        f"📊 <b>배당·가치주 화요일 정산 리포트</b>\n"
        f"🕓 {report_date.isoformat()} {now:%H:%M} KST\n\n"
        f"<b>① 화요일 스크리닝</b>\n{scan_info}\n\n"
        f"<b>② 이번 주 확인 체결 (월~화 집계 시점)</b>\n"
        f"매수 {len(buy)}건 · {sum(t['qty'] for t in buy)}주 · {krw(sum(t['amount'] for t in buy))}\n"
        f"매도 {len(sell)}건 · {sum(t['qty'] for t in sell)}주 · {krw(sum(t['amount'] for t in sell))}\n"
        f"이번 주 실현손익 {money(realized_week)} ({pct(realized_week,sale_basis_week)})\n"
        f"누적 실현손익 {money(realized_all)} ({pct(realized_all,sale_basis_all)})\n\n"
        f"<b>③ 배당 전략 보유·평가</b>\n"
        f"보유 {len(inventory)}종목 · {sum(p['qty'] for p in inventory.values())}주\n"
        f"체결 원가 {krw(total_cost)}\n"
        f"평가액/수익률 {valuation}\n"
        f"미확정 주문 {summary['unresolved']}건\n"
        f"※ 매매원장 기준이며 실계좌와 별도 실시간 대조하지 않았습니다.\n"
        f"※ 수수료·세금·수령 배당금 제외. 가격은 조회 시점의 KIS 최근 체결가입니다."
    )
    messages = [header]
    def detail_lines(kind, trades):
        lines = []
        for t in trades:
            name = html.escape(names.get(t["code"], t["code"]))
            extra = f" · 손익 {money(t['profit'])} ({pct(t['profit'],t['basis'])})" if kind == "매도" else ""
            lines.append(f"{name}({t['code']}) {t['qty']}주 · {krw(t['amount'])}{extra}")
        return lines
    if buy or sell:
        lines = ["<b>④ 금주 매매 체결 상세</b>", "<b>매수</b>"]
        lines.extend(detail_lines("매수", buy) or ["없음"])
        lines.append("<b>매도</b>")
        lines.extend(detail_lines("매도", sell) or ["없음"])
        messages.extend(chunk_lines(lines, 2900))
    if inventory:
        lines = ["<b>⑤ 현재 배당·가치 보유종목별 평가</b>"]
        for code, p in sorted(inventory.items()):
            name = html.escape(names.get(code, code))
            avg = p["cost"] / p["qty"]
            if code in quotes:
                gain = quotes[code] * p["qty"] - p["cost"]
                mark = f"현재 {krw(quotes[code])} · {money(gain)} ({pct(gain,p['cost'])})"
            else:
                mark = "시세 미확인 / 수익률 계산 제외"
            lines.append(f"{name}({code}) {p['qty']}주 · 평단 {krw(avg)} · {mark}")
        messages.extend(chunk_lines(lines, 2900))
    return messages


def chunk_lines(lines, max_chars):
    chunks, current = [], ""
    for line in lines:
        if len(line) > max_chars:
            raise ReportError("리포트 출력 행이 지나치게 깁니다.")
        if len(current) + len(line) + 1 > max_chars:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return chunks


def send_messages(messages, *, session=None):
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ReportError("텔레그램 설정(Secrets)이 누락됐습니다.")
    session = session or requests.Session()
    for message in messages:
        if len(message) > MAX_MESSAGE_SIZE:
            raise ReportError("텔레그램 메시지 길이가 제한을 초과했습니다.")
        try:
            response = session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=(10, 20))
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise ReportError("텔레그램 서버가 보고서 수신을 승인하지 않았습니다.")
        except (requests.RequestException, ValueError):
            # Never interpolate the exception: URL contains the private bot token.
            raise ReportError("텔레그램 네트워크 연결 또는 응답 해석 실패(전송 결과 불확실)") from None
    return len(messages)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("render", "send"))
    parser.add_argument("--scan-dir", type=Path, default=Path("results/dividend-value"))
    parser.add_argument("--state-dir", type=Path, default=Path("report-input"))
    parser.add_argument("--file", type=Path, default=Path("weekly-telegram-messages.json"))
    args = parser.parse_args(argv)
    if args.action == "send":
        messages = json.loads(args.file.read_text(encoding="utf-8"))
        if not isinstance(messages, list) or not all(isinstance(m, str) for m in messages):
            raise ReportError("전송할 텔레그램 보고서 형식이 유효하지 않습니다.")
        print(f"Telegram sent messages: {send_messages(messages)}")
        return
    now = datetime.now(KST)
    meta = json.loads((args.scan_dir / "run_metadata.json").read_text(encoding="utf-8"))
    rows = json.loads((args.scan_dir / "screening_details.json").read_text(encoding="utf-8"))
    history = json.loads((args.state_dir / "trade_history.json").read_text(encoding="utf-8"))
    state = json.loads((args.state_dir / "dividend_state.json").read_text(encoding="utf-8"))
    holdings = state.get("positions", {})
    quotes = fetch_quotes(sorted(holdings))
    msgs = build_messages(meta, rows, history, state, quotes, now=now)
    args.file.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
    print(f"Tuesday report prepared: messages={len(msgs)} holdings={len(holdings)} prices={len(quotes)} orders_sent=0")


if __name__ == "__main__":
    main()
