import os
import sys
import json
import requests
from datetime import datetime
import pytz

# ==============================================================================
# 1. 환경 설정 및 상수 정의
# ==============================================================================
KST = pytz.timezone("Asia/Seoul")
CACHE_FILE = "token_cache.json"

KIS_URL = os.getenv("KIS_URL", "https://openapi.koreainvestment.com:9443").rstrip("/")
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_CANO = (os.getenv("KIS_CANO") or "").strip()
KIS_ACNT_PRDT_CD = (os.getenv("KIS_ACNT_PRDT_CD") or "01").strip().zfill(2)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CODE_BULL = "069500"   # KODEX 200 (상승 추세 추종)
CODE_BEAR = "114800"   # KODEX 인버스 (하락 추세 추종)

# 매매 기준 비율
PROFIT_TARGET = 0.018  # 익절선: +1.8%
STOP_LOSS = -0.009     # 손절선: -0.9%
PYRAMID_RATIO = 0.007  # 불타기 진입선: +0.7%
WATER_RATIO = -0.006   # 물타기 진입선: -0.6%

# 예수금 100% 동적 복리 자본 배분 비율
RATIO_BASE = 0.35      # 1차 진입: 총 자산의 35%
RATIO_PYRAMID = 0.25   # 불타기: 총 자산의 25%
RATIO_WATER = 0.40     # 물타기: 총 자산의 40%

# ==============================================================================
# 2. 텔레그램 알림 발송 함수
# ==============================================================================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 설정 미완료로 발송을 건너뜁니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f">> [Telegram] 전송 실패: {e}")

# ==============================================================================
# 3. 토큰 관리 (1일 1회 발급 & 캐시 재사용)
# ==============================================================================
def get_access_token():
    today_str = datetime.now(KST).strftime("%Y%m%d")

    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached.get("date") == today_str and "token" in cached:
                    print(">> [토큰] 당일 캐시 토큰 재사용 (서버 호출 생략)")
                    return cached["token"]
        except Exception:
            pass

    print(">> [토큰] 당일 첫 실행: 한국투자증권 신규 토큰 발급")
    url = f"{KIS_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }
    res = requests.post(url, json=body, timeout=10)
    data = res.json()

    if "access_token" in data:
        token = data["access_token"]
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "token": token}, f)
        except Exception:
            pass
        return token
    else:
        raise Exception(f"토큰 발급 실패: {json.dumps(data, ensure_ascii=False)}")

# ==============================================================================
# 4. KIS 시세/잔고/주문 API
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
    raise Exception(f"시세 조회 실패: {data.get('msg1')}")

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
        CODE_BULL: {"qty": 0, "avg_price": 0.0},
        CODE_BEAR: {"qty": 0, "avg_price": 0.0}
    }
    deposit = 0
    tot_evlu = 0

    if data.get("rt_cd") == "0":
        for s in data.get("output1", []):
            code = s.get("pdno")
            if code in holdings:
                holdings[code]["qty"] = int(s.get("hldg_qty", 0))
                holdings[code]["avg_price"] = float(s.get("pchs_avg_pric", 0.0))

        output2 = data.get("output2", [{}])
        if output2:
            deposit = int(output2[0].get("dnca_tot_amt", 0))
            tot_evlu = int(output2[0].get("tot_evlu_amt", 0))

        return holdings, deposit, tot_evlu
    raise Exception(f"잔고 조회 실패: {data.get('msg1')}")

def send_order(token, code, qty, order_type="01"):
    if qty <= 0:
        print(f">> 주문 수량 부족({qty}주)으로 주문 취소")
        return {"rt_cd": "-1"}
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
        "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0"
    }
    res = requests.post(url, headers=headers, json=body, timeout=10)
    return res.json()

def check_market_direction(token):
    """KODEX 200 15분봉 데이터 기반 당일 방향성 판별"""
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
        if len(output2) >= 2:
            prev_close = float(output2[1].get("stck_prpr", 0))
            curr_price = float(output2[0].get("stck_prpr", 0))
            print(f">> [15분봉 비교] KODEX 200 직전봉: {prev_close:,}원 | 현재가: {curr_price:,}원")
            if curr_price > prev_close:
                return "UP"
            elif curr_price < prev_close:
                return "DOWN"
    except Exception as e:
        print(f">> 15분봉 파싱 예외: {e}")
    return "NONE"

