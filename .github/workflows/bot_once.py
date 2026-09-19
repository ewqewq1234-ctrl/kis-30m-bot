import os
import sys
import json
import time
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("KIS_URL", "https://openapivts.koreainvestment.com:29443")
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
CANO = os.getenv("KIS_CANO")
ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD", "01")
IS_VIRTUAL = "openapivts" in BASE_URL

KODEX_200 = "069500"
KODEX_INVERSE = "114800"
TARGET_PROFIT = 0.018   # 익절 +1.8%
STOP_LOSS = -0.009      # 손절 -0.9%
TRADE_QTY = 1


def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    if res.status_code == 200:
        data = res.json()
        return f"{data.get('token_type', 'Bearer')} {data.get('access_token')}"
    raise Exception(f"토큰 발급 실패: {res.text}")


def get_balance_and_position(token):
    """현재 계좌의 보유 종목 및 평가 수익률 확인"""
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
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
        "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params)
    time.sleep(0.5)
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
    """당일 분봉 데이터에서 최근 30분 단위 종가 수집"""
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
    res = requests.get(url, headers=headers, params=params)
    time.sleep(0.5)
    if res.status_code == 200:
        items = res.json().get("output2", [])
        closes = []
        for item in items:
            t = item.get("stck_cntg_hour", "")
            if len(t) >= 4 and t[2:4] in ["00", "30"]:
                p = float(item.get("stck_prpr", 0))
                if p > 0:
                    closes.append(p)
        closes.reverse()
        return closes[-25:]
    return []


def send_order(token, ticker, qty, order_type="buy"):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    tr_id = "VTTC0802U" if (IS_VIRTUAL and order_type == "buy") else \
            "VTTC0801U" if (IS_VIRTUAL and order_type == "sell") else \
            "TTTC0802U" if order_type == "buy" else "TTTC0801U"

    headers = {
        "content-type": "application/json",
        "authorization": token,
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    body = {
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": ticker, "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0"
    }
    res = requests.post(url, headers=headers, data=json.dumps(body))
    time.sleep(0.5)
    if res.status_code == 200 and res.json().get("rt_cd") == "0":
        print(f"[주문 완료] {ticker} {qty}주 {'매수' if order_type=='buy' else '매도'} 체결")
        return True
    print(f"[주문 실패] {res.text}")
    return False


def main():
    now = datetime.now()
    cur_hm = now.strftime("%H%M")
    print(f"=== [트레이딩 봇 1회 검사 시작: {now.strftime('%Y-%m-%d %H:%M:%S')}] ===")

    token = get_access_token()
    hold_ticker, hold_qty, profit_rate = get_balance_and_position(token)

    # 1. 15:15 이후 장 마감 강제 청산 (오버나잇 배제)
    if cur_hm >= "1515":
        if hold_ticker:
            print(f">> [15:15 장마감 청산] {hold_ticker} {hold_qty}주 전량 매도")
            send_order(token, hold_ticker, hold_qty, "sell")
        else:
            print(">> 보유 중인 포지션 없음. 현금 100% 장 마감 확인.")
        return

    # 2. 보유 포지션이 있을 경우 익절/손절 체크
    if hold_ticker:
        print(f">> [{hold_ticker}] {hold_qty}주 보유 중 (현재 수익률: {profit_rate*100:+.2f}%)")
        if profit_rate >= TARGET_PROFIT:
            print(">> [목표가 달성] +1.8% 익절 청산 집행")
            send_order(token, hold_ticker, hold_qty, "sell")
        elif profit_rate <= STOP_LOSS:
            print(">> [손절선 이탈] -0.9% 스탑로스 청산 집행")
            send_order(token, hold_ticker, hold_qty, "sell")
        else:
            print(">> 손익 구간 유지 중. 포지션 홀딩.")
        return

    # 3. 미보유 상태: 30분봉 신호 판별 후 진입 (09:30 ~ 14:30 사이만 진입 허용)
    if "0930" <= cur_hm < "1430":
        candles = get_30m_candles(token, KODEX_200)
        if len(candles) < 20:
            print(">> 분봉 데이터 수집 부족. 이번 회차를 건너뜁니다.")
            return

        cur_p = candles[-1]
        ma20 = sum(candles[-20:]) / 20.0
        print(f">> [30분봉 분석] 코스피200: {cur_p:,.0f}원 | 20이평선: {ma20:,.0f}원")

        if cur_p > ma20:
            print(">> [매수 신호] 20일선 돌파 ➔ KODEX 200 매수")
            send_order(token, KODEX_200, TRADE_QTY, "buy")
        elif cur_p < ma20:
            print(">> [헷지 신호] 20일선 이탈 ➔ KODEX 인버스 매수")
            send_order(token, KODEX_INVERSE, TRADE_QTY, "buy")
    else:
        print(">> 신규 진입 제한 시간대(14:30 이후)이거나 장 시작 대기 중입니다.")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(f"[실행 오류] {err}")
        sys.exit(1)
