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
import math
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
LIVE_TRADING_START_DATE = datetime(2026, 10, 20).date()
BASE_DIR = Path(os.getenv("BOT_STATE_DIR", Path(__file__).resolve().parent)).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = BASE_DIR / "token_cache.json"
SECURE_TOKEN_FILE = BASE_DIR / "token_cache.secure"
DAY_STATE_FILE = BASE_DIR / "day_state.json"
SWING_STATE_FILE = BASE_DIR / "swing_state.json"
BAA_STATE_FILE = BASE_DIR / "baa_state.json"
TRADE_HISTORY_FILE = BASE_DIR / "trade_history.json"
HOLIDAY_CACHE_FILE = BASE_DIR / "holiday_cache.json"
RISK_STATE_FILE = BASE_DIR / "risk_state.json"
SCHEDULE_STATE_FILE = BASE_DIR / "schedule_state.json"
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
SWING_BUDGET_RATIO = 0.30
SWING_INITIAL_WEIGHT = 0.50
BAA_TARGET_RATIO = 0.80

CANARY_UNIVERSE = {
    "360750": "TIGER 미국S&P500",
    "251350": "KODEX MSCI선진국",
    "195980": "PLUS 신흥국MSCI(합성 H)",
    "437080": "KODEX 미국종합채권ESG액티브(H)",
}
ATTACK_UNIVERSE = {
    "133690": "TIGER 미국나스닥100",
    "122630": "KODEX 레버리지", "381180": "TIGER 미국필라델피아반도체나스닥",
}
DEFENSE_UNIVERSE = {
    "423160": "KODEX KOFR금리액티브(합성)",
    "449170": "TIGER KOFR금리액티브(합성)",
    "453850": "ACE 미국30년국채액티브(H)",
    "411060": "ACE KRX금현물",
}
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
    """Broker and strategy quantities must be explicit nonnegative integers."""
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
    if filepath.name not in {"trade_history.json", "day_state.json", "swing_state.json", "baa_state.json", "risk_state.json", "holiday_cache.json", "schedule_state.json", "token_cache.secure"}:
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


def is_market_open_date(token: str, target_date) -> bool:
    date_text = target_date.strftime("%Y%m%d")
    cache = load_json(HOLIDAY_CACHE_FILE, {})
    if target_date == now_kst().date() and cache.get("date") == date_text and cache.get("is_open") in (True, False):
        return cache["is_open"]
    result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/chk-holiday", token,
                            tr_id="CTCA0903R", params={"BASS_DT": date_text, "CTX_AREA_FK": "", "CTX_AREA_NK": ""})
    entries = result.get("output") or []
    if isinstance(entries, dict):
        entries = [entries]
    for item in entries:
        if str(item.get("bass_dt")) == date_text and item.get("opnd_yn") in ("Y", "N"):
            is_open = item["opnd_yn"] == "Y"
            if target_date == now_kst().date():
                save_json(HOLIDAY_CACHE_FILE, {"date": date_text, "is_open": is_open})
            return is_open
    fail(f"{date_text} 휴장 여부를 검증하지 못했습니다. 평일 추정 주문 금지")


def is_market_open_today(token: str) -> bool:
    return is_market_open_date(token, now_kst().date())


def previous_exchange_session(token: str, session_date):
    for offset in range(1, 16):
        candidate = session_date - timedelta(days=offset)
        if is_market_open_date(token, candidate):
            return candidate
    fail("이전 거래일을 KIS 거래일 조회로 확인하지 못했습니다")


def next_exchange_session(token: str, session_date):
    for offset in range(1, 16):
        candidate = session_date + timedelta(days=offset)
        if is_market_open_date(token, candidate):
            return candidate
    fail("다음 거래일을 KIS 거래일 조회로 확인하지 못했습니다")


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


