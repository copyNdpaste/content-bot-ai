#!/usr/bin/env python3
# version: token_manager_v2_x_integrated
"""Meta + X 토큰 자동 관리 (Threads + Instagram + X, 멀티 계정)

목표:
  사장님이 .env 에 앱 시크릿 + OAuth 직후 받은 단기 토큰만 한 번 넣으면
  - 60일 장기 토큰 자동 발급
  - 만료 7일 전 자동 갱신
  - 이론상 영구 사용

사용법:
  # 1) .env 작성 (또는 환경변수)
  vi _company/_agents/instagram/.env

  # 2) 초기 부트스트랩 (1회) — 단기→장기 교환, tokens.json 생성
  python3 token_manager.py --bootstrap

  # 3) 상태 확인 (언제든지)
  python3 token_manager.py --status

  # 4) 자동 갱신 (cron 또는 money-ai 스케줄러에 등록 권장)
  python3 token_manager.py --refresh

.env 템플릿 (계정 4개 예시):
  META_APP_ID=1327871115947327
  META_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  META_THREADS_SHORT_TOKEN_JP=TH...
  META_THREADS_SHORT_TOKEN_KR=TH...
  META_IG_SHORT_TOKEN_JP=EAA...
  META_IG_SHORT_TOKEN_KR=EAA...

보안:
  - 앱 시크릿 / 토큰은 절대 stdout/stderr 에 평문 출력하지 않습니다.
  - .env 와 tokens.json 은 .gitignore 에 반드시 추가하세요.

LLM 호출 0회.
"""
import argparse
import json
import os
import re
import sys
import time
import datetime as dt
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
# tokens.json 은 도구 폴더(tools/) 안에 저장. 도구가 시드된 위치와 동일.
TOKENS_PATH = os.path.join(HERE, "tokens.json")
# .env 우선순위: 가능한 모든 레이아웃 커버.
#   1) HERE/.env                                     ← 시드된 tools/ 내부
#   2) HERE/../.env                                  ← _agents/instagram/.env (시드 후 위치)
#   3) HERE/../../../_company/_agents/instagram/.env ← 직접 실행 (assets/tool-seeds/instagram/ 에서)
#   4) HERE/../../../../_company/_agents/instagram/.env ← 한 단계 더 깊을 때
ENV_CANDIDATES = [
    os.path.join(HERE, ".env"),
    os.path.normpath(os.path.join(HERE, "..", ".env")),
    os.path.normpath(os.path.join(HERE, "..", "..", "..", "_company", "_agents", "instagram", ".env")),
    os.path.normpath(os.path.join(HERE, "..", "..", "..", "..", "_company", "_agents", "instagram", ".env")),
]

GRAPH_BASE = "https://graph.facebook.com/v18.0"
IG_BASE = "https://graph.instagram.com"
IG_API_BASE = "https://graph.instagram.com/v18.0"
THREADS_BASE = "https://graph.threads.net"
THREADS_API_BASE = "https://graph.threads.net/v1.0"
X_OAUTH_TOKEN_URL = "https://api.x.com/2/oauth2/token"

REFRESH_THRESHOLD_DAYS = 7  # 만료 N일 이내면 갱신 (Threads/IG)
X_REFRESH_THRESHOLD_SECONDS = 30 * 60  # X access_token 은 2시간 — 30분 이하면 갱신

# ─── env loading ───────────────────────────────────────────────────────────

def _load_env_files():
    """간단한 .env 로더 (외부 deps 없이). 기존 os.environ 우선."""
    for p in ENV_CANDIDATES:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            sys.stderr.write(f"⚠️  .env 로드 실패 ({p}): {e}\n")


def _mask(s: str) -> str:
    """토큰/시크릿 마스킹. 앞 4자 + ... + 길이."""
    if not s:
        return "<empty>"
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...({len(s)}자)"


# ─── HTTP helpers ──────────────────────────────────────────────────────────

def _http_json(url: str, *, method: str = "GET", data: Optional[dict] = None, timeout: int = 30) -> dict:
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err_body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 실패: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"응답 JSON 파싱 실패: {raw[:200]}")


# ─── tokens.json I/O ───────────────────────────────────────────────────────

