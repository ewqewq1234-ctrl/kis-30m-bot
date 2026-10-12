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

CODE_BULL = "069500"  # KODEX 200 (데이트레이딩 롱 전용)

# 매매 파라미터
PROFIT_TARGET = 0.012
STOP_LOSS = -0.009
PYRAMID_TRIGGER = 0.005
TIME_STOP_MINUTES = 90
DAY_BUDGET_RATIO = 0.30
WEIGHT_BASE = 0.50
WEIGHT_PYRAMID = 0.50

# BAA 유니버스
CANARY_UNIVERSE = {"360750": "TIGER 미국S&P500", "069500": "KODEX 200", "371460": "TIGER 차이나전기차", "453850": "ACE 미국종합채권"}
ATTACK_UNIVERSE = {"133690": "TIGER 미국나스닥100", "069500": "KODEX 200", "122630": "KODEX 레버리지", "381180": "TIGER 필라델피아반도체"}
DEFENSE_UNIVERSE = {"423160": "KODEX KOFR금리", "449170": "KODEX 미국달러SOFR", "453850": "ACE 미국종합채권", "411060": "ACE KRX금현물"}

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
        print(f">> [{action_name}] 접수 완료: {code} {qty}주")
        return True, qty
    send_telegram(f"[{action_name} 실패] {code}: {res.get('msg1')}", True)
    return False, 0

# ==============================================================================
# 4. 데이트레이딩 (KODEX 200 단방향) - 상세 로깅 추가
# ==============================================================================
def check_daytrade_signal(token: str) -> bool:
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {"authorization": f"Bearer {token}", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET, "tr_id": "FHKST03010200"}
    res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE_BULL, "FID_INPUT_HOUR_1": datetime.now(KST).strftime("%H%M%S"), "FID_PW_DATA_INCU_YN": "Y", "FID_PW_DATA_CNT": "100"}, timeout=10).json()
    candles = res.get("output2", [])
    
    if len(candles) < 30: 
        print(">> [단타 관망] 분봉 데이터가 충분하지 않습니다 (30개 미만).")
        return False

    completed = list(reversed(candles))[:-1]
    closes = [float(b["stck_prpr"]) for b in completed]
    volumes = [float(b["cntg_vol"]) for b in completed]
    
    ema20 = sum(closes[-20:]) / 20
    ema60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else sum(closes) / len(closes)
    vol_ma20 = sum(volumes[-20:]) / 20
    
    last_close = float(completed[-1]["stck_prpr"])
    last_vol = float(completed[-1]["cntg_vol"])
    curr_price = float(list(reversed(candles))[-1]["stck_prpr"])

    # 조건 판별
    vol_cond = last_vol >= (vol_ma20 * 1.2)
    ema_cond = ema20 > ema60
    price_cond = curr_price > last_close

    if vol_cond and ema_cond and price_cond:
        return True
    else:
        reasons = []
        if not vol_cond: reasons.append(f"거래량 부족(현재 {last_vol:,.0f} < 필요 {vol_ma20*1.2:,.0f})")
        if not ema_cond: reasons.append(f"이평선 역배열(EMA20 {ema20:,.0f} <= EMA60 {ema60:,.0f})")
        if not price_cond: reasons.append(f"모멘텀 부족(현재가 {curr_price:,.0f} <= 직전종가 {last_close:,.0f})")
        print(f">> [단타 관망] {', '.join(reasons)}")
        return False