def get_buyable_qty(token: str, code: str, price: int, ord_dvsn="01") -> int:
    result, _ = api_request("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", token,
                            tr_id="TTTC8908R" if KIS_ENV == "real" else "VTTC8908R",
                            params={"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD,
                                    "PDNO": code, "ORD_UNPR": str(price), "ORD_DVSN": ord_dvsn,
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


def default_swing_state():
    return {
        "schema_version": 1, "strategy": "SWING", "migration_status": "READY",
        "signal_date": None, "signal_status": None, "entry_signal_date": None,
        "entry_signal_close": None, "signal_atr14": None, "entry_atr14": None,
        "entry_opportunity_date": None,
        "entry_opportunity_status": None, "entry_skip_reason": None,
        "entry_order_intent_id": None, "owned_qty": 0, "verified_entry_price": 0.0,
        "entry_session": None, "initial_stop": None, "active_stop": None,
        "highest_completed_close": None, "last_processed_bar_date": None,
        "exit_intent": None, "exit_order_intent_id": None, "last_exit_session": None,
        "data_status": "NOT_CHECKED", "data_error": None, "data_as_of_date": None,
    }


def migrate_swing_state_if_safe(holdings: dict):
    exists = SWING_STATE_FILE.exists()
    if exists:
        state = load_json(SWING_STATE_FILE, {})
        if state.get("schema_version") != 1 or state.get("strategy") != "SWING":
            fail("SWING 상태 버전/전략 식별 오류")
    day = load_json(DAY_STATE_FILE, {})
    baa = load_json(BAA_STATE_FILE, {})
    entries = read_journal().get("orders", [])
    if not isinstance(entries, list) or any(not isinstance(order, dict) for order in entries):
        fail("069500 원장 기록이 모호합니다. SWING 자동화를 중단합니다")
    legacy_records = [
        order for order in entries
        if order.get("strategy") == "DAY"
        or (order.get("code") == CODE_BULL and order.get("strategy") != "SWING")
    ]
    if (number(day.get("owned_qty")) or day.get("entry_time") or day.get("pyramided")
            or legacy_records or baa.get("target_code") == CODE_BULL):
        fail("069500 legacy DAY/BAA 상태 또는 잔고가 남아 있습니다. SWING 마이그레이션은 수동 대조 후에만 허용됩니다")
    actual_qty = share_quantity(holdings.get(CODE_BULL, {}).get("qty", 0))
    if (not exists and actual_qty) or (exists and actual_qty != share_quantity(state.get("owned_qty", 0))):
        fail("069500 SWING 상태와 계좌 잔고가 일치하지 않습니다")
    if exists:
        return state
    state = default_swing_state()
    save_json(SWING_STATE_FILE, state)
    return state


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
    swing_qty, swing_entry_price, swing_entry_atr, swing_entry_session = 0, 0.0, None, None
    swing_last_exit_session = None
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
        elif order.get("strategy") == "SWING":
            if order.get("code") != CODE_BULL:
                fail("SWING 원장에 허용되지 않은 종목이 있습니다")
            if delta > 0:
                if swing_qty:
                    fail("SWING 추가매수 기록이 있습니다. 수동 원장 확인 필요")
                swing_qty = filled
                if swing_qty > 1:
                    fail("SWING 체결수량이 계좌 1주 상한을 초과합니다")
                swing_entry_price = number(order.get("confirmed_fill_amt")) / filled
                metadata = order.get("strategy_metadata") or {}
                try:
                    swing_entry_atr = float(metadata.get("entry_atr14"))
                    datetime.strptime(metadata.get("entry_session", ""), "%Y-%m-%d")
                except (TypeError, ValueError):
                    fail("SWING 매수 체결 원장에 검증된 ATR/세션 메타데이터가 없습니다")
                swing_entry_session = metadata.get("entry_session")
                if (swing_entry_price <= 0 or not math.isfinite(swing_entry_atr)
                        or swing_entry_atr <= 0):
                    fail("SWING 매수 체결 원장에 검증된 ATR/세션 메타데이터가 없습니다")
            else:
                if filled > swing_qty:
                    fail("SWING 매도 체결수량이 추적 보유수량을 초과합니다")
                swing_qty -= filled
                if swing_qty == 0:
                    swing_entry_price, swing_entry_atr, swing_entry_session = 0.0, None, None
                    swing_last_exit_session = order.get("created_at", "")[:10]
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
    swing = load_json(SWING_STATE_FILE, default_swing_state())
    if swing_qty:
        initial_stop = swing_entry_price - 2 * float(swing_entry_atr)
        swing.update({
            "owned_qty": swing_qty,
            "verified_entry_price": swing_entry_price,
            "entry_atr14": swing_entry_atr,
            "initial_stop": initial_stop,
            "active_stop": max(
                float(swing.get("active_stop") or initial_stop),
                initial_stop,
            ),
            "entry_session": swing_entry_session,
        })
    else:
        swing.update({
            "owned_qty": 0, "verified_entry_price": 0.0, "entry_atr14": None,
            "initial_stop": None, "active_stop": None, "entry_session": None,
        })
    if swing_last_exit_session:
        swing["last_exit_session"] = swing_last_exit_session
    # Preserve legacy reinvest_pool only as a report field; never double-count it in NAV.
    save_json(DAY_STATE_FILE, day)
    save_json(BAA_STATE_FILE, baa)
    if SWING_STATE_FILE.exists() or any(x.get("strategy") == "SWING" for x in entries):
        save_json(SWING_STATE_FILE, swing)


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
                  price=0, action_name="주문", strategy="BAA", cost_basis=0.0,
                  strategy_metadata=None):
    """Return (fully_filled, confirmed_qty), never requested/accepted quantity."""
    if qty <= 0:
        return False, 0
    if not DRY_RUN and now_kst().date() < LIVE_TRADING_START_DATE:
        fail(f"실거래 시작일({LIVE_TRADING_START_DATE}) 이전에는 주문을 차단합니다")
    if order_type not in ("01", "02") or strategy not in ("SWING", "BAA"):
        fail("잘못된 주문 방향/전략")
    if ord_dvsn not in ("00", "01") or (ord_dvsn == "00" and number(price) <= 0):
        fail("잘못된 주문구분/지정가")
    if not order_window() or not is_market_open_today(token):
        fail("정규장 시간 외 또는 휴장일에 주문 차단")
    assert_no_pending(token)
    side = "BUY" if order_type == "02" else "SELL"
    if side == "BUY":
        check_daily_loss(token)
        current = get_current_price(token, code)
        if ord_dvsn == "00" and current > number(price):
            fail("현재가가 보호 지정가를 초과하여 매수 주문을 차단합니다")
        allowed = get_buyable_qty(token, code, number(price) if ord_dvsn == "00" else current,
                                  ord_dvsn=ord_dvsn)
        if qty > allowed:
            fail(f"매수가능 {allowed}주 < 요청 {qty}주: 수량 자동변경 없이 주문 차단")
        if code == CODE_BULL:
            if strategy != "SWING":
                fail("069500 매수는 SWING 전략만 허용")
            holdings, _, _ = get_account_balance(token)
            actual_qty = share_quantity(holdings.get(code, {}).get("qty", 0))
            swing = load_json(SWING_STATE_FILE, {})
            baa = load_json(BAA_STATE_FILE, {})
            day = load_json(DAY_STATE_FILE, {})
            if (actual_qty != share_quantity(swing.get("owned_qty", 0))
                    or number(day.get("owned_qty"))
                    or (baa.get("target_code") == code
                        and share_quantity(baa.get("owned_qty", 0)) > 0)):
                fail("069500 주문 직전 잔고/전략 원장 불일치")
            if qty != 1 or actual_qty + qty > 1:
                fail("069500 계좌 총보유량 1주 상한 초과: 매수 차단")
    else:
        holdings, _, _ = get_account_balance(token)
        if code == CODE_BULL:
            if strategy != "SWING":
                fail("기존 DAY/기타 전략의 069500 자동 매도는 비활성화되어 있습니다")
            swing = load_json(SWING_STATE_FILE, {})
            day = load_json(DAY_STATE_FILE, {})
            baa = load_json(BAA_STATE_FILE, {})
            actual_qty = share_quantity(holdings.get(code, {}).get("qty", 0))
            if (actual_qty != share_quantity(swing.get("owned_qty", 0))
                    or actual_qty > 1
                    or number(day.get("owned_qty"))
                    or (baa.get("target_code") == code and number(baa.get("owned_qty")))):
                fail("069500 매도 직전 잔고/전략 소유권 불일치")
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
    if strategy_metadata:
        order["strategy_metadata"] = strategy_metadata
    journal.setdefault("orders", []).append(order)
    save_json(TRADE_HISTORY_FILE, journal)
    body = {"CANO": KIS_CANO, "ACNT_PRDT_CD": KIS_ACNT_PRDT_CD, "PDNO": code,
            "ORD_DVSN": ord_dvsn, "ORD_QTY": str(qty),
            "ORD_UNPR": str(number(price)) if ord_dvsn == "00" else "0",
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
def get_daily_closes(token: str, code: str, target_count: int = 252, *,
                     adjusted: bool = False) -> list:
    # 100 records maximum per request. 120-calendar-day windows avoid truncated history.
    as_of = now_kst().date()
    end = as_of
    by_date = {}
    for _ in range(7):
        start = end - timedelta(days=119)
        result, _ = api_request("GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                                token, tr_id="FHKST03010100", params={
                                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                                    "FID_PERIOD_DIV_CODE": "D",
                                    "FID_ORG_ADJ_PRC": "1" if adjusted else "0",
                                })
        rows = result.get("output2")
        if not isinstance(rows, list):
            fail(f"{code} 일봉 응답 목록 누락/형식 오류")
        for row in rows:
            if not isinstance(row, dict):
                fail(f"{code} 일봉 행 형식 오류")
            day = row.get("stck_bsop_date")
            if (not isinstance(day, str) or len(day) != 8
                    or not day.isascii() or not day.isdigit()):
                fail(f"{code} 일봉 날짜 형식 오류")
            try:
                session_date = datetime.strptime(day, "%Y%m%d").date()
            except ValueError:
                fail(f"{code} 일봉 날짜 형식 오류")
            if session_date < start or session_date > end or session_date > as_of:
                fail(f"{code} 일봉 날짜 범위 오류: {day}")
            try:
                close = float(str(row.get("stck_clpr", "")).replace(",", ""))
            except (ValueError, TypeError):
                fail(f"{code} 일봉 종가 형식 오류: {day}")
            if not math.isfinite(close) or close <= 0:
                fail(f"{code} 일봉 종가 비정상: {day}")
            if day in by_date:
                fail(f"{code} 일봉 날짜 중복: {day}")
            by_date[day] = close
        if len(by_date) >= target_count:
            break
        end = start - timedelta(days=1)
        time.sleep(0.16)
    if len(by_date) < target_count:
        fail(f"{code} 일봉 부족: {len(by_date)}/{target_count} (추정값으로 모멘텀 계산 금지)")
    dates = sorted(by_date)[-target_count:]
    return [by_date[x] for x in dates]


def get_validated_daily_bars(token: str, session_date, target_count=100):
    expected_latest = previous_exchange_session(token, session_date)
    end = session_date
    by_date = {}
    seen_dates = set()
    for _ in range(7):
        start = end - timedelta(days=119)
        result, _ = api_request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", token,
            tr_id="FHKST03010100", params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE_BULL,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            })
        rows = result.get("output2")
        if not isinstance(rows, list):
            fail("069500 일봉 응답 목록 누락/형식 오류")
        for row in rows:
            if not isinstance(row, dict):
                fail("069500 일봉 행 형식 오류")
            day = row.get("stck_bsop_date")
            if not isinstance(day, str) or len(day) != 8 or not day.isascii() or not day.isdigit():
                fail("069500 일봉 날짜 형식 오류")
            try:
                bar_date = datetime.strptime(day, "%Y%m%d").date()
            except ValueError:
                fail(f"069500 일봉 날짜 오류: {day}")
            if bar_date < start or bar_date > session_date:
                fail(f"069500 일봉 날짜 범위 오류: {day}")
            if day in seen_dates:
                fail(f"069500 일봉 날짜 중복: {day}")
            seen_dates.add(day)
            if bar_date == session_date:
                continue
            if bar_date > session_date:
                fail(f"069500 미래 일봉 데이터: {day}")
            values = {}
            for key, field in (("open", "stck_oprc"), ("high", "stck_hgpr"),
                               ("low", "stck_lwpr"), ("close", "stck_clpr")):
                try:
                    value = float(str(row.get(field, "")).replace(",", ""))
                except (TypeError, ValueError):
                    fail(f"069500 일봉 {key} 형식 오류: {day}")
                if not math.isfinite(value) or value <= 0:
                    fail(f"069500 일봉 {key} 비정상: {day}")
                values[key] = value
            try:
                volume = float(str(row.get("acml_vol", "")).replace(",", ""))
            except (TypeError, ValueError):
                fail(f"069500 일봉 거래량 형식 오류: {day}")
            if not math.isfinite(volume) or volume < 0:
                fail(f"069500 일봉 거래량 비정상: {day}")
            if (values["high"] < max(values["open"], values["close"], values["low"])
                    or values["low"] > min(values["open"], values["close"])):
                fail(f"069500 일봉 OHLC 불일치: {day}")
            values["date"] = bar_date
            values["volume"] = volume
            by_date[day] = values
        if len(by_date) >= target_count:
            break
        end = start - timedelta(days=1)
        time.sleep(0.16)
    dates = sorted(by_date)
    if not dates or dates[-1] != expected_latest.strftime("%Y%m%d"):
        fail("069500 최신 완료 일봉이 KIS 거래일 기준 직전 거래일과 일치하지 않습니다")
    if len(dates) < target_count:
        fail(f"069500 검증 일봉 부족: {len(dates)}/{target_count}")
    return [by_date[day] for day in dates[-target_count:]]