def _load_tokens() -> dict:
    if not os.path.isfile(TOKENS_PATH):
        return {"threads": {}, "instagram": {}, "x": {}}
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"⚠️  tokens.json 파싱 실패: {e}\n")
        return {"threads": {}, "instagram": {}, "x": {}}
    data.setdefault("threads", {})
    data.setdefault("instagram", {})
    data.setdefault("x", {})
    return data


def _save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, TOKENS_PATH)
    try:
        os.chmod(TOKENS_PATH, 0o600)  # owner-only
    except Exception:
        pass


def _now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _expires_at_iso(seconds_from_now: int) -> str:
    expiry = dt.datetime.utcnow() + dt.timedelta(seconds=int(seconds_from_now))
    return expiry.replace(microsecond=0).isoformat() + "Z"


def _parse_iso(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        # accept trailing Z
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _days_until(iso: str) -> Optional[float]:
    d = _parse_iso(iso)
    if not d:
        return None
    delta = d - dt.datetime.utcnow()
    return delta.total_seconds() / 86400.0


# ─── account discovery from env ────────────────────────────────────────────

_THREADS_KEY_RE = re.compile(r"^META_THREADS_SHORT_TOKEN_(.+)$")
_IG_KEY_RE = re.compile(r"^META_IG_SHORT_TOKEN_(.+)$")
_X_TOKEN_KEY_RE = re.compile(r"^X_OAUTH_TOKEN_(.+)$")
_X_REFRESH_KEY_RE = re.compile(r"^X_OAUTH_REFRESH_TOKEN_(.+)$")


def _discover_accounts() -> Tuple[dict, dict, dict]:
    """환경에서 토큰 키들을 찾아 세 dict 반환.
       - threads: {account_lower: short_token}
       - ig: {account_lower: short_token}
       - x: {account_lower: {"access_token": "...", "refresh_token": "..."}}
    """
    threads = {}
    ig = {}
    x = {}
    for k, v in os.environ.items():
        v = (v or "").strip()
        if not v:
            continue
        m = _THREADS_KEY_RE.match(k)
        if m:
            threads[m.group(1).lower()] = v
            continue
        m = _IG_KEY_RE.match(k)
        if m:
            ig[m.group(1).lower()] = v
            continue
        m = _X_TOKEN_KEY_RE.match(k)
        if m:
            acct = m.group(1).lower()
            x.setdefault(acct, {})["access_token"] = v
            continue
        m = _X_REFRESH_KEY_RE.match(k)
        if m:
            acct = m.group(1).lower()
            x.setdefault(acct, {})["refresh_token"] = v
    return threads, ig, x


# ─── Threads token exchange ────────────────────────────────────────────────

def _threads_exchange_long(short_token: str, app_secret: str) -> dict:
    """단기 → 장기 (60일). expires_in 초 단위."""
    q = urllib.parse.urlencode({
        "grant_type": "th_exchange_token",
        "client_secret": app_secret,
        "access_token": short_token,
    })
    return _http_json(f"{THREADS_BASE}/access_token?{q}")


def _threads_refresh(long_token: str) -> dict:
    q = urllib.parse.urlencode({
        "grant_type": "th_refresh_token",
        "access_token": long_token,
    })
    return _http_json(f"{THREADS_BASE}/refresh_access_token?{q}")


def _threads_me_id(access_token: str) -> str:
    q = urllib.parse.urlencode({"fields": "id", "access_token": access_token})
    data = _http_json(f"{THREADS_API_BASE}/me?{q}")
    return str(data.get("id") or "")


# ─── Instagram token exchange ──────────────────────────────────────────────
# Instagram Business Login flow — graph.instagram.com 직접 호출.
# IGAAY... 접두사 토큰은 이 엔드포인트를 써야 함 (graph.facebook.com 아님).
# 시크릿은 Instagram sub-앱 시크릿 (Meta 메인 앱 시크릿과 별개).

def _ig_exchange_long(short_token: str, ig_app_secret: str) -> dict:
    """단기 IG 토큰 → 60일 장기 (graph.instagram.com + ig_exchange_token grant)."""
    q = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": ig_app_secret,
        "access_token": short_token,
    })
    return _http_json(f"{IG_BASE}/access_token?{q}")


def _ig_refresh(long_token: str) -> dict:
    """장기 IG 토큰 갱신 (만료 임박 시)."""
    q = urllib.parse.urlencode({
        "grant_type": "ig_refresh_token",
        "access_token": long_token,
    })
    return _http_json(f"{IG_BASE}/refresh_access_token?{q}")