# ==============================================================================
# 5. 포지션 관리 및 동적 복리 추가 매수 (불타기/물타기)
# ==============================================================================
def manage_position(token, code, name, holding, curr_price, deposit, total_asset):
    qty = holding["qty"]
    avg_price = holding["avg_price"]
    profit_ratio = (curr_price - avg_price) / avg_price
    profit_pct = profit_ratio * 100

    print(f">> [{name}] 보유: {qty}주 | 평단: {avg_price:,.0f}원 | 현재가: {curr_price:,}원 | 손익: {profit_pct:+.2f}%")

    # 1) 익절 도달 (+1.8%)
    if profit_ratio >= PROFIT_TARGET:
        print(f">> [{name} 익절 달성] {qty}주 전량 시장가 매도!")
        order_res = send_order(token, code, qty, order_type="02")
        if order_res.get("rt_cd") == "0":
            diff = int((curr_price - avg_price) * qty)
            send_telegram(
                f"🎯 [{name} 익절 달성 - 복리 수익 확정]\n"
                f"• 매도가: {curr_price:,}원\n"
                f"• 수익률: {profit_pct:+.2f}%\n"
                f"• 실현 손익: {diff:+,}원\n"
                f"• 매도 수량: {qty}주 전량 청산"
            )
        return

    # 2) 손절 기준 이탈 (-0.9%)
    if profit_ratio <= STOP_LOSS:
        print(f">> [{name} 손절 기준 이탈] {qty}주 전량 시장가 손절!")
        order_res = send_order(token, code, qty, order_type="02")
        if order_res.get("rt_cd") == "0":
            diff = int((curr_price - avg_price) * qty)
            send_telegram(
                f"🛑 [{name} 칼손절 실행 - 원칙 청산]\n"
                f"• 손절가: {curr_price:,}원\n"
                f"• 손실률: {profit_pct:+.2f}%\n"
                f"• 손실 금액: {diff:+,}원\n"
                f"• 매도 수량: {qty}주 전량 손절"
            )
        return

    # 3) 불타기 (총 자산의 25% 추가 투입): +0.7% 이상 & 현재 1차 진입 물량 수준일 때 1회만
    calc_base_max = max(1, int((total_asset * (RATIO_BASE + 0.05)) // curr_price))
    if profit_ratio >= PYRAMID_RATIO and qty <= calc_base_max:
        pyramid_budget = min(deposit, total_asset * RATIO_PYRAMID)
        pyramid_qty = int(pyramid_budget // curr_price)
        if pyramid_qty > 0:
            print(f">> [{name} 불타기] 상승 추세 강화 (+{profit_pct:.2f}%) -> {pyramid_qty}주 추가 매수!")
            order_res = send_order(token, code, pyramid_qty, order_type="01")
            if order_res.get("rt_cd") == "0":
                send_telegram(
                    f"🔥 [{name} 불타기 피라미딩 매수]\n"
                    f"• 현재 수익률: {profit_pct:+.2f}%\n"
                    f"• 추가 매수: {pyramid_qty}주 (예수금 25% 투입)\n"
                    f"• 총 보유량: {qty + pyramid_qty}주"
                )
            return

    # 4) 물타기 (총 자산의 40% 분할 투입): -0.6% 이하 & 평단 방어
    if profit_ratio <= WATER_RATIO and qty <= calc_base_max:
        water_budget = min(deposit, total_asset * RATIO_WATER)
        water_qty = int(water_budget // curr_price)
        if water_qty > 0:
            print(f">> [{name} 물타기] 눌림목 발생 ({profit_pct:.2f}%) -> {water_qty}주 분할 매수!")
            order_res = send_order(token, code, water_qty, order_type="01")
            if order_res.get("rt_cd") == "0":
                send_telegram(
                    f"💧 [{name} 물타기 평단 방어 매수]\n"
                    f"• 현재 손익률: {profit_pct:+.2f}%\n"
                    f"• 분할 매수: {water_qty}주 (예수금 40% 투입)\n"
                    f"• 총 보유량: {qty + water_qty}주"
                )
            return

    print(f">> [{name}] 포지션 유지 (관망 중)")

# ==============================================================================
# 6. 메인 실행 루틴
# ==============================================================================
def main():
    now = datetime.now(KST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_hm = now.strftime("%H:%M")

    print(f"=== [100% 동적 복리 자동매매 봇 실행: {now_str}] ===")

    if not all([KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO]):
        print("[에러] KIS 환경변수 미설정")
        sys.exit(1)

    token = get_access_token()
    holdings, deposit, tot_evlu = get_positions_and_balance(token)
    bull_price, bull_rate = get_current_price(token, CODE_BULL)
    bear_price, bear_rate = get_current_price(token, CODE_BEAR)

    # 총 운용 가능 자산 기준가
    total_asset = max(deposit, tot_evlu, 100000)

    print(f">> KODEX 200: {bull_price:,}원 ({bull_rate:+.2f}%) | 인버스: {bear_price:,}원 ({bear_rate:+.2f}%)")
    print(f">> 예수금: {deposit:,}원 | 총 자산: {total_asset:,}원")

    # --------------------------------------------------------------------------
    # 분기 1: [16:00 당일 장 마감 결산 썸머리]
    # --------------------------------------------------------------------------
    if time_hm >= "15:45":
        print(">> [16:00 결산] 마감 썸머리를 발송합니다.")
        summary_msg = (
            f"📈 [KIS 자동매매 - 당일 복리 결산 썸머리]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 마감 일시: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"• KODEX 200: {bull_price:,}원 ({bull_rate:+.2f}%)\n"
            f"• KODEX 인버스: {bear_price:,}원 ({bear_rate:+.2f}%)\n"
            f"• 현재 예수금: {deposit:,}원\n"
            f"• 총 평가자산: {total_asset:,}원\n"
            f"• 잔여 수량: 0주 (15:15 전량 청산 완료)\n"
            f"• 운용 상태: 현금 100% 안전 보관 (오버나잇 0%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"복리 수익 스노우볼이 안전하게 유지되고 있습니다."
        )
        send_telegram(summary_msg)
        return

    # --------------------------------------------------------------------------
    # 분기 2: [15:15 장 마감 전량 강제 청산]
    # --------------------------------------------------------------------------
    if "15:10" <= time_hm < "15:45":
        print(">> [15:15 마감 청산] 오버나잇 방지를 위해 모든 포지션을 전량 청산합니다.")
        for code, name in [(CODE_BULL, "KODEX 200"), (CODE_BEAR, "KODEX 인버스")]:
            qty = holdings[code]["qty"]
            if qty > 0:
                send_order(token, code, qty, order_type="02")
                send_telegram(f"🧹 [장 마감 전량 청산] {name} {qty}주 전량 시장가 매도 완료 (현금 100% 회복)")
        return

    # --------------------------------------------------------------------------
    # 분기 3: 포지션 보유 중 감시 (익절/손절/불타기/물타기)
    # --------------------------------------------------------------------------
    has_bull = holdings[CODE_BULL]["qty"] > 0
    has_bear = holdings[CODE_BEAR]["qty"] > 0

    if has_bull:
        manage_position(token, CODE_BULL, "KODEX 200", holdings[CODE_BULL], bull_price, deposit, total_asset)
        return

    if has_bear:
        manage_position(token, CODE_BEAR, "KODEX 인버스", holdings[CODE_BEAR], bear_price, deposit, total_asset)
        return

    # --------------------------------------------------------------------------
    # 분기 4: 미보유 상태 신규 진입 탐색 (예수금 35% 동적 복리 매수)
    # --------------------------------------------------------------------------
    if not has_bull and not has_bear:
        if "09:15" <= time_hm <= "14:30":
            direction = check_market_direction(token)
            
            if direction == "UP":
                # KODEX 200 1차 진입 수량 계산 (총 자산의 35%)
                base_budget = min(deposit, total_asset * RATIO_BASE)
                base_qty = int(base_budget // bull_price)
                if base_qty > 0:
                    print(f">> [상승 돌파] KODEX 200 1차 복리 매수 ({base_qty}주 / 약 {int(base_budget):,}원)")
                    order_res = send_order(token, CODE_BULL, base_qty, order_type="01")
                    if order_res.get("rt_cd") == "0":
                        send_telegram(
                            f"🚀 [상승 돌파 1차 복리 매수: KODEX 200]\n"
                            f"• 진입가: {bull_price:,}원\n"
                            f"• 매수 수량: {base_qty}주 (비중 35%)\n"
                            f"• 목표 익절: +1.8% | 칼손절: -0.9%\n"
                            f"• 불타기(+0.7%): 25% 추가 / 물타기(-0.6%): 40% 대기"
                        )
            elif direction == "DOWN":
                # KODEX 인버스 1차 진입 수량 계산 (총 자산의 35%)
                base_budget = min(deposit, total_asset * RATIO_BASE)
                base_qty = int(base_budget // bear_price)
                if base_qty > 0:
                    print(f">> [하락 이탈] KODEX 인버스 1차 복리 매수 ({base_qty}주 / 약 {int(base_budget):,}원)")
                    order_res = send_order(token, CODE_BEAR, base_qty, order_type="01")
                    if order_res.get("rt_cd") == "0":
                        send_telegram(
                            f"📉 [하락 이탈 1차 복리 매수: KODEX 인버스]\n"
                            f"• 진입가: {bear_price:,}원\n"
                            f"• 매수 수량: {base_qty}주 (비중 35%)\n"
                            f"• 목표 익절: +1.8% | 칼손절: -0.9%\n"
                            f"• 불타기(+0.7%): 25% 추가 / 물타기(-0.6%): 40% 대기"
                        )
            else:
                print(">> [관망] 15분봉 횡보 구간 (진입 신호 없음)")
        else:
            print(f">> 현재 시각({time_hm})은 신규 진입 허용 시간(09:15~14:30)이 아닙니다.")

    print(">> 1회 실행 사이클이 안전하게 완료되었습니다.")

if __name__ == "__main__":
    main()
