#!/usr/bin/env python3
# version: x_uploader_v1
"""X (Twitter) 자동 업로더 (draft mode 기본, 멀티 계정 + OAuth 2.0).

X API v2 + OAuth 2.0 (PKCE / Authorization Code) 기반.
외부 라이브러리 없음 — urllib.request 만 사용.

사용법:
  1) draft 모드 (토큰 없어도 가능):
       python3 x_uploader.py --text "안녕 X"
     → drafts/x-YYYYMMDD-HHMMSS-default.md 로 저장

  2) 멀티 계정 게시:
       # 사전: token_manager.py --bootstrap (X 토큰까지 같이 관리)
       python3 x_uploader.py --text "안녕" --account jp
       python3 x_uploader.py --text "안녕" --account kr

  3) 이미지/영상 첨부:
       python3 x_uploader.py --text "오늘의 풍경" \\
         --media-url "https://cdn/foo.jpg" --media-type image --account jp
       python3 x_uploader.py --text "릴" \\
         --media-url "https://cdn/clip.mp4" --media-type video --account jp

  4) 답글:
       python3 x_uploader.py --text "감사합니다" \\
         --reply-to 1234567890 --account jp

옵션:
  --text          본문 (필수, 280자 제한)
  --media-url     미디어 URL (다중 가능 — --media-url u1 --media-url u2)
  --media-type    image | video (미디어 있을 때 필수)
  --account       jp / kr / ... (tokens.json 의 x[*] 키)
  --reply-to      답글 대상 tweet_id (선택)
  --dry-run       토큰 있어도 강제 draft

토큰 소스 우선순위:
  1) tokens.json 의 x[{account}] (token_manager.py 가 관리)
  2) access_token 만료/임박 → refresh_token 으로 자동 재발급 시도
  3) 환경변수 X_OAUTH_TOKEN_{ACCT} + X_OAUTH_REFRESH_TOKEN_{ACCT}

LLM 호출 0회.

────────────────────────────────────────────────────────────────────
X Developer Portal — User OAuth 2.0 토큰 발급 6단계 (요약)
────────────────────────────────────────────────────────────────────
1) https://developer.x.com → Projects & Apps → 신규 App 생성
2) App → Settings → "User authentication settings" 활성화
3) Type = "Confidential client" 선택 (refresh_token 받으려면 필수)
   App permissions = "Read and write" (게시용)
   Callback URI = http://localhost:8080/callback (PKCE 콜백)
4) 발급된 Client ID / Client Secret 복사 → .env 에 X_CLIENT_ID / X_CLIENT_SECRET
5) PKCE flow 로 user access_token / refresh_token 받기:
     https://twitter.com/i/oauth2/authorize?
       response_type=code&
       client_id={CLIENT_ID}&
       redirect_uri=http://localhost:8080/callback&
       scope=tweet.read%20tweet.write%20users.read%20offline.access&
       state=state&
       code_challenge=challenge&code_challenge_method=plain
   브라우저 승인 → code 추출 → POST https://api.x.com/2/oauth2/token
     grant_type=authorization_code&code={CODE}&
     redirect_uri=...&code_verifier=challenge
6) 응답의 access_token (2시간) + refresh_token (장기) 을 .env 에
   X_OAUTH_TOKEN_JP / X_OAUTH_REFRESH_TOKEN_JP 처럼 계정별 저장.
   이후 token_manager.py --bootstrap → tokens.json 에 통합 + 자동 갱신.
"""
import argparse
import base64
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
TOKEN_MANAGER_PATH = os.path.join(REPO_ROOT, "src", "auth", "token_manager.py")

API_TWEETS = "https://api.x.com/2/tweets"
API_OAUTH_TOKEN = "https://api.x.com/2/oauth2/token"
API_MEDIA_UPLOAD_V2 = "https://api.x.com/2/media/upload"
UPLOAD_V1 = "https://upload.twitter.com/1.1/media/upload.json"

# X access token 은 2시간 — 30분 이하면 자동 갱신.
REFRESH_THRESHOLD_SECONDS = 30 * 60


# ─── 유틸 ────────────────────────────────────────────────────────────────────

def _ensure_drafts_dir():
    os.makedirs(DRAFTS_DIR, exist_ok=True)


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _preview(text: str, n: int = 100) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n] + "..."


def _push_telegram(message: str):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": message[:4000]}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _save_draft(text: str, media_urls, media_type: str, reply_to: str, account: str) -> str:
    _ensure_drafts_dir()
    stamp = _now_stamp()
    path = os.path.join(DRAFTS_DIR, f"x-{stamp}-{account}.md")
    fm = [
        "---",
        "status: draft",
        "target: x",
        f"account: {account}",
        f"created_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
    ]
    if media_urls:
        fm.append(f"media_type: {media_type}")
        for u in media_urls:
            fm.append(f"media_url: {u}")
    if reply_to:
        fm.append(f"reply_to: {reply_to}")
    fm.append("---")
    body = "\n".join(fm) + "\n\n" + (text or "") + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# ─── HTTP 헬퍼 ──────────────────────────────────────────────────────────────