def _ig_business_user_id(access_token: str) -> str:
    """IG Business Login 은 토큰이 곧 본인 IG 계정이라 /me 한 번이면 끝."""
    q = urllib.parse.urlencode({"fields": "id,username,account_type", "access_token": access_token})
    me = _http_json(f"{IG_API_BASE}/me?{q}")
    return str(me.get("id") or "")


# ─── X (Twitter) OAuth 2.0 ─────────────────────────────────────────────────
# Confidential client (Basic auth) 또는 Public PKCE (client_id only).
# refresh_token 으로 access_token 새로 발급. expires_in = 7200 (2시간) 기본.

def _x_refresh(refresh_token: str, client_id: str, client_secret: str) -> dict:
    """X OAuth 2.0 refresh_token → 새 access_token (+ rolling refresh_token)."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode("utf-8")
    req = urllib.request.Request(X_OAUTH_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if client_secret:
        import base64
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = ""
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"X refresh HTTP {e.code}: {err[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"X refresh 네트워크 실패: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"X refresh 응답 JSON 파싱 실패")


def _x_me_user_id(access_token: str) -> str:
    """현재 토큰의 user_id 조회 (https://api.x.com/2/users/me)."""
    req = urllib.request.Request("https://api.x.com/2/users/me")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return ""
    return str(((data.get("data") or {}).get("id")) or "")


def _x_seconds_until(iso: str) -> Optional[float]:
    d = _parse_iso(iso)
    if not d:
        return None
    return (d - dt.datetime.utcnow()).total_seconds()


# ─── telegram notify (옵션) ─────────────────────────────────────────────────

def _push_telegram(message: str):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": message[:4000],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


# ─── commands ──────────────────────────────────────────────────────────────

def cmd_bootstrap() -> int:
    _load_env_files()
    app_id = (os.environ.get("META_APP_ID") or "").strip()
    app_secret = (os.environ.get("META_APP_SECRET") or "").strip()
    # IG sub-앱은 메인 Meta 앱과 별도 ID/Secret. 비우면 메인 값으로 폴백 (단일 앱 단순 케이스).
    ig_app_secret = (os.environ.get("META_IG_APP_SECRET") or app_secret).strip()
    # Threads 의 경우 Meta UI '사용자 토큰 생성기' 가 이미 장기 토큰을 발급함.
    # 단기→장기 교환 (th_exchange_token) 은 이미 장기 토큰에 대해서는 실패하므로,
    # 항상 /me 검증 + refresh 경로로 처리 (단기 토큰이어도 refresh 가 알아서 처리).
    threads_assume_long = True
    sys.stderr.write("ℹ️  Threads: /me 검증 + refresh 로 진행 (이미 장기 토큰 가정).\n")
    if not ig_app_secret:
        sys.stderr.write("⚠️  META_IG_APP_SECRET 미설정 — Instagram 교환은 실패합니다 (Threads 만 진행).\n")

    threads_shorts, ig_shorts, x_shorts = _discover_accounts()
    x_client_id = (os.environ.get("X_CLIENT_ID") or "").strip()
    x_client_secret = (os.environ.get("X_CLIENT_SECRET") or "").strip()

    if not threads_shorts and not ig_shorts and not x_shorts:
        sys.stderr.write(
            "❌ 단기 토큰을 하나도 찾지 못했습니다.\n"
            "   .env 에 META_THREADS_SHORT_TOKEN_<계정> / META_IG_SHORT_TOKEN_<계정>\n"
            "   또는 X_OAUTH_TOKEN_<계정> + X_OAUTH_REFRESH_TOKEN_<계정> 를 추가하세요.\n"
        )
        return 1

    tokens = _load_tokens()
    results = []
    any_fail = False

    # Threads
    for acct, short in threads_shorts.items():
        try:
            if threads_assume_long:
                # 시크릿 없음 → 이미 장기 토큰으로 간주. /me 검증 후 refresh 로 expires_in 확인.
                user_id = _threads_me_id(short)
                try:
                    refresh_resp = _threads_refresh(short)
                    long_token = refresh_resp.get("access_token") or short
                    expires_in = int(refresh_resp.get("expires_in") or 60 * 86400)
                except Exception:
                    # refresh 실패시 토큰 그대로 + 보수적 60일 기본
                    long_token = short
                    expires_in = 60 * 86400
            else:
                resp = _threads_exchange_long(short, app_secret)
                long_token = resp.get("access_token") or ""
                expires_in = int(resp.get("expires_in") or 0)
                if not long_token or not expires_in:
                    raise RuntimeError(f"교환 응답에 access_token/expires_in 없음: {list(resp.keys())}")
                try:
                    user_id = _threads_me_id(long_token)
                except Exception as e:
                    user_id = ""
                    sys.stderr.write(f"⚠️  Threads[{acct}] user_id 조회 실패: {e}\n")
            tokens["threads"][acct] = {
                "access_token": long_token,
                "user_id": user_id,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            results.append(f"✅ Threads[{acct}] OK — token={_mask(long_token)} expires_in≈{expires_in // 86400}d")
        except Exception as e:
            any_fail = True
            results.append(f"❌ Threads[{acct}] 실패: {e}")

    # Instagram (graph.instagram.com 직접 호출)
    # Threads 와 같은 패턴 — Business Login UI/OAuth 가 발급하는 토큰은 이미 장기 토큰이라
    # ig_exchange_token 은 실패. refresh 가 항상 통하므로 그 경로 우선.
    for acct, short in ig_shorts.items():
        try:
            # 1) /me 로 토큰 유효성 + user_id 확인
            user_id = _ig_business_user_id(short)
            # 2) refresh 시도 (이미 장기 토큰이면 새 60일, 단기 토큰이면 실패 → exchange 폴백)
            try:
                resp = _ig_refresh(short)
                long_token = resp.get("access_token") or short
                expires_in = int(resp.get("expires_in") or 60 * 86400)
            except Exception as refresh_err:
                # 진짜 단기 토큰일 가능성 → exchange 시도
                if not ig_app_secret:
                    raise RuntimeError(f"refresh 실패 + 시크릿 없음: {refresh_err}")
                resp = _ig_exchange_long(short, ig_app_secret)
                long_token = resp.get("access_token") or ""
                expires_in = int(resp.get("expires_in") or 60 * 86400)
                if not long_token:
                    raise RuntimeError(f"교환 응답에 access_token 없음: {list(resp.keys())}")
            tokens["instagram"][acct] = {
                "access_token": long_token,
                "user_id": user_id,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            results.append(f"✅ Instagram[{acct}] OK — token={_mask(long_token)} expires_in≈{expires_in // 86400}d user_id={user_id or '(없음)'}")
        except Exception as e:
            any_fail = True
            results.append(f"❌ Instagram[{acct}] 실패: {e}")

    # X (Twitter) — env 의 access/refresh 를 tokens.json 에 통합 + 1회 즉시 refresh 시도
    for acct, pair in x_shorts.items():
        try:
            at = (pair.get("access_token") or "").strip()
            rt = (pair.get("refresh_token") or "").strip()
            if not at and not rt:
                raise RuntimeError("access_token / refresh_token 모두 비어있음")
            # 가능하면 즉시 refresh — 새 access_token + rolling refresh_token 확보
            if rt and x_client_id:
                try:
                    resp = _x_refresh(rt, x_client_id, x_client_secret)
                    new_at = resp.get("access_token") or at
                    new_rt = resp.get("refresh_token") or rt
                    expires_in = int(resp.get("expires_in") or 7200)
                    at = new_at
                    rt = new_rt
                except Exception as re_err:
                    # refresh 실패시 env 값 그대로 (만료 곧 닥칠 수 있음)
                    expires_in = 7200
                    sys.stderr.write(f"⚠️  X[{acct}] refresh 실패 — env 값 그대로 저장: {re_err}\n")
            else:
                expires_in = 7200
                if not x_client_id:
                    sys.stderr.write(f"⚠️  X_CLIENT_ID 미설정 — X[{acct}] refresh 불가\n")

            user_id = _x_me_user_id(at) if at else ""
            tokens["x"][acct] = {
                "access_token": at,
                "refresh_token": rt,
                "user_id": user_id,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            results.append(
                f"✅ X[{acct}] OK — at={_mask(at)} rt={_mask(rt)} expires_in≈{expires_in // 60}min"
            )
        except Exception as e:
            any_fail = True
            results.append(f"❌ X[{acct}] 실패: {e}")

    _save_tokens(tokens)

    print("\n".join(results))
    print(f"\n💾 저장 위치: {TOKENS_PATH}")
    if any_fail:
        _push_telegram("⚠️ Meta 토큰 bootstrap 일부 실패\n" + "\n".join(results))
        return 1
    _push_telegram("✅ Meta 토큰 bootstrap 완료\n" + "\n".join(results))
    return 0


def cmd_refresh() -> int:
    _load_env_files()
    app_id = (os.environ.get("META_APP_ID") or "").strip()
    app_secret = (os.environ.get("META_APP_SECRET") or "").strip()
    ig_app_secret = (os.environ.get("META_IG_APP_SECRET") or app_secret).strip()
    x_client_id = (os.environ.get("X_CLIENT_ID") or "").strip()
    x_client_secret = (os.environ.get("X_CLIENT_SECRET") or "").strip()

    tokens = _load_tokens()
    has_any = bool(tokens["threads"]) or bool(tokens["instagram"]) or bool(tokens.get("x"))
    if not has_any:
        sys.stderr.write("❌ tokens.json 이 비어 있습니다. 먼저 --bootstrap 을 실행하세요.\n")
        return 1

    refreshed = []
    skipped = []
    failed = []

    # Threads refresh
    for acct, info in list(tokens["threads"].items()):
        days = _days_until(info.get("expires_at", ""))
        if days is None:
            failed.append(f"Threads[{acct}]: expires_at 누락 — bootstrap 재실행 필요")
            continue
        if days > REFRESH_THRESHOLD_DAYS:
            skipped.append(f"Threads[{acct}] 건강 ({days:.1f}d 남음)")
            continue
        try:
            resp = _threads_refresh(info["access_token"])
            new_token = resp.get("access_token") or info["access_token"]
            expires_in = int(resp.get("expires_in") or 0)
            if not expires_in:
                expires_in = 60 * 86400
            tokens["threads"][acct] = {
                **info,
                "access_token": new_token,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            refreshed.append(f"Threads[{acct}] 갱신 OK (+{expires_in // 86400}d)")
        except Exception as e:
            failed.append(f"Threads[{acct}]: {e}")

    # Instagram refresh — re-exchange 방식 (현재 long token 을 단기 자리에 넣음)
    for acct, info in list(tokens["instagram"].items()):
        days = _days_until(info.get("expires_at", ""))
        if days is None:
            failed.append(f"Instagram[{acct}]: expires_at 누락 — bootstrap 재실행 필요")
            continue
        if days > REFRESH_THRESHOLD_DAYS:
            skipped.append(f"Instagram[{acct}] 건강 ({days:.1f}d 남음)")
            continue
        try:
            # IG long-lived 갱신은 ig_refresh_token grant (시크릿 불필요)
            resp = _ig_refresh(info["access_token"])
            new_token = resp.get("access_token") or info["access_token"]
            expires_in = int(resp.get("expires_in") or 0)
            if not expires_in:
                expires_in = 60 * 86400
            tokens["instagram"][acct] = {
                **info,
                "access_token": new_token,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            refreshed.append(f"Instagram[{acct}] 갱신 OK (+{expires_in // 86400}d)")
        except Exception as e:
            failed.append(f"Instagram[{acct}]: {e}")

    # X (Twitter) — 30분 임계값. 만료 임박이면 refresh_token 으로 새 access_token 발급.
    for acct, info in list(tokens.get("x", {}).items()):
        secs = _x_seconds_until(info.get("expires_at", ""))
        if secs is None:
            failed.append(f"X[{acct}]: expires_at 누락 — bootstrap 재실행 필요")
            continue
        if secs > X_REFRESH_THRESHOLD_SECONDS:
            skipped.append(f"X[{acct}] 건강 ({secs/60:.1f}min 남음)")
            continue
        rt = info.get("refresh_token") or ""
        if not rt:
            failed.append(f"X[{acct}]: refresh_token 없음 — 재발급 필요")
            continue
        if not x_client_id:
            failed.append(f"X[{acct}]: X_CLIENT_ID 미설정 — .env 확인")
            continue
        try:
            resp = _x_refresh(rt, x_client_id, x_client_secret)
            new_at = resp.get("access_token") or info["access_token"]
            new_rt = resp.get("refresh_token") or rt
            expires_in = int(resp.get("expires_in") or 7200)
            tokens["x"][acct] = {
                **info,
                "access_token": new_at,
                "refresh_token": new_rt,
                "expires_at": _expires_at_iso(expires_in),
                "refreshed_at": _now_iso(),
            }
            refreshed.append(f"X[{acct}] 갱신 OK (+{expires_in // 60}min)")
        except Exception as e:
            failed.append(f"X[{acct}]: {e}")

    if refreshed or failed:
        _save_tokens(tokens)

    if not refreshed and not failed:
        print("✅ All tokens healthy — 갱신 필요 없음")
        return 0

    if refreshed:
        print("\n".join("✅ " + s for s in refreshed))
    if skipped:
        print("\n".join("⏸  " + s for s in skipped))
    if failed:
        sys.stderr.write("\n".join("❌ " + s for s in failed) + "\n")
        _push_telegram("⚠️ Meta 토큰 갱신 일부 실패\n" + "\n".join(failed))
        return 1

    _push_telegram("✅ Meta 토큰 갱신 완료\n" + "\n".join(refreshed))
    return 0


def cmd_status() -> int:
    _load_env_files()
    tokens = _load_tokens()
    has_any = bool(tokens["threads"]) or bool(tokens["instagram"]) or bool(tokens.get("x"))
    if not has_any:
        sys.stderr.write("❌ No tokens initialized. Run --bootstrap first.\n")
        return 1

    rows = []
    expired = 0
    warning = 0
    for platform in ("threads", "instagram", "x"):
        for acct in sorted(tokens.get(platform, {}).keys()):
            info = tokens[platform][acct]
            refreshed = info.get("refreshed_at", "(없음)")
            user_id = info.get("user_id", "") or "(없음)"
            if platform == "x":
                # X 는 분 단위 표시
                secs = _x_seconds_until(info.get("expires_at", ""))
                if secs is None:
                    mark = "?"
                    days_str = "?"
                elif secs < 0:
                    mark = "❌"
                    days_str = f"{secs/60:.1f}min"
                    expired += 1
                elif secs <= X_REFRESH_THRESHOLD_SECONDS:
                    mark = "⚠️"
                    days_str = f"{secs/60:.1f}min"
                    warning += 1
                else:
                    mark = "✅"
                    days_str = f"{secs/60:.1f}min"
            else:
                days = _days_until(info.get("expires_at", ""))
                if days is None:
                    mark = "?"
                    days_str = "?"
                elif days < 0:
                    mark = "❌"
                    days_str = f"{days:.1f}d"
                    expired += 1
                elif days <= REFRESH_THRESHOLD_DAYS:
                    mark = "⚠️"
                    days_str = f"{days:.1f}d"
                    warning += 1
                else:
                    mark = "✅"
                    days_str = f"{days:.1f}d"
            rows.append((mark, platform, acct, days_str, refreshed, user_id))

    # 표 출력 (X 는 분 단위, 그 외는 일 단위)
    header = ("", "Platform", "Account", "Time left", "Last refresh", "User ID")
    widths = [max(len(str(r[i])) for r in (rows + [header])) for i in range(len(header))]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*header))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*r))

    if expired:
        sys.stderr.write(f"\n❌ 만료된 토큰 {expired}개 — bootstrap 재실행 또는 OAuth 재발급 필요\n")
        return 1
    if warning:
        print(f"\n⚠️  만료 임박 토큰 {warning}개 — --refresh 권장")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Meta 토큰 자동 관리 (Threads + Instagram 멀티 계정)",
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--bootstrap", action="store_true",
                       help="단기 토큰 → 장기(60d) 교환, tokens.json 생성")
    group.add_argument("--refresh", action="store_true",
                       help="만료 7일 이내 토큰만 자동 갱신")
    group.add_argument("--status", action="store_true",
                       help="모든 토큰 남은 일수 표 출력 (cron 알람용)")
    args = ap.parse_args()

    if args.bootstrap:
        return cmd_bootstrap()
    if args.refresh:
        return cmd_refresh()
    if args.status:
        return cmd_status()
    return 1


if __name__ == "__main__":
    sys.exit(main())
