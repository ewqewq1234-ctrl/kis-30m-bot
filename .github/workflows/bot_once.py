import os
import sys
import json
import time
from datetime import datetime
import requests
from dotenv import load_dotenv

# .env 파일 로드 (로컬 테스트용, GitHub Actions에서는 Secrets 환경변수 자동 적용)
load_dotenv()

BASE_URL = os.getenv("KIS_URL", "https://openapivts.koreainvestment.com:29443")
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
CANO = os.getenv("KIS_CANO")
ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD", "01")
IS_VIRTUAL = "openapivts" in BASE_URL

# 종목 코드 정의
KODEX_200 = "069500"      # 코스피 200 ETF
KODEX_INVERSE = "114800"  # 인버스 ETF

# 백테스팅 최적 파라미터
TARGET_PROFIT = 0.018     # 익절 +1.8%
STOP_LOSS = -0.009        # 손절 -0.9%
TRADE_QTY = 1             # 1회 주문 수량 (모의투자 테스트 1주)


def get_access_token():
    """OAuth 2.0 접근 토큰 발급"""
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
    if res.status_code == 200:
        data = res.json()
        return f"{data.get('token_type', 'Bearer')} {data.get('access_token')}"
    raise Exception(f"토큰 발급 실패: {res.text}")


def get_balance_and_position(token):
    """현재 계좌의 보유 종목 및 수익률 확인"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    tr_id = "VTTC8434R" if IS_VIRTUAL else "TTTC8434R"
    headers = {
        "content-type": "application/json",
        "authorization": token,
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    time.sleep(0.3)
    if res.status_code == 200:
        holdings = res.json().get("output1", [])
        for h in holdings:
            ticker = h.get("pdno")
            qty = int(h.get("hldg_qty", 0))
            if ticker in [KODEX_200, KODEX_INVERSE] and qty > 0:
                profit_rt = float(h.get("evlu_erng_rt", 0)) / 100.0
                return ticker, qty, profit_rt
    return None, 0, 0.0


def get_30m_candles(token, ticker):
    """당일 분봉 데이터에서 30분 단위 종가 수집 (20이평선 계산용)"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers = {
        "content-type": "application/json",
        "authorization": token,
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010200"
    }
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
        "FID_INPUT_HOUR_1": datetime.now().strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y"
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    time.sleep(0.3)
    if res.status_code == 200:
        items = res.json().get("output2", [])
        closes = []
        for item in items:
            t = item.get("stck_cntg_hour", "")
            if len(t) >= 4 and t[2:4] in ["00", "30"]:
                price = float(item.get("stck_prpr", 0))
                if price > 0:
                    closes.append(price)
        closes.reverse()
        return closes[-25:]
    return []


def send_order(token, ticker, qty, order_type="buy"):
    """시장가 매수/매도 주문 전송"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    if order_type == "buy":
        tr_id = "VTTC0802U" if IS_VIRTUAL else "TTTC0802U"
    else:
        tr_id = "VTTC0801U" if IS_VIRTUAL else "TTTC0801U"

    headers = {
        "content-type": "application/json",
        "authorization": token,
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_DVSN": "01",  # 01: 시장가
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": "0"
    }
    res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
    time.sleep(0.5)
    if res.status_code == 200:
        data = res.json()
        if data.get("rt_cd") == "0":
            action = "매수" if order_type == "buy" else "매도"
            print(f">> [주문 성공] {ticker} {qty}주 {action} 접수 완료")
            return True
        print(f">> [주문 거절] {data.get('msg1')}")
    else:
        print(f">> [주문 API 오류] {res.text}")
    return False


def main():
    now = datetime.now()
    cur_hm = now.strftime("%H%M")
    weekday = now.weekday()
    print(f"==================================================")
    print(f"[{'모의투자' if IS_VIRTUAL else '실전'}] 트레이딩 봇 1회 검사 시작: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"==================================================")

    # 1. 증권사 API 토큰 발급
    token = get_access_token()
    print(">> 접근 토큰 발급 성공")

    # 2. 계좌 잔고 및 보유 종목 확인
    hold_ticker, hold_qty, profit_rate = get_balance_and_position(token)

    # 3. 15:15 이후 장 마감 강제 전량 청산 (오버나잇 방지)
    if cur_hm >= "1515":
        if hold_ticker and hold_qty > 0:
            print(f">> [15:15 장마감 청산] {hold_ticker} {hold_qty}주 전량 시장가 매도 집행")
            send_order(token, hold_ticker, hold_qty, order_type="sell")
        else:
            print(">> 보유 포지션 없음. 당일 현금 100% 정상 마감.")
        return

    # 4. 포지션 보유 중인 경우: 실시간 손익비 체크
    if hold_ticker and hold_qty > 0:
        print(f">> 보유 종목: {hold_ticker} | 보유 수량: {hold_qty}주 | 현재 수익률: {profit_rate*100:+.2f}%")
        if profit_rate >= TARGET_PROFIT:
            print(f">> [목표 달성 익절] +{profit_rate*100:.2f}% (기준: +1.8%) 전량 청산")
            send_order(token, hold_ticker, hold_qty, order_type="sell")
        elif profit_rate <= STOP_LOSS:
            print(f">> [손절선 이탈] {profit_rate*100:.2f}% (기준: -0.9%) 전량 손절")
            send_order(token, hold_ticker, hold_qty, order_type="sell")
        else:
            print(">> 손익 기준선 유지 중. 포지션 홀딩.")
        return

    # 5. 미보유 상태: 30분봉 신호 판별 후 신규 진입 (09:30 ~ 14:30)
    if weekday < 5 and "0930" <= cur_hm < "1430":
        candles = get_30m_candles(token, KODEX_200)
        if len(candles) < 20:
            print(">> 30분봉 데이터 수량 부족 (20개 미만). 이번 회차는 대기합니다.")
            return

        cur_price = candles[-1]
        ma20 = sum(candles[-20:]) / 20.0
        print(f">> [30분봉 분석] KODEX 200 종가: {cur_price:,.0f}원 | 20이평선: {ma20:,.0f}원")

        if cur_price > ma20:
            print(">> [매수 신호 감지] 종가 > 20이평선 ➔ KODEX 200 매수 집행")
            send_order(token, KODEX_200, TRADE_QTY, order_type="buy")
        elif cur_price < ma20:
            print(">> [헷지 신호 감지] 종가 < 20이평선 ➔ KODEX 인버스 매수 집행")
            send_order(token, KODEX_INVERSE, TRADE_QTY, order_type="buy")
    else:
        print(">> 현재는 신규 진입 허용 시간(평일 09:30~14:30)이 아니므로 시세 감시만 마치고 종료합니다.")


if __name__ == "__main__":
    try:
        main()
        print(">> 1회 검사 작업이 안전하게 완료되었습니다.")
        sys.exit(0)
    except Exception as e:
        print(f">> [실행 중 예외 발생] {e}")
        sys.exit(1)
