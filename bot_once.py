import os
import sys
import json
import requests
from datetime import datetime

# ==============================================================================
# 1. 환경변수 로드 및 설정
# ==============================================================================
KIS_URL = os.getenv("KIS_URL", "https://openapi.koreainvestment.com:9443").rstrip("/")
KIS_APP_KEY = os.getenv("KIS_APP_KEY")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_CANO = os.getenv("KIS_CANO")
KIS_ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD", "01")

# 텔레그램 설정값 (GitHub Secrets 연동)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_CODE = "069500"  # KODEX 200
TRADE_QTY = 1           # 1회 진입 수량 (1주)
PROFIT_TARGET = 0.018   # 익절 기준: +1.8%
STOP_LOSS = -0.009      # 손절 기준: -0.9%

# ==============================================================================
# 2. 텔레그램 메시지 발송 함수
# ==============================================================================
def send_telegram(message: str):
    """
    텔레그램 메시지 발송 함수.
    토큰이나 Chat ID가 없거나 발송 중 네트워크 오류가 나도 매매 스크립트는 중단되지 않고 정상 진행됩니다.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 토큰 또는 Chat ID가 설정되지 않아 알림 발송을 건너뜁니다.")
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
# 3. KIS API 통신 헬퍼 함수
# ==============================================================================
def get_access_token():
    """OAuth2 접근 토큰 발급"""
    url = f"{KIS_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET
    }
    res = requests.post(url, json=body, timeout=10)
    data = res.json()
    if "access_token" in data:
        return data["access_token"]
    else:
        raise Exception(f"토큰 발급 실패: {json.dumps(data, ensure_ascii=False)}")

def get_current_price(token, code):
    """현재가 조회"""
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
        return int(data["output"]["stck_prpr"])
    else:
        raise Exception(f"현재가 조회 실패: {data.get('msg1')}")

def get_balance(token):
    """계좌 잔고 및 예수금 조회 (보유수량, 평균매입가, 예수금 반환)"""
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
    avg_price = 0
    deposit = 0

    if data.get("rt_cd") == "0":
        # 보유 종목 탐색
        for stock in data.get("output1", []):
            if stock.get("pdno") == TARGET_CODE:
                holding_qty = int(stock.get("hldg_qty", 0))
                avg_price = float(stock.get("pchs_avg_pric", 0))
                break
        
        # 총 예수금 파악
        output2 = data.get("output2", [{}])
        if output2:
            deposit = int(output2[0].get("dnca_tot_amt", 0))
            
        return holding_qty, avg_price, deposit
    else:
        raise Exception(f"잔고 조회 실패: {data.get('msg1')}")

def send_order(token, code, qty, order_type="01"):
    """
    주식 주문 전송 (시장가 기본)
    order_type: "01" (매수), "02" (매도)
    """
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

def check_entry_signal(token, code):
    """
    30분봉 데이터 기반 진입 신호 판별
    (실제 시장에서 직전 봉 대비 상승 돌파 여부 확인)
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
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y"
    }
    try:
        res = requests.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        output2 = data.get("output2", [])
        if len(output2) >= 2:
            prev_close = float(output2[1].get("stck_prpr", 0))
            curr_price = float(output2[0].get("stck_prpr", 0))
            return curr_price > prev_close  # 직전 30분봉 대비 상승 시 진입 신호
    except Exception as e:
        print(f">> 30분봉 신호 분석 예외: {e}")
    
    return False

