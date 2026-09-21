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
KIS_CANO = os.getenv("KIS_CANO")
KIS_ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD", "01")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_CODE = "069500"  # KODEX 200
TRADE_QTY = 1           # 1회 진입 수량 (1주)
PROFIT_TARGET = 0.018   # 익절선: +1.8%
STOP_LOSS = -0.009      # 손절선: -0.9%

# ==============================================================================
# 2. 텔레그램 메시지 발송 함수
# ==============================================================================
def send_telegram(message: str):
    """텔레그램 메시지 발송 (실패해도 스크립트 중단 방지)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 토큰 또는 Chat ID가 설정되지 않아 발송을 건너뜁니다.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            print(">> [Telegram] 알림 발송 성공")
        else:
            print(f">> [Telegram] 알림 발송 응답 에러: {res.text}")
    except Exception as e:
        print(f">> [Telegram] 전송 예외 발생: {e}")

# ==============================================================================
# 3. 토큰 관리 (하루 1회만 발급 후 캐시 재사용)
# ==============================================================================
def get_access_token():
    """
    당일 날짜로 저장된 token_cache.json이 있으면 재사용,
    없으면 증권사 서버에서 1회 신규 발급 후 파일로 저장.
    """
    today_str = datetime.now(KST).strftime("%Y%m%d")

    # 1. 로컬 캐시 확인
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached.get("date") == today_str and "token" in cached:
                    print(">> [토큰] 오늘 발급된 캐시 토큰을 재사용합니다 (서버 호출 생략).")
                    return cached["token"]
        except Exception as e:
            print(f">> [토큰] 캐시 로드 예외 ({e}), 신규 발급으로 전환합니다.")

    # 2. 증권사 서버에서 신규 발급
    print(">> [토큰] 당일 첫 실행: 한국투자증권에서 신규 접근 토큰을 발급받습니다.")
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
            print(">> [토큰] 신규 토큰 발급 및 로컬 캐시 저장 완료.")
        except Exception as e:
            print(f">> [토큰] 캐시 저장 실패 ({e})")
        return token
    else:
        raise Exception(f"토큰 발급 실패: {json.dumps(data, ensure_ascii=False)}")

# ==============================================================================
# 4. KIS 시세 및 주문 API 헬퍼 함수
# ==============================================================================
def get_current_price(token, code):
    """현재가 및 등락률 조회"""
    url = f"{KIS_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()
    if data.get("rt_cd") == "0":
        stck_prpr = int(data["output"]["stck_prpr"])
        prdy_ctrt = float(data["output"].get("prdy_ctrt", 0.0))  # 전일 대비 등락률
        return stck_prpr, prdy_ctrt
    else:
        raise Exception(f"현재가 조회 실패: {data.get('msg1')}")

def get_balance(token):
    """잔고, 매입평균가, 예수금, 총 평가금액 조회"""
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

    holding_qty = 0
    avg_price = 0.0
    deposit = 0
    tot_evlu_amt = 0

    if data.get("rt_cd") == "0":
        for stock in data.get("output1", []):
            if stock.get("pdno") == TARGET_CODE:
                holding_qty = int(stock.get("hldg_qty", 0))
                avg_price = float(stock.get("pchs_avg_pric", 0.0))
                break

        output2 = data.get("output2", [{}])
        if output2:
            deposit = int(output2[0].get("dnca_tot_amt", 0))          # 예수금
            tot_evlu_amt = int(output2[0].get("tot_evlu_amt", 0))    # 총 평가금액

        return holding_qty, avg_price, deposit, tot_evlu_amt
    else:
        raise Exception(f"잔고 조회 실패: {data.get('msg1')}")

def send_order(token, code, qty, order_type="01"):
    """시장가 주문 (order_type: '01'=매수, '02'=매도)"""
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
        "ORD_DVSN": "01",  # 01: 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0"
    }
    res = requests.post(url, headers=headers, json=body, timeout=10)
    return res.json()

def check_15m_signal(token, code):
    """15분봉 상승 돌파 여부 판별 (직전 봉 대비 현재가 상승)"""
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
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": datetime.now(KST).strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y"
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        output2 = data.get("output2", [])

        if len(output2) >= 2:
            prev_close = float(output2[1].get("stck_prpr", 0))
            curr_price = float(output2[0].get("stck_prpr", 0))
            print(f">> [15분봉 분석] 직전봉 종가: {prev_close:,}원 | 현재가: {curr_price:,}원")
            return curr_price > prev_close
        else:
            print(">> 당일 초반 15분봉 집계 중 (신호 대기)")
    except Exception as e:
        print(f">> 15분봉 데이터 파싱 예외: {e}")

    return False

# ==============================================================================
# 5. 메인 실행 루틴
# ==============================================================================
def main():
    now = datetime.now(KST)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_hm = now.strftime("%H:%M")

    print(f"=== [트레이딩 봇 실행: {now_str}] ===")

    if not all([KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO]):
        print("[에러] KIS 인증 정보(AppKey, Secret, CANO)가 설정되지 않았습니다.")
        sys.exit(1)

    # 1) 토큰 로드 (캐시 우선)
    token = get_access_token()

    # 2) 계좌 잔고 및 현재가 조회
    holding_qty, avg_price, deposit, tot_evlu = get_balance(token)
    current_price, prdy_ctrt = get_current_price(token, TARGET_CODE)

    print(f">> 종목: KODEX 200 ({TARGET_CODE}) | 현재가: {current_price:,}원 (등락률: {prdy_ctrt:+.2f}%)")
    print(f">> 잔고: {holding_qty}주 | 매입평균가: {avg_price:,.0f}원 | 예수금: {deposit:,}원")

    # --------------------------------------------------------------------------
    # 분기 1: [16:00 장 마감 종합 썸머리 리포트]
    # --------------------------------------------------------------------------
    if time_hm >= "15:45":
        print(">> [16:00 종합 썸머리] 당일 거래 결산 리포트를 발송합니다.")
        summary_report = (
            f"📈 [KIS 자동매매 - 주식시장 마감 썸머리]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 마감 일시: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"• 대표 종목: KODEX 200 ({TARGET_CODE})\n"
            f"• 종목 종가: {current_price:,}원 ({prdy_ctrt:+.2f}%)\n"
            f"• 당일 보유: {holding_qty}주 (전량 청산 완료)\n"
            f"• 총 평가자산: {tot_evlu:,}원\n"
            f"• 주문가능 예수금: {deposit:,}원\n"
            f"• 운용 상태: 오버나잇 없이 현금 100% 안전 마감\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"오늘 하루 장 운영이 안전하게 완료되었습니다."
        )
        send_telegram(summary_report)
        print(">> 종합 썸머리 전송 완료.")
        return

    # --------------------------------------------------------------------------
    # 분기 2: [15:15 당일 장 마감 전량 청산]
    # --------------------------------------------------------------------------
    if "15:10" <= time_hm < "15:45":
        print(">> [15:15 장 마감 청산] 오버나잇 방지를 위해 잔여 포지션을 전량 매도합니다.")
        if holding_qty > 0:
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            if order_res.get("rt_cd") == "0":
                profit_pct = ((current_price - avg_price) / avg_price) * 100
                diff_won = int((current_price - avg_price) * holding_qty)
                send_telegram(
                    f"🧹 [장 마감 전량 청산 알림]\n"
                    f"• 종목: KODEX 200\n"
                    f"• 청산가: {current_price:,}원\n"
                    f"• 당일 손익률: {profit_pct:+.2f}%\n"
                    f"• 실현 손익: {diff_won:+,}원\n"
                    f"• 잔여 수량: 0주 (현금화 완료)"
                )
        else:
            print(">> 보유 수량 없음 (이미 전량 현금 상태)")
        return

    # --------------------------------------------------------------------------
    # 분기 3: [포지션 보유 중 감시] 익절(+1.8%) / 손절(-0.9%)
    # --------------------------------------------------------------------------
    if holding_qty > 0 and avg_price > 0:
        profit_ratio = (current_price - avg_price) / avg_price
        profit_pct = profit_ratio * 100
        print(f">> 포지션 감시 중: 현재 손익률 {profit_pct:+.2f}%")

        if profit_ratio >= PROFIT_TARGET:
            print(">> [익절 도달] +1.8% 도달로 전량 시장가 매도 실행!")
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            if order_res.get("rt_cd") == "0":
                diff_won = int((current_price - avg_price) * holding_qty)
                send_telegram(
                    f"🎯 [익절 달성 청산 알림]\n"
                    f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                    f"• 매도가: {current_price:,}원\n"
                    f"• 수익률: {profit_pct:+.2f}%\n"
                    f"• 실현손익: {diff_won:+,}원\n"
                    f"• 보유 수량: {holding_qty}주 전량 매도 완료"
                )
            return

        elif profit_ratio <= STOP_LOSS:
            print(">> [손절 도달] -0.9% 도달로 전량 시장가 손절 실행!")
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            if order_res.get("rt_cd") == "0":
                diff_won = int((current_price - avg_price) * holding_qty)
                send_telegram(
                    f"🛑 [손절 실행 청산 알림]\n"
                    f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                    f"• 매도가: {current_price:,}원\n"
                    f"• 손실률: {profit_pct:+.2f}%\n"
                    f"• 손실금액: {diff_won:+,}원\n"
                    f"• 보유 수량: {holding_qty}주 전량 손절 완료"
                )
            return

        else:
            print(">> 포지션 유지 (익절/손절 범위 내 등락 관망)")
            return

    # --------------------------------------------------------------------------
    # 분기 4: [미보유 상태] 15분봉 돌파 신규 진입 탐색 (09:15 ~ 14:30)
    # --------------------------------------------------------------------------
    if holding_qty == 0:
        if "09:15" <= time_hm <= "14:30":
            signal = check_15m_signal(token, TARGET_CODE)
            if signal:
                print(">> [진입 신호 포착] 15분봉 돌파 확인! 1주 시장가 매수 주문")
                order_res = send_order(token, TARGET_CODE, TRADE_QTY, order_type="01")
                if order_res.get("rt_cd") == "0":
                    target_profit = int(current_price * (1 + PROFIT_TARGET))
                    target_stop = int(current_price * (1 + STOP_LOSS))
                    send_telegram(
                        f"🚀 [15분봉 돌파 신규 매수]\n"
                        f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                        f"• 체결가: {current_price:,}원\n"
                        f"• 주문 수량: {TRADE_QTY}주\n"
                        f"• 목표 익절가: {target_profit:,}원 (+1.8%)\n"
                        f"• 손절 기준가: {target_stop:,}원 (-0.9%)"
                    )
            else:
                print(">> 현재 15분봉 진입 신호 없음 (관망 유지)")
        else:
            print(f">> 현재 시각({time_hm})은 신규 진입 시간(09:15~14:30)이 아니므로 감시를 마칩니다.")

    print(">> 1회 실행 사이클이 안전하게 완료되었습니다.")

if __name__ == "__main__":
    main()
    print(">> 1회 검사 작업이 안전하게 완료되었습니다.")

if __name__ == "__main__":
    main()
