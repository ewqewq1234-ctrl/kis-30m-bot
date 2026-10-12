# bot_once.py
import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta
import pytz

# ==============================================================================
# 1. 환경 변수 및 글로벌 설정
# ==============================================================================
KST = pytz.timezone("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_FILE = os.path.join(BASE_DIR, "token_cache.json")
DAY_STATE_FILE = os.path.join(BASE_DIR, "day_state.json")
BAA_STATE_FILE = os.path.join(BASE_DIR, "baa_state.json")
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")

KIS_URL = os.getenv("KIS_URL", "https://openapi.koreainvestment.com:9443").rstrip("/")
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_CANO = (os.getenv("KIS_CANO") or "").strip()
KIS_ACNT_PRDT_CD = (os.getenv("KIS_ACNT_PRDT_CD") or "01").strip().zfill(2)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CODE_BULL = "069500"  # KODEX 200 (데이트레이딩 단방향 롱)

# 데이트레이딩 매매 파라미터
PROFIT_TARGET = 0.012
STOP_LOSS = -0.009
PYRAMID_TRIGGER = 0.005
TIME_STOP_MINUTES = 90

DAY_BUDGET_RATIO = 0.30
WEIGHT_BASE = 0.50
WEIGHT_PYRAMID = 0.50

# 공격형 BAA 유니버스
CANARY_UNIVERSE = {"360750": "TIGER 미국S&P500", "069500": "KODEX 200", "371460": "TIGER 차이나전기차SOLACTIVE", "453850": "ACE 미국종합채권액티브"}
ATTACK_UNIVERSE = {"133690": "TIGER 미국나스닥100", "069500": "KODEX 200", "122630": "KODEX 레버리지", "381180": "TIGER 미국필라델피아반도체나스닥"}
DEFENSE_UNIVERSE = {"423160": "KODEX KOFR금리액티브(합성)", "449170": "KODEX 미국달러SOFR금리액티브(합성)", "453850": "ACE 미국종합채권액티브", "411060": "ACE KRX금현물"}

# ==============================================================================
# 2. 유틸리티 (텔레그램, 파일 입출력, 휴장일 판별)
# ==============================================================================
def send_telegram(message: str, is_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    prefix = "🚨 [비상 경고]\n" if is_alert else ""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"{prefix}{message}", "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f">> Telegram 통신 에러: {e}")

def load_json(filepath: str, default: dict):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return default

def save_json(filepath: str, data: dict):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_access_token():
    now_ts = int(time.time())
    cached = load_json(CACHE_FILE, {})
    if cached.get("expires_at", 0) > (now_ts + 600) and "token" in cached:
        return cached["token"]

    url = f"{KIS_URL}/oauth2/tokenP"
    res = requests.post(url, json={"grant_type": "client_credentials", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET}, timeout=10)
    data = res.json()
    if "access_token" in data:
        token = data["access_token"]
        save_json(CACHE_FILE, {"token": token, "expires_at": now_ts + int(data.get("expires_in", 86400))})
        return token
    raise RuntimeError(f"토큰 발급 실패: {data}")

def is_market_open_today(token: str) -> bool:
    today_str = datetime.now(KST).strftime("%Y%m%d")
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "CTCA0903R"}
    try:
        res = requests.get(url, headers=headers, params={"BASS_DT": today_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}, timeout=5).json()
        if res.get("rt_cd") == "0":
            for item in res.get("output", []):
                if item.get("bass_dt") == today_str:
                    return item.get("opnd_yn") == "Y"
    except: pass
    return datetime.now(KST).weekday() < 5

def get_next_business_day(token: str, start_date: datetime) -> datetime:
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "CTCA0903R"}
    for i in range(1, 10):
        check_dt = start_date + timedelta(days=i)
        params = {"BASS_DT": check_dt.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5).json()
            if res.get("rt_cd") == "0":
                for item in res.get("output", []):
                    if item.get("bass_dt") == check_dt.strftime("%Y%m%d") and item.get("opnd_yn") == "Y":
                        return check_dt
        except: pass
    return start_date + timedelta(days=1)

