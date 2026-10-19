#!/usr/bin/env python3
"""Read-only KIS market-data verification for the BAA Canary change."""
import math
import re
import sys
import time
from datetime import datetime

import bot_once as bot


AS_OF = bot.KST.localize(datetime(2026, 10, 19, 23, 59))
OLD_CANARY = ("360750", "069500", "371460", "453850")
OLD_CANARY_UNIVERSE = {
    "360750": "TIGER 미국S&P500",
    "069500": "KODEX 200",
    "371460": "TIGER 차이나전기차SOLACTIVE",
    "453850": "ACE 미국종합채권액티브",
}
NEW_CANARY = tuple(bot.CANARY_UNIVERSE)
QUOTE_CODES = ("251350", "195980", "437080", "449170", "453850")
API_REQUEST = bot.api_request
HISTORY_AUDIT = {}
EXPECTED_DEFENSE_NAMES = {
    "449170": bot.DEFENSE_UNIVERSE["449170"],
    "453850": bot.DEFENSE_UNIVERSE["453850"],
}


def normalized_name(value):
    return "".join(re.findall(r"[0-9A-Za-z가-힣]", value))


def read_only_request(method, path, token=None, **kwargs):
    if method == "POST" and path == "/oauth2/tokenP":
        return API_REQUEST(method, path, token, **kwargs)
    if method != "GET" or path not in {
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
    }:
        raise bot.TradeSafetyError(f"Read-only verifier blocked unexpected request: {method} {path}")
    response = API_REQUEST(method, path, token, **kwargs)
    if path.endswith("/inquire-daily-itemchartprice"):
        params = kwargs.get("params") or {}
        code = params.get("FID_INPUT_ISCD")
        result = response[0]
        rows = result.get("output2")
        audit = HISTORY_AUDIT.setdefault(code, [])
        audit.append({
            "start": params.get("FID_INPUT_DATE_1"),
            "end": params.get("FID_INPUT_DATE_2"),
            "rows": rows if isinstance(rows, list) else None,
        })
    return response


def verify_quote(token, code):
    result, _ = read_only_request(
        "GET",
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        token,
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )
    output = result.get("output")
    if not isinstance(output, dict):
        raise bot.TradeSafetyError(f"{code} KIS quote output missing")
    response_code = str(output.get("stck_shrn_iscd") or "").strip()
    name = str(output.get("hts_kor_isnm") or "").strip()
    try:
        price = float(str(output.get("stck_prpr", "")).replace(",", ""))
    except (ValueError, TypeError):
        raise bot.TradeSafetyError(f"{code} KIS current price malformed")
    if response_code != code or not name or not math.isfinite(price) or price <= 0:
        raise bot.TradeSafetyError(f"{code} KIS quote identity/price verification failed")
    expected_name = EXPECTED_DEFENSE_NAMES.get(code)
    if expected_name and normalized_name(name) != normalized_name(expected_name):
        raise bot.TradeSafetyError(
            f"{code} KIS product name mismatch: expected {expected_name}; received {name}"
        )
    print(f"QUOTE {code} | {name} | {price:.2f} KRW")


def momentum_metrics(closes):
    if len(closes) != 252:
        raise bot.TradeSafetyError(f"expected 252 closes, received {len(closes)}")
    returns = {
        "1m": closes[-1] / closes[-21] - 1,
        "3m": closes[-1] / closes[-63] - 1,
        "6m": closes[-1] / closes[-126] - 1,
        "12m": closes[-1] / closes[-252] - 1,
    }
    score = bot.calc_fast_baa(closes)
    return returns, score


def report_canary_metrics(label, codes, histories):
    scores = {}
    print(f"UNIVERSE {label} | cutoff=2026-10-19 KST")
    for code in codes:
        closes = histories[code]
        returns, score = momentum_metrics(closes)
        scores[code] = score
        print(
            f"  {code} | closes={len(closes)} | "
            f"1m={returns['1m']:.6%} 3m={returns['3m']:.6%} "
            f"6m={returns['6m']:.6%} 12m={returns['12m']:.6%} "
            f"weighted={score:.8f}"
        )
    positive = all(scores[code] > 0 for code in codes)
    print(f"  canary={'POSITIVE' if positive else 'NONPOSITIVE'}")
    return positive


