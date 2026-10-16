#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KIS one-shot quant bot, fail-closed execution.

Requirements: requests, pytz; Python 3.11+ recommended.
Default: DRY_RUN=Y (no order POST).
Live requires DRY_RUN=N AND ENABLE_LIVE_ORDERS=YES AND KIS_ENV=real.
State files must persist across executions; do not share an account with other bots.
"""
import os
import sys
import json
import time
import fcntl
import tempfile
import html
import subprocess
import hashlib
import hmac
import base64
import requests
from datetime import datetime, timedelta
from pathlib import Path
import pytz

# ==============================================================================
# 1. 환경 변수 및 글로벌 설정
# ==============================================================================
KST = pytz.timezone("Asia/Seoul")
BASE_DIR = Path(os.getenv("BOT_STATE_DIR", Path(__file__).resolve().parent)).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = BASE_DIR / "token_cache.json"
SECURE_TOKEN_FILE = BASE_DIR / "token_cache.secure"
DAY_STATE_FILE = BASE_DIR / "day_state.json"
BAA_STATE_FILE = BASE_DIR / "baa_state.json"
TRADE_HISTORY_FILE = BASE_DIR / "trade_history.json"
HOLIDAY_CACHE_FILE = BASE_DIR / "holiday_cache.json"
RISK_STATE_FILE = BASE_DIR / "risk_state.json"
LOCK_FILE = BASE_DIR / ".bot.lock"

KIS_ENV = os.getenv("KIS_ENV", "real").strip().lower()
DEFAULT_URL = "https://openapi.koreainvestment.com:9443" if KIS_ENV == "real" else "https://openapivts.koreainvestment.com:29443"
KIS_URL = os.getenv("KIS_URL", DEFAULT_URL).rstrip("/")
KIS_APP_KEY = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_CANO = os.getenv("KIS_CANO", "").strip()
KIS_ACNT_PRDT_CD = (os.getenv("KIS_ACNT_PRDT_CD") or "01").strip().zfill(2)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DRY_RUN = os.getenv("DRY_RUN", "Y").upper() != "N"
ENABLE_LIVE_ORDERS = os.getenv("ENABLE_LIVE_ORDERS", "NO") == "YES"
FORCE_BAA = os.getenv("FORCE_BAA", "N").upper() == "Y"
RUN_MIDDAY_REPORT = os.getenv("RUN_MIDDAY_REPORT", "N").upper() == "Y"
# FORCE_BAA is diagnostic only. It never bypasses market or live-order guards.
ORDER_POLL_SECONDS = 3
MAX_DAILY_LOSS_RATIO = float(os.getenv("MAX_DAILY_LOSS_RATIO", "0.02"))
MARKET_START = "09:00"
MARKET_END = "15:20"  # conservative KRX ordinary-order cut-off; check broker-specific schedule

CODE_BULL = "069500"
PROFIT_TARGET = 0.012
STOP_LOSS = -0.009
PYRAMID_TRIGGER = 0.005       # 기존 코드에서 미구현: 자동 활성화하지 않음
TIME_STOP_MINUTES = 90       # 기존 코드에서 미구현: 자동 활성화하지 않음
DAY_BUDGET_RATIO = 0.30
WEIGHT_BASE = 0.50
WEIGHT_PYRAMID = 0.50

CANARY_UNIVERSE = {
    "360750": "TIGER 미국S&P500", "069500": "KODEX 200",
    "371460": "TIGER 차이나전기차SOLACTIVE", "453850": "ACE 미국종합채권액티브",
}
ATTACK_UNIVERSE = {
    "133690": "TIGER 미국나스닥100", "069500": "KODEX 200",
    "122630": "KODEX 레버리지", "381180": "TIGER 미국필라델피아반도체나스닥",
}
DEFENSE_UNIVERSE = {
    "423160": "KODEX KOFR금리액티브(합성)",
    "449170": "KODEX 미국달러SOFR금리액티브(합성)",
    "453850": "ACE 미국종합채권액티브", "411060": "ACE KRX금현물",
}
BAA_UNIVERSE = set(ATTACK_UNIVERSE) | set(DEFENSE_UNIVERSE)
SESSION = requests.Session()
ACCESS_TOKEN = None
# In-process counter: increments only after durable journal checkpoint, before order POST.
ACCOUNT_ORDER_ATTEMPTS = 0


class TradeSafetyError(RuntimeError):
    pass


def now_kst():
    return datetime.now(KST)


def number(value, default=0):
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return default


def share_quantity(value):
    """Broker/state quantities must be explicit nonnegative integers."""
    text = str(value).strip()
    if not text or not text.isascii() or not text.isdigit():
        fail("보유 수량 형식 오류: 매매 차단")
    try:
        quantity = int(text)
    except ValueError:
        fail("보유 수량 범위 오류: 매매 차단")
    if quantity > 2**63 - 1:
        fail("보유 수량 범위 오류: 매매 차단")
    return quantity


# ==============================================================================
# 2. 유틸리티 및 원자적 상태 저장
# ==============================================================================
def send_telegram(message: str, is_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram 미설정/콘솔출력]\n{message}")
        return
    prefix = "🚨 [비상 경고]\n" if is_alert else ""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": prefix + message, "parse_mode": "HTML"},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException as ex:
        print(f">> ⚠️ 텔레그램 전송 실패: {type(ex).__name__}")


def load_json(filepath, default):
    filepath = Path(filepath)
    if not filepath.exists():
        return json.loads(json.dumps(default))
    try:
        with filepath.open("r", encoding="utf-8") as f:
            result = json.load(f)
        if not isinstance(result, dict):
            raise TradeSafetyError(f"상태 파일이 객체가 아님: {filepath.name}")
        return result
    except (ValueError, OSError) as ex:
        raise TradeSafetyError(f"상태 파일 손상/읽기 실패: {filepath.name}: {ex}") from ex


def durable_checkpoint(filepath):
    """Commit and push critical state BEFORE any subsequent broker order POST.

    The state checkout is a private branch. A failed push blocks execution.
    """
    if os.getenv("GITHUB_STATE_MODE") != "git":
        return
    filepath = Path(filepath).resolve()
    if filepath.name not in {"trade_history.json", "day_state.json", "baa_state.json", "risk_state.json", "holiday_cache.json", "token_cache.secure"}:
        return
    if filepath.parent != BASE_DIR:
        raise TradeSafetyError("상태 파일이 지정된 영구 저장소 밖에 있습니다")
    def git(*args):
        result = subprocess.run(["git", "-C", str(BASE_DIR), *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise TradeSafetyError(f"영구 상태 Git 작업 실패: {args[0]}: {result.stderr[-350:]}")
        return result.stdout
    git("add", "--", filepath.name)
    status = git("status", "--porcelain", "--", filepath.name)
    if status:
        git("-c", "user.name=kis-bot", "-c", "user.email=kis-bot@users.noreply.github.com",
            "commit", "-m", f"checkpoint {filepath.name}", "--", filepath.name)
        git("push", "origin", "HEAD:refs/heads/bot-state")


def save_json(filepath, data):
    filepath = Path(filepath)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", dir=filepath.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, filepath)
        tmp_name = None
        dir_fd = os.open(filepath.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, TypeError, ValueError) as ex:
        raise TradeSafetyError(f"상태 저장 실패: {filepath.name}: {ex}") from ex
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
    durable_checkpoint(filepath)


def fail(message):
    print(f">> 🚨 [안전 중단] {message}")
    send_telegram(html.escape(message), is_alert=True)
    raise TradeSafetyError(message)


def validate_environment():
    if KIS_ENV not in ("real", "demo"):
        fail("KIS_ENV는 real/demo 중 하나여야 합니다.")
    expected = "openapi.koreainvestment.com" if KIS_ENV == "real" else "openapivts.koreainvestment.com"
    if not KIS_URL.startswith(f"https://{expected}:"):
        fail("KIS_ENV와 KIS_URL이 일치하지 않습니다. 공식 HTTPS 주소만 허용합니다.")
    for key, val in (("KIS_APP_KEY", KIS_APP_KEY), ("KIS_APP_SECRET", KIS_APP_SECRET),
                     ("KIS_CANO", KIS_CANO)):
        if not val:
            fail(f"환경변수 {key} 누락")
    if len(KIS_CANO) != 8 or not KIS_CANO.isdigit():
        fail("KIS_CANO는 종합계좌번호 앞 8자리여야 합니다.")
    if not DRY_RUN and (KIS_ENV != "real" or not ENABLE_LIVE_ORDERS):
        fail("실거래는 KIS_ENV=real, DRY_RUN=N, ENABLE_LIVE_ORDERS=YES 모두 필요")
    if FORCE_BAA and not DRY_RUN:
        fail("FORCE_BAA는 DRY_RUN=Y 진단에서만 허용합니다.")
    if not (0 < MAX_DAILY_LOSS_RATIO < 1):
        fail("MAX_DAILY_LOSS_RATIO는 0과 1 사이여야 합니다")


def api_request(method, path, token=None, *, tr_id=None, params=None, body=None,
                timeout=10, retries=1, tr_cont=None):
    """Fail-closed KIS request. Retry only transient GET errors; never POST."""
    global ACCESS_TOKEN
    headers = {"content-type": "application/json; charset=utf-8", "appkey": KIS_APP_KEY,
               "appsecret": KIS_APP_SECRET}
    if token:
        headers["authorization"] = f"Bearer {ACCESS_TOKEN or token}"
    if tr_id:
        headers["tr_id"] = tr_id
    # Official KIS balance example: first page '', subsequent pages 'N'.
    if tr_cont is not None:
        headers["tr_cont"] = tr_cont
    attempts = (retries + 1) if method == "GET" else 1
    for attempt in range(attempts):
        response = None
        try:
            response = SESSION.request(method, KIS_URL + path, headers=headers, params=params,
                                       json=body, timeout=timeout)
            if response.status_code == 401 and method == "GET" and token and attempt + 1 < attempts:
                # Only reauthorize GET; POST order outcome may be uncertain.
                fresh = get_access_token(force=True)
                headers["authorization"] = f"Bearer {fresh}"
                time.sleep(0.5)
                continue
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise TradeSafetyError("API 응답 형식 오류")
            if "rt_cd" in result and result["rt_cd"] != "0":
                raise TradeSafetyError(
                    f"KIS API 거부 [{path}]: {result.get('msg_cd')} {result.get('msg1')}")
            return result, response.headers
        except TradeSafetyError:
            # Business errors are not transient; do not resend.
            raise
        except (requests.RequestException, ValueError) as ex:
            status = (response.status_code if response is not None else None)
            retryable = method == "GET" and (
                status in (429, 500, 502, 503, 504)
                or (status is None and isinstance(ex,
                    (requests.Timeout, requests.ConnectionError)))
            )
            if not retryable or attempt + 1 >= attempts:
                # Avoid printing account number from requests' full URL.
                detail = f"HTTP {status}" if status is not None else type(ex).__name__
                raise TradeSafetyError(f"API 호출 실패 [{path}]: {detail}; "
                                       f"요청 시도 {attempt + 1}회") from ex
            wait = min(4.0, 1.0 * (2 ** attempt))
            if status == 429 and response is not None:
                try:
                    wait = min(10.0, max(wait, float(response.headers.get("Retry-After", 0))))
                except (ValueError, TypeError):
                    pass
            print(f">> [KIS API 일시 오류] {path}, "
                  f"{('HTTP ' + str(status)) if status is not None else type(ex).__name__}; "
                  f"{wait:.1f}초 후 GET 재시도 ({attempt + 1}/{attempts - 1})")
            time.sleep(wait)
    raise TradeSafetyError("알 수 없는 API 호출 실패")

def _token_keys(salt):
    # The existing KIS credentials are the encryption root; no new Secret needed.
    # If these credentials change, the old cache is unreadable and a new token is issued.
    material = (KIS_APP_KEY + "\0" + KIS_APP_SECRET).encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", material, salt, 200_000, dklen=64)
    return derived[:32], derived[32:]


def _openssl_cbc(payload, key, iv, decrypt=False):
    command = ["openssl", "enc", "-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex(), "-nosalt"]
    if decrypt:
        command.append("-d")
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TradeSafetyError(f"토큰 암호화 도구 실행 실패: {type(exc).__name__}") from exc
    if result.returncode:
        raise TradeSafetyError("토큰 암호화/복호화 실패")
    return result.stdout


def _read_secure_token():
    if not SECURE_TOKEN_FILE.exists():
        return {}
    try:
        blob = SECURE_TOKEN_FILE.read_bytes()
        if len(blob) < 4 + 16 + 16 + 32 or blob[:4] != b"KTC1":
            raise TradeSafetyError("암호화 토큰 형식 오류")
        salt, iv, ciphertext, tag = blob[4:20], blob[20:36], blob[36:-32], blob[-32:]
        enc_key, mac_key = _token_keys(salt)
        if not hmac.compare_digest(tag, hmac.new(mac_key, blob[:-32], hashlib.sha256).digest()):
            raise TradeSafetyError("암호화 토큰 인증 실패: 키 변경 또는 데이터 손상")
        result = json.loads(_openssl_cbc(ciphertext, enc_key, iv, decrypt=True).decode("utf-8"))
        if not isinstance(result, dict):
            raise TradeSafetyError("암호화 토큰 내용 형식 오류")
        return result
    except (OSError, ValueError, UnicodeError) as exc:
        raise TradeSafetyError(f"암호화 토큰 읽기 실패: {type(exc).__name__}") from exc


def _save_secure_token(token, expires_at):
    salt, iv = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _token_keys(salt)
    payload = json.dumps({"token": token, "expires_at": expires_at}).encode("utf-8")
    encrypted = _openssl_cbc(payload, enc_key, iv)
    blob = b"KTC1" + salt + iv + encrypted
    blob += hmac.new(mac_key, blob, hashlib.sha256).digest()
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp_token_", dir=BASE_DIR)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, SECURE_TOKEN_FILE)
        tmp_name = None
    except OSError as exc:
        raise TradeSafetyError(f"암호화 토큰 저장 실패: {type(exc).__name__}") from exc
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
    # The encrypted cache must be pushed before the token is used.
    durable_checkpoint(SECURE_TOKEN_FILE)


def get_access_token(force=False):
    global ACCESS_TOKEN
    now_ts = int(time.time())
    cached = _read_secure_token()
    if not force and cached.get("expires_at", 0) > now_ts + 600 and cached.get("token"):
        ACCESS_TOKEN = cached["token"]
        print(">> [KIS 토큰] 암호화 캐시 재사용 (신규 발급 없음)")
        return ACCESS_TOKEN
    result, _ = api_request("POST", "/oauth2/tokenP", body={
        "grant_type": "client_credentials", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
    }, timeout=10)
    token = result.get("access_token")
    if not token:
        fail(f"토큰 발급 실패: {result.get('error_description', result.get('msg1'))}")
    _save_secure_token(token, now_ts + number(result.get("expires_in"), 86400))
    ACCESS_TOKEN = token
    print(">> [KIS 토큰] 신규 발급 후 암호화 상태 저장 완료")
    return token


def is_market_open_today(token: str) -> bool:
    today = now_kst().strftime("%Y%m%d")
    if now_kst().weekday() >= 5:
        return False
    cache = load_json(HOLIDAY_CACHE_FILE, {})
    if cache.get("date") == today and cache.get("is_open") in (True, False):
        return cache["is_open"]
    result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", token,
                            tr_id="CTCA0903R", params={"BASS_DT": today, "CTX_AREA_FK": "", "CTX_AREA_NK": ""})
    entries = result.get("output") or []
    if isinstance(entries, dict):
        entries = [entries]
    for item in entries:
        if str(item.get("bass_dt")) == today and item.get("opnd_yn") in ("Y", "N"):
            is_open = item["opnd_yn"] == "Y"
            save_json(HOLIDAY_CACHE_FILE, {"date": today, "is_open": is_open})
            return is_open
    fail("오늘 휴장 여부를 검증하지 못했습니다. 평일 추정 주문 금지")


def order_window() -> bool:
    return now_kst().weekday() < 5 and MARKET_START <= now_kst().strftime("%H:%M") <= MARKET_END


def check_daily_loss(token: str, allow_sell=False):
    """Session NAV loss guard (deposits/withdrawals require manual rebaseline)."""
    if allow_sell:
        return
    _, _, nav = get_account_balance(token)
    state = load_json(RISK_STATE_FILE, {})
    if state.get("date") != now_kst().strftime("%Y-%m-%d"):
        fail("당일 위험기준 순자산 스냅샷이 없습니다. 매수 차단")
    opening_nav = number(state.get("opening_nav"))
    if opening_nav <= 0:
        fail("일간 손실 기준 순자산 오류")
    loss_ratio = (nav - opening_nav) / opening_nav
    if loss_ratio <= -MAX_DAILY_LOSS_RATIO:
        fail(f"일일 계좌 손실한도 초과 ({loss_ratio*100:.2f}% <= -{MAX_DAILY_LOSS_RATIO*100:.2f}%). 신규 매수 금지")



# ==============================================================================
# 3. KIS API 시세/잔고/주문 집행 + 체결 확인
# ==============================================================================
def get_current_price(token: str, code: str):
    result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", token,
                            tr_id="FHKST01010100", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    price = number((result.get("output") or {}).get("stck_prpr"))
    if price <= 0:
        fail(f"현재가 조회 결과 비정상: {code}")
    return price


def get_account_balance(token: str):
    params = {"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD, "AFHR_FLPR_YN": "N",
              "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
              "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    holdings, summary, seen_codes = {}, None, set()
    for i in range(10):
        result, heads = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", token,
                                    tr_id="TTTC8434R" if KIS_ENV == "real" else "VTTC8434R",
                                    params=params, retries=2, tr_cont="" if i == 0 else "N")
        rows = result.get("output1")
        if not isinstance(rows, list):
            fail("잔고 종목 목록 누락/형식 오류")
        for item in rows:
            if not isinstance(item, dict):
                fail("잔고 종목 행 형식 오류")
            code = item.get("pdno")
            if (not isinstance(code, str) or len(code) != 6
                    or not code.isascii() or not code.isdigit() or code in seen_codes):
                fail("잔고 종목 코드 누락/중복/형식 오류")
            seen_codes.add(code)
            qty = share_quantity(item.get("hldg_qty"))
            if qty > 0:
                holdings[code] = {"name": item.get("prdt_name", code), "qty": qty,
                                  "avg_price": float(item.get("pchs_avg_pric") or 0)}
        values = result.get("output2") or []
        if values and summary is None:
            summary = values[0]
        fk, nk = result.get("ctx_area_fk100", ""), result.get("ctx_area_nk100", "")
        if heads.get("tr_cont") not in ("M", "F"):
            break
        if not (fk or nk):
            fail("잔고 연속 조회 키 없음")
        params.update({"CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk})
        time.sleep(0.15)
    else:
        fail("잔고 페이지 제한 초과")
    if summary is None:
        fail("잔고 요약 데이터 누락")
    deposit = number(summary.get("dnca_tot_amt"))
    tot_eval = number(summary.get("tot_evlu_amt"))
    if deposit < 0 or tot_eval <= 0:
        fail(f"잔고 수치 비정상: 예수금={deposit} 평가액={tot_eval}")
    return holdings, deposit, tot_eval


def get_order_records(token: str, *, day=None):
    """Fetch all daily orders; no assumption that 'accepted' equals 'filled'."""
    day = day or now_kst().strftime("%Y%m%d")
    params = {
        "CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
        "INQR_STRT_DT": day, "INQR_END_DT": day,
        "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
        "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "EXCG_ID_DVSN_CD": "KRX",
    }
    found, seen_pages = [], set()
    for _ in range(10):
        result, heads = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", token,
                                    tr_id="TTTC0081R" if KIS_ENV == "real" else "VTTC0081R", params=params)
        found.extend(result.get("output1") or [])
        fk, nk = result.get("ctx_area_fk100", ""), result.get("ctx_area_nk100", "")
        if heads.get("tr_cont") not in ("M", "F"):
            return found
        key = (fk, nk)
        if not (fk or nk) or key in seen_pages:
            fail("체결조회 연속 조회 키 오류")
        seen_pages.add(key)
        params.update({"CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk})
        time.sleep(0.15)
    fail("체결조회 페이지 제한 초과")


def get_buyable_qty(token: str, code: str, price: int) -> int:
    result, _ = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", token,
                            tr_id="TTTC8908R" if KIS_ENV == "real" else "VTTC8908R",
                            params={"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
                                    "PDNO": code, "ORD_UNPR": str(price), "ORD_DVSN": "01",
                                    "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
    output = result.get("output") or {}
    if not isinstance(output, dict) or "nrcvb_buy_qty" not in output:
        fail(f"{code} 미수없는 매수가능수량 확인 실패")
    return max(0, number(output["nrcvb_buy_qty"]))


def read_journal():
    return load_json(TRADE_HISTORY_FILE, {"orders": []})


def journal_orders():
    return read_journal().get("orders", [])


def update_journal(order_id, **updates):
    journal = read_journal()
    rows = journal.get("orders", [])
    matches = [row for row in rows if row.get("id") == order_id]
    if len(matches) != 1:
        fail(f"주문 저널 키 불일치: {order_id}")
    matches[0].update(updates)
    save_json(TRADE_HISTORY_FILE, journal)


def order_key(code, side, strategy, action_name):
    return f"{now_kst().strftime('%Y%m%d')}-{strategy}-{code}-{side}-{action_name}"


def open_or_uncertain_orders():
    statuses = {"INTENT", "UNCERTAIN", "ACCEPTED", "PARTIAL", "UNKNOWN"}
    return [x for x in journal_orders() if x.get("status") in statuses]


# One-time, explicitly approved legacy position bootstrap. This is a record-only
# migration: it MUST NOT send an order. It is permitted ONLY while DRY_RUN=Y.
# Account snapshot provided by the owner on 2026-10-14: 423160, six shares,
# cost amount KRW 664,707 and displayed average KRW 110,785.
LEGACY_BAA_CODE = "423160"
LEGACY_BAA_QTY = 6
LEGACY_BAA_COST = 664707
LEGACY_BAA_AVG = 110785.0
LEGACY_BAA_ID = "LEGACY-BAA-423160-6-20261014"


def adopt_verified_legacy_baa(token: str, holdings: dict):
    """Permanently journal one verified position; no brokerage order request.

    Initial migration is only allowed in dry-run, with an exact one-position
    account match and no conflicting BAA history. All later runs are no-ops.
    Failure to persist the journal leaves trading stopped (fail closed).
    """
    journal = read_journal()
    entries = journal.get("orders", [])
    if not isinstance(entries, list):
        fail("주문 원장 형식 오류: orders는 리스트여야 합니다")
    legacy = [o for o in entries if isinstance(o, dict) and o.get("id") == LEGACY_BAA_ID]
    if legacy:
        if len(legacy) != 1 or legacy[0].get("status") != "FILLED" or \
                number(legacy[0].get("confirmed_fill_qty")) != LEGACY_BAA_QTY or \
                legacy[0].get("code") != LEGACY_BAA_CODE or legacy[0].get("strategy") != "BAA":
            fail("기존 보유분 인수 원장이 변조되었거나 중복되었습니다")
        return False
    # Do not create a new owner record in live mode under any circumstances.
    if not DRY_RUN:
        return False
    # Restrict the one-time bootstrap to accounts whose only holding is exactly
    # the owner-approved position. Never infer ownership of unrelated assets.
    if set(holdings) != {LEGACY_BAA_CODE}:
        return False
    owned = holdings.get(LEGACY_BAA_CODE, {})
    if number(owned.get("qty")) != LEGACY_BAA_QTY:
        fail("기존 보유분 인수 중단: 423160 수량이 승인된 6주와 다릅니다")
    try:
        avg = float(owned.get("avg_price"))
    except (TypeError, ValueError):
        fail("기존 보유분 인수 중단: 매입단가 조회 오류")
    if not (LEGACY_BAA_AVG - 5 <= avg <= LEGACY_BAA_AVG + 5):
        fail("기존 보유분 인수 중단: 기존 매입단가가 승인된 내역과 다릅니다")
    if any(not isinstance(o, dict) for o in entries):
        fail("주문 원장 구조 오류: 이전 기록 검토 필요")
    if any(o.get("strategy") == "BAA" for o in entries):
        fail("기존 BAA 주문 기록이 있어 자동 인수를 중단합니다")
    if open_or_uncertain_orders():
        fail("미확정 주문이 있어 기존 보유분 인수를 중단합니다")
    state = load_json(BAA_STATE_FILE, {})
    if state.get("target_code") not in (None, "") or number(state.get("owned_qty")):
        fail("기존 BAA 상태가 있어 자동 인수를 중단합니다")
    # main() already calls assert_no_pending(); repeat the broker-side check as
    # a defense-in-depth guard for the migration itself.
    assert_no_pending(token)
    # Append a synthetic FILLED record so rebuild_strategy_state() obtains the
    # correct owned_qty. Its source field distinguishes it from a broker trade.
    entries.append({
        "id": LEGACY_BAA_ID,
        "strategy": "BAA", "code": LEGACY_BAA_CODE, "side": "BUY",
        "requested_qty": LEGACY_BAA_QTY,
        "confirmed_fill_qty": LEGACY_BAA_QTY,
        "confirmed_fill_amt": LEGACY_BAA_COST,
        "status": "FILLED", "source": "LEGACY_POSITION_ADOPTION_NO_ORDER",
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "odno": None, "sell_cost_basis": LEGACY_BAA_AVG,
    })
    save_json(TRADE_HISTORY_FILE, journal)  # Commit BEFORE derived state.
    rebuild_strategy_state()
    print(">> ✅ [BAA 기존 보유 인수] 423160 6주 원장 등록 완료 (주문 전송 없음)")
    return True


def rebuild_strategy_state():
    """Rebuild position ownership from the journal (source of truth).

    Thus, a crash between saving a fill and saving strategy state does not
    double-apply it on the next run. Any pre-existing legacy position that was
    not recorded in this journal is intentionally NOT adopted.
    """
    entries = read_journal().get("orders", [])
    day = load_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False, "owned_qty": 0})
    baa = load_json(BAA_STATE_FILE, {"last_executed_date": None, "reinvest_pool": 0,
                                     "target_code": None, "owned_qty": 0})
    day_qty, entry_time = 0, None
    baa_net = {}
    realized = 0.0
    for order in entries:
        filled = number(order.get("confirmed_fill_qty"))
        if filled < 0 or filled > number(order.get("requested_qty")):
            fail("주문 저널의 체결 수량이 잘못되었습니다")
        if not filled:
            continue
        delta = filled if order["side"] == "BUY" else -filled
        if order.get("strategy") == "DAY":
            day_qty += delta
            if day_qty < 0:
                fail("단타 추적 수량이 음수입니다. 체결 저널 수동 검증 필요")
            if delta > 0 and not entry_time:
                entry_time = order.get("created_at")
            if day_qty == 0:
                entry_time = None
            if delta < 0:
                realized += number(order.get("confirmed_fill_amt")) - filled * float(order.get("sell_cost_basis") or 0)
        elif order.get("strategy") == "BAA":
            code = order["code"]
            baa_net[code] = baa_net.get(code, 0) + delta
            if baa_net[code] < 0:
                fail(f"BAA {code} 추적 수량 음수")
    positive = [(c, q) for c, q in baa_net.items() if q > 0]
    if len(positive) > 1:
        fail("BAA 종목이 복수로 남아 있습니다. 자동 리밸런싱 중단")
    new_baa_code, new_baa_qty = positive[0] if positive else (None, 0)
    if not entries and (number(day.get("owned_qty")) or number(baa.get("owned_qty"))):
        fail("과거 보유 상태만 존재하고 체결 저널이 없음. 자동 이전 불가")
    day.update({"owned_qty": day_qty, "entry_time": entry_time,
                "pyramided": bool(day.get("pyramided", False))})
    if day_qty == 0:
        day["pyramided"] = False
    baa.update({"target_code": new_baa_code, "owned_qty": new_baa_qty,
                "realized_daytrade_pnl": round(realized, 2)})
    # Preserve legacy reinvest_pool only as a report field; never double-count it in NAV.
    save_json(DAY_STATE_FILE, day)
    save_json(BAA_STATE_FILE, baa)


def reconcile_orders(token: str):
    # Rebuild first even if no unresolved orders: recovery from a previous crash.
    rebuild_strategy_state()
    orders = journal_orders()
    pending = [o for o in orders if o.get("status") not in ("FILLED", "REJECTED", "CANCELED")]
    if not pending:
        return
    days = sorted(set(o.get("created_at", "")[:10].replace("-", "") for o in pending))
    if any(day != now_kst().strftime("%Y%m%d") for day in days):
        fail("전일 이전의 미확정 주문이 있습니다. 원장 대조 후 수동 정리 필요")
    rows = get_order_records(token)
    for entry in pending:
        odno = str(entry.get("odno") or "")
        if not odno:
            # Unknown POST outcomes cannot be retried safely.
            continue
        matching = [r for r in rows if str(r.get("odno", "")).lstrip("0") == odno.lstrip("0")
                    and str(r.get("pdno", "")) == entry["code"]]
        if len(matching) != 1:
            continue  # Query may be delayed; stay pending.
        row = matching[0]
        qty = number(row.get("ord_qty"))
        filled = number(row.get("tot_ccld_qty"))
        amount = number(row.get("tot_ccld_amt"))
        canceled = number(row.get("cncl_cfrm_qty"))
        # Broker-confirmed cancellation closes the remaining quantity; any
        # already-filled shares still belong in the position journal.
        if (qty <= 0 or filled < 0 or canceled < 0 or filled + canceled > qty
                or qty != number(entry["requested_qty"])):
            fail(f"주문조회 수량 불일치: {odno}")
        if filled > 0 and amount <= 0:
            fail(f"체결금액 누락: {odno}")
        if filled < number(entry.get("confirmed_fill_qty")):
            fail(f"체결 수량 역행: {odno}")
        if filled == qty:
            status = "FILLED"
        elif canceled > 0 and filled + canceled == qty:
            status = "CANCELED"
        else:
            status = "PARTIAL" if filled > 0 else "ACCEPTED"
        # The journal is committed BEFORE derived state. Recovery is idempotent.
        update_journal(entry["id"], status=status, confirmed_fill_qty=filled,
                       confirmed_fill_amt=amount)
    rebuild_strategy_state()


def assert_no_pending(token):
    reconcile_orders(token)
    unresolved = open_or_uncertain_orders()
    if unresolved:
        ids = ", ".join(f"{x['id']}:{x['status']}" for x in unresolved[:3])
        fail(f"미확정/미체결 주문 {len(unresolved)}건 존재 ({ids}). 자동 재주문 차단")
    # Prevent bot competing with any other KRX order in this account.
    for row in get_order_records(token):
        qty = number(row.get("ord_qty"))
        filled = number(row.get("tot_ccld_qty"))
        canceled = number(row.get("cncl_cfrm_qty"))
        if qty > 0 and filled + canceled < qty:
            fail("계좌에 미체결 주문이 존재합니다. 자동 신규주문 중지")


def execute_order(token: str, code: str, qty: int, order_type="02", ord_dvsn="01",
                  price=0, action_name="주문", strategy="BAA", cost_basis=0.0):
    """Return (fully_filled, confirmed_qty), never requested/accepted quantity."""
    if qty <= 0:
        return False, 0
    if order_type not in ("01", "02") or strategy not in ("DAY", "BAA"):
        fail("잘못된 주문 방향/전략")
    if ord_dvsn != "01":
        fail("이 버전은 시장가 주문만 승인합니다")
    if not order_window() or not is_market_open_today(token):
        fail("정규장 시간 외 또는 휴장일에 주문 차단")
    assert_no_pending(token)
    side = "BUY" if order_type == "02" else "SELL"
    if side == "BUY":
        check_daily_loss(token)
        current = get_current_price(token, code)
        allowed = get_buyable_qty(token, code, current)
        if qty > allowed:
            fail(f"매수가능 {allowed}주 < 요청 {qty}주: 수량 자동변경 없이 주문 차단")
        if code == CODE_BULL:
            # Refresh AFTER price/buying-power reads, immediately before intent.
            if strategy != "DAY":
                fail("069500 매수는 DAY 전략만 허용")
            holdings, _, _ = get_account_balance(token)
            actual_qty = share_quantity(holdings.get(code, {}).get("qty", 0))
            day = load_json(DAY_STATE_FILE, {})
            baa = load_json(BAA_STATE_FILE, {})
            if (actual_qty != share_quantity(day.get("owned_qty", 0))
                    or (baa.get("target_code") == code
                        and share_quantity(baa.get("owned_qty", 0)) > 0)):
                fail("069500 주문 직전 잔고/전략 원장 불일치")
            if actual_qty + qty > 1:
                fail("069500 계좌 총보유량 1주 상한 초과: 매수 차단")
    else:
        holdings, _, _ = get_account_balance(token)
        if holdings.get(code, {}).get("qty", 0) < qty:
            fail(f"보유보다 많은 매도 차단 {code}:{qty}주")
    key = order_key(code, side, strategy, action_name)
    if any(o.get("id") == key for o in journal_orders()):
        fail(f"당일 동일 주문 의도 재전송 차단: {key}")
    if DRY_RUN:
        print(f">> 🧪 [DRY_RUN {action_name}] {side} {code} {qty}주 (주문 POST 없음)")
        return False, 0
    # Persist intent before POST. No retry if outcome is unknown.
    journal = read_journal()
    order = {"id": key, "strategy": strategy, "code": code, "side": side,
             "requested_qty": qty, "status": "INTENT", "created_at": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
             "odno": None, "applied_fill_qty": 0, "applied_fill_amount": 0,
             "sell_cost_basis": float(cost_basis)}
    journal.setdefault("orders", []).append(order)
    save_json(TRADE_HISTORY_FILE, journal)
    body = {"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD, "PDNO": code,
            "ORD_DVSN": ord_dvsn, "ORD_QTY": str(qty), "ORD_UNPR": "0",
            "EXCG_ID_DVSN_CD": "KRX"}
    tr_id = ("TTTC0012U" if side == "BUY" else "TTTC0011U") if KIS_ENV == "real" else ("VTTC0012U" if side == "BUY" else "VTTC0011U")
    # As soon as a real order POST is attempted, later balance snapshots must refresh.
    global ACCOUNT_ORDER_ATTEMPTS
    ACCOUNT_ORDER_ATTEMPTS += 1
    try:
        result, _ = api_request("POST", "/uapi/domestic-stock/v1/trading/order-cash", token,
                                tr_id=tr_id, body=body, timeout=10, retries=0)
    except TradeSafetyError:
        update_journal(key, status="UNCERTAIN")
        fail(f"[{action_name}] 주문 접수 결과 불명: 자동 재주문 금지. 계좌 주문내역 수동 확인 ({key})")
    odno = str((result.get("output") or {}).get("ODNO") or "")
    if not odno:
        update_journal(key, status="UNCERTAIN")
        fail(f"[{action_name}] 응답 주문번호 누락: 자동 재주문 금지")
    update_journal(key, status="ACCEPTED", odno=odno)
    print(f">> ✅ [{action_name}] 접수 성공! 종목: {code} | 수량: {qty}주 | 주문번호: {odno} (체결 미확정)")
    for _ in range(ORDER_POLL_SECONDS):
        time.sleep(1)
        reconcile_orders(token)
        entry = next(x for x in journal_orders() if x.get("id") == key)
        if entry["status"] == "FILLED":
            filled = number(entry.get("confirmed_fill_qty"))
            print(f">> ✅ [{action_name}] 실제 체결 완료: {filled}주")
            return True, filled
    fail(f"[{action_name}] 미체결/부분체결 또는 체결조회 지연. 후속 주문 중단. 주문번호={odno}")


# ==============================================================================
# 4. 일봉 수집 및 수학적 모멘텀 스코어링
# ==============================================================================
def get_daily_closes(token: str, code: str, target_count: int = 252) -> list:
    # 100 records maximum per request. 120-calendar-day windows avoid truncated history.
    end = now_kst().date()
    by_date = {}
    for _ in range(7):
        start = end - timedelta(days=119)
        result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                token, tr_id="FHKST03010100", params={
                                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
                                })
        for row in result.get("output2") or []:
            day, close = str(row.get("stck_bsop_date", "")), number(row.get("stck_clpr"))
            if len(day) == 8 and close > 0 and day <= now_kst().strftime("%Y%m%d"):
                by_date[day] = float(close)
        if len(by_date) >= target_count:
            break
        end = start - timedelta(days=1)
        time.sleep(0.16)
    if len(by_date) < target_count:
        fail(f"{code} 일봉 부족: {len(by_date)}/{target_count} (추정값으로 모멘텀 계산 금지)")
    dates = sorted(by_date)[-target_count:]
    return [by_date[x] for x in dates]


def calc_fast_baa(closes: list) -> float:
    n = len(closes)
    if n < 252 or any(x <= 0 for x in closes):
        raise TradeSafetyError(f"BAA 모멘텀 계산에 최소 252일 필요: {n}")
    curr = closes[-1]
    p_1m, p_3m, p_6m, p_12m = closes[-21], closes[-63], closes[-126], closes[-252]
    return (16.0 * (curr / p_1m - 1) + 6.0 * (curr / p_3m - 1)
            + 2.0 * (curr / p_6m - 1) + (curr / p_12m - 1))


def collect_minute_candles(token: str):
    cursor = now_kst().replace(second=0, microsecond=0)
    collected = {}
    for _ in range(4):
        result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                                token, tr_id="FHKST03010200", params={
                                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE_BULL,
                                    "FID_INPUT_HOUR_1": cursor.strftime("%H%M%S"),
                                    "FID_PW_DATA_INCU_YN": "Y", "FID_ETC_CLS_CODE": "",
                                })
        rows = result.get("output2") or []
        times = []
        for c in rows:
            timestamp = str(c.get("stck_cntg_hour", ""))
            price, vol = number(c.get("stck_prpr")), number(c.get("cntg_vol"))
            if len(timestamp) >= 6 and timestamp[:6].isdigit() and price > 0 and vol >= 0:
                stamp = timestamp[:6]
                collected[stamp] = {"time": stamp, "price": price, "volume": vol}
                times.append(stamp)
        if not times:
            break
        oldest = min(times)
        cursor = cursor.replace(hour=int(oldest[:2]), minute=int(oldest[2:4])) - timedelta(minutes=1)
        if cursor.strftime("%H:%M") < MARKET_START:
            break
        if len(collected) >= 75:
            break
        time.sleep(0.16)
    return [collected[k] for k in sorted(collected)]


# ==============================================================================
# 5. 데이트레이딩 (KODEX 200) 감시
# ==============================================================================
def check_daytrade_signal(token: str) -> bool:
    candles = collect_minute_candles(token)
    # Last current 1-minute bar may be still open; exclude it by timestamp.
    now_minute = now_kst().strftime("%H%M")
    completed = [x for x in candles if x["time"][:4] < now_minute]
    if len(completed) < 60:
        print(f">> [단타 관망] 확정 분봉 데이터 부족 ({len(completed)}개, 최소 60개 필요)")
        return False
    closes = [float(x["price"]) for x in completed]
    volumes = [float(x["volume"]) for x in completed]
    ema20 = sum(closes[-20:]) / 20  # 기존 구현과 동일한 단순평균(SMA); 명칭만 legacy
    ema60 = sum(closes[-60:]) / 60
    vol_ma20 = sum(volumes[-20:]) / 20
    last_close, last_vol = closes[-1], volumes[-1]
    curr_price = float(get_current_price(token, CODE_BULL))
    vol_cond = vol_ma20 > 0 and last_vol >= vol_ma20 * 1.2
    ema_cond = ema20 > ema60
    price_cond = curr_price > last_close
    if vol_cond and ema_cond and price_cond:
        return True
    reasons = []
    if not vol_cond:
        reasons.append(f"거래량 부족(현재 {last_vol:,.0f} < 필요 {vol_ma20*1.2:,.0f})")
    if not ema_cond:
        reasons.append(f"이평선 역배열(SMA20 {ema20:,.0f} <= SMA60 {ema60:,.0f})")
    if not price_cond:
        reasons.append(f"모멘텀 부족(현재가 {curr_price:,.0f} <= 직전종가 {last_close:,.0f})")
    print(f">> [단타 관망] {', '.join(reasons)}")
    return False


def run_intraday(token: str, holdings: dict, deposit: int, total_asset: int):
    now = now_kst()
    time_hm = now.strftime("%H:%M")
    actual = holdings.get(CODE_BULL, {"qty": 0, "avg_price": 0.0})
    actual_qty = number(actual["qty"])
    state = load_json(DAY_STATE_FILE, {"entry_time": None, "pyramided": False, "owned_qty": 0})
    owned = number(state.get("owned_qty"))
    baa = load_json(BAA_STATE_FILE, {"owned_qty": 0, "target_code": None})
    if actual_qty < owned or (baa.get("target_code") == CODE_BULL and number(baa.get("owned_qty")) > 0):
        fail("069500 단타/BAA 추적 물량 충돌: 수동 확인 전 거래 중단")
    if actual_qty != owned:
        print(">> [단타 안전대기] 069500 계좌 보유량과 단타 추적량 불일치 (외부/기존 보유). 거래 차단")
        return
    curr_price = get_current_price(token, CODE_BULL)
    intraday_budget = total_asset * DAY_BUDGET_RATIO
    print(f"\n--- [데이트레이딩 점검: {time_hm}] ---")
    print(f"• KODEX 200 단타 추적: {owned}주 | 현재가: {curr_price:,}원 | 단타 배정예산: {intraday_budget:,.0f}원")
    if owned == 0:
        if "09:15" <= time_hm <= "14:30":
            if check_daytrade_signal(token):
                base_budget = min(float(deposit), float(intraday_budget * WEIGHT_BASE)) * 0.96
                base_qty = min(1, int(base_budget // curr_price))
                if base_qty > 0:
                    execute_order(token, CODE_BULL, base_qty, "02", action_name="단타 1차 매수", strategy="DAY")
                else:
                    print(">> [단타 관망] 안전 예산으로 1주도 매수할 수 없습니다.")
        else:
            print(">> [단타 대기] 신규 진입 시간대(09:15~14:30) 외 구간입니다.")
    else:
        avg = float(actual.get("avg_price") or 0)
        if avg <= 0:
            fail("단타 평균 매입 단가 확인 실패")
        profit_ratio = (curr_price - avg) / avg
        if profit_ratio >= PROFIT_TARGET:
            execute_order(token, CODE_BULL, owned, "01", action_name="목표익절", strategy="DAY", cost_basis=avg)
        elif profit_ratio <= STOP_LOSS:
            execute_order(token, CODE_BULL, owned, "01", action_name="칼손절", strategy="DAY", cost_basis=avg)


# ==============================================================================
# 6. 공격형 BAA 전략 및 장 마감 일일 결산 리포트
# ==============================================================================
def select_baa_target(token):
    canary_logs = []
    daily_cache = {}
    def closes(code):
        if code not in daily_cache:
            daily_cache[code] = get_daily_closes(token, code)
        return daily_cache[code]
    is_defense = False
    for code, name in CANARY_UNIVERSE.items():
        score = calc_fast_baa(closes(code))
        canary_logs.append(f"{name}: {score:.2f}")
        print(f"   • {name} ({code}) [수집: {len(closes(code))}일]: 스코어 {score:.2f}")
        if score <= 0:
            is_defense = True
    scores = {}
    if not is_defense:
        for code in ATTACK_UNIVERSE:
            prices = closes(code)
            score = calc_fast_baa(prices)
            ma60 = sum(prices[-60:]) / 60
            ma200 = sum(prices[-200:]) / 200
            if prices[-1] >= ma60 and prices[-1] >= ma200:
                scores[code] = score
        if not scores:
            is_defense = True
    if is_defense:
        scores = {code: calc_fast_baa(closes(code)) for code in DEFENSE_UNIVERSE}
    target = max(scores, key=scores.get)
    name = (DEFENSE_UNIVERSE if is_defense else ATTACK_UNIVERSE)[target]
    return target, name, is_defense, canary_logs, closes(target)


def handle_daily_closing_report(token: str, holdings: dict, total_asset: int):
    now = now_kst()
    # GitHub marks the dedicated 14:53 KST report event; runner delays are safe.
    if not (RUN_MIDDAY_REPORT or FORCE_BAA):
        return
    state = load_json(BAA_STATE_FILE, {})
    if state.get("last_report_date") == now.strftime("%Y-%m-%d") and not FORCE_BAA:
        return
    print(">> [중간 결산] 14:53 결산 예약의 장중 카나리아·BAA 잠정 리포트를 생성합니다.")
    code, name, is_defense, logs, _ = select_baa_target(token)
    msg = (f"📋 <b>[장중 중간 결산(잠정) - 14:53 결산 예약]</b>\n📅 날짜: {now:%Y-%m-%d} / 실제 조회: {now:%H:%M} KST\n"
           f"━━━━━━━━━━━━━━━━━━━━\n• 운용 모드: {'🛡️ 수비 모드' if is_defense else '🚀 공격 모드'}\n"
           f"• 선정 타깃: <b>{html.escape(name)} ({code})</b>\n"
           f"• 카나리아 점검: {html.escape(', '.join(logs))}\n"
           f"• 총 평가 자산: {total_asset:,.0f}원\n━━━━━━━━━━━━━━━━━━━━")
    send_telegram(msg)
    if not FORCE_BAA:
        state["last_report_date"] = now.strftime("%Y-%m-%d")
        save_json(BAA_STATE_FILE, state)
    print(">> ✅ 장중 중간 결산 리포트 전송 시도 완료.")


def run_baa(token: str, holdings: dict, deposit: int, total_asset: int, force_run: bool = False):
    now = now_kst()
    time_str = now.strftime("%H:%M")
    print(f"\n=== 🛡️ [공격형 BAA 전략 정밀 진단: {time_str}] ===")
    if not ("11:00" <= time_str <= "11:25") and not force_run:
        print(">> [BAA 대기] 정규 실행 시간(11:00~11:25)이 아닙니다.")
        return
    state = load_json(BAA_STATE_FILE, {"last_executed_date": None, "reinvest_pool": 0,
                                       "target_code": None, "owned_qty": 0})
    last_date = state.get("last_executed_date")
    if last_date and not force_run:
        days_passed = (now.date() - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        if days_passed < 13:
            print(f">> [BAA 대기] 최근 리밸런싱일로부터 {days_passed}일 경과 (최소 13일 필요).")
            return
    print(">> [1단계: 4대 카나리아 모멘텀 점검]")
    code, name, is_defense, canary_logs, target_closes = select_baa_target(token)
    print(f">> [2단계] {'수비' if is_defense else '공격'} 타깃 선정: {name} ({code})")
    curr_target_price = get_current_price(token, code)
    sma20 = sum(target_closes[-20:]) / 20
    sma60 = sum(target_closes[-60:]) / 60
    is_bull = sma20 >= sma60
    # Total-asset target = 70%, *not* available-cash target. Reinvestment already in NAV.
    total_budget = total_asset * 0.70
    target_stage_budget = total_budget if is_bull else total_budget * 0.50
    intended_value = target_stage_budget * 0.96
    target_total_qty = int(intended_value // curr_target_price)
    if target_total_qty <= 0:
        print(">> [BAA 대기] 안전 예산으로 목표 1주를 만들 수 없습니다. 실행일 변경 없음.")
        return
    old_code = state.get("target_code")
    tracked_qty = number(state.get("owned_qty"))
    day_owned = number(load_json(DAY_STATE_FILE, {"owned_qty": 0}).get("owned_qty"))
    if day_owned > 0 and code == CODE_BULL:
        fail("BAA 타깃이 단타 보유 069500과 충돌: 리밸런싱 매매 차단")
    if code == CODE_BULL:
        fail("BAA 타깃 069500은 단타와 계좌 내 물량 식별 불가: 매매 차단 (계좌 분리 필요)")
    if old_code == CODE_BULL and tracked_qty:
        fail("기존 BAA 069500 보유는 계좌 분리 또는 수동 정리 필요")
    if tracked_qty > holdings.get(old_code, {}).get("qty", 0):
        fail("BAA 추적 보유량과 실제 잔고 불일치")
    # Without owner record, must not liquidate user's other holdings.
    unknown = [c for c, item in holdings.items() if c != CODE_BULL and item["qty"] > 0
               and not (c == old_code and item["qty"] == tracked_qty)]
    if unknown:
        fail(f"BAA 출처 미확인 보유 종목 {unknown}. 기존 보유 자동 매도 차단")
    current_qty = tracked_qty if old_code == code else 0
    qty_diff = target_total_qty - current_qty
    print(f"• 타깃 현재가: {curr_target_price:,.0f}원 | 20/60 추세: {'정배열' if is_bull else '역배열'}")
    print(f"• 타깃 평가금액(96% 버퍼): {intended_value:,.0f}원 | 목표: {target_total_qty}주"
          f" (현재 BAA 소유: {current_qty}주) -> 필요: {qty_diff:+}주")
    send_telegram(f"📊 <b>[공격형 BAA 전략 정밀 진단]</b>\n"
                  f"• 모드: {'수비' if is_defense else '공격'}\n"
                  f"• 선정 타깃: {html.escape(name)} ({code})\n"
                  f"• 목표 수량: {target_total_qty}주 / 차이: {qty_diff:+}주\n"
                  f"• 배정예산: {intended_value:,.0f}원")
    if DRY_RUN or force_run:
        print(">> 🧪 [BAA 모의진단] 매수·매도 주문 없음. 리밸런싱 실행일 미갱신.")
        return
    # Liquidate the *tracked* old BAA position first. Block if not fully filled.
    if old_code and old_code != code and tracked_qty:
        execute_order(token, old_code, tracked_qty, "01", action_name=f"BAA 매도({old_code})", strategy="BAA")
        holdings, deposit, total_asset = get_account_balance(token)
        assert_no_pending(token)
    if old_code == code and qty_diff < 0:
        execute_order(token, code, -qty_diff, "01", action_name=f"BAA 비중축소({code})", strategy="BAA")
        holdings, deposit, total_asset = get_account_balance(token)
        assert_no_pending(token)
    # Recalculate after real sells, retaining original current NAV target and 96% buffer.
    state = load_json(BAA_STATE_FILE, {})
    current_qty = number(state.get("owned_qty")) if state.get("target_code") == code else 0
    buy_qty = max(0, target_total_qty - current_qty)
    requested_buy_qty = buy_qty
    if buy_qty:
        buy_qty = min(buy_qty, int(max(0, deposit) * 0.96 // curr_target_price))
        if buy_qty:
            execute_order(token, code, buy_qty, "02", action_name=f"BAA 매수({name})", strategy="BAA")
        if buy_qty < requested_buy_qty:
            fail(f"BAA 목표수량 미달: {requested_buy_qty}주 필요, 현금기준 {buy_qty}주만 가능")
    # Only committed when every order required by this rebalance was fully filled.
    assert_no_pending(token)
    state = load_json(BAA_STATE_FILE, {})
    state["last_executed_date"] = now.strftime("%Y-%m-%d")
    # Reinvest_pool is informational only; profits were already reflected in net assets.
    state["reinvest_pool"] = 0
    save_json(BAA_STATE_FILE, state)
    print(">> ✅ [BAA 리밸런싱] 확정 체결 및 상태 저장 완료")


# ==============================================================================
# 7. 메인 실행 엔트리포인트
# ==============================================================================
def main():
    now = now_kst()
    print("==================================================")
    print(f"🤖 [한투 퀀트 트레이딩 봇 시작: {now:%Y-%m-%d %H:%M:%S}]")
    print("==================================================")
    validate_environment()
    print(f">> 모드: {'DRY_RUN 주문 차단' if DRY_RUN else '실전 주문 허용'} / KIS_ENV={KIS_ENV}")
    with LOCK_FILE.open("a+") as file_lock:
        try:
            fcntl.flock(file_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("이미 실행 중인 봇이 있습니다. 중복 실행 중단")
        token = get_access_token()
        if not is_market_open_today(token):
            print(f">> 🏖️ 오늘({now:%Y-%m-%d})은 한국거래소 휴장일입니다.")
            return
        # Reconcile before any trading decisions; never silently swallow a safety exception.
        assert_no_pending(token)
        holdings, deposit, tot_eval = get_account_balance(token)
        risk = load_json(RISK_STATE_FILE, {})
        today = now_kst().strftime("%Y-%m-%d")
        if risk.get("date") != today:
            save_json(RISK_STATE_FILE, {"date": today, "opening_nav": tot_eval})
        total_asset = tot_eval
        print(f">> 계좌 예수금: {deposit:,}원 | 총 평가자산: {total_asset:,}원 | 보유종목수: {len(holdings)}개")
        adopt_verified_legacy_baa(token, holdings)
        before_intraday_orders = ACCOUNT_ORDER_ATTEMPTS
        run_intraday(token, holdings, deposit, total_asset)
        # Always reconcile before BAA; no automatic reorders on uncertain fills.
        assert_no_pending(token)
        # Refresh before BAA when the bot placed an order, or when BAA may trade.
        # In other windows, reuse the initial snapshot to avoid needless API calls.
        baa_time = now_kst().strftime("%H:%M")
        if (ACCOUNT_ORDER_ATTEMPTS != before_intraday_orders or FORCE_BAA
                or "11:00" <= baa_time <= "11:25"):
            holdings, deposit, tot_eval = get_account_balance(token)
        before_baa_orders = ACCOUNT_ORDER_ATTEMPTS
        run_baa(token, holdings, deposit, tot_eval, force_run=FORCE_BAA)
        assert_no_pending(token)
        if ACCOUNT_ORDER_ATTEMPTS != before_baa_orders:
            holdings, deposit, tot_eval = get_account_balance(token)
        handle_daily_closing_report(token, holdings, tot_eval)
        print("\n>> 🏁 1회 실행 사이클이 완료되었습니다.")


if __name__ == "__main__":
    try:
        main()
    except (TradeSafetyError, requests.RequestException, ValueError, OSError) as exc:
        print(f">> ❌ [안전 중단] {exc}")
        sys.exit(1)