def _http_json(url: str, *, method: str = "GET",
               headers: dict = None, data=None, timeout: int = 60) -> dict:
    """data: bytes | dict(form-urlencoded). JSON body 는 caller 가 직접 직렬화."""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = ""
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 실패: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"X API 응답 JSON 파싱 실패: {raw[:200]}")


def _http_json_body(url: str, *, body: dict, bearer: str, method: str = "POST") -> dict:
    raw_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw_bytes, method=method)
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = ""
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 실패: {e.reason}")


def _http_download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _multipart_post(url: str, *, bearer: str, fields: dict,
                    file_field: str = None, file_bytes: bytes = None,
                    filename: str = "file.bin", content_type: str = "application/octet-stream") -> dict:
    """간단한 multipart/form-data POST. urllib 만 사용."""
    boundary = "----xupload" + secrets.token_hex(8)
    lines = []
    for k, v in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{k}"'.encode())
        lines.append(b"")
        lines.append(str(v).encode("utf-8"))
    if file_field and file_bytes is not None:
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
        )
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {bearer}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = ""
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {err[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 실패: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"upload 응답 파싱 실패: {raw[:200]}")


# ─── tokens.json ────────────────────────────────────────────────────────────

def _load_tokens() -> dict:
    if not os.path.isfile(TOKENS_PATH):
        return {}
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, TOKENS_PATH)
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except Exception:
        pass


def _seconds_until(iso: str):
    if not iso:
        return None
    try:
        s = iso[:-1] if iso.endswith("Z") else iso
        d = dt.datetime.fromisoformat(s)
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return (d - now).total_seconds()
    except Exception:
        return None


def _try_auto_refresh():
    if not os.path.isfile(TOKEN_MANAGER_PATH):
        return False
    try:
        subprocess.run(
            [sys.executable, TOKEN_MANAGER_PATH, "--refresh"],
            timeout=60,
            capture_output=True,
        )
        return True
    except Exception:
        return False