# ==============================================================================
# 3. KIS API 통신 및 주문
# ==============================================================================
def get_current_price(token: str, code: str):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "FHKST01010100"}
    res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=10).json()
    if res.get("rt_cd") == "0":
        return int(res["output"]["stck_prpr"])
    return 0

def get_account_balance(token: str):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "TTTC8434R"}
    params = {"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    res = requests.get(url, headers=headers, params=params, timeout=10).json()
    
    holdings = {}
    for item in res.get("output1", []):
        qty = int(item.get("hldg_qty", 0))
        if qty > 0:
            holdings[item.get("pdno")] = {"name": item.get("prdt_name"), "qty": qty, "avg_price": float(item.get("pchs_avg_pric", 0.0))}
    
    deposit = int(res.get("output2", [{}])[0].get("dnca_tot_amt", 0))
    tot_eval = int(res.get("output2", [{}])[0].get("tot_evlu_amt", 0))
    return holdings, deposit, tot_eval

def execute_order(token: str, code: str, qty: int, order_type="01", ord_dvsn="01", price=0, action_name="주문"):
    if qty <= 0: return False, 0
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "TTTC0802U" if order_type == "01" else "TTTC0801U"}
    body = {"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD, "PDNO": code, "ORD_DVSN": ord_dvsn, "ORD_QTY": str(qty), "ORD_UNPR": "0" if ord_dvsn == "01" else str(int(price))}
    
    res = requests.post(url, headers=headers, json=body, timeout=10).json()
    if res.get("rt_cd") == "0":
        return True, qty
    send_telegram(f"[{action_name} 실패] {code}: {res.get('msg1')}", True)
    return False, 0

# ==============================================================================
# 4. 거래 기록 및 정밀 보고서 엔진 (일/주/월) & 재투자
# ==============================================================================
def record_trade(profit_loss: int, action: str):
    history = load_json(TRADE_HISTORY_FILE, {"trades": []})
    history["trades"].append({
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "profit_loss": profit_loss
    })
    save_json(TRADE_HISTORY_FILE, history)

    # 코스피 단타 수익 발생 시 BAA 재투자 풀로 즉시 스윕
    if profit_loss > 0:
        baa_state = load_json(BAA_STATE_FILE, {"reinvest_pool": 0})
        baa_state["reinvest_pool"] = baa_state.get("reinvest_pool", 0) + profit_loss
        save_json(BAA_STATE_FILE, baa_state)

def generate_quant_report(trades: list, title: str):
    if not trades:
        return f"📊 <b>{title}</b>\n━━━━━━━━━━━━━━━━\n해당 기간 청산 거래가 없습니다."

    wins = [t for t in trades if t["profit_loss"] > 0]
    losses = [t for t in trades if t["profit_loss"] < 0]
    
    win_rate = (len(wins) / len(trades)) * 100
    gross_profit = sum(t["profit_loss"] for t in wins)
    gross_loss = abs(sum(t["profit_loss"] for t in losses))
    net_profit = gross_profit - gross_loss
    
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.99 if gross_profit > 0 else 0)
    avg_win = (gross_profit / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0

    peak, mdd, cum_pnl = 0, 0, 0
    for t in trades:
        cum_pnl += t["profit_loss"]
        if cum_pnl > peak: peak = cum_pnl
        if (peak - cum_pnl) > mdd: mdd = peak - cum_pnl

    return (
        f"📊 <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"▪ <b>총 거래:</b> {len(trades)}회 ({len(wins)}승 {len(losses)}패)\n"
        f"▪ <b>승률:</b> {win_rate:.1f}%\n"
        f"▪ <b>순손익:</b> {net_profit:+,}원\n"
        f"▪ <b>총 이익:</b> +{gross_profit:,}원\n"
        f"▪ <b>총 손실:</b> -{gross_loss:,}원\n"
        f"▪ <b>Profit Factor:</b> {profit_factor:.2f}\n"
        f"▪ <b>손익비(Payoff):</b> {payoff_ratio:.2f}\n"
        f"▪ <b>MDD(최대낙폭):</b> -{mdd:,}원\n"
        f"━━━━━━━━━━━━━━━━"
    )

def handle_reporting(token: str):
    now = datetime.now(KST)
    if now.strftime("%H:%M") < "16:05": return

    history = load_json(TRADE_HISTORY_FILE, {"trades": [], "daily_sent": "", "weekly_sent": "", "monthly_sent": ""})
    today_str = now.strftime("%Y-%m-%d")
    
    # 일일 결산 보고 (16:05 이후 1회)
    if history.get("daily_sent") != today_str:
        today_trades = [t for t in history["trades"] if t["timestamp"].startswith(today_str)]
        tot_pnl = sum(t["profit_loss"] for t in today_trades)
        wins = len([t for t in today_trades if t["profit_loss"] > 0])
        win_rate = (wins / len(today_trades) * 100) if today_trades else 0
        pool = load_json(BAA_STATE_FILE, {}).get("reinvest_pool", 0)
        
        msg = (f"📅 <b>[당일 트레이딩 마감 요약]</b>\n"
               f"▪ 거래: {len(today_trades)}회 ({wins}승)\n"
               f"▪ 승률: {win_rate:.1f}%\n"
               f"▪ 당일 실현손익: {tot_pnl:+,}원\n"
               f"▪ BAA 누적 재투자풀: {pool:,}원 스윕 완료")
        send_telegram(msg)
        history["daily_sent"] = today_str

    next_biz = get_next_business_day(token, now)
    
    # 주간 결산 (오늘이 이번주 마지막 영업일인 경우)
    current_week = f"{now.year}-W{now.isocalendar()[1]}"
    if next_biz.isocalendar()[1] != now.isocalendar()[1] and history.get("weekly_sent") != current_week:
        start_of_week = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        week_trades = [t for t in history["trades"] if t["timestamp"] >= start_of_week]
        send_telegram(generate_quant_report(week_trades, f"{current_week} 주간 퀀트 성과 리포트"))
        history["weekly_sent"] = current_week

    # 월간 결산 (오늘이 이번달 마지막 영업일인 경우)
    current_month = now.strftime("%Y-%m")
    if next_biz.month != now.month and history.get("monthly_sent") != current_month:
        month_trades = [t for t in history["trades"] if t["timestamp"].startswith(current_month)]
        send_telegram(generate_quant_report(month_trades, f"{current_month} 월간 퀀트 성과 리포트"))
        history["monthly_sent"] = current_month

    save_json(TRADE_HISTORY_FILE, history)

# ==============================================================================
# 5. 데이트레이딩 (KODEX 200 단방향)
# ==============================================================================
def check_daytrade_signal(token: str) -> bool:
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "FHKST03010200"}
    res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE_BULL, "FID_INPUT_HOUR_1": datetime.now(KST).strftime("%H%M%S"), "FID_PW_DATA_INCU_YN": "Y", "FID_PW_DATA_CNT": "100"}, timeout=10).json()
    candles = res.get("output2", [])
    if len(candles) < 30: return False

    completed = list(reversed(candles))[:-1]
    closes = [float(b["stck_prpr"]) for b in completed]
    volumes = [float(b["cntg_vol"]) for b in completed]
    
    ema20 = sum(closes[-20:]) / 20
    ema60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else sum(closes) / len(closes)
    vol_ma20 = sum(volumes[-20:]) / 20
    
    last_close = float(completed[-1]["stck_prpr"])
    last_vol = float(completed[-1]["cntg_vol"])
    curr_price = float(list(reversed(candles))[-1]["stck_prpr"])

    return (last_vol >= vol_ma20 * 1.2) and (ema20 > ema60) and (curr_price > last_close)

