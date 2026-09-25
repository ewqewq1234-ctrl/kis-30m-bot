import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta
import pytz

# ==============================================================================
# 1. 환경 변수 및 설정값
# ==============================================================================
KST = pytz.timezone("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "token_cache.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

KIS_URL = os.getenv("KIS_URL", "https://openapi.koreainvestment.com:9443").rstrip("/")
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_CANO = (os.getenv("KIS_CANO") or "").strip()
KIS_ACNT_PRDT_CD = (os.getenv("KIS_ACNT_PRDT_CD") or "01").strip().zfill(2)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CODE_BULL = "069500"   # KODEX 200
CODE_BEAR = "114800"   # KODEX 인버스

PROFIT_TARGET = 0.018  # 익절: +1.8%
STOP_LOSS = -0.009     # 손절: -0.9%
TIME_STOP_MINUTES = 90 # 타임스탑: 90분 (미체결/횡보 탈출)

# ==============================================================================
# 2. 텔레그램 알림 및 에러 보고
# ==============================================================================
def send_telegram(message: str, is_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 설정 미완료로 발송 생략")
        return
    prefix = "🚨 [비상 경고]\n" if is_alert else ""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"{prefix}{message}"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f">> [Telegram] 전송 예외: {e}")

# ==============================================================================
# 3. 토큰 발급 및 캐시 영속화
# ==============================================================================
def get_access_token():
    now_ts = int(time.time())

    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached.get("expires_at", 0) > (now_ts + 600) and "token" in cached:
                    print(f">> [토큰] 유효한 캐시 토큰 재사용 (경로: {CACHE_FILE})")
                    return cached["token"]
        except Exception as e:
            print(f">> [토큰] 캐시 로드 실패: {e}")

    print(">> [토큰] 신규 토큰 발급 요청 (한국투자증권 API)")
    url = f"{KIS_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }
    try:
        res = requests.post(url, json=body, timeout=10)
        data = res.json()
        if "access_token" in data:
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 86400))
            cache_data = {
                "token": token,
                "expires_at": now_ts + expires_in,
                "created_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            print(f">> [토큰 저장 완료] 캐시 파일 생성됨: {CACHE_FILE}")
            return token
        else:
            err_msg = f"토큰 발급 실패: {data.get('error_description', data)}"
            send_telegram(err_msg, is_alert=True)
            raise Exception(err_msg)
    except Exception as e:
        send_telegram(f"토큰 발급 네트워크 오류: {e}", is_alert=True)
        raise e

# ==============================================================================
# 4. KIS 시세, 잔고 및 State 동기화
# ==============================================================================
def get_current_price(token, code):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()
    if data.get("rt_cd") == "0":
        return int(data["output"]["stck_prpr"]), float(data["output"].get("prdy_ctrt", 0.0))
    
    send_telegram(f"[{code}] 시세 조회 API 실패: {data.get('msg1')}", is_alert=True)
    raise Exception(f"시세 조회 실패: {data.get('msg1')}")

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f">> [State] 저장 예외: {e}")

def get_positions_and_balance(token):
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type": "application/json; charset=utf-8",
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
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()

    holdings = {
        CODE_BULL: {"qty": 0, "avg_price": 0.0, "bfdy_qty": 0},
        CODE_BEAR: {"qty": 0, "avg_price": 0.0, "bfdy_qty": 0}
    }
    deposit = 0
    tot_evlu = 0

    if data.get("rt_cd") == "0":
        for s in data.get("output1", []):
            code = s.get("pdno")
            if code in holdings:
                holdings[code]["qty"] = int(s.get("hldg_qty", 0))
                holdings[code]["avg_price"] = float(s.get("pchs_avg_pric", 0.0))
                holdings[code]["bfdy_qty"] = int(s.get("bfdy_buy_qty", 0))

        output2 = data.get("output2", [{}])
        if output2:
            deposit = int(output2[0].get("dnca_tot_amt", 0))
            tot_evlu = int(output2[0].get("tot_evlu_amt", 0))

        state = load_state()
        state.update({
            "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "deposit": deposit,
            "tot_evlu": tot_evlu,
            "holdings": holdings
        })
        save_state(state)
        return holdings, deposit, tot_evlu
    
    send_telegram(f"계좌 잔고 조회 API 실패: {data.get('msg1')}", is_alert=True)
    raise Exception(f"잔고 조회 실패: {data.get('msg1')}")