# ==============================================================================
# 4. 메인 실행 흐름 (30분봉 1회 검사)
# ==============================================================================
def main():
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_hm = now.strftime("%H:%M")
    
    print(f"=== [트레이딩 봇 1회 검사 시작: {now_str}] ===")

    # 1) 필수 키 점검
    if not all([KIS_APP_KEY, KIS_APP_SECRET, KIS_CANO]):
        print("[에러] KIS 인증 정보(AppKey, Secret, CANO)가 누락되었습니다.")
        sys.exit(1)

    # 2) 토큰 발급
    token = get_access_token()
    print(">> 접근 토큰 발급 성공")

    # 3) 잔고 및 현재가 조회
    holding_qty, avg_price, deposit = get_balance(token)
    current_price = get_current_price(token, TARGET_CODE)
    
    print(f">> 대상 종목: KODEX 200 ({TARGET_CODE}) | 현재가: {current_price:,}원")
    print(f">> 보유 수량: {holding_qty}주 | 매입 평균가: {avg_price:,.0f}원 | 예수금: {deposit:,}원")

    # --------------------------------------------------------------------------
    # 케이스 A: 15:15 장 마감 전량 청산 (당일 오버나잇 방지)
    # --------------------------------------------------------------------------
    if time_hm >= "15:10":
        print(">> [15:15 장 마감 점검] 당일 전량 청산 및 마감 리포트를 준비합니다.")
        
        realized_msg = "금일 매매 없음"
        if holding_qty > 0:
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            ret_code = order_res.get("rt_cd")
            if ret_code == "0":
                profit_pct = ((current_price - avg_price) / avg_price) * 100
                diff_won = int((current_price - avg_price) * holding_qty)
                realized_msg = f"{profit_pct:+.2f}% ({diff_won:+,}원)"
                print(f">> 장 마감 시장가 전량 매도 완료: {holding_qty}주 체결")
            else:
                print(f">> 장 마감 매도 실패: {order_res.get('msg1')}")

        # 텔레그램 일일 장 마감 리포트 전송
        closing_report = (
            f"📊 [KIS 30분봉 봇 - 장 마감 결산]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 실행 일시: {now_str}\n"
            f"• 대상 종목: KODEX 200 ({TARGET_CODE})\n"
            f"• 종가: {current_price:,}원\n"
            f"• 장 마감 결과: {realized_msg}\n"
            f"• 계좌 예수금: {deposit:,}원\n"
            f"• 잔여 수량: 0주 (전량 현금화 완료)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"오늘 하루 매매가 안전하게 마감되었습니다."
        )
        send_telegram(closing_report)
        print(">> 1회 검사 작업이 안전하게 완료되었습니다.")
        return

    # --------------------------------------------------------------------------
    # 케이스 B: 기존 포지션 보유 중 (익절 / 손절 감시)
    # --------------------------------------------------------------------------
    if holding_qty > 0 and avg_price > 0:
        profit_ratio = (current_price - avg_price) / avg_price
        profit_pct = profit_ratio * 100
        print(f">> 포지션 감시 중: 수익률 {profit_pct:+.2f}% (목표: +1.8% / 손절: -0.9%)")

        # 익절(+1.8%) 도달
        if profit_ratio >= PROFIT_TARGET:
            print(">> [익절 신호] 목표 수익률(+1.8%) 도달. 전량 시장가 매도 실행!")
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            if order_res.get("rt_cd") == "0":
                diff_won = int((current_price - avg_price) * holding_qty)
                msg = (
                    f"🎯 [익절 달성 청산 알림]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                    f"• 매도가: {current_price:,}원\n"
                    f"• 수익률: {profit_pct:+.2f}%\n"
                    f"• 실현손익: {diff_won:+,}원\n"
                    f"• 수량: {holding_qty}주 전량 매도 완료"
                )
                send_telegram(msg)
            else:
                print(f">> 매도 주문 실패: {order_res.get('msg1')}")
            return

        # 손절(-0.9%) 도달
        elif profit_ratio <= STOP_LOSS:
            print(">> [손절 신호] 손절선(-0.9%) 도달. 전량 시장가 매도 실행!")
            order_res = send_order(token, TARGET_CODE, holding_qty, order_type="02")
            if order_res.get("rt_cd") == "0":
                diff_won = int((current_price - avg_price) * holding_qty)
                msg = (
                    f"🛑 [손절 실행 청산 알림]\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                    f"• 매도가: {current_price:,}원\n"
                    f"• 손실률: {profit_pct:+.2f}%\n"
                    f"• 손실금액: {diff_won:+,}원\n"
                    f"• 수량: {holding_qty}주 전량 손절 완료"
                )
                send_telegram(msg)
            else:
                print(f">> 매도 주문 실패: {order_res.get('msg1')}")
            return

        else:
            print(">> 포지션 유지 (익절/손절 범위 내 등락)")
            return

    # --------------------------------------------------------------------------
    # 케이스 C: 미보유 상태 (신규 진입 여부 탐색)
    # --------------------------------------------------------------------------
    if holding_qty == 0:
        # 신규 진입은 09:30 ~ 14:30 사이에만 허용
        if "09:30" <= time_hm <= "14:30":
            signal = check_entry_signal(token, TARGET_CODE)
            if signal:
                print(">> [진입 신호 포착] 직전 30분봉 돌파 확인! 1주 시장가 매수 주문")
                order_res = send_order(token, TARGET_CODE, TRADE_QTY, order_type="01")
                if order_res.get("rt_cd") == "0":
                    target_profit = int(current_price * (1 + PROFIT_TARGET))
                    target_stop = int(current_price * (1 + STOP_LOSS))
                    msg = (
                        f"🚀 [신규 매수 체결 알림]\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• 종목: KODEX 200 ({TARGET_CODE})\n"
                        f"• 매수가: {current_price:,}원\n"
                        f"• 매수 수량: {TRADE_QTY}주\n"
                        f"• 목표 익절가: {target_profit:,}원 (+1.8%)\n"
                        f"• 손절 기준가: {target_stop:,}원 (-0.9%)\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"자동 포지션 감시를 시작합니다."
                    )
                    send_telegram(msg)
                else:
                    print(f">> 매수 주문 실패: {order_res.get('msg1')}")
            else:
                print(">> 현재 진입 신호 없음 (관망 유지)")
        else:
            print(f">> 현재 시간({time_hm})은 신규 진입 허용 시간(09:30~14:30)이 아니므로 감시를 종료합니다.")

    print(">> 1회 검사 작업이 안전하게 완료되었습니다.")

if __name__ == "__main__":
    main()