def _x_refresh_inline(refresh_token: str, client_id: str, client_secret: str) -> dict:
    """우리가 직접 한 번 더 시도 (token_manager 없을 때 fallback)."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode("utf-8")
    req = urllib.request.Request(API_OAUTH_TOKEN, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if client_secret:
        # Confidential client → Basic auth
        import base64
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"refresh HTTP {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"refresh 네트워크 실패: {e.reason}")


def _resolve_credentials(account: str):
    """우선순위:
       1) tokens.json[x][{account}] (token_manager.py 관리)
       2) 만료 임박 → token_manager.py --refresh 자동 호출 후 재로딩
       3) env: X_OAUTH_TOKEN_{ACCT} (+ X_OAUTH_REFRESH_TOKEN_{ACCT})
       반환: (access_token, refresh_token, source)
    """
    tokens = _load_tokens()
    x_section = (tokens.get("x") or {})
    info = x_section.get(account)
    if info and info.get("access_token"):
        secs = _seconds_until(info.get("expires_at", ""))
        if secs is not None and secs <= REFRESH_THRESHOLD_SECONDS:
            # 만료 임박 → token_manager 갱신 시도
            _try_auto_refresh()
            tokens = _load_tokens()
            info = (tokens.get("x") or {}).get(account) or info
            # 그래도 부족하면 inline refresh
            secs2 = _seconds_until(info.get("expires_at", ""))
            if secs2 is None or secs2 <= 60:
                rt = info.get("refresh_token") or ""
                cid = (os.environ.get("X_CLIENT_ID") or "").strip()
                csec = (os.environ.get("X_CLIENT_SECRET") or "").strip()
                if rt and cid:
                    try:
                        resp = _x_refresh_inline(rt, cid, csec)
                        new_at = resp.get("access_token") or info.get("access_token")
                        new_rt = resp.get("refresh_token") or rt
                        exp_in = int(resp.get("expires_in") or 7200)
                        info = {
                            **info,
                            "access_token": new_at,
                            "refresh_token": new_rt,
                            "expires_at": (
                                dt.datetime.now(dt.UTC).replace(tzinfo=None)
                                + dt.timedelta(seconds=exp_in)
                            ).replace(microsecond=0).isoformat() + "Z",
                            "refreshed_at": dt.datetime.now(dt.UTC).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z",
                        }
                        tokens.setdefault("x", {})[account] = info
                        _save_tokens(tokens)
                    except Exception:
                        pass
        return info.get("access_token", ""), info.get("refresh_token", ""), "tokens.json"

    env_at = (os.environ.get(f"X_OAUTH_TOKEN_{account.upper()}") or "").strip()
    env_rt = (os.environ.get(f"X_OAUTH_REFRESH_TOKEN_{account.upper()}") or "").strip()
    if env_at:
        return env_at, env_rt, "env"
    return "", "", "none"


# ─── 미디어 업로드 ─────────────────────────────────────────────────────────

def _guess_content_type(url: str, media_type: str) -> tuple:
    """returns (content_type, filename)."""
    lower = url.lower().split("?", 1)[0]
    if media_type == "video" or lower.endswith(".mp4"):
        return ("video/mp4", "clip.mp4")
    if lower.endswith(".png"):
        return ("image/png", "img.png")
    if lower.endswith(".gif"):
        return ("image/gif", "img.gif")
    if lower.endswith(".webp"):
        return ("image/webp", "img.webp")
    return ("image/jpeg", "img.jpg")


def _upload_image(media_bytes: bytes, content_type: str, filename: str, bearer: str) -> str:
    """X API v2 media upload for OAuth 2.0 user tokens."""
    payload = {
        "media": base64.b64encode(media_bytes).decode("ascii"),
        "media_category": "tweet_image",
        "media_type": content_type,
        "shared": False,
    }
    resp = _http_json_body(API_MEDIA_UPLOAD_V2, body=payload, bearer=bearer, method="POST")
    data = resp.get("data") or {}
    mid = str(data.get("id") or data.get("media_id") or data.get("media_key") or "")
    if not mid:
        raise RuntimeError(f"image upload 응답에 media_id 없음: {resp}")
    return mid


def _upload_video_chunked(media_bytes: bytes, bearer: str,
                          content_type: str = "video/mp4") -> str:
    """v1.1 chunked: INIT → APPEND × N → FINALIZE → STATUS poll."""
    total = len(media_bytes)

    # INIT
    init = _multipart_post(
        UPLOAD_V1,
        bearer=bearer,
        fields={
            "command": "INIT",
            "total_bytes": total,
            "media_type": content_type,
            "media_category": "tweet_video",
        },
    )
    mid = init.get("media_id_string") or str(init.get("media_id") or "")
    if not mid:
        raise RuntimeError(f"video INIT 응답에 media_id 없음: {init}")

    # APPEND (5MB chunk)
    CHUNK = 5 * 1024 * 1024
    seg = 0
    for off in range(0, total, CHUNK):
        part = media_bytes[off: off + CHUNK]
        _multipart_post(
            UPLOAD_V1,
            bearer=bearer,
            fields={
                "command": "APPEND",
                "media_id": mid,
                "segment_index": seg,
            },
            file_field="media",
            file_bytes=part,
            filename="chunk.bin",
            content_type="application/octet-stream",
        )
        seg += 1

    # FINALIZE
    fin = _multipart_post(
        UPLOAD_V1,
        bearer=bearer,
        fields={"command": "FINALIZE", "media_id": mid},
    )

    # STATUS poll (processing_info 있을 때만)
    pi = fin.get("processing_info") or {}
    state = pi.get("state")
    check_after = int(pi.get("check_after_secs") or 5)
    tries = 0
    while state in ("pending", "in_progress") and tries < 30:
        time.sleep(min(check_after, 10))
        status_url = f"{UPLOAD_V1}?command=STATUS&media_id={urllib.parse.quote(mid)}"
        req = urllib.request.Request(status_url)
        req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"STATUS HTTP {e.code}")
        pi = resp.get("processing_info") or {}
        state = pi.get("state")
        check_after = int(pi.get("check_after_secs") or 5)
        tries += 1
    if state == "failed":
        raise RuntimeError(f"video 처리 실패: {pi.get('error', pi)}")
    return mid


def _ingest_media(media_urls, media_type: str, bearer: str) -> list:
    out = []
    for u in media_urls:
        b = _http_download(u)
        ct, fn = _guess_content_type(u, media_type)
        if media_type == "video":
            mid = _upload_video_chunked(b, bearer, content_type=ct)
        else:
            mid = _upload_image(b, ct, fn, bearer)
        out.append(mid)
    return out


# ─── 트윗 게시 ──────────────────────────────────────────────────────────────

def _post_tweet(text: str, media_ids: list, reply_to: str, bearer: str) -> dict:
    body = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    return _http_json_body(API_TWEETS, body=body, bearer=bearer, method="POST")


def _real_post(text: str, media_urls, media_type: str, reply_to: str,
               access_token: str) -> dict:
    media_ids = []
    if media_urls:
        media_ids = _ingest_media(media_urls, media_type, access_token)
    resp = _post_tweet(text, media_ids, reply_to, access_token)
    data = resp.get("data") or {}
    tweet_id = str(data.get("id") or "")
    if not tweet_id:
        raise RuntimeError(f"tweet 응답에 id 없음: {resp}")
    permalink = f"https://x.com/i/web/status/{tweet_id}"
    return {"tweet_id": tweet_id, "permalink": permalink}


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="X (Twitter) 자동 업로더 (멀티 계정)")
    ap.add_argument("--text", required=True, help="트윗 본문 (≤ 280자)")
    ap.add_argument("--account", default="default",
                    help="tokens.json 의 x[<account>] 키 (예: jp, kr). 기본: default")
    ap.add_argument("--media-url", action="append", default=[],
                    help="미디어 URL (다중 가능 — --media-url u1 --media-url u2)")
    ap.add_argument("--media-type", choices=["image", "video"], default=None,
                    help="media-url 있을 때 필수: image | video")
    ap.add_argument("--reply-to", default="",
                    help="(선택) 답글 대상 tweet_id")
    ap.add_argument("--dry-run", action="store_true",
                    help="토큰 있어도 강제 draft 저장")
    args = ap.parse_args()

    account = (args.account or "default").lower()
    media_urls = [u for u in (args.media_url or []) if u]
    media_type = (args.media_type or "").lower()

    if media_urls and not media_type:
        sys.stderr.write("❌ --media-url 이 있으면 --media-type image|video 필수\n")
        return 2

    # 280자 워닝 (Free tier). 실제 제한은 X 가 다시 검증.
    if len(args.text or "") > 280:
        sys.stderr.write(f"⚠️  본문이 280자 초과 ({len(args.text)}자) — Free tier 거부 가능\n")

    draft_env = (os.environ.get("DRAFT_MODE") or "").strip().lower() in ("1", "true", "yes")
    access_token, refresh_token, source = _resolve_credentials(account)
    use_draft = args.dry_run or draft_env or (not access_token)

    if use_draft:
        path = _save_draft(args.text, media_urls, media_type or "text",
                           args.reply_to, account)
        preview = _preview(args.text)
        _push_telegram(f"✏️ 새 X draft 저장됨 [{account}]\n{preview}\n📁 {path}")
        print(json.dumps({
            "status": "drafted",
            "account": account,
            "path": path,
            "preview": preview,
            "token_source": source,
        }, ensure_ascii=False))
        return 0

    try:
        result = _real_post(args.text, media_urls, media_type, args.reply_to,
                            access_token)
    except Exception as e:
        msg = str(e)
        # 401 → refresh 한 번 시도 후 재시도
        if "401" in msg or "expired" in msg.lower() or "invalid_token" in msg.lower():
            cid = (os.environ.get("X_CLIENT_ID") or "").strip()
            csec = (os.environ.get("X_CLIENT_SECRET") or "").strip()
            refreshed = False
            if refresh_token and cid:
                try:
                    resp = _x_refresh_inline(refresh_token, cid, csec)
                    access_token = resp.get("access_token") or access_token
                    new_rt = resp.get("refresh_token") or refresh_token
                    exp_in = int(resp.get("expires_in") or 7200)
                    tokens = _load_tokens()
                    tokens.setdefault("x", {}).setdefault(account, {}).update({
                        "access_token": access_token,
                        "refresh_token": new_rt,
                        "expires_at": (
                            dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(seconds=exp_in)
                        ).replace(microsecond=0).isoformat() + "Z",
                        "refreshed_at": dt.datetime.now(dt.UTC).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z",
                    })
                    _save_tokens(tokens)
                    refreshed = True
                except Exception:
                    pass
            if not refreshed:
                _try_auto_refresh()
                access_token, refresh_token, source = _resolve_credentials(account)
                refreshed = bool(access_token)
            if refreshed:
                try:
                    result = _real_post(args.text, media_urls, media_type,
                                        args.reply_to, access_token)
                except Exception as e2:
                    sys.stderr.write(f"❌ X 게시 실패 [{account}]: {e2}\n")
                    return 1
            else:
                sys.stderr.write(f"❌ X 게시 실패 [{account}] (refresh 불가): {e}\n")
                sys.stderr.write("   → X Developer Portal 에서 PKCE flow 로 재발급 권장\n")
                return 1
        else:
            sys.stderr.write(f"❌ X 게시 실패 [{account}]: {e}\n")
            return 1

    print(json.dumps({
        "status": "posted",
        "account": account,
        "permalink": result.get("permalink", ""),
        "tweet_id": result.get("tweet_id", ""),
        "token_source": source,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