# ==============================================================================
# 5. 주문 전송 및 실체결 검증
# ==============================================================================
def verify_order_execution(token, odno):
    time.sleep(1.5)
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "TTTC8001R"
    }
    today_str = datetime.now(KST).strftime("%Y%m%d")
    params = {
        "CANO": KIS_CANO,
        "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
        "INQR_STRT_DT": today_str,
        "INQR_END_DT": today_str,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "01",
        "ORD_GNO_BRNO": "",
        "ODNO": odno,
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get("rt_cd") == "0":
            for item in data.get("output1", []):
                if item.get("odno") == odno:
                    tot_qty = int(item.get("tot_ccld_qty", 0))
                    avg_prc = float(item.get("avg_prvs", 0.0))
                    return tot_qty, avg_prc
    except Exception as e:
        print(f">> 체결 검증 조회 실패: {e}")
    return 0, 0.0

def execute_order(token, code, qty, order_type="01", ord_dvsn="01", action_name="주문"):
    if qty <= 0:
        return False, 0, 0.0

    tr_id = "TTTC0802U" if order_type == "01" else "TTTC0801U"
    url = f"{KIS_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "content-type": "application/json; charset=utf-8",
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
        "ORD_UNPR": "0"
    }

    try:
        res = requests.post(url, headers=headers, json=body, timeout=10)
        data = res.json()
        
        if data.get("rt_cd") != "0":
            err_msg = f"[{action_name} 접수 실패]\n종목: {code} | 수량: {qty}주\n사유: {data.get('msg1')}"
            send_telegram(err_msg, is_alert=True)
            return False, 0, 0.0

        odno = data["output"].get("ODNO")
        print(f">> [{action_name}] 주문 접수 성공 (주문번호: {odno}) -> 실체결 검증 시작")

        ccld_qty, ccld_price = verify_order_execution(token, odno)
        if ccld_qty > 0:
            print(f">> [체결 완료] {ccld_qty}주 체결 | 평균 체결단가: {ccld_price:,.0f}원")
            return True, ccld_qty, ccld_price
        else:
            print(">> [주의] 주문이 접수되었으나 체결 대기 중입니다.")
            return True, qty, 0.0

    except Exception as e:
        send_telegram(f"[{action_name}] 주문 전송 중 치명적 예외: {e}", is_alert=True)
        return False, 0, 0.0