def wilder_atr(bars, period=14):
    if len(bars) < period + 1:
        fail(f"ATR{period} 계산에 최소 {period + 1}개 완료 일봉이 필요합니다")
    ranges = []
    for prior, current in zip(bars, bars[1:]):
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - prior["close"]),
            abs(current["low"] - prior["close"]),
        ))
    if len(ranges) < period:
        fail(f"ATR{period} 초기화 이력 부족")
    atr = sum(ranges[:period]) / period
    for true_range in ranges[period:]:
        atr = (13 * atr + true_range) / 14
    return atr


def swing_signal(bars):
    if len(bars) < 100:
        fail("SWING 신호 계산에 최소 100개 완료 일봉이 필요합니다")
    closes = [bar["close"] for bar in bars]
    latest = closes[-1]
    breakout = latest > max(closes[-21:-1])
    sma20 = sum(closes[-20:]) / 20
    sma60 = sum(closes[-60:]) / 60
    atr14 = wilder_atr(bars, 14)
    trend_exit = latest < min(closes[-11:-1])
    return {
        "entry": breakout and sma20 > sma60,
        "trend_exit": trend_exit,
        "close": latest, "sma20": sma20, "sma60": sma60,
        "atr14": atr14, "signal_date": bars[-1]["date"].isoformat(),
    }


