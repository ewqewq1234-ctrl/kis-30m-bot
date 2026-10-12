import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta
import pytz

# ... (환경 변수, 글로벌 설정, 유틸리티 함수 및 KIS API 통신 함수는 기존과 동일하게 유지) ...

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

# ==============================================================================
# 5. 공격형 BAA 전략 (상세 로깅 및 강제 실행 기능 추가)
# ==============================================================================
def run_baa(token: str, holdings: dict, total_asset: int):
    now = datetime.now(KST)
    time_str = now.strftime("%H:%M")
    
    # 환경변수에서 FORCE_BAA 플래그 읽기 (기본값 N)
    force_baa = os.getenv("FORCE_BAA", "N").upper()

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

    # 4. 공격 & 수비 자산 점검
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
                print("    -> [탈락] 이평선 필터 미달")
                
        if scores:
            target_code = max(scores, key=scores.get)
            target_name = ATTACK_UNIVERSE[target_code]
            print(f">> 📈 최종 공격 타깃 선정: {target_name} (스코어: {scores[target_code]:.2f})")
        else:
            print(">> 🚨 모든 공격 자산이 이평선 필터를 탈락하여 수비 모드로 전환합니다.")
            is_defense = True
            
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

    # FORCE_BAA가 켜져 있으면 텔레그램으로도 진단 결과를 전송
    if force_baa == "Y":
        send_telegram(
            f"🛠️ <b>[BAA 강제 실행 진단 결과]</b>\n"
            f"▪ 카나리아 상태: {'🚨 붕괴 (수비전환)' if is_defense else '✅ 안전 (공격유지)'}\n"
            f"▪ 최종 타깃 자산: {target_name} ({target_code})"
        )

    # 실제 매매 로직
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

# ... (main 함수 등 나머지 코드 유지) ...