# ==============================================================================
# 6. 3중 복합 필터 추세 분석 (EMA 20/60, Volume, ATR)
# ==============================================================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def check_market_direction_with_triple_filters(token):
    """
    1) 20 EMA / 60 EMA 정배열/역배열
    2) 거래량 필터 (직전봉 거래량 > 20봉 평균 거래량 * 1.2)
    3) ATR(14) 필터 (직전봉 변동폭 > ATR 14)
    """
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST03010200"
    }
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": CODE_BULL,
        "FID_INPUT_HOUR_1": datetime.now(KST).strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y"
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        output2 = res.json().get("output2", [])
        
        # 60 EMA 및 20 거래량/14 ATR 계산을 위해 최소 65개 이상의 15분봉 데이터 필요
        if len(output2) < 65:
            print(f">> [필터 검증] 분봉 데이터 수 부족 ({len(output2)}개)")
            return "NONE"

        # output2[0]은 현재 형성 중인 미완성 봉, output2[1]부터가 완성된 직전봉
        completed_bars = output2[1:]
        
        # 과거부터 최근 순으로 정렬 (인덱스 역순)
        closes = [float(b.get("stck_prpr", 0)) for b in reversed(completed_bars)]
        highs = [float(b.get("stck_hgpr", 0)) for b in reversed(completed_bars)]
        lows = [float(b.get("stck_lwpr", 0)) for b in reversed(completed_bars)]
        volumes = [float(b.get("cntg_vol", 0)) for b in reversed(completed_bars)]

        last_close = float(output2[1].get("stck_prpr", 0))
        last_high = float(output2[1].get("stck_hgpr", 0))
        last_low = float(output2[1].get("stck_lwpr", 0))
        last_vol = float(output2[1].get("cntg_vol", 0))
        curr_price = float(output2[0].get("stck_prpr", 0))

        # 1. 20 EMA & 60 EMA 계산
        ema20 = calculate_ema(closes, 20)
        ema60 = calculate_ema(closes, 60)

        # 2. 거래량 20봉 평균 대비 1.2배 돌파 확인
        vol_ma20 = sum(volumes[-20:]) / 20
        volume_filter = last_vol >= (vol_ma20 * 1.2)

        # 3. ATR(14) 계산 및 변동폭 필터
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_list.append(tr)
        
        atr14 = sum(tr_list[-14:]) / 14 if len(tr_list) >= 14 else 0
        last_range = last_high - last_low
        atr_filter = last_range >= atr14

        print(f">> [3중 필터 점검] EMA20: {ema20:,.0f} | EMA60: {ema60:,.0f}")
        print(f">> [3중 필터 점검] 직전봉 거래량: {last_vol:,} (기준: {vol_ma20 * 1.2:,.0f}) -> {volume_filter}")
        print(f">> [3중 필터 점검] 직전봉 변동폭: {last_range:,.0f} (기준 ATR: {atr14:,.0f}) -> {atr_filter}")

        if not (volume_filter and atr_filter and ema20 and ema60):
            print(">> [3중 필터 미달] 거래량 또는 ATR 변동성 기준을 충족하지 못해 진입을 차단합니다.")
            return "NONE"

        # 상승 돌파: 20 EMA > 60 EMA (정배열) & 현재가 > 직전 완성봉 종가
        if ema20 > ema60 and curr_price > last_close:
            return "UP"

        # 하락 이탈: 20 EMA < 60 EMA (역배열) & 현재가 < 직전 완성봉 종가
        if ema20 < ema60 and curr_price < last_close:
            return "DOWN"

    except Exception as e:
        send_telegram(f"3중 필터 분석 실패: {e}", is_alert=False)
        print(f">> 15분봉 3중 필터 예외: {e}")

    return "NONE"

# ==============================================================================
# 7. 단일 포지션 관리 (익절, 손절, 90분 타임스탑)
# ==============================================================================
def manage_position(token, code, name, holding, curr_price):
    qty = holding["qty"]
    avg_price = holding["avg_price"]
    profit_ratio = (curr_price - avg_price) / avg_price
    profit_pct = profit_ratio * 100

    state = load_state()
    entry_time_str = state.get("entry_time")
    elapsed_minutes = 0
    if entry_time_str:
        try:
            entry_dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            elapsed_minutes = (datetime.now(KST) - entry_dt).total_seconds() / 60
        except Exception:
            pass

    print(f">> [{name}] 보유: {qty}주 | 평단: {avg_price:,.0f}원 | 현재가: {curr_price:,}원 | 손익: {profit_pct:+.2f}% | 경과: {elapsed_minutes:.1f}분")

    # 1) 목표 익절 (+1.8%)
    if profit_ratio >= PROFIT_TARGET:
        ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="01", action_name=f"{name} 익절")
        if ok:
            diff = int((curr_price - avg_price) * qty)
            send_telegram(f"🎯 [{name} 목표 익절 달성]\n• 매도가: {curr_price:,}원\n• 수익률: {profit_pct:+.2f}%\n• 실현 손익: {diff:+,}원\n• 보유 시간: {elapsed_minutes:.0f}분")
            state["entry_time"] = None
            save_state(state)
        return

    # 2) 원칙 손절 (-0.9%)
    if profit_ratio <= STOP_LOSS:
        ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="01", action_name=f"{name} 칼손절")
        if ok:
            diff = int((curr_price - avg_price) * qty)
            send_telegram(f"🛑 [{name} 원칙 칼손절 집행]\n• 손절가: {curr_price:,}원\n• 손실률: {profit_pct:+.2f}%\n• 손실 금액: {diff:+,}원\n• 보유 시간: {elapsed_minutes:.0f}분")
            state["entry_time"] = None
            save_state(state)
        return

    # 3) 90분 타임스탑 (Time-Stop 청산)
    if elapsed_minutes >= TIME_STOP_MINUTES:
        print(f">> [{name}] 90분 타임스탑 기준 도달 ({elapsed_minutes:.1f}분 경과) -> 시장가 청산 집행")
        ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="01", action_name=f"{name} 90분 타임스탑 청산")
        if ok:
            diff = int((curr_price - avg_price) * qty)
            send_telegram(f"⏱️ [{name} 90분 타임스탑 청산]\n• 체결가: {curr_price:,}원\n• 최종 손익: {profit_pct:+.2f}% ({diff:+,}원)\n• 사유: 횡보로 인한 자금 락업 해제")
            state["entry_time"] = None
            save_state(state)
        return

    print(f">> [{name}] 포지션 유지 (익절/손절/타임스탑 대기)")

