#!/usr/bin/env python3
# version: slack_notifier_v1
"""Slack 인터랙티브 콘텐츠 승인 노티파이어.

draft .md 파일을 Slack 채널에 카드(Block Kit + 버튼)로 게시한다.
승인/거절/수정 버튼은 slack_approval_worker.py 가 처리한다.

사용법:
    python3 slack_notifier.py \
        --draft-path drafts/threads-jp-20260521-101010.md \
        --platform threads \
        --account jp

환경변수 (없으면 자동 폴백):
    SLACK_BOT_TOKEN   xoxb-... (필수)
    SLACK_CHANNEL_ID  C0...   (필수)
    SLACK_APP_TOKEN   xapp-... (worker 가 사용; notifier 는 무관)

폴백:
    1) SLACK_BOT_TOKEN 미설정 → Telegram 으로 알림 시도
    2) Telegram 도 미설정 → stdout 에 안내만 출력 후 exit 0
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SLACK_API_BASE = "https://slack.com/api"
PREVIEW_LIMIT = 600  # Slack section block text 안전 한도(3000) 보다 충분히 짧게


# ─── frontmatter 파싱 ──────────────────────────────────────────────────────

def _parse_draft(path: str):
    """draft .md 의 frontmatter + body 파싱. (meta_dict, body) 반환."""
    if not os.path.isfile(path):
        raise SystemExit(f"❌ draft 파일 없음: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    meta = {}
    body = raw
    if raw.startswith("---"):
        # frontmatter 영역만 잘라냄
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            body = parts[2].lstrip("\n")
            for line in fm_text.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    return meta, body


def _write_draft(path: str, meta: dict, body: str):
    """meta + body 를 frontmatter 형식으로 다시 저장."""
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    out = "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


# ─── 보조 ──────────────────────────────────────────────────────────────────

def _truncate(text: str, n: int = PREVIEW_LIMIT) -> str:
    t = (text or "").strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "...\n_(더보기는 draft 파일 참조)_"


def _platform_emoji(platform: str) -> str:
    return {
        "threads": "🧵",
        "instagram": "📷",
        "x": "𝕏",
    }.get(platform, "📝")


def _country_flag(account: str) -> str:
    return {
        "jp": "🇯🇵",
        "kr": "🇰🇷",
        "us": "🇺🇸",
        "en": "🇺🇸",
        "default": "🌐",
    }.get(account.lower(), "🌐")


def _draft_id(draft_path: str) -> str:
    """파일명 기반 안정적 ID — action_id 에 그대로 사용."""
    base = os.path.basename(draft_path)
    if base.endswith(".md"):
        base = base[:-3]
    # action_id 는 영숫자/대시/언더스코어/콜론만 허용 (Slack 권장)
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in base)


# ─── Telegram 폴백 ─────────────────────────────────────────────────────────

def _push_telegram(message: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": message[:4000],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


# ─── Slack API ────────────────────────────────────────────────────────────

def _slack_post(method: str, payload: dict, bot_token: str) -> dict:
    """Slack Web API JSON POST. ok=true 확인 후 반환."""
    url = f"{SLACK_API_BASE}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {bot_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Slack {method} HTTP {e.code}: {err_body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Slack {method} 네트워크 실패: {e.reason}")

    try:
        res = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"Slack {method} 응답 파싱 실패: {body[:200]}")

    if not res.get("ok"):
        raise RuntimeError(f"Slack {method} 실패: {res.get('error', 'unknown')}")
    return res


def _build_blocks(meta: dict, body: str, platform: str, account: str,
                  draft_id: str) -> list:
    flag = _country_flag(account)
    pemoji = _platform_emoji(platform)
    header_text = f"{flag} {pemoji} {platform.upper()} · @{account}"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text[:150], "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate(body)},
        },
    ]

    image_url = (meta.get("image_url") or "").strip()
    video_url = (meta.get("video_url") or "").strip()

    if image_url and image_url.startswith(("http://", "https://")):
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": f"{platform} preview",
        })

    if video_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🎬 *영상:* <{video_url}|미리보기 열기>"},
        })

    meta_bits = []
    if meta.get("reply_control"):
        meta_bits.append(f"💬 {meta['reply_control']}")
    if meta.get("media_type"):
        meta_bits.append(f"🖼️ {meta['media_type']}")
    if meta.get("created_at"):
        meta_bits.append(f"🕘 {meta['created_at']}")
    if meta_bits:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(meta_bits)}],
        })

    blocks.append({
        "type": "actions",
        "block_id": f"approval_{draft_id}",
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "✅ 승인 → 업로드", "emoji": True},
                "action_id": f"approve_{draft_id}",
                "value": draft_id,
            },
            {
                "type": "button",
                "style": "danger",
                "text": {"type": "plain_text", "text": "❌ 거절", "emoji": True},
                "action_id": f"reject_{draft_id}",
                "value": draft_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📝 수정 요청", "emoji": True},
                "action_id": f"edit_{draft_id}",
                "value": draft_id,
            },
        ],
    })

    return blocks


# ─── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Slack 콘텐츠 승인 노티파이어")
    ap.add_argument("--draft-path", required=True, help="draft .md 파일 경로")
    ap.add_argument("--platform", required=True,
                    choices=["threads", "instagram", "x"],
                    help="플랫폼")
    ap.add_argument("--account", required=True, help="계정 (jp/kr/...)")
    args = ap.parse_args()

    draft_path = os.path.abspath(args.draft_path)
    meta, body = _parse_draft(draft_path)
    draft_id = _draft_id(draft_path)

    bot_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    channel_id = (os.environ.get("SLACK_CHANNEL_ID") or "").strip()

    if not bot_token or not channel_id:
        # 폴백 1: Telegram
        tg_msg = (
            f"📨 새 콘텐츠 draft [{args.platform}/{args.account}]\n"
            f"📁 {draft_path}\n\n"
            f"{_truncate(body, 800)}"
        )
        tg_sent = _push_telegram(tg_msg)
        print(json.dumps({
            "status": "fallback",
            "reason": "SLACK_BOT_TOKEN/SLACK_CHANNEL_ID 미설정",
            "guide": "assets/tool-seeds/instagram/slack_setup.md 참조",
            "draft_path": draft_path,
            "telegram_sent": tg_sent,
            "message": "Slack 미설정 — 콘텐츠를 직접 검토하세요",
        }, ensure_ascii=False))
        return 0

    blocks = _build_blocks(meta, body, args.platform, args.account, draft_id)
    fallback_text = f"[{args.platform}/{args.account}] {_truncate(body, 120)}"

    try:
        res = _slack_post("chat.postMessage", {
            "channel": channel_id,
            "text": fallback_text,
            "blocks": blocks,
            "metadata": {
                "event_type": "money_ai_content_approval",
                "event_payload": {
                    "draft_path": draft_path,
                    "platform": args.platform,
                    "account": args.account,
                },
            },
        }, bot_token)
    except Exception as e:
        sys.stderr.write(f"❌ Slack 게시 실패: {e}\n")
        return 1

    ts = res.get("ts", "")
    channel = res.get("channel", channel_id)

    # draft frontmatter 에 slack_ts/channel 기록 (worker 가 update 할 때 사용)
    meta["slack_ts"] = ts
    meta["slack_channel"] = channel
    meta["slack_platform"] = args.platform
    meta["slack_account"] = args.account
    meta.setdefault("status", "awaiting_approval")
    try:
        _write_draft(draft_path, meta, body)
    except Exception as e:
        sys.stderr.write(f"⚠️ slack_ts 저장 실패 (메시지는 게시됨): {e}\n")

    print(json.dumps({
        "status": "posted",
        "channel": channel,
        "ts": ts,
        "draft_path": draft_path,
        "platform": args.platform,
        "account": args.account,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