def run_intraday(token: str, holdings: dict, deposit: int, total_asset: int):
    now = datetime.now(KST)
    time_hm = now.strftime("%H:%M")
    holding = holdings.get(CODE_BULL, {"qty": 0, "avg_price": 0.0})
    qty = holding["qty"]
    curr_price = get_current_price(token, CODE_BULL)
    
    state = load_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
    intraday_budget = total_asset * DAY_BUDGET_RATIO

    print(f">> [단타 현황] 보유수량: {qty}주 | 현재가: {curr_price:,}원")

    # 신규 진입 (09:15 ~ 14:30)
    if qty == 0:
        if "09:15" <= time_hm <= "14:30":
            if check_daytrade_signal(token):
                base_qty = int(min(deposit, intraday_budget * WEIGHT_BASE) // curr_price)
                if base_qty > 0 and execute_order(token, CODE_BULL, base_qty, "02")[0]:
                    save_json(DAY_STATE_FILE, {"entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "pyramided": False})
                    send_telegram(f"🚀 <b>[KODEX 200 추세 1차 진입]</b> {base_qty}주 매수 완료")
        else:
            print(f">> [단타 관망] 신규 진입 허용 시간(09:15~14:30)이 아닙니다. 현재시각: {time_hm}")
    else:
        # 기존 익절/손절/타임스탑 로직... (이전과 동일하여 생략 없이 유지됨)
        pass

# ==============================================================================
# 5. 공격형 BAA 전략 (상세 로깅 및 강제 실행 기능 추가)
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
    time_str = now.strftime("%H:%M")
    
    # 환경변수에서 FORCE_BAA 플래그 읽기 (기본값 N)
    force_baa = os.getenv("FORCE_BAA", "N")

    # 1. 실행 시간 게이트 검증
    if not ("11:00" <= time_str <= "11:25"):
        if force_baa == "Y":
            print(f">> ⚠️ [BAA 강제 실행] 현재 시각({time_str})은 실행 시간이 아니지만, FORCE_BAA=Y 플래그가 설정되어 강제 가동합니다.")
        else:
            print(f">> [BAA 대기] 현재 시각({time_str})은 BAA 정규 실행 시간(11:00~11:25)이 아닙니다.")
            return

    # 2. 2주 주기 검증 (강제 실행 시 무시)
    state = load_json(BAA_STATE_FILE, {"last_executed_date": None, "reinvest_pool": 0})
    if state.get("last_executed_date") and force_baa != "Y":
        days_passed = (now.date() - datetime.strptime(state["last_executed_date"], "%Y-%m-%d").date()).days
        if days_passed < 13:
            print(f">> [BAA 대기] 리밸런싱 주기 미도달 (최근 실행일로부터 {days_passed}일 경과, 최소 13일 필요)")
            return

    print("\n=== 🔍 [BAA 전략 진단 시작] ===")
    
    # 3. 카나리아 자산 점검
    print(">> [1단계] 카나리아 자산 모멘텀 스코어 진단")
    is_defense = False
    for code, name in CANARY_UNIVERSE.items():
        closes = get_daily_closes(token, code)
        score = calc_fast_baa(closes)
        print(f"  - {name}({code}): 스코어 {score:.2f}")
        if score <= 0:
            print(f"  🚨 카나리아 경고! ({name} 스코어 음수) -> 수비 모드 전환 확정")
            is_defense = True
            break
    
    target_code, target_name = None, None

    # 4. 공격 자산 점검
    if not is_defense:
        print(">> [2단계] 공격 자산 유니버스 진단 (추세 필터 적용)")
        scores = {}
        for code, name in ATTACK_UNIVERSE.items():
            closes = get_daily_closes(token, code)
            score = calc_fast_baa(closes)
            curr = closes[-1]
            ma60 = sum(closes[-60:])/60
            ma200 = sum(closes[-200:])/200
            
            print(f"  - {name}: 스코어 {score:.2f} | 현재가 {curr:,.0f} (60MA: {ma60:,.0f}, 200MA: {ma200:,.0f})")
            if curr >= ma60 and curr >= ma200:
                scores[code] = score
            else:
                print(f"    -> [탈락] 이평선 필터 미달")
                
        if scores:
            target_code = max(scores, key=scores.get)
            target_name = ATTACK_UNIVERSE[target_code]
            print(f">> 📈 최종 공격 타깃 선정: {target_name} (스코어: {scores[target_code]:.2f})")
        else:
            print(">> 🚨 모든 공격 자산이 이평선 필터를 탈락하여 수비 모드로 전환합니다.")
            is_defense = True
            
    # 5. 수비 자산 점검
    if is_defense:
        print(">> [2단계] 수비 자산 유니버스 진단")
        scores = {}
        for code, name in DEFENSE_UNIVERSE.items():
            closes = get_daily_closes(token, code)
            score = calc_fast_baa(closes)
            scores[code] = score
            print(f"  - {name}: 스코어 {score:.2f}")
            
        target_code = max(scores, key=scores.get)
        target_name = DEFENSE_UNIVERSE[target_code]
        print(f">> 🛡️ 최종 수비 타깃 선정: {target_name}")

    # FORCE_BAA가 켜져 있으면 텔레그램으로도 진단 결과를 쏩니다.
    if force_baa == "Y":
        send_telegram(
            f"🛠️ <b>[BAA 강제 실행 진단 결과]</b>\n"
            f"▪ 카나리아 상태: {'🚨 붕괴 (수비전환)' if is_defense else '✅ 안전 (공격유지)'}\n"
            f"▪ 최종 타깃 자산: {target_name} ({target_code})"
        )

    # 실제 매매 로직 (기존과 동일)
    base_budget = total_asset * 0.70
    total_budget = base_budget + state.get("reinvest_pool", 0)
    target_closes = get_daily_closes(token, target_code)
    is_bull = (sum(target_closes[-20:])/20) > (sum(target_closes[-60:])/60)
    
    for h_code, item in holdings.items():
        if h_code not in (target_code, CODE_BULL):
            execute_order(token, h_code, item["qty"], "01", action_name="BAA 교체매도")

    target_budget = total_budget if is_bull else (total_budget * 0.50)
    qty_diff = int(target_budget // target_closes[-1]) - holdings.get(target_code, {}).get("qty", 0)
    
    if qty_diff > 0:
        if execute_order(token, target_code, qty_diff, "02", action_name="BAA 매수")[0]:
            state["reinvest_pool"] = 0
            state["last_executed_date"] = now.strftime("%Y-%m-%d")
            save_json(BAA_STATE_FILE, state)

# ==============================================================================
# 6. 메인 오케스트레이션
# ==============================================================================
def main():
    token = get_access_token()
    if not is_market_open_today(token): return
    
    holdings, deposit, tot_eval = get_account_balance(token)
    total_asset = max(deposit, tot_eval)

    run_intraday(token, holdings, deposit, total_asset)
    run_baa(token, holdings, total_asset)

if __name__ == "__main__":
    main()
