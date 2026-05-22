#!/usr/bin/env python3
# version: threads_uploader_v3_video
"""Threads 자동 업로더 (draft mode 기본, 멀티 계정 + 이미지/영상 지원)

사용법:
  1. draft 모드 (기본 — 토큰 없어도 가능):
       python threads_uploader.py --text "안녕"
     → drafts/threads-YYYYMMDD-HHMMSS.md 로 저장

  2. 멀티 계정 텍스트 게시:
       # 사전: token_manager.py --bootstrap 으로 tokens.json 생성
       python threads_uploader.py --text "안녕" --account jp

  3. 이미지 게시:
       python threads_uploader.py --text "오늘의 한 컷" \
         --media-type image --image-url "https://cdn/foo.jpg" --account jp

  4. 영상 게시 (status polling 자동):
       python threads_uploader.py --text "릴" \
         --media-type video --video-url "https://cdn/clip.mp4" --account jp

  5. Legacy 단일 계정 (호환):
       META_THREADS_ACCESS_TOKEN=xxx \
       META_THREADS_USER_ID=yyy \
       python threads_uploader.py --text "안녕"

옵션:
  --account          jp / kr / ... (tokens.json 의 threads[*] 키). 기본: default
  --media-type       text | image | video (기본 text — image/video 면 URL 인자 필요)
  --image-url        이미지 URL (--media-type image 일 때)
  --video-url        영상 URL (--media-type video 일 때, mp4 권장)
  --reply-control    everyone | mentioned | followers (기본 everyone)
  --dry-run          토큰이 있어도 강제로 draft mode

토큰 소스 우선순위:
  1) tokens.json 의 threads[{account}] (token_manager.py 가 관리)
  2) 만료 임박/만료 → token_manager.py --refresh 자동 호출 후 재시도
  3) 환경변수 META_THREADS_ACCESS_TOKEN + META_THREADS_USER_ID (legacy fallback)

LLM 호출 0회 — 컨텐츠 초안 생성은 별도 에이전트가 미리 처리.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import datetime as dt
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
TOKEN_MANAGER_PATH = os.path.join(REPO_ROOT, "src", "auth", "token_manager.py")
THREADS_API_BASE = "https://graph.threads.net/v1.0"

REFRESH_THRESHOLD_DAYS = 7


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


def _save_draft(text: str, image_url: str, video_url: str, media_type: str,
                reply_control: str, account: str) -> str:
    _ensure_drafts_dir()
    stamp = _now_stamp()
    path = os.path.join(DRAFTS_DIR, f"threads-{account}-{stamp}.md")
    fm_lines = [
        "---",
        "status: draft",
        "target: threads",
        f"account: {account}",
        f"created_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"reply_control: {reply_control}",
        f"media_type: {media_type}",
    ]
    if image_url:
        fm_lines.append(f"image_url: {image_url}")
    if video_url:
        fm_lines.append(f"video_url: {video_url}")
    fm_lines.append("---")
    body = "\n".join(fm_lines) + "\n\n" + (text or "") + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _http_post(url: str, payload: dict) -> dict:
    """form-urlencoded POST, JSON 응답 반환."""
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"Threads API 응답 파싱 실패: {raw[:200]}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Threads API HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Threads API 네트워크 실패: {e.reason}")


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
        raise RuntimeError(f"Threads API HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Threads API 네트워크 실패: {e.reason}")


def _poll_container_status(creation_id: str, access_token: str,
                           max_tries: int = 30, interval: int = 5) -> str:
    """media_type=VIDEO 컨테이너는 인코딩 시간 필요. status=FINISHED 까지 폴링."""
    q = urllib.parse.urlencode({
        "fields": "status,error_message",
        "access_token": access_token,
    })
    status_url = f"{THREADS_API_BASE}/{urllib.parse.quote(creation_id)}?{q}"
    last = ""
    for _ in range(max_tries):
        try:
            data = _http_get(status_url)
            status = (data.get("status") or "").upper()
            last = status
            if status == "FINISHED":
                return status
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(
                    f"Threads container 처리 실패 (status={status}): {data.get('error_message', '')}"
                )
        except RuntimeError:
            # 일시적 네트워크 — 다음 시도
            pass
        time.sleep(interval)
    raise RuntimeError(f"Threads container 인코딩 타임아웃 (last status={last or '?'})")


THREADS_TEXT_LIMIT = 500  # 글자 수 제한 (Meta 공식)
# 체이닝 시 "1/N " 접두 들어가니까 본문은 약간 여유 두고 자름 (480자 안전권)
THREADS_CHUNK_BUDGET = 480
MAX_CHUNKS = 10  # 안전 cap — 10개 넘어가면 게시 안 함 (사장님 검토 요)


def _chunk_text(text: str, budget: int = THREADS_CHUNK_BUDGET) -> list:
    """긴 글을 자연스러운 경계 (문단 → 문장 → 단어) 에서 잘라 N 조각.
       각 조각 앞에 "i/N " 접두 붙임. 단일 조각이면 접두 없음.
    """
    text = (text or "").strip()
    if len(text) <= THREADS_TEXT_LIMIT:
        return [text]

    raw_chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= budget:
            raw_chunks.append(remaining.strip())
            break
        # 1) 문단 경계
        cut = remaining.rfind("\n\n", 0, budget)
        # 2) 문장 경계 (한·일·영 종결부호)
        if cut < int(budget * 0.5):
            best = -1
            for punct in [". ", "! ", "? ", "。", "！", "？", "…", "\n"]:
                p = remaining.rfind(punct, 0, budget)
                if p > best:
                    best = p + len(punct)
            cut = best if best > int(budget * 0.5) else cut
        # 3) 단어 경계
        if cut < int(budget * 0.5):
            cut = remaining.rfind(" ", 0, budget)
        # 4) hard cut
        if cut < int(budget * 0.5):
            cut = budget
        raw_chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    n = len(raw_chunks)
    if n == 1:
        return raw_chunks
    if n > MAX_CHUNKS:
        # 너무 길면 마지막 조각에 "(중략)" 표시하고 자름
        raw_chunks = raw_chunks[:MAX_CHUNKS]
        raw_chunks[-1] = raw_chunks[-1].rstrip() + " (이하 생략)"
        n = MAX_CHUNKS
    return [f"{i+1}/{n} {c}" for i, c in enumerate(raw_chunks)]


def _real_post_thread(chunks: list, reply_control: str,
                      access_token: str, user_id: str) -> dict:
    """N개 텍스트 조각을 reply_to_id 로 연결된 thread 로 순차 게시.
       조각 1 = root, 조각 2~N = 직전 게시물의 답글로 체인."""
    thread_ids = []
    parent_id = None
    permalink = ""

    create_url = f"{THREADS_API_BASE}/{urllib.parse.quote(user_id)}/threads"
    publish_url = f"{THREADS_API_BASE}/{urllib.parse.quote(user_id)}/threads_publish"

    for i, chunk_text in enumerate(chunks):
        payload = {
            "media_type": "TEXT",
            "text": chunk_text,
            "access_token": access_token,
            "reply_control": reply_control,
        }
        if parent_id:
            payload["reply_to_id"] = parent_id

        created = _http_post(create_url, payload)
        creation_id = created.get("id")
        if not creation_id:
            raise RuntimeError(f"Threads create 응답에 id 없음 (chunk {i+1}/{len(chunks)}): {created}")

        published = _http_post(publish_url, {
            "creation_id": creation_id,
            "access_token": access_token,
        })
        thread_id = published.get("id") or creation_id
        thread_ids.append(thread_id)
        parent_id = thread_id

        # 첫 게시물의 permalink 가 thread 전체의 root URL
        if i == 0:
            try:
                q = urllib.parse.urlencode({
                    "fields": "permalink",
                    "access_token": access_token,
                })
                meta_url = f"{THREADS_API_BASE}/{urllib.parse.quote(thread_id)}?{q}"
                with urllib.request.urlopen(meta_url, timeout=15) as r:
                    meta = json.loads(r.read().decode("utf-8", errors="replace"))
                    permalink = meta.get("permalink", "") or ""
            except Exception:
                pass

        # rate limit 보호 — 답글 간 짧은 대기
        if i < len(chunks) - 1:
            time.sleep(1.5)

    return {
        "thread_id": thread_ids[0],
        "thread_ids": thread_ids,
        "chunks": len(chunks),
        "permalink": permalink,
    }


def _real_post(text: str, image_url: str, video_url: str, media_type: str,
               reply_control: str, access_token: str, user_id: str) -> dict:
    # 1) media container 생성
    create_url = f"{THREADS_API_BASE}/{urllib.parse.quote(user_id)}/threads"
    mt = (media_type or "text").lower()
    if mt == "image" and image_url:
        api_mt = "IMAGE"
    elif mt == "video" and video_url:
        api_mt = "VIDEO"
    else:
        api_mt = "TEXT"

    create_payload = {
        "media_type": api_mt,
        "text": text,
        "access_token": access_token,
        "reply_control": reply_control,
    }
    if api_mt == "IMAGE":
        create_payload["image_url"] = image_url
    elif api_mt == "VIDEO":
        create_payload["video_url"] = video_url

    created = _http_post(create_url, create_payload)
    creation_id = created.get("id")
    if not creation_id:
        raise RuntimeError(f"Threads create 응답에 id 없음: {created}")

    # VIDEO 는 publish 전 status=FINISHED 폴링 필요
    if api_mt == "VIDEO":
        _poll_container_status(creation_id, access_token)

    # 2) publish
    publish_url = f"{THREADS_API_BASE}/{urllib.parse.quote(user_id)}/threads_publish"
    published = _http_post(publish_url, {
        "creation_id": creation_id,
        "access_token": access_token,
    })
    thread_id = published.get("id") or creation_id

    # 3) permalink 조회 (best-effort)
    permalink = ""
    try:
        q = urllib.parse.urlencode({
            "fields": "permalink",
            "access_token": access_token,
        })
        meta_url = f"{THREADS_API_BASE}/{urllib.parse.quote(thread_id)}?{q}"
        with urllib.request.urlopen(meta_url, timeout=15) as r:
            meta = json.loads(r.read().decode("utf-8", errors="replace"))
            permalink = meta.get("permalink", "") or ""
    except Exception:
        pass

    return {"thread_id": thread_id, "permalink": permalink}


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
        s = iso[:-1] if iso.endswith("Z") else iso
        d = dt.datetime.fromisoformat(s)
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return (d - now).total_seconds() / 86400.0
    except Exception:
        return None


def _try_auto_refresh():
    """token_manager.py --refresh 호출 시도. 실패해도 조용히."""
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
    """우선순위: tokens.json[threads][account] → env. (token, user_id, source) 반환."""
    tokens = _load_tokens()
    threads = (tokens.get("threads") or {})
    info = threads.get(account)
    if info and info.get("access_token") and info.get("user_id"):
        days = _days_until(info.get("expires_at", ""))
        if days is not None and days <= REFRESH_THRESHOLD_DAYS:
            # 만료 임박/만료 → 자동 갱신 시도 후 재로딩
            _try_auto_refresh()
            tokens = _load_tokens()
            info = (tokens.get("threads") or {}).get(account) or info
        return info.get("access_token", ""), info.get("user_id", ""), "tokens.json"

    # legacy env fallback (단일 계정)
    env_token = (os.environ.get("META_THREADS_ACCESS_TOKEN") or "").strip()
    env_uid = (os.environ.get("META_THREADS_USER_ID") or "").strip()
    if env_token and env_uid:
        return env_token, env_uid, "env"
    return "", "", "none"


def main():
    ap = argparse.ArgumentParser(description="Threads 자동 업로더 (멀티 계정 + 영상)")
    ap.add_argument("--text", required=True, help="게시 본문")
    ap.add_argument("--account", default="default",
                    help="tokens.json 의 threads[<account>] 키 (예: jp, kr). 기본: default")
    ap.add_argument("--media-type", default="text",
                    choices=["text", "image", "video"],
                    help="미디어 타입 (기본 text)")
    ap.add_argument("--image-url", default="",
                    help="(선택) 이미지 URL — --media-type image 시")
    ap.add_argument("--video-url", default="",
                    help="(선택) 영상 URL — --media-type video 시")
    ap.add_argument("--reply-control", default="everyone",
                    choices=["everyone", "mentioned", "followers"])
    ap.add_argument("--dry-run", action="store_true",
                    help="토큰이 있어도 강제로 draft 저장")
    args = ap.parse_args()

    account = (args.account or "default").lower()
    media_type = (args.media_type or "text").lower()

    # 인자 유효성 검증
    if media_type == "image" and not args.image_url:
        sys.stderr.write("❌ --media-type image 면 --image-url 필요\n")
        return 2
    if media_type == "video" and not args.video_url:
        sys.stderr.write("❌ --media-type video 면 --video-url 필요\n")
        return 2

    access_token, user_id, source = _resolve_credentials(account)
    use_draft = args.dry_run or (not access_token) or (not user_id)

    if use_draft:
        path = _save_draft(args.text, args.image_url, args.video_url,
                           media_type, args.reply_control, account)
        preview = _preview(args.text)
        _push_telegram(f"✏️ 새 Threads draft 저장됨 [{account}]\n{preview}\n📁 {path}")
        print(json.dumps({
            "status": "drafted",
            "account": account,
            "media_type": media_type,
            "path": path,
            "preview": preview,
            "token_source": source,
        }, ensure_ascii=False))
        return 0

    # 텍스트가 500자 초과 + media_type=text 면 자동 chunking (reply chain).
    # 이미지·영상 포함 게시는 단일 글로 (Meta API 가 multi-media thread 미지원).
    needs_chain = (media_type == "text") and (len(args.text or "") > THREADS_TEXT_LIMIT)
    if needs_chain:
        chunks = _chunk_text(args.text)
    else:
        chunks = None

    def _do_post():
        if chunks:
            return _real_post_thread(chunks, args.reply_control, access_token, user_id)
        return _real_post(args.text, args.image_url, args.video_url,
                          media_type, args.reply_control,
                          access_token, user_id)

    try:
        result = _do_post()
    except Exception as e:
        # 일부 401/190 류 에러면 한 번 더 refresh 시도 후 재시도
        msg = str(e)
        if any(s in msg for s in ("401", "190", "expired", "Invalid")):
            if _try_auto_refresh():
                access_token, user_id, source = _resolve_credentials(account)
                try:
                    result = _do_post()
                except Exception as e2:
                    sys.stderr.write(f"❌ Threads 게시 실패 [{account}]: {e2}\n")
                    sys.stderr.write("   → python3 token_manager.py --bootstrap 으로 재발급 권장\n")
                    return 1
            else:
                sys.stderr.write(f"❌ Threads 게시 실패 [{account}]: {e}\n")
                return 1
        else:
            sys.stderr.write(f"❌ Threads 게시 실패 [{account}]: {e}\n")
            return 1

    print(json.dumps({
        "status": "posted",
        "account": account,
        "permalink": result.get("permalink", ""),
        "thread_id": result.get("thread_id", ""),
        "token_source": source,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
