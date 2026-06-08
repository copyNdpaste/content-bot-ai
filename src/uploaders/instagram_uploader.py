#!/usr/bin/env python3
# version: instagram_uploader_v3_reels_carousel
"""Instagram 자동 업로더 (멀티 계정 + IMAGE/REELS/CAROUSEL 실제 구현).

사용법:
  1. draft 모드 (기본 — 토큰 없어도 가능):
       python instagram_uploader.py --caption "오늘의 한 컷" \
           --media-url "https://cdn/photo.jpg"

  2. 단일 이미지:
       python instagram_uploader.py --caption "..." \
         --media-url "https://cdn/photo.jpg" --media-type IMAGE --account jp

  3. REELS (영상, ≤ 90초 권장):
       python instagram_uploader.py --caption "..." \
         --media-url "https://cdn/clip.mp4" --media-type REELS --account jp

  4. CAROUSEL (2~10장, image/video 혼합 가능):
       python instagram_uploader.py --caption "..." \
         --media-url "https://cdn/a.jpg" \
         --media-url "https://cdn/b.mp4" \
         --media-url "https://cdn/c.jpg" \
         --media-type CAROUSEL --carousel-types image,video,image \
         --account jp

  5. Legacy 단일 계정 (호환):
       META_IG_ACCESS_TOKEN=xxx META_IG_USER_ID=yyy \
       python instagram_uploader.py --caption "..." --image-url "..."

옵션:
  --account         jp / kr / ... (tokens.json 의 instagram[*] 키). 기본: default
  --media-type      IMAGE | REELS | CAROUSEL (기본 IMAGE)
  --media-url       (다중) — CAROUSEL 일 때 2~10개
  --image-url       (legacy 호환) — 단일 IMAGE 용, --media-url 와 동등
  --carousel-types  CAROUSEL 에서 각 미디어 타입 ('image,video,image' 등)
  --dry-run         토큰이 있어도 강제 draft 저장

토큰 소스 우선순위:
  1) tokens.json 의 instagram[{account}] (token_manager.py 가 관리)
  2) 만료 임박/만료 → token_manager.py --refresh 자동 호출 후 재시도
  3) 환경변수 META_IG_ACCESS_TOKEN + META_IG_USER_ID (legacy fallback)

LLM 호출 0회.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
TOKEN_MANAGER_PATH = os.path.join(REPO_ROOT, "src", "auth", "token_manager.py")
RUNTIME_DIR = os.path.join(REPO_ROOT, ".runtime")
COOLDOWNS_PATH = os.path.join(RUNTIME_DIR, "instagram_cooldowns.json")
# Instagram Business Login 토큰(IGAA...)은 graph.instagram.com 엔드포인트를 써야 한다.
IG_API_BASE = "https://graph.instagram.com/v18.0"

REFRESH_THRESHOLD_DAYS = 7
ACTION_BLOCK_COOLDOWN_HOURS = 24


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
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": message[:4000],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _save_draft(caption: str, media_urls, carousel_types,
                media_type: str, account: str) -> str:
    _ensure_drafts_dir()
    stamp = _now_stamp()
    path = os.path.join(DRAFTS_DIR, f"instagram-{account}-{stamp}.md")
    fm = [
        "---",
        "status: draft",
        "target: instagram",
        f"account: {account}",
        f"created_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"media_type: {media_type}",
    ]
    for u in (media_urls or []):
        fm.append(f"media_url: {u}")
    if carousel_types:
        fm.append(f"carousel_types: {','.join(carousel_types)}")
    fm.append("---")
    body = "\n".join(fm) + "\n\n" + (caption or "") + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _http_post(url: str, payload: dict) -> dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"IG API 응답 파싱 실패: {raw[:200]}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"IG API HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"IG API 네트워크 실패: {e.reason}")


def _http_get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"IG API HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"IG API 네트워크 실패: {e.reason}")


def _extract_error_json(message: str) -> dict:
    """RuntimeError 문자열에 포함된 Graph API JSON body 를 best-effort 로 추출."""
    if not message:
        return {}
    start = message.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(message[start:])
    except Exception:
        return {}


def _is_action_block_error(message: str) -> bool:
    data = _extract_error_json(message)
    err = data.get("error") or {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    user_title = str(err.get("error_user_title") or "")
    user_msg = str(err.get("error_user_msg") or "")
    text = " ".join([message, user_title, user_msg])
    return (
        code == 4
        or subcode == 2207051
        or "Application request limit reached" in text
        or "행동이 차단" in text
    )


def _is_media_not_available_error(message: str) -> bool:
    data = _extract_error_json(message)
    err = data.get("error") or {}
    text = " ".join([
        message or "",
        str(err.get("message") or ""),
        str(err.get("error_user_msg") or ""),
    ])
    return (
        err.get("code") == 9007
        or err.get("error_subcode") == 2207027
        or "Media ID is not available" in text
        or "아직 준비가 완료되지 않아" in text
    )


def _load_cooldowns() -> dict:
    if not os.path.isfile(COOLDOWNS_PATH):
        return {}
    try:
        with open(COOLDOWNS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_cooldowns(data: dict) -> None:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    tmp = COOLDOWNS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, COOLDOWNS_PATH)


def _parse_utc(iso: str):
    if not iso:
        return None
    try:
        s = iso[:-1] if iso.endswith("Z") else iso
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _cooldown_remaining(account: str) -> tuple[bool, str]:
    info = (_load_cooldowns().get(account) or {})
    until = _parse_utc(info.get("until", ""))
    if not until:
        return False, ""
    remaining = (until - _utc_now()).total_seconds()
    if remaining <= 0:
        return False, ""
    hours = max(1, int((remaining + 3599) // 3600))
    return True, (
        f"Instagram 계정 [{account}]은 Meta 행동 제한으로 쿨다운 중입니다. "
        f"약 {hours}시간 후 재시도하세요. until={info.get('until')}"
    )


def _set_action_block_cooldown(account: str, raw_error: str) -> str:
    until = (
        _utc_now() + dt.timedelta(hours=ACTION_BLOCK_COOLDOWN_HOURS)
    ).replace(microsecond=0).isoformat() + "Z"
    data = _load_cooldowns()
    data[account] = {
        "until": until,
        "reason": "instagram_action_block",
        "last_error": raw_error[:1000],
        "updated_at": _utc_now().replace(microsecond=0).isoformat() + "Z",
    }
    _save_cooldowns(data)
    return until


def _poll_container_status(container_id: str, access_token: str,
                           max_tries: int = 30, interval: int = 5) -> str:
    """컨테이너가 publish 가능한 FINISHED 상태가 될 때까지 폴링."""
    q = urllib.parse.urlencode({
        "fields": "status_code,status",
        "access_token": access_token,
    })
    status_url = f"{IG_API_BASE}/{urllib.parse.quote(container_id)}?{q}"
    last = ""
    for _ in range(max_tries):
        try:
            data = _http_get(status_url)
        except RuntimeError as e:
            last = str(e)[:160]
            time.sleep(interval)
            continue

        sc = (data.get("status_code") or "").upper()
        last = sc
        if sc == "FINISHED":
            return sc
        if sc in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                f"IG container 처리 실패 (status_code={sc}): {data.get('status', '')}"
            )
        time.sleep(interval)
    raise RuntimeError(f"IG container 인코딩 타임아웃 (last={last or '?'})")


def _create_container(ig_user_id: str, payload: dict) -> str:
    create_url = f"{IG_API_BASE}/{urllib.parse.quote(ig_user_id)}/media"
    created = _http_post(create_url, payload)
    cid = created.get("id")
    if not cid:
        raise RuntimeError(f"IG create 응답에 id 없음: {created}")
    return cid


def _publish_container(ig_user_id: str, creation_id: str, access_token: str) -> dict:
    publish_url = f"{IG_API_BASE}/{urllib.parse.quote(ig_user_id)}/media_publish"
    last_error = ""
    for attempt in range(6):
        try:
            return _http_post(publish_url, {
                "creation_id": creation_id,
                "access_token": access_token,
            })
        except RuntimeError as e:
            last_error = str(e)
            if not _is_media_not_available_error(last_error) or attempt == 5:
                raise
            _poll_container_status(creation_id, access_token, max_tries=6, interval=5)
            time.sleep(5)
    raise RuntimeError(last_error or "IG media_publish 실패")


def _real_post(caption: str, media_urls, carousel_types, media_type: str,
               access_token: str, ig_user_id: str) -> dict:
    mt = (media_type or "IMAGE").upper()

    # ─── CAROUSEL ──────────────────────────────────────────────────────
    if mt == "CAROUSEL":
        if not media_urls or len(media_urls) < 2:
            raise RuntimeError("CAROUSEL 은 미디어 2개 이상 필요")
        if len(media_urls) > 10:
            raise RuntimeError("CAROUSEL 은 최대 10개")
        # carousel_types 가 비었으면 모두 image 로 가정
        types = [t.strip().lower() for t in (carousel_types or [])]
        while len(types) < len(media_urls):
            types.append("image")

        child_ids = []
        for url, t in zip(media_urls, types):
            payload = {
                "is_carousel_item": "true",
                "access_token": access_token,
            }
            if t == "video":
                payload["media_type"] = "VIDEO"
                payload["video_url"] = url
            else:
                payload["image_url"] = url
            cid = _create_container(ig_user_id, payload)
            # 영상 child 는 status polling 필요
            if t == "video":
                _poll_container_status(cid, access_token)
            child_ids.append(cid)

        # 부모 컨테이너
        parent_payload = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": access_token,
        }
        parent_id = _create_container(ig_user_id, parent_payload)
        _poll_container_status(parent_id, access_token)
        publish_creation_id = parent_id

    # ─── REELS ─────────────────────────────────────────────────────────
    elif mt == "REELS":
        if not media_urls:
            raise RuntimeError("REELS 는 영상 URL 1개 필요")
        payload = {
            "media_type": "REELS",
            "video_url": media_urls[0],
            "caption": caption,
            "access_token": access_token,
        }
        creation_id = _create_container(ig_user_id, payload)
        _poll_container_status(creation_id, access_token)
        publish_creation_id = creation_id

    # ─── IMAGE (default) ───────────────────────────────────────────────
    else:
        if not media_urls:
            raise RuntimeError("IMAGE 는 이미지 URL 1개 필요")
        payload = {
            "image_url": media_urls[0],
            "caption": caption,
            "access_token": access_token,
        }
        creation_id = _create_container(ig_user_id, payload)
        _poll_container_status(creation_id, access_token)
        publish_creation_id = creation_id

    # publish
    published = _publish_container(ig_user_id, publish_creation_id, access_token)
    media_id = published.get("id") or publish_creation_id

    # permalink (best-effort)
    permalink = ""
    try:
        q = urllib.parse.urlencode({
            "fields": "permalink",
            "access_token": access_token,
        })
        meta_url = f"{IG_API_BASE}/{urllib.parse.quote(media_id)}?{q}"
        with urllib.request.urlopen(meta_url, timeout=15) as r:
            meta = json.loads(r.read().decode("utf-8", errors="replace"))
            permalink = meta.get("permalink", "") or ""
    except Exception:
        pass

    return {"media_id": media_id, "permalink": permalink}


# ─── tokens.json 헬퍼 ───────────────────────────────────────────────────────

def _load_tokens() -> dict:
    if not os.path.isfile(TOKENS_PATH):
        return {}
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _days_until(iso: str):
    if not iso:
        return None
    try:
        d = _parse_utc(iso)
        if not d:
            return None
        now = _utc_now()
        return (d - now).total_seconds() / 86400.0
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


def _resolve_credentials(account: str):
    tokens = _load_tokens()
    ig = (tokens.get("instagram") or {})
    info = ig.get(account)
    if info and info.get("access_token") and info.get("user_id"):
        days = _days_until(info.get("expires_at", ""))
        if days is not None and days <= REFRESH_THRESHOLD_DAYS:
            _try_auto_refresh()
            tokens = _load_tokens()
            info = (tokens.get("instagram") or {}).get(account) or info
        return info.get("access_token", ""), info.get("user_id", ""), "tokens.json"

    env_token = (os.environ.get("META_IG_ACCESS_TOKEN") or "").strip()
    env_uid = (os.environ.get("META_IG_USER_ID") or "").strip()
    if env_token and env_uid:
        return env_token, env_uid, "env"
    return "", "", "none"


def main():
    ap = argparse.ArgumentParser(description="Instagram 자동 업로더 (멀티 계정 + IMAGE/REELS/CAROUSEL)")
    ap.add_argument("--caption", required=True, help="캡션 본문")
    ap.add_argument("--media-url", action="append", default=[],
                    help="미디어 URL (다중 가능; CAROUSEL 일 때 2~10개)")
    ap.add_argument("--image-url", default="",
                    help="(legacy) 이미지 URL 1개 — --media-url 와 동등")
    ap.add_argument("--carousel-types", default="",
                    help="CAROUSEL 의 각 미디어 타입 (예: 'image,video,image')")
    ap.add_argument("--account", default="default",
                    help="tokens.json 의 instagram[<account>] 키 (예: jp, kr). 기본: default")
    ap.add_argument("--media-type", default="IMAGE",
                    choices=["IMAGE", "REELS", "CAROUSEL"])
    ap.add_argument("--dry-run", action="store_true",
                    help="토큰이 있어도 강제 draft 저장")
    args = ap.parse_args()

    account = (args.account or "default").lower()
    media_type = (args.media_type or "IMAGE").upper()

    # 미디어 URL 통합: --media-url 다중 + legacy --image-url
    media_urls = list(args.media_url or [])
    if args.image_url and args.image_url not in media_urls:
        media_urls.insert(0, args.image_url)

    carousel_types = []
    if args.carousel_types:
        carousel_types = [t.strip() for t in args.carousel_types.split(",") if t.strip()]

    # 인자 유효성
    if not media_urls:
        sys.stderr.write("❌ --media-url (또는 --image-url) 최소 1개 필요\n")
        return 2
    if media_type == "CAROUSEL" and len(media_urls) < 2:
        sys.stderr.write("❌ CAROUSEL 은 --media-url 2개 이상\n")
        return 2

    access_token, ig_user_id, source = _resolve_credentials(account)
    use_draft = args.dry_run or (not access_token) or (not ig_user_id)

    if use_draft:
        path = _save_draft(args.caption, media_urls, carousel_types,
                           media_type, account)
        preview = _preview(args.caption)
        _push_telegram(f"✏️ 새 Instagram draft 저장됨 [{account}]\n{preview}\n📁 {path}")
        print(json.dumps({
            "status": "drafted",
            "account": account,
            "media_type": media_type,
            "media_count": len(media_urls),
            "path": path,
            "preview": preview,
            "token_source": source,
        }, ensure_ascii=False))
        return 0

    blocked, cooldown_msg = _cooldown_remaining(account)
    if blocked:
        sys.stderr.write(f"❌ Instagram 게시 보류 [{account}]: {cooldown_msg}\n")
        info = (_load_cooldowns().get(account) or {})
        if info.get("until"):
            sys.stderr.write(f"COOLDOWN_UNTIL={info['until']}\n")
        return 1

    try:
        result = _real_post(args.caption, media_urls, carousel_types,
                            media_type, access_token, ig_user_id)
    except Exception as e:
        msg = str(e)
        if any(s in msg for s in ("401", "190", "expired", "Invalid")):
            if _try_auto_refresh():
                access_token, ig_user_id, source = _resolve_credentials(account)
                try:
                    result = _real_post(args.caption, media_urls, carousel_types,
                                        media_type, access_token, ig_user_id)
                except Exception as e2:
                    if _is_action_block_error(str(e2)):
                        until = _set_action_block_cooldown(account, str(e2))
                        sys.stderr.write(
                            f"❌ Instagram 게시 실패 [{account}]: Meta 행동 제한 감지. "
                            f"{until}까지 자동 재시도를 보류합니다.\n{e2}\n"
                        )
                        sys.stderr.write(f"COOLDOWN_UNTIL={until}\n")
                        return 1
                    sys.stderr.write(f"❌ Instagram 게시 실패 [{account}]: {e2}\n")
                    sys.stderr.write("   → python3 token_manager.py --bootstrap 으로 재발급 권장\n")
                    return 1
            else:
                sys.stderr.write(f"❌ Instagram 게시 실패 [{account}]: {e}\n")
                return 1
        else:
            if _is_action_block_error(msg):
                until = _set_action_block_cooldown(account, msg)
                sys.stderr.write(
                    f"❌ Instagram 게시 실패 [{account}]: Meta 행동 제한 감지. "
                    f"{until}까지 자동 재시도를 보류합니다.\n{e}\n"
                )
                sys.stderr.write(f"COOLDOWN_UNTIL={until}\n")
                return 1
            sys.stderr.write(f"❌ Instagram 게시 실패 [{account}]: {e}\n")
            return 1

    print(json.dumps({
        "status": "posted",
        "account": account,
        "permalink": result.get("permalink", ""),
        "media_id": result.get("media_id", ""),
        "token_source": source,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
