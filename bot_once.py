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

# BAA 유니버스 매핑 (국내 상장 대표 ETF)
CANARY_UNIVERSE = {
    "360750": "TIGER 미국S&P500",
    "069500": "KODEX 200",
    "371460": "TIGER 차이나전기차SOLACTIVE",
    "453850": "ACE 미국종합채권액티브"
}

ATTACK_UNIVERSE = {
    "133690": "TIGER 미국나스닥100",
    "069500": "KODEX 200",
    "122630": "KODEX 레버리지",
    "381180": "TIGER 미국필라델피아반도체나스닥"
}

DEFENSE_UNIVERSE = {
    "423160": "KODEX KOFR금리액티브(합성)",
    "449170": "KODEX 미국달러SOFR금리액티브(합성)",
    "453850": "ACE 미국종합채권액티브",
    "411060": "ACE KRX금현물"
}

# ==============================================================================
# 2. 유틸리티 (텔레그램, 파일 입출력, 인증, 휴장일)
# ==============================================================================
def send_telegram(message: str, is_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram 미설정/콘솔출력]\n{message}")
        return
    prefix = "🚨 [비상 경고]\n" if is_alert else ""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"{prefix}{message}", "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            print(f">> Telegram 전송 실패 응답: {res.text}")
    except Exception as e:
        print(f">> Telegram 통신 에러: {e}")

def load_json(filepath: str, default: dict):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(filepath: str, data: dict):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f">> 파일 저장 실패 ({filepath}): {e}")

def get_access_token():
    now_ts = int(time.time())
    cached = load_json(CACHE_FILE, {})
    if cached.get("expires_at", 0) > (now_ts + 600) and "token" in cached:
        return cached["token"]

    url = f"{KIS_URL}/oauth2/tokenP"
    res = requests.post(
        url,
        json={"grant_type": "client_credentials", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
        timeout=10
    )
    data = res.json()
    if "access_token" in data:
        token = data["access_token"]
        save_json(CACHE_FILE, {"token": token, "expires_at": now_ts + int(data.get("expires_in", 86400))})
        return token
    raise RuntimeError(f"토큰 발급 실패: {data}")

def is_market_open_today(token: str) -> bool:
    today_str = datetime.now(KST).strftime("%Y%m%d")
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/chk-holiday"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "CTCA0903R"
    }
    params = {"BASS_DT": today_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5).json()
        if res.get("rt_cd") == "0":
            for item in res.get("output", []):
                if item.get("bass_dt") == today_str:
                    is_open = item.get("opnd_yn") == "Y"
                    print(f">> [개장일 조회] 오늘({today_str}) 개장 여부: {is_open}")
                    return is_open
    except Exception:
        pass
    return datetime.now(KST).weekday() < 5

# ==============================================================================
# 3. KIS API 시세/잔고/주문 집행
# ==============================================================================
def get_current_price(token: str, code: str):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    res = requests.get(url, headers=headers, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=10).json()
    if res.get("rt_cd") == "0":
        return int(res["output"]["stck_prpr"])
    return 0

def get_account_balance(token: str):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "TTTC8434R"
    }
    params = {
        "CANO": KIS_CANO,
        "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params, timeout=10).json()
    holdings = {}
    for item in res.get("output1", []):
        qty = int(item.get("hldg_qty", 0))
        if qty > 0:
            holdings[item.get("pdno")] = {
                "name": item.get("prdt_name"),
                "qty": qty,
                "avg_price": float(item.get("pchs_avg_pric", 0.0))
            }

    deposit = int(res.get("output2", [{}])[0].get("dnca_tot_amt", 0))
    tot_eval = int(res.get("output2", [{}])[0].get("tot_evlu_amt", 0))
    return holdings, deposit, tot_eval

def execute_order(token: str, code: str, qty: int, order_type="02", ord_dvsn="01", price=0, action_name="주문"):
    if qty <= 0:
        return False, 0
    tr_id = "TTTC0802U" if order_type == "01" else "TTTC0801U"
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id
    }
    body = {
        "CANO": KIS_CANO,
        "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0" if ord_dvsn == "01" else str(int(price))
    }
    res = requests.post(url, headers=headers, json=body, timeout=10).json()
    if res.get("rt_cd") == "0":
        odno = res.get("output", {}).get("ODNO", "")
        print(f">> ✅ [{action_name}] 접수 성공! 종목: {code} | 수량: {qty}주 | 주문번호: {odno}")
        return True, qty
    err_msg = res.get("msg1", "알 수 없는 오류")
    print(f">> ❌ [{action_name} 실패] 종목: {code} | 사유: {err_msg}")
    # 수량 초과로 인한 실패일 경우 상위 로직에서 재시도 하도록 에러 텔레그램은 생략 (또는 커스텀 처리)
    if "수량" not in err_msg:
        send_telegram(f"[{action_name} 실패] {code}: {err_msg}", is_alert=True)
    return False, 0

