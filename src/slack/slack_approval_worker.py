#!/usr/bin/env python3
# version: slack_approval_worker_v1
"""Slack Socket Mode 워커 — 콘텐츠 승인 버튼 처리.

사용자가 Slack 카드의 ✅/❌/📝 버튼을 누르면:
    ✅ approve_<id>  → draft 의 platform/account 로 적절한 uploader.py 실행 → 메시지 update
    ❌ reject_<id>   → 모달로 사유 입력 → drafts/rejected/ 로 이동 → 메시지 update
    📝 edit_<id>     → 모달로 본문 수정 → draft 본문 교체 → 메시지 update (버튼 유지)

의존성:
    pip install slack-sdk  (또는: pip3 install --user slack-sdk)

환경변수 필요:
    SLACK_BOT_TOKEN   xoxb-...
    SLACK_APP_TOKEN   xapp-...  (Socket Mode connections:write 스코프)
    SLACK_CHANNEL_ID  C0...     (선택; 메시지 갱신은 payload 의 채널 사용)

이 워커는 launchd 로 백그라운드 데몬화하는 것을 권장한다.
    plist: ~/Library/LaunchAgents/com.moneyai.slack-worker.plist
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import traceback

# ─── slack-sdk 의존성 확인 ─────────────────────────────────────────────────
try:
    from slack_sdk.web import WebClient  # type: ignore
    from slack_sdk.socket_mode import SocketModeClient  # type: ignore
    from slack_sdk.socket_mode.request import SocketModeRequest  # type: ignore
    from slack_sdk.socket_mode.response import SocketModeResponse  # type: ignore
except ImportError:
    sys.stderr.write(
        "❌ slack-sdk 가 설치되어 있지 않습니다.\n"
        "   다음 명령으로 설치하세요:\n"
        "     /opt/homebrew/bin/python3 -m pip install --user slack-sdk\n"
        "   (homebrew python 이 아니면 `python3 -m pip install --user slack-sdk`)\n"
    )
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
DRAFTS_DIR = os.path.join(HERE, "drafts")
REJECTED_DIR = os.path.join(DRAFTS_DIR, "rejected")

THREADS_UPLOADER = os.path.join(HERE, "threads_uploader.py")
INSTAGRAM_UPLOADER = os.path.join(HERE, "instagram_uploader.py")
X_UPLOADER = os.path.join(HERE, "x_uploader.py")  # 병렬 에이전트가 생성 중


# ─── frontmatter 파싱/저장 ────────────────────────────────────────────────

def parse_draft(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    meta = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def write_draft(path: str, meta: dict, body: str):
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    out = "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


# ─── action_id → draft path 매핑 ──────────────────────────────────────────

def _find_draft_by_id(draft_id: str) -> str | None:
    """action_id 의 draft_id 로 drafts/ 폴더에서 .md 파일 찾기."""
    if not os.path.isdir(DRAFTS_DIR):
        return None
    target_base = draft_id + ".md"
    # 1) 정확 매칭
    direct = os.path.join(DRAFTS_DIR, target_base)
    if os.path.isfile(direct):
        return direct
    # 2) 부분 매칭(혹시 변환 차이가 있으면)
    for name in os.listdir(DRAFTS_DIR):
        if not name.endswith(".md"):
            continue
        normalized = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in name[:-3]
        )
        if normalized == draft_id:
            return os.path.join(DRAFTS_DIR, name)
    return None


# ─── uploader 호출 ─────────────────────────────────────────────────────────

def _run_uploader(platform: str, account: str, meta: dict, body: str) -> dict:
    """플랫폼별 uploader.py 를 subprocess 로 호출. {ok, permalink, error} 반환."""
    py = sys.executable or "/opt/homebrew/bin/python3"

    if platform == "threads":
        if not os.path.isfile(THREADS_UPLOADER):
            return {"ok": False, "error": "threads_uploader.py 없음"}
        cmd = [py, THREADS_UPLOADER, "--text", body, "--account", account]
        if meta.get("image_url"):
            cmd += ["--image-url", meta["image_url"]]
        if meta.get("reply_control"):
            cmd += ["--reply-control", meta["reply_control"]]
    elif platform == "instagram":
        if not os.path.isfile(INSTAGRAM_UPLOADER):
            return {"ok": False, "error": "instagram_uploader.py 없음"}
        cmd = [py, INSTAGRAM_UPLOADER, "--caption", body, "--account", account]
        if meta.get("image_url"):
            cmd += ["--image-url", meta["image_url"]]
        if meta.get("video_url"):
            cmd += ["--video-url", meta["video_url"]]
        if meta.get("media_type"):
            cmd += ["--media-type", meta["media_type"]]
    elif platform == "x":
        if not os.path.isfile(X_UPLOADER):
            return {"ok": False,
                    "error": "x_uploader.py 미구현 (병렬 에이전트 작업 중)"}
        cmd = [py, X_UPLOADER, "--text", body, "--account", account]
        if meta.get("image_url"):
            cmd += ["--image-url", meta["image_url"]]
    else:
        return {"ok": False, "error": f"알 수 없는 플랫폼: {platform}"}

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "uploader 타임아웃 (180s)"}
    except Exception as e:
        return {"ok": False, "error": f"subprocess 실패: {e}"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": err or f"exit {proc.returncode}"}

    # uploader 의 마지막 줄이 JSON 이라고 가정
    permalink = ""
    status = ""
    try:
        last = out.splitlines()[-1] if out else ""
        parsed = json.loads(last)
        permalink = parsed.get("permalink", "") or ""
        status = parsed.get("status", "") or ""
    except Exception:
        pass

    if status == "drafted":
        return {"ok": False,
                "error": "uploader 가 draft 모드 — 토큰 없음 (token_manager.py --status 확인)"}

    return {"ok": True, "permalink": permalink, "raw": out[-400:]}


# ─── Slack 메시지 갱신 ─────────────────────────────────────────────────────

def _update_message(web: WebClient, channel: str, ts: str, text: str,
                    blocks: list | None = None):
    try:
        web.chat_update(
            channel=channel,
            ts=ts,
            text=text[:200],
            blocks=blocks if blocks is not None else [],
        )
    except Exception as e:
        sys.stderr.write(f"⚠️ chat_update 실패: {e}\n")


def _result_blocks(header_text: str, detail: str) -> list:
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{header_text}*\n{detail[:2900]}"}},
    ]


def _retry_blocks(draft_id: str, error: str) -> list:
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*❌ 업로드 실패*\n```{error[:2700]}```"}},
        {"type": "actions",
         "block_id": f"retry_{draft_id}",
         "elements": [
             {"type": "button", "style": "primary",
              "text": {"type": "plain_text", "text": "🔁 재시도", "emoji": True},
              "action_id": f"approve_{draft_id}", "value": draft_id},
             {"type": "button", "style": "danger",
              "text": {"type": "plain_text", "text": "❌ 포기", "emoji": True},
              "action_id": f"reject_{draft_id}", "value": draft_id},
         ]},
    ]


def _approval_blocks_for_edit(draft_id: str, preview: str) -> list:
    """edit 후 다시 ✅/❌ 버튼이 붙은 카드로 복원."""
    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📝 수정된 콘텐츠", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": preview[:2900]}},
        {"type": "actions",
         "block_id": f"approval_{draft_id}",
         "elements": [
             {"type": "button", "style": "primary",
              "text": {"type": "plain_text", "text": "✅ 승인 → 업로드", "emoji": True},
              "action_id": f"approve_{draft_id}", "value": draft_id},
             {"type": "button", "style": "danger",
              "text": {"type": "plain_text", "text": "❌ 거절", "emoji": True},
              "action_id": f"reject_{draft_id}", "value": draft_id},
             {"type": "button",
              "text": {"type": "plain_text", "text": "📝 다시 수정", "emoji": True},
              "action_id": f"edit_{draft_id}", "value": draft_id},
         ]},
    ]


# ─── 모달 ─────────────────────────────────────────────────────────────────

def _open_reject_modal(web: WebClient, trigger_id: str, draft_id: str,
                       channel: str, ts: str):
    private = json.dumps({"draft_id": draft_id, "channel": channel, "ts": ts})
    view = {
        "type": "modal",
        "callback_id": "reject_modal",
        "private_metadata": private,
        "title": {"type": "plain_text", "text": "거절 사유 입력"},
        "submit": {"type": "plain_text", "text": "거절"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input",
             "block_id": "reason_block",
             "label": {"type": "plain_text", "text": "거절 사유"},
             "element": {
                 "type": "plain_text_input",
                 "action_id": "reason",
                 "multiline": True,
                 "placeholder": {"type": "plain_text", "text": "예: 너무 광고스러움 / 사실관계 오류"},
             }},
        ],
    }
    try:
        web.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:
        sys.stderr.write(f"⚠️ reject modal 열기 실패: {e}\n")


def _open_edit_modal(web: WebClient, trigger_id: str, draft_id: str,
                     channel: str, ts: str, current_body: str):
    private = json.dumps({"draft_id": draft_id, "channel": channel, "ts": ts})
    view = {
        "type": "modal",
        "callback_id": "edit_modal",
        "private_metadata": private,
        "title": {"type": "plain_text", "text": "본문 수정"},
        "submit": {"type": "plain_text", "text": "저장"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input",
             "block_id": "body_block",
             "label": {"type": "plain_text", "text": "본문"},
             "element": {
                 "type": "plain_text_input",
                 "action_id": "body",
                 "multiline": True,
                 "initial_value": current_body[:2900],
             }},
        ],
    }
    try:
        web.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:
        sys.stderr.write(f"⚠️ edit modal 열기 실패: {e}\n")


# ─── 핵심 핸들러 ───────────────────────────────────────────────────────────

def handle_block_action(web: WebClient, payload: dict):
    actions = payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id", "")
    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id", "")
    ts = container.get("message_ts") or (payload.get("message") or {}).get("ts", "")
    trigger_id = payload.get("trigger_id", "")

    if action_id.startswith("approve_"):
        draft_id = action_id[len("approve_"):]
        _handle_approve(web, draft_id, channel, ts)
    elif action_id.startswith("reject_"):
        draft_id = action_id[len("reject_"):]
        _open_reject_modal(web, trigger_id, draft_id, channel, ts)
    elif action_id.startswith("edit_"):
        draft_id = action_id[len("edit_"):]
        path = _find_draft_by_id(draft_id)
        body = ""
        if path:
            _meta, body = parse_draft(path)
        _open_edit_modal(web, trigger_id, draft_id, channel, ts, body)
    else:
        sys.stderr.write(f"⚠️ 알 수 없는 action_id: {action_id}\n")


def _handle_approve(web: WebClient, draft_id: str, channel: str, ts: str):
    path = _find_draft_by_id(draft_id)
    if not path:
        _update_message(web, channel, ts, "❌ draft 파일을 찾을 수 없음",
                        _result_blocks("❌ draft 없음", f"id: `{draft_id}`"))
        return

    meta, body = parse_draft(path)
    platform = meta.get("slack_platform") or meta.get("target") or "threads"
    account = meta.get("slack_account") or meta.get("account") or "default"

    _update_message(web, channel, ts, "⏳ 업로드 중...",
                    _result_blocks("⏳ 업로드 중...",
                                   f"`{platform}` / `{account}`"))

    result = _run_uploader(platform, account, meta, body)
    if result.get("ok"):
        permalink = result.get("permalink") or "(permalink 없음)"
        meta["status"] = "posted"
        meta["posted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if permalink and permalink != "(permalink 없음)":
            meta["permalink"] = permalink
        try:
            write_draft(path, meta, body)
        except Exception:
            pass
        _update_message(
            web, channel, ts,
            f"✅ 업로드 완료: {permalink}",
            _result_blocks(
                "✅ 업로드 완료",
                f"`{platform}` / `{account}`\n🔗 {permalink}",
            ),
        )
    else:
        err = result.get("error", "unknown")
        _update_message(web, channel, ts, f"❌ 업로드 실패: {err}",
                        _retry_blocks(draft_id, err))


def handle_view_submission(web: WebClient, payload: dict):
    view = payload.get("view") or {}
    callback_id = view.get("callback_id", "")
    private = view.get("private_metadata", "") or "{}"
    try:
        meta_pm = json.loads(private)
    except Exception:
        meta_pm = {}
    draft_id = meta_pm.get("draft_id", "")
    channel = meta_pm.get("channel", "")
    ts = meta_pm.get("ts", "")
    values = (view.get("state") or {}).get("values") or {}

    path = _find_draft_by_id(draft_id)

    if callback_id == "reject_modal":
        reason = (((values.get("reason_block") or {}).get("reason") or {})
                  .get("value") or "").strip()
        if not path:
            return
        meta, body = parse_draft(path)
        meta["status"] = "rejected"
        meta["reason"] = reason or "(no reason)"
        meta["rejected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        os.makedirs(REJECTED_DIR, exist_ok=True)
        new_path = os.path.join(REJECTED_DIR, os.path.basename(path))
        write_draft(path, meta, body)
        try:
            os.replace(path, new_path)
        except Exception as e:
            sys.stderr.write(f"⚠️ 거절 파일 이동 실패: {e}\n")
        if channel and ts:
            _update_message(
                web, channel, ts,
                f"❌ 거절됨: {reason[:80]}",
                _result_blocks("❌ 거절됨",
                               f"사유: {reason or '(없음)'}\n📁 `{new_path}`"),
            )

    elif callback_id == "edit_modal":
        new_body = (((values.get("body_block") or {}).get("body") or {})
                    .get("value") or "").strip()
        if not path:
            return
        meta, _old_body = parse_draft(path)
        meta["status"] = "awaiting_approval"
        meta["edited_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_draft(path, meta, new_body)
        if channel and ts:
            preview = new_body[:1500] + ("..." if len(new_body) > 1500 else "")
            _update_message(
                web, channel, ts,
                f"📝 수정됨 (재승인 대기) [{draft_id}]",
                _approval_blocks_for_edit(draft_id, preview),
            )


# ─── Socket Mode 진입점 ───────────────────────────────────────────────────

def on_request(client: SocketModeClient, req: SocketModeRequest):
    # 항상 ack 먼저
    try:
        client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )
    except Exception as e:
        sys.stderr.write(f"⚠️ ack 실패: {e}\n")
        return

    if req.type != "interactive":
        return

    payload = req.payload or {}
    p_type = payload.get("type", "")
    try:
        if p_type == "block_actions":
            handle_block_action(client.web_client, payload)
        elif p_type == "view_submission":
            handle_view_submission(client.web_client, payload)
        else:
            # 무시 (shortcut/message_action 등은 미사용)
            pass
    except Exception:
        sys.stderr.write("❌ 핸들러 예외:\n" + traceback.format_exc())


def main():
    bot_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    app_token = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if not bot_token or not app_token:
        sys.stderr.write(
            "❌ SLACK_BOT_TOKEN / SLACK_APP_TOKEN 미설정.\n"
            "   _company/_agents/instagram/.env 에 값을 채운 후\n"
            "   launchctl 로 워커를 재시작하세요.\n"
            "   가이드: assets/tool-seeds/instagram/slack_setup.md\n"
        )
        return 2

    web = WebClient(token=bot_token)
    sm = SocketModeClient(app_token=app_token, web_client=web)
    sm.socket_mode_request_listeners.append(on_request)

    sys.stdout.write("🟢 money-ai slack worker 시작\n")
    sys.stdout.flush()
    sm.connect()

    # 데몬 루프
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        sys.stdout.write("🛑 종료 신호 — 워커 셧다운\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