# ==============================================================================
# 8. 메인 스케줄 실행 루틴
# ==============================================================================
def main():
    now = datetime.now(KST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_hm = now.strftime("%H:%M")

    print(f"=== [KIS 퀀트 트레이딩 봇 실행: {now_str}] ===")

    if not all([KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO]):
        err_msg = "KIS 필수 환경변수(AppKey, Secret, Cano) 미설정"
        print(f"[에러] {err_msg}")
        send_telegram(err_msg, is_alert=True)
        sys.exit(1)

    token = get_access_token()
    holdings, deposit, tot_evlu = get_positions_and_balance(token)
    bull_price, bull_rate = get_current_price(token, CODE_BULL)
    bear_price, bear_rate = get_current_price(token, CODE_BEAR)
    total_asset = max(deposit, tot_evlu, 100000)

    print(f">> KODEX 200: {bull_price:,}원 ({bull_rate:+.2f}%) | 인버스: {bear_price:,}원 ({bear_rate:+.2f}%)")
    print(f">> 예수금(100% 투입 가능): {deposit:,}원 | 총 자산: {total_asset:,}원")

    # --------------------------------------------------------------------------
    # 분기 0: [오전 09:00 ~ 09:14] 전일 미청산 오버나잇 잔고 개장 즉시 강제 청산
    # --------------------------------------------------------------------------
    if "09:00" <= time_hm < "09:14":
        print(">> [개장 직후 잔고 체크] 전일 미청산 잔고 시장가 강제 청산")
        for code, name in [(CODE_BULL, "KODEX 200"), (CODE_BEAR, "KODEX 인버스")]:
            qty = holdings[code]["qty"]
            if qty > 0:
                ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="01", action_name=f"{name} 개장 강제청산")
                if ok:
                    send_telegram(f"🚨 [개장 즉시 오버나잇 청산] {name} {qty}주 매도 완료 (현금 확보)")
                    state = load_state()
                    state["entry_time"] = None
                    save_state(state)
        return

    # --------------------------------------------------------------------------
    # 분기 1: [15:00 ~ 15:20] 1차 정규장 시장가(01) 전량 청산
    # --------------------------------------------------------------------------
    if "15:00" <= time_hm <= "15:20":
        print(">> [1차 정규장 마감 청산] 정규장 시장가(01) 전량 매도")
        for code, name in [(CODE_BULL, "KODEX 200"), (CODE_BEAR, "KODEX 인버스")]:
            qty = holdings[code]["qty"]
            if qty > 0:
                ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="01", action_name=f"{name} 정규장 마감청산")
                if ok:
                    send_telegram(f"🧹 [정규장 마감 전량 청산] {name} {qty}주 매도 완료")
                    state = load_state()
                    state["entry_time"] = None
                    save_state(state)
        return

    # --------------------------------------------------------------------------
    # 분기 2: [15:40 ~ 16:00] 2차 장후 시간외 종가(05) 강제 청산
    # --------------------------------------------------------------------------
    if "15:40" <= time_hm <= "16:00":
        print(">> [2차 장후 시간외 청산] 장후 시간외 종가(05) 전량 매도")
        for code, name in [(CODE_BULL, "KODEX 200"), (CODE_BEAR, "KODEX 인버스")]:
            qty = holdings[code]["qty"]
            if qty > 0:
                ok, _, _ = execute_order(token, code, qty, order_type="02", ord_dvsn="05", action_name=f"{name} 장후시간외 청산")
                if ok:
                    send_telegram(f"🌙 [장후 시간외 종가 청산] {name} {qty}주 매도 완료")
                    state = load_state()
                    state["entry_time"] = None
                    save_state(state)
        return

    # --------------------------------------------------------------------------
    # 분기 3: [16:01 이후] 최종 당일 마감 썸머리 (동적 상태 검증)
    # --------------------------------------------------------------------------
    if time_hm >= "16:01":
        print(">> [마감 썸머리] 최종 리포트 발송")
        total_remaining = holdings[CODE_BULL]["qty"] + holdings[CODE_BEAR]["qty"]
        if total_remaining == 0:
            status_text = "100% 현금 안전 마감 (오버나잇 제로)"
        else:
            status_text = f"⚠️ 미청산 잔고 보유 마감 (총 {total_remaining}주 보유 - 익일 개장 즉시 청산 예정)"

        summary_msg = (
            f"📈 [KIS 자동매매 - 당일 결산 썸머리]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 마감 일시: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"• KODEX 200 종가: {bull_price:,}원 ({bull_rate:+.2f}%)\n"
            f"• KODEX 인버스 종가: {bear_price:,}원 ({bear_rate:+.2f}%)\n"
            f"• KODEX 200 잔고: {holdings[CODE_BULL]['qty']}주\n"
            f"• KODEX 인버스 잔고: {holdings[CODE_BEAR]['qty']}주\n"
            f"• 현재 예수금: {deposit:,}원\n"
            f"• 총 평가자산: {total_asset:,}원\n"
            f"• 오버나잇 상태: {status_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        send_telegram(summary_msg)
        return

    # --------------------------------------------------------------------------
    # 분기 4: 포지션 보유 중 감시 (단일 포지션 관리)
    # --------------------------------------------------------------------------
    has_bull = holdings[CODE_BULL]["qty"] > 0
    has_bear = holdings[CODE_BEAR]["qty"] > 0

    if has_bull:
        manage_position(token, CODE_BULL, "KODEX 200", holdings[CODE_BULL], bull_price)
        return

    if has_bear:
        manage_position(token, CODE_BEAR, "KODEX 인버스", holdings[CODE_BEAR], bear_price)
        return

    # --------------------------------------------------------------------------
    # 분기 5: 신규 진입 탐색 (예수금 100% 활용 풀베팅 모델)
    # --------------------------------------------------------------------------
    if not has_bull and not has_bear:
        if "09:15" <= time_hm <= "14:30":
            direction = check_market_direction_with_triple_filters(token)
            
            # 예수금의 100%를 전부 투입하여 매수 가능 수량 계산
            if direction == "UP":
                full_qty = int(deposit // bull_price)
                if full_qty > 0:
                    ok, c_qty, c_prc = execute_order(token, CODE_BULL, full_qty, order_type="01", ord_dvsn="01", action_name="KODEX 200 100% 진입")
                    if ok:
                        state = load_state()
                        state["entry_time"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                        save_state(state)
                        send_telegram(f"🚀 [3중 필터 돌파 진입] KODEX 200 {full_qty}주 (예수금 100% 가동)\n• 진입가: {bull_price:,}원\n• 총 투입금: {full_qty * bull_price:,}원\n• 90분 타임스탑 타이머 가동")
                else:
                    print(f">> [예수금 부족] 현재 예수금({deposit:,}원)으로 1주도 살 수 없습니다.")

            elif direction == "DOWN":
                full_qty = int(deposit // bear_price)
                if full_qty > 0:
                    ok, c_qty, c_prc = execute_order(token, CODE_BEAR, full_qty, order_type="01", ord_dvsn="01", action_name="KODEX 인버스 100% 진입")
                    if ok:
                        state = load_state()
                        state["entry_time"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                        save_state(state)
                        send_telegram(f"📉 [3중 필터 이탈 진입] KODEX 인버스 {full_qty}주 (예수금 100% 가동)\n• 진입가: {bear_price:,}원\n• 총 투입금: {full_qty * bear_price:,}원\n• 90분 타임스탑 타이머 가동")
                else:
                    print(f">> [예수금 부족] 현재 예수금({deposit:,}원)으로 1주도 살 수 없습니다.")

            else:
                print(">> [관망] 3중 필터 조건을 만족하는 셋업이 없습니다.")
        else:
            print(f">> 현재 시각({time_hm})은 신규 진입 허용 시간이 아닙니다.")

    print(">> 1회 실행 사이클이 안전하게 완료되었습니다.")

if __name__ == "__main__":
    main()