# ==============================================================================
# 4. 일봉 수집 및 수학적 모멘텀 스코어링
# ==============================================================================
def get_daily_closes(token: str, code: str, target_count: int = 150) -> list:
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010400"
    }
    all_closes = []
    end_date = datetime.now(KST).strftime("%Y%m%d")

    for _ in range(2):
        start_date = (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=150)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0"
        }
        time.sleep(0.1)
        res = requests.get(url, headers=headers, params=params, timeout=10).json()
        output = res.get("output", [])
        if not output:
            break

        for c in output:
            if c.get("stck_clpr"):
                all_closes.append(float(c["stck_clpr"]))

        if len(all_closes) >= target_count:
            break

        oldest_date = output[-1].get("stck_bsop_date")
        if not oldest_date or oldest_date == end_date:
            break
        end_date = (datetime.strptime(oldest_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    sorted_closes = list(reversed(all_closes))
    if not sorted_closes:
        curr_p = get_current_price(token, code)
        if curr_p > 0:
            sorted_closes = [float(curr_p)]

    return sorted_closes

def calc_fast_baa(closes: list) -> float:
    n = len(closes)
    if n < 2:
        return 0.0

    curr = closes[-1]
    p_1m = closes[-min(21, n)]
    p_3m = closes[-min(63, n)]
    p_6m = closes[-min(126, n)]
    p_12m = closes[-min(252, n)]

    r1 = (curr - p_1m) / p_1m if p_1m > 0 else 0.0
    r3 = (curr - p_3m) / p_3m if p_3m > 0 else 0.0
    r6 = (curr - p_6m) / p_6m if p_6m > 0 else 0.0
    r12 = (curr - p_12m) / p_12m if p_12m > 0 else 0.0

    score = (16.0 * r1) + (6.0 * r3) + (2.0 * r6) + (1.0 * r12)
    return score

# ==============================================================================
# 5. 데이트레이딩 (KODEX 200) 감시
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
    if curr_price == 0:
        return

    state = load_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
    intraday_budget = total_asset * DAY_BUDGET_RATIO

    print(f"\n--- [데이트레이딩 점검: {time_hm}] ---")
    print(f"• KODEX 200 보유: {qty}주 | 현재가: {curr_price:,}원 | 단타 배정예산: {intraday_budget:,.0f}원")

    # 신규 1차 진입
    if qty == 0:
        if "09:15" <= time_hm <= "14:30":
            if check_daytrade_signal(token):
                # [적용] 증거금/수수료 안전 버퍼 96% 곱연산 적용
                base_budget = min(float(deposit), float(intraday_budget * WEIGHT_BASE)) * 0.96
                base_qty = int(base_budget // curr_price)
                
                # [적용] 1주 차감 재시도(Fallback Retry) 로직
                while base_qty > 0:
                    ok, _ = execute_order(token, CODE_BULL, base_qty, "02", action_name="단타 1차 매수")
                    if ok:
                        save_json(DAY_STATE_FILE, {"entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "pyramided": False})
                        send_telegram(f"🚀 <b>[KODEX 200 1차 진입]</b> {base_qty}주 매수 완료 (@{curr_price:,}원)")
                        break
                    else:
                        print(f">> ⚠️ [수량 초과 의심] 1주 차감 후 재시도합니다. ({base_qty}주 -> {base_qty-1}주)")
                        base_qty -= 1
                        time.sleep(0.5)
        else:
            print(f">> [단타 대기] 신규 진입 시간대(09:15~14:30) 외 구간입니다.")
    else:
        avg_price = holding["avg_price"]
        profit_ratio = (curr_price - avg_price) / avg_price
        diff = int((curr_price - avg_price) * qty)
        print(f"• 포지션 평가손익: {profit_ratio*100:+.2f}% ({diff:+,}원)")

        if profit_ratio >= PROFIT_TARGET:
            if execute_order(token, CODE_BULL, qty, "01", action_name="목표익절")[0]:
                send_telegram(f"🎯 <b>[KODEX 200 목표 익절]</b> +{profit_ratio*100:.2f}% | 이익: <b>+{diff:,}원</b>")
                baa_state = load_json(BAA_STATE_FILE, {"reinvest_pool": 0})
                baa_state["reinvest_pool"] = baa_state.get("reinvest_pool", 0) + diff
                save_json(BAA_STATE_FILE, baa_state)
                save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        elif profit_ratio <= STOP_LOSS:
            if execute_order(token, CODE_BULL, qty, "01", action_name="칼손절")[0]:
                send_telegram(f"🛑 <b>[KODEX 200 원칙 칼손절]</b> {profit_ratio*100:.2f}% | 손실: <b>{diff:,}원</b>")
                save_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False})
        elif profit_ratio >= PYRAMID_TRIGGER and not state.get("pyramided", False):
            # [적용] 증거금/수수료 안전 버퍼 96% 적용 및 불타기 재시도 로직
            pyr_budget = min(float(deposit), float(intraday_budget * WEIGHT_PYRAMID)) * 0.96
            pyr_qty = int(pyr_budget // curr_price)
            
            while pyr_qty > 0:
                ok, _ = execute_order(token, CODE_BULL, pyr_qty, "02", action_name="단타 불타기")
                if ok:
                    state["pyramided"] = True
                    save_json(DAY_STATE_FILE, state)
                    send_telegram(f"🔥 <b>[KODEX 200 불타기]</b> {pyr_qty}주 추가 진입 완료")
                    break
                else:
                    print(f">> ⚠️ [수량 초과 의심] 1주 차감 후 재시도합니다. ({pyr_qty}주 -> {pyr_qty-1}주)")
                    pyr_qty -= 1
                    time.sleep(0.5)

# ==============================================================================
# 6. 공격형 BAA 전략
# ==============================================================================
def run_baa(token: str, holdings: dict, deposit: int, total_asset: int, force_run: bool = False):
    now = datetime.now(KST)
    time_str = now.strftime("%H:%M")

    print(f"\n=== 🛡️ [공격형 BAA 전략 정밀 진단: {time_str}] ===")

    if not ("11:00" <= time_str <= "11:25"):
        if force_run:
            print(f">> ⚠️ [BAA 강제 실행 모드] 시간 제한을 우회하여 스코어링 및 즉시 매수를 진행합니다.")
        else:
            print(f">> [BAA 대기] 정규 실행 시간(11:00~11:25)이 아닙니다.")
            return

    state = load_json(BAA_STATE_FILE, {"last_executed_date": None, "reinvest_pool": 0})
    if state.get("last_executed_date") and not force_run:
        days_passed = (now.date() - datetime.strptime(state["last_executed_date"], "%Y-%m-%d").date()).days
        if days_passed < 13:
            print(f">> [BAA 대기] 최근 리밸런싱일로부터 {days_passed}일 경과 (최소 13일 필요).")
            return

    print(">> [1단계: 4대 카나리아 모멘텀 점검]")
    is_defense = False
    canary_logs = []
    for code, name in CANARY_UNIVERSE.items():
        closes = get_daily_closes(token, code)
        score = calc_fast_baa(closes)
        canary_logs.append(f"{name}: {score:.2f}")
        print(f"   • {name} ({code}) [수집: {len(closes)}일]: 스코어 {score:.2f}")
        if score <= 0.0:
            print(f"     🚨 위험 감지! {name} 모멘텀 음수 이하 ({score:.2f})")
            is_defense = True

    target_code, target_name = None, None

    if not is_defense:
        print(">> [2단계: 공격 자산 모멘텀 및 60/200일선 추세 필터]")
        scores = {}
        for code, name in ATTACK_UNIVERSE.items():
            closes = get_daily_closes(token, code)
            score = calc_fast_baa(closes)
            curr = closes[-1]
            ma60 = sum(closes[-min(60, len(closes)):]) / min(60, len(closes))
            ma200 = sum(closes[-min(200, len(closes)):]) / min(200, len(closes))

            passed = (curr >= ma60 and curr >= ma200)
            print(f"   • {name} ({code}): 스코어 {score:.2f} | 현재가 {curr:,.0f}원 -> {'통과' if passed else '이평선 미달'}")
            if passed:
                scores[code] = score

        if scores:
            target_code = max(scores, key=scores.get)
            target_name = ATTACK_UNIVERSE[target_code]
            print(f"   🎯 최종 공격 타깃 선정: {target_name} ({target_code}, 스코어: {scores[target_code]:.2f})")
        else:
            print("   🚨 모든 공격 자산이 이평선 필터를 탈락하여 수비 모드로 자동 전환합니다.")
            is_defense = True

    if is_defense:
        print(">> [2단계: 수비 자산 모멘텀 랭킹]")
        scores = {}
        for code, name in DEFENSE_UNIVERSE.items():
            closes = get_daily_closes(token, code)
            score = calc_fast_baa(closes)
            scores[code] = score
            print(f"   • {name} ({code}) [수집: {len(closes)}일]: 스코어 {score:.2f}")

        target_code = max(scores, key=scores.get)
        target_name = DEFENSE_UNIVERSE[target_code]
        print(f"   🛡️ 최종 수비 타깃 선정: {target_name} ({target_code}, 스코어: {scores[target_code]:.2f})")

    reinvest_amt = state.get("reinvest_pool", 0)
    base_budget = total_asset * 0.70
    total_budget = base_budget + reinvest_amt

    target_closes = get_daily_closes(token, target_code)
    curr_target_price = target_closes[-1]
    if curr_target_price <= 0:
        curr_target_price = get_current_price(token, target_code)

    ema20 = sum(target_closes[-min(20, len(target_closes)):]) / min(20, len(target_closes))
    ema60 = sum(target_closes[-min(60, len(target_closes)):]) / min(60, len(target_closes))
    is_bull = ema20 >= ema60

    # [적용] 증거금/수수료 안전 버퍼 96% 곱연산 적용
    target_stage_budget = total_budget if is_bull else (total_budget * 0.50)
    usable_cash = min(float(deposit), float(target_stage_budget)) * 0.96
    
    target_total_qty = int(usable_cash // curr_target_price) if curr_target_price > 0 else 0
    current_holding_qty = holdings.get(target_code, {}).get("qty", 0)
    qty_diff = target_total_qty - current_holding_qty

    print(f"\n• 타깃 현재가: {curr_target_price:,.0f}원 | 20/60 추세: {'정배열' if is_bull else '역배열'}")
    print(f"• 가용 예산(96% 버퍼 적용): {usable_cash:,.0f}원 | 주문 가능 수량: {target_total_qty}주 (현재 보유: {current_holding_qty}주) -> 매수 필요: {qty_diff}주")

    diag_msg = (
        f"📊 <b>[공격형 BAA 전략 정밀 진단]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"• 운용 모드: {'🛡️ 수비 모드 (Crash Defense)' if is_defense else '🚀 공격 모드 (Aggressive)'}\n"
        f"• 선정 타깃: <b>{target_name} ({target_code})</b>\n"
        f"• 매수 예정: {qty_diff}주 (@{curr_target_price:,.0f}원)\n"
        f"• 총 배정예산: {total_budget:,.0f}원 (재투자 {reinvest_amt:,}원 포함)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    send_telegram(diag_msg)

    for h_code, item in holdings.items():
        if h_code != target_code and h_code != CODE_BULL:
            print(f">> [BAA 교체 매도] {item['name']} ({item['qty']}주)")
            execute_order(token, h_code, item["qty"], order_type="01", action_name=f"BAA 매도({item['name']})")
            time.sleep(1.0)

    # [적용] BAA 매수 1주 차감 재시도(Fallback Retry) 로직
    while qty_diff > 0:
        ok, executed_q = execute_order(token, target_code, qty_diff, order_type="02", action_name=f"BAA 매수({target_name})")
        if ok:
            send_telegram(
                f"🎉 <b>[BAA 자산배분 매수 체결 완료]</b>\n"
                f"• 종목: {target_name} ({target_code})\n"
                f"• 체결 수량: {executed_q}주 (@{curr_target_price:,.0f}원)"
            )
            state["reinvest_pool"] = 0
            break
        else:
            print(f">> ⚠️ [증거금 부족/수량 초과 의심] 1주 차감 후 재시도합니다. ({qty_diff}주 -> {qty_diff-1}주)")
            qty_diff -= 1
            time.sleep(0.5)

    state["last_executed_date"] = now.strftime("%Y-%m-%d")
    save_json(BAA_STATE_FILE, state)

# ==============================================================================
# 7. 메인 실행 엔트리포인트
# ==============================================================================
def main():
    now = datetime.now(KST)
    print(f"==================================================")
    print(f"🤖 [한투 퀀트 트레이딩 봇 시작: {now.strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"==================================================")

    force_baa_flag = (os.getenv("FORCE_BAA") or "N").strip().upper() == "Y"
    if force_baa_flag:
        print(">> ⚡ FORCE_BAA=Y 플래그 감지: 휴장일 검사 및 시간 제약을 무시하고 즉시 실행합니다.")

    token = get_access_token()

    if not force_baa_flag:
        if not is_market_open_today(token):
            print(f">> 🏖️ 오늘({now.strftime('%Y-%m-%d')})은 한국거래소 휴장일입니다. 안전하게 종료합니다.")
            return
    else:
        print(">> ⏩ 강제 실행 모드이므로 휴장일 판별을 건너뛰고 계속 진행합니다.")

    holdings, deposit, tot_eval = get_account_balance(token)
    total_asset = max(deposit, tot_eval)
    print(f">> 계좌 예수금: {deposit:,}원 | 총 평가자산: {total_asset:,}원 | 보유종목수: {len(holdings)}개")

    try:
        run_intraday(token, holdings, deposit, total_asset)
    except Exception as e:
        print(f">> 데이트레이딩 루틴 예외: {e}")

    try:
        run_baa(token, holdings, deposit, total_asset, force_run=force_baa_flag)
    except Exception as e:
        print(f">> BAA 전략 루틴 예외: {e}")

    print("\n>> 🏁 1회 실행 사이클이 완료되었습니다.")

if __name__ == "__main__":
    main()