def limit_buy_tick(price):
    bands = ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
             (200000, 100), (500000, 500), (float("inf"), 1000))
    for ceiling, tick in bands:
        if price < ceiling:
            return int(price // tick * tick)
    return int(price)


def calc_fast_baa(closes: list) -> float:
    n = len(closes)
    if n < 252 or any(x <= 0 for x in closes):
        raise TradeSafetyError(f"BAA 모멘텀 계산에 최소 252일 필요: {n}")
    curr = closes[-1]
    p_1m, p_3m, p_6m, p_12m = closes[-21], closes[-63], closes[-126], closes[-252]
    return (16.0 * (curr / p_1m - 1) + 6.0 * (curr / p_3m - 1)
            + 2.0 * (curr / p_6m - 1) + (curr / p_12m - 1))


# ==============================================================================
# 5. KODEX 200 SWING strategy
# ==============================================================================
def run_swing(token: str, holdings: dict, deposit: int, total_asset: int):
    now = now_kst()
    today = now.date()
    time_hm = now.strftime("%H:%M")
    try:
        state = migrate_swing_state_if_safe(holdings)
    except TradeSafetyError as exc:
        print(f">> [SWING 중단/BAA 유지] {exc}")
        return
    actual_qty = share_quantity(holdings.get(CODE_BULL, {}).get("qty", 0))
    owned = share_quantity(state.get("owned_qty", 0))
    day = load_json(DAY_STATE_FILE, {})
    baa = load_json(BAA_STATE_FILE, {})
    if (actual_qty != owned or actual_qty > 1 or number(day.get("owned_qty"))
            or (baa.get("target_code") == CODE_BULL and number(baa.get("owned_qty")))):
        try:
            fail("069500 잔고/전략 소유권 불일치: SWING 자동화를 중단합니다")
        except TradeSafetyError as exc:
            print(f">> [SWING 중단/BAA 유지] {exc}")
        return
    if not owned and state.get("exit_intent"):
        state["exit_intent"] = None
        state["exit_order_intent_id"] = None
        state["last_exit_session"] = state.get("last_exit_session") or today.isoformat()
        save_json(SWING_STATE_FILE, state)
    if owned:
        actual_avg = float(holdings.get(CODE_BULL, {}).get("avg_price") or 0)
        verified_avg = float(state.get("verified_entry_price") or 0)
        if actual_avg <= 0 or verified_avg <= 0 or not math.isclose(actual_avg, verified_avg, abs_tol=1.0):
            try:
                fail("069500 계좌 평균매입가와 SWING 확인 체결가가 다릅니다")
            except TradeSafetyError as exc:
                print(f">> [SWING 중단/BAA 유지] {exc}")
            return

    bars = None
    signal = None
    try:
        bars = get_validated_daily_bars(token, today)
        signal = swing_signal(bars)
        state["data_status"] = "CURRENT"
        state["data_error"] = None
        state["data_as_of_date"] = signal["signal_date"]
    except TradeSafetyError as exc:
        state["data_status"] = "INVALID_OR_STALE"
        state["data_error"] = str(exc)
        save_json(SWING_STATE_FILE, state)
        print(f">> [SWING 진입 차단] 일봉 데이터 검증 실패: {exc}")

    if bars:
        latest_date = signal["signal_date"]
        if state.get("last_processed_bar_date") != latest_date:
            if owned and state.get("entry_session"):
                entry_date = datetime.strptime(state["entry_session"], "%Y-%m-%d").date()
                eligible_closes = [bar["close"] for bar in bars if bar["date"] >= entry_date]
                if eligible_closes:
                    highest = max(
                        float(state.get("highest_completed_close") or 0),
                        max(eligible_closes),
                    )
                    initial_stop = float(state.get("initial_stop") or 0)
                    fixed_atr = float(state.get("entry_atr14") or 0)
                    state["highest_completed_close"] = highest
                    state["active_stop"] = max(
                        float(state.get("active_stop") or initial_stop),
                        initial_stop, highest - 3 * fixed_atr,
                    )
            state["last_processed_bar_date"] = latest_date
            if state.get("signal_date") != latest_date:
                try:
                    next_session = next_exchange_session(token, bars[-1]["date"])
                except TradeSafetyError as exc:
                    print(f">> [SWING 진입 차단] 다음 거래일 검증 실패: {exc}")
                    print(">> [SWING 신호 차단] 최신 완료 일봉의 다음 거래일이 오늘과 다릅니다.")
                    state["signal_status"] = "DATA_SESSION_MISMATCH"
                    state["entry_opportunity_status"] = "NONE"
                else:
                    if next_session != today:
                        print(">> [SWING 신호 차단] 최신 완료 일봉의 다음 거래일이 오늘과 다릅니다.")
                        state["signal_status"] = "DATA_SESSION_MISMATCH"
                        state["entry_opportunity_status"] = "NONE"
                    else:
                        state.update({
                            "signal_date": latest_date,
                            "signal_status": "ENTRY" if signal["entry"] else "NO_ENTRY",
                            "entry_signal_date": latest_date if signal["entry"] else None,
                            "entry_signal_close": signal["close"] if signal["entry"] else None,
                            "signal_atr14": signal["atr14"] if signal["entry"] else None,
                            "entry_opportunity_date": today.isoformat() if signal["entry"] else None,
                            "entry_opportunity_status": "PENDING" if signal["entry"] else "NONE",
                            "entry_skip_reason": None,
                        })
            if owned and signal["trend_exit"]:
                state["exit_intent"] = state.get("exit_intent") or "TREND"
            save_json(SWING_STATE_FILE, state)
        else:
            save_json(SWING_STATE_FILE, state)

    if state.get("entry_opportunity_status") == "PENDING":
        opportunity = state.get("entry_opportunity_date")
        if opportunity and opportunity < today.isoformat():
            state["entry_opportunity_status"] = "EXPIRED"
            state["entry_skip_reason"] = "next exchange session passed"
            save_json(SWING_STATE_FILE, state)

    if not owned:
        if (not bars or state.get("entry_opportunity_date") != today.isoformat()
                or state.get("entry_opportunity_status") != "PENDING"
                or state.get("last_exit_session") == today.isoformat()):
            return
        if not ("09:10" <= time_hm <= "14:30"):
            print(">> [SWING 대기] 신규 진입 시간(09:10-14:30) 밖이며 기회는 유지됩니다.")
            return
        try:
            quote = get_current_price(token, CODE_BULL)
        except TradeSafetyError as exc:
            state["entry_opportunity_status"] = "SKIPPED"
            state["entry_skip_reason"] = f"current quote unavailable: {exc}"
            save_json(SWING_STATE_FILE, state)
            print(f">> [SWING 진입 차단] {exc}")
            return
        signal_close = float(state["entry_signal_close"])
        signal_atr = float(state["signal_atr14"])
        max_price = signal_close + signal_atr
        limit_price = limit_buy_tick(max_price)

        def skip_entry(reason):
            state["entry_opportunity_status"] = "SKIPPED"
            state["entry_skip_reason"] = reason
            save_json(SWING_STATE_FILE, state)
            print(f">> [SWING 진입 생략] {reason}")

        if quote > max_price:
            skip_entry("anti-chasing: quote above signal close + ATR14")
            return
        if quote > limit_price:
            skip_entry("anti-chasing: no valid tick price at or below the signal cap")
            return
        sleeve_budget = min(
            float(total_asset) * SWING_BUDGET_RATIO * SWING_INITIAL_WEIGHT,
            float(deposit),
        ) * 0.96
        if limit_price <= 0 or limit_price > sleeve_budget:
            skip_entry("former DAY sleeve or available cash cannot fund one share")
            return
        try:
            buyable_qty = get_buyable_qty(token, CODE_BULL, limit_price, ord_dvsn="00")
        except TradeSafetyError as exc:
            skip_entry(f"broker buyability check failed: {exc}")
            return
        if buyable_qty < 1:
            skip_entry("broker-reported buyable quantity is below one share")
            return
        intent_id = order_key(CODE_BULL, "BUY", "SWING", "SWING entry")
        state["entry_opportunity_status"] = "SUBMITTING"
        state["entry_order_intent_id"] = intent_id
        save_json(SWING_STATE_FILE, state)
        filled, fill_qty = execute_order(
            token, CODE_BULL, 1, "02", ord_dvsn="00", price=limit_price,
            action_name="SWING entry", strategy="SWING",
            strategy_metadata={
                "entry_atr14": signal_atr, "signal_date": state["entry_signal_date"],
                "signal_close": signal_close, "entry_session": today.isoformat(),
            })
        if filled and fill_qty == 1:
            rebuild_strategy_state()
            state = load_json(SWING_STATE_FILE, default_swing_state())
            state["entry_opportunity_status"] = "FILLED"
            state["entry_atr14"] = signal_atr
            state["initial_stop"] = state["verified_entry_price"] - 2 * signal_atr
            state["active_stop"] = state["initial_stop"]
            save_json(SWING_STATE_FILE, state)
        elif DRY_RUN:
            state["entry_opportunity_status"] = "DRY_RUN"
            state["entry_skip_reason"] = "dry-run: no order submitted"
            save_json(SWING_STATE_FILE, state)
        return

    if not ("09:00" <= time_hm <= "15:20"):
        return
    try:
        quote = get_current_price(token, CODE_BULL)
    except TradeSafetyError as exc:
        print(f">> [SWING 보유 위험관리 보류] 유효 현재가 없음: {exc}")
        return
    active_stop = state.get("active_stop")
    if active_stop is not None and quote <= float(active_stop):
        state["exit_intent"] = "PROTECTIVE_STOP"
        save_json(SWING_STATE_FILE, state)
    if state.get("exit_intent"):
        avg_price = float(state.get("verified_entry_price") or 0)
        if avg_price <= 0:
            fail("SWING 확인 체결 평균가가 없어 매도를 중단합니다")
        reason = state["exit_intent"]
        state["exit_order_intent_id"] = order_key(
            CODE_BULL, "SELL", "SWING", f"SWING exit {reason}"
        )
        save_json(SWING_STATE_FILE, state)
        filled, fill_qty = execute_order(
            token, CODE_BULL, owned, "01", action_name=f"SWING exit {reason}",
            strategy="SWING", cost_basis=avg_price,
        )
        if filled and fill_qty == owned:
            rebuild_strategy_state()
            state = load_json(SWING_STATE_FILE, default_swing_state())
            state["exit_intent"] = None
            state["exit_order_intent_id"] = None
            state["last_exit_session"] = today.isoformat()
            state["entry_opportunity_status"] = "CLOSED"
            save_json(SWING_STATE_FILE, state)


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
    if not is_defense and target == CODE_BULL:
        fail("069500은 단기매매 전용으로 공격형 BAA 선정에서 제외되어야 합니다")
    name = (DEFENSE_UNIVERSE if is_defense else ATTACK_UNIVERSE)[target]
    return target, name, is_defense, canary_logs, closes(target)


def handle_daily_closing_report(token: str, holdings: dict, total_asset: int):
    now = now_kst()
    if not (RUN_MIDDAY_REPORT or FORCE_BAA):
        return
    if not FORCE_BAA and now.strftime("%H:%M") < "14:53":
        return
    state = load_json(BAA_STATE_FILE, {})
    if state.get("last_report_date") == now.strftime("%Y-%m-%d") and not FORCE_BAA:
        return
    recovered = not FORCE_BAA and now.strftime("%H:%M") > "14:53"
    print(">> [중간 결산] 14:53 결산 예약의 장중 카나리아·BAA 잠정 리포트를 생성합니다.")
    code, name, is_defense, logs, _ = select_baa_target(token)
    swing = load_json(SWING_STATE_FILE, default_swing_state())
    swing_status = (f"수량 {number(swing.get('owned_qty'))}주 / 확인 체결가 "
                    f"{number(swing.get('verified_entry_price')):,}원 / "
                    f"신호 {swing.get('signal_date') or '없음'} "
                    f"({swing.get('signal_status') or '미평가'})")
    swing_levels = (f"고정 진입 ATR {number(swing.get('entry_atr14')):,.0f}원 / "
                    f"초기·활성 스톱 {number(swing.get('initial_stop')):,}원 / "
                    f"{number(swing.get('active_stop')):,}원")
    swing_entry = (f"진입기회 {swing.get('entry_opportunity_status') or '없음'}"
                   f"{': ' + str(swing.get('entry_skip_reason')) if swing.get('entry_skip_reason') else ''}")
    swing_exit = (f"최근 완료 일봉 {swing.get('last_processed_bar_date') or '없음'} / "
                  f"고점 종가 {number(swing.get('highest_completed_close')):,}원 / "
                  f"청산 의도 {swing.get('exit_intent') or '없음'}")
    swing_data = (f"일봉 {swing.get('data_status') or '미확인'} "
                  f"({swing.get('data_as_of_date') or '기준일 없음'})"
                  f"{': ' + str(swing.get('data_error')) if swing.get('data_error') else ''}")
    pending_swing = [
        f"{order.get('id')}:{order.get('status')}"
        for order in journal_orders()
        if order.get("strategy") == "SWING"
        and order.get("status") in {"INTENT", "UNCERTAIN", "ACCEPTED", "PARTIAL", "UNKNOWN"}
    ]
    msg = (f"📋 <b>[장중 중간 결산(잠정) - {'지연 복구' if recovered else '14:53 예약'}]</b>\n📅 날짜: {now:%Y-%m-%d} / 실제 조회: {now:%H:%M} KST\n"
           f"━━━━━━━━━━━━━━━━━━━━\n• 운용 모드: {'🛡️ 수비 모드' if is_defense else '🚀 공격 모드'}\n"
           f"• 선정 타깃: <b>{html.escape(name)} ({code})</b>\n"
           f"• 카나리아 점검: {html.escape(', '.join(logs))}\n"
           f"• 총 평가 자산: {total_asset:,.0f}원\n"
           f"• SWING(KODEX 200): {html.escape(swing_status)}\n"
           f"• {html.escape(swing_levels)}\n"
           f"• {html.escape(swing_entry)}\n"
           f"• {html.escape(swing_exit)}\n"
           f"• 데이터 신선도: {html.escape(swing_data)}\n"
           f"• 미확정 SWING 주문: {html.escape(', '.join(pending_swing) if pending_swing else '없음')}\n"
           f"• 현재 실행 시각: {now:%H:%M:%S} KST; 종가 확정 전 잠정 보고\n━━━━━━━━━━━━━━━━━━━━")
    send_telegram(msg, is_alert=recovered)
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
    # Total-asset target = 80%, *not* available-cash target. Reinvestment already in NAV.
    total_budget = total_asset * BAA_TARGET_RATIO
    target_stage_budget = total_budget if is_bull else total_budget * 0.50
    intended_value = target_stage_budget * 0.96
    target_total_qty = int(intended_value // curr_target_price)
    if target_total_qty <= 0:
        print(">> [BAA 대기] 안전 예산으로 목표 1주를 만들 수 없습니다. 실행일 변경 없음.")
        return
    old_code = state.get("target_code")
    tracked_qty = number(state.get("owned_qty"))
    swing_owned = number(load_json(SWING_STATE_FILE, {"owned_qty": 0}).get("owned_qty"))
    if swing_owned > 0 and code == CODE_BULL:
        fail("BAA 타깃이 SWING 보유 069500과 충돌: 리밸런싱 매매 차단")
    if code == CODE_BULL:
        fail("BAA 타깃 069500은 SWING과 계좌 내 물량 식별 불가: 매매 차단 (계좌 분리 필요)")
    if old_code == CODE_BULL:
        fail("기존 BAA 069500 상태 기록이 있습니다. SWING/BAA 소유권 수동 계좌 대조 필요")
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


def monitor_scheduled_execution():
    now = now_kst()
    state = load_json(SCHEDULE_STATE_FILE, {})
    previous = state.get("last_scheduled_run")
    if previous:
        if not isinstance(previous, str):
            fail("예약 실행 추적 상태 형식 오류")
        try:
            previous_at = KST.localize(datetime.strptime(previous, "%Y-%m-%d %H:%M:%S"))
        except ValueError as exc:
            fail(f"예약 실행 추적 상태 형식 오류: {exc}")
        current_minutes = now.hour * 60 + now.minute
        previous_minutes = previous_at.hour * 60 + previous_at.minute
        afternoon_start, afternoon_end = 14 * 60, 15 * 60 + 20
        if (previous_at.date() == now.date()
                and afternoon_start <= previous_minutes <= afternoon_end
                and afternoon_start <= current_minutes <= afternoon_end
                and now - previous_at >= timedelta(minutes=10)):
            gap_minutes = int((now - previous_at).total_seconds() // 60)
            send_telegram(
                f"예약 실행 지연/누락 감지: 직전 KST 실행 {previous_at:%H:%M}, "
                f"현재 {now:%H:%M} (간격 {gap_minutes}분).",
                is_alert=True,
            )
    state["last_scheduled_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save_json(SCHEDULE_STATE_FILE, state)


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
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        monitor_scheduled_execution()
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
        run_swing(token, holdings, deposit, total_asset)
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