def main():
    if bot.KIS_ENV != "real" or not bot.KIS_APP_KEY or not bot.KIS_APP_SECRET:
        raise bot.TradeSafetyError("KIS real-market credentials are unavailable")
    if not bot.KIS_URL.startswith("https://openapi.koreainvestment.com:"):
        raise bot.TradeSafetyError("KIS_URL is not the official real-market endpoint")

    bot.api_request = read_only_request
    token = bot.get_access_token()
    for code in QUOTE_CODES:
        verify_quote(token, code)
        time.sleep(0.2)

    required_codes = tuple(dict.fromkeys((
        *OLD_CANARY, *NEW_CANARY, *bot.ATTACK_UNIVERSE, *bot.DEFENSE_UNIVERSE
    )))
    histories = {}
    original_now_kst = bot.now_kst
    bot.now_kst = lambda: AS_OF
    try:
        for code in required_codes:
            histories[code] = bot.get_daily_closes(token, code, 252)
            if len(histories[code]) != 252:
                raise bot.TradeSafetyError(f"{code} did not return exactly 252 closes")
            pages = HISTORY_AUDIT.get(code, [])
            records = []
            for page in pages:
                if not isinstance(page["rows"], list) or len(page["rows"]) > 100:
                    raise bot.TradeSafetyError(f"{code} KIS daily page malformed or over 100 rows")
                records.extend(
                    (str(row["stck_bsop_date"]), float(row["stck_clpr"]))
                    for row in page["rows"]
                )
            dates = [day for day, _ in records]
            if len(dates) != len(set(dates)):
                raise bot.TradeSafetyError(f"{code} duplicate KIS historical dates")
            if not records:
                raise bot.TradeSafetyError(f"{code} KIS history returned no dates")
            for newer, older in zip(pages, pages[1:]):
                newer_start = datetime.strptime(newer["start"], "%Y%m%d").date()
                older_end = datetime.strptime(older["end"], "%Y%m%d").date()
                if older_end + bot.timedelta(days=1) != newer_start:
                    raise bot.TradeSafetyError(f"{code} KIS history pages overlap or have a gap")
            ordered_closes = [
                close for _, close in sorted(records, key=lambda item: item[0])[-252:]
            ]
            if ordered_closes != histories[code]:
                raise bot.TradeSafetyError(f"{code} KIS closes are not chronological")
            date_range = f"{min(dates)}..{max(dates)}"
            print(
                f"HISTORY {code} | pages={len(pages)} | rows={len(records)} | "
                f"raw_unique_dates={len(set(dates))} | returned_closes=252 | "
                f"cutoff=20261019 | dates={date_range}"
            )
            time.sleep(0.2)
    finally:
        bot.now_kst = original_now_kst

    report_canary_metrics("OLD", OLD_CANARY, histories)
    report_canary_metrics("NEW", NEW_CANARY, histories)
    original_universe = bot.CANARY_UNIVERSE
    original_get_daily_closes = bot.get_daily_closes
    bot.get_daily_closes = lambda _token, code, target_count=252: histories[code]
    try:
        bot.CANARY_UNIVERSE = OLD_CANARY_UNIVERSE
        old = bot.select_baa_target("verified-kis-history")
        bot.CANARY_UNIVERSE = dict(original_universe)
        new = bot.select_baa_target("verified-kis-history")
    finally:
        bot.CANARY_UNIVERSE = original_universe
        bot.get_daily_closes = original_get_daily_closes
    print(f"FINAL OLD MODE: {'DEFENSIVE' if old[2] else 'AGGRESSIVE'} target={old[0]}")
    print(f"FINAL NEW MODE: {'DEFENSIVE' if new[2] else 'AGGRESSIVE'} target={new[0]}")
    print(f"COMPARISON old={('DEFENSIVE' if old[2] else 'AGGRESSIVE')} "
          f"new={('DEFENSIVE' if new[2] else 'AGGRESSIVE')}")
    print("PRICE BASIS: KIS unadjusted close (FID_ORG_ADJ_PRC=0); "
          "review corporate-action/distribution effects separately.")


if __name__ == "__main__":
    try:
        main()
    except bot.TradeSafetyError as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