def run_intraday(token: str, holdings: dict, deposit: int, total_asset: int):
    now = datetime.now(KST)
    time_hm = now.strftime("%H:%M")
    holding = holdings.get(CODE_BULL, {"qty": 0, "avg_price": 0.0})
    qty = holding["qty"]
    curr_price = get_current_price(token, CODE_BULL)
    
    state = load_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
    intraday_budget = total_asset * DAY_BUDGET_RATIO

    # 1. 09:00 전일 오버나잇 청산
    if "09:00" <= time_hm < "09:14" and qty > 0:
        if execute_order(token, CODE_BULL, qty, "01")[0]:
            diff = int((curr_price - holding["avg_price"]) * qty)
            send_telegram(f"🚨 <b>[개장 청산]</b> KODEX 200 전량 매도 | 손익: {diff:+,}원")
            record_trade(diff, "오버나잇 청산")
            save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        return

    # 2. 15:00 정규장 마감 청산
    if "15:00" <= time_hm <= "15:20" and qty > 0:
        if execute_order(token, CODE_BULL, qty, "01")[0]:
            diff = int((curr_price - holding["avg_price"]) * qty)
            send_telegram(f"🧹 <b>[정규장 마감 청산]</b> 전량 매도 | 손익: {diff:+,}원")
            record_trade(diff, "정규장 마감청산")
            save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        return

    # 3. 15:40 시간외 종가 청산
    if "15:40" <= time_hm <= "16:00" and qty > 0:
        if execute_order(token, CODE_BULL, qty, "01", ord_dvsn="05", price=curr_price)[0]:
            diff = int((curr_price - holding["avg_price"]) * qty)
            record_trade(diff, "장후시간외 청산")
            save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        return

    # 4. 실시간 포지션 감시
    if qty > 0:
        avg_price = holding["avg_price"]
        profit_ratio = (curr_price - avg_price) / avg_price
        diff = int((curr_price - avg_price) * qty)
        e_time = state.get("entry_time")
        elapsed = (now - datetime.strptime(e_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)).total_seconds()/60 if e_time else 0

        if profit_ratio >= PROFIT_TARGET:
            if execute_order(token, CODE_BULL, qty, "01")[0]:
                send_telegram(f"🎯 <b>[KODEX 200 익절]</b> +{profit_ratio*100:.2f}% | 수익 <b>{diff:+,}원</b>")
                record_trade(diff, "목표익절")
                save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        
        elif profit_ratio <= STOP_LOSS:
            if execute_order(token, CODE_BULL, qty, "01")[0]:
                send_telegram(f"🛑 <b>[KODEX 200 칼손절]</b> {profit_ratio*100:.2f}% | 손실 <b>{diff:+,}원</b>")
                record_trade(diff, "칼손절")
                save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})

        elif profit_ratio >= PYRAMID_TRIGGER and not state.get("pyramided", False):
            pyr_qty = int(min(deposit, intraday_budget * WEIGHT_PYRAMID) // curr_price)
            if pyr_qty > 0 and execute_order(token, CODE_BULL, pyr_qty, "02")[0]:
                state["pyramided"] = True
                save_json(DAY_STATE_FILE, state)
                send_telegram(f"🔥 <b>[KODEX 200 불타기]</b> {pyr_qty}주 추가 진입 (현재 +{profit_ratio*100:.2f}%)")

        elif elapsed >= TIME_STOP_MINUTES:
            if execute_order(token, CODE_BULL, qty, "01")[0]:
                send_telegram(f"⏱️ <b>[90분 타임스탑]</b> 횡보 청산 | 손익 <b>{diff:+,}원</b>")
                record_trade(diff, "타임스탑")
                save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})

    # 5. 신규 진입 (09:15 ~ 14:30)
    elif "09:15" <= time_hm <= "14:30" and check_daytrade_signal(token):
        base_qty = int(min(deposit, intraday_budget * WEIGHT_BASE) // curr_price)
        if base_qty > 0 and execute_order(token, CODE_BULL, base_qty, "02")[0]:
            save_json(DAY_STATE_FILE, {"entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "pyramided": False})
            send_telegram(f"🚀 <b>[KODEX 200 추세 1차 진입]</b> {base_qty}주 매수 완료")

# ==============================================================================
# 6. 공격형 BAA 전략 (수익 재투자 스윕 & 휴일 연기)
# ==============================================================================
def get_daily_closes(token: str, code: str) -> list:
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "FHKST01010400"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": (datetime.now(KST)-timedelta(days=400)).strftime("%Y%m%d"), "FID_INPUT_DATE_2": datetime.now(KST).strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
    time.sleep(0.1)
    res = requests.get(url, headers=headers, params=params, timeout=10).json()
    return [float(c["stck_clpr"]) for c in reversed(res.get("output", [])) if c.get("stck_clpr")]

def calc_fast_baa(closes: list):
    if len(closes) < 252: return -999
    curr = closes[-1]
    return 16*((curr-closes[-21])/closes[-21]) + 6*((curr-closes[-63])/closes[-63]) + 2*((curr-closes[-126])/closes[-126]) + 1*((curr-closes[-252])/closes[-252])

def run_baa(token: str, holdings: dict, total_asset: int):
    now = datetime.now(KST)
    # 화요일 오전 11:00 전후 실행 (workflow_dispatch 강제 실행 시 패스)
    if not ("11:00" <= now.strftime("%H:%M") <= "11:25") and os.getenv("FORCE_BAA") != "Y": return
    
    state = load_json(BAA_STATE_FILE, {"last_executed_date": None, "reinvest_pool": 0})
    if state.get("last_executed_date"):
        if (now.date() - datetime.strptime(state["last_executed_date"], "%Y-%m-%d").date()).days < 13: return

    base_budget = total_asset * 0.70
    # 단타 이익 풀 전액 스윕 합산
    total_budget = base_budget + state.get("reinvest_pool", 0)

    is_defense = any(calc_fast_baa(get_daily_closes(token, code)) <= 0 for code in CANARY_UNIVERSE)
    
    target_code, target_name = None, None
    if not is_defense:
        scores = {}
        for code, name in ATTACK_UNIVERSE.items():
            closes = get_daily_closes(token, code)
            curr = closes[-1]
            if curr >= sum(closes[-60:])/60 and curr >= sum(closes[-200:])/200:
                scores[code] = calc_fast_baa(closes)
        if scores:
            target_code = max(scores, key=scores.get)
            target_name = ATTACK_UNIVERSE[target_code]
        else:
            is_defense = True
            
    if is_defense:
        scores = {c: calc_fast_baa(get_daily_closes(token, c)) for c in DEFENSE_UNIVERSE}
        target_code = max(scores, key=scores.get)
        target_name = DEFENSE_UNIVERSE[target_code]

    target_closes = get_daily_closes(token, target_code)
    is_bull = (sum(target_closes[-20:])/20) > (sum(target_closes[-60:])/60)
    
    # 타깃이 아닌 BAA 보유종목 전량 교체 매도 (KODEX 200 단타 수량 제외)
    for h_code, item in holdings.items():
        if h_code not in (target_code, CODE_BULL):
            execute_order(token, h_code, item["qty"], "01", action_name="BAA 교체매도")

    target_budget = total_budget if is_bull else (total_budget * 0.50)
    qty_diff = int(target_budget // target_closes[-1]) - holdings.get(target_code, {}).get("qty", 0)
    
    if qty_diff > 0:
        if execute_order(token, target_code, qty_diff, "02", action_name="BAA 매수")[0]:
            msg = (f"🛡️ <b>[BAA 자산배분 리밸런싱 완료]</b>\n"
                   f"▪ 편입 자산: {target_name} ({qty_diff}주)\n"
                   f"▪ 현재 모드: {'수비 모드 📉' if is_defense else '공격 모드 📈'}\n"
                   f"▪ 단타 수익 스윕(재투자): {state.get('reinvest_pool', 0):,}원 투입됨")
            send_telegram(msg)
            # 재투자 성공 시 풀 초기화
            state["reinvest_pool"] = 0

    state["last_executed_date"] = now.strftime("%Y-%m-%d")
    save_json(BAA_STATE_FILE, state)

# ==============================================================================
# 7. 메인 오케스트레이션
# ==============================================================================
def main():
    token = get_access_token()
    if not is_market_open_today(token): return
    
    holdings, deposit, tot_eval = get_account_balance(token)
    total_asset = max(deposit, tot_eval)

    run_intraday(token, holdings, deposit, total_asset)
    run_baa(token, holdings, total_asset)
    handle_reporting(token)

if __name__ == "__main__":
    main()
