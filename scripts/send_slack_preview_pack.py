#!/usr/bin/env python3
"""Create manual preview drafts and send Slack review cards only."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SLACK_NOTIFIER = os.path.join(ROOT, "src", "slack", "slack_notifier.py")
DRAFTS_DIR = os.path.join(ROOT, "drafts")


def _load_env_file(path: str) -> dict[str, str]:
    env = os.environ.copy()
    if not os.path.isfile(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value and not (value.startswith('"') or value.startswith("'")):
                hash_idx = value.find("#")
                if hash_idx >= 0:
                    value = value[:hash_idx].rstrip()
            env.setdefault(key, value.strip('"').strip("'"))
    return env


COPY = {
    ("kr", "instagram"): """카페에서 일본 친구랑 휴대폰 하나 놓고
가고 싶은 동네 얘기하다가 웃음 터지는 순간.

언어가 완벽하지 않아도 괜찮고,
처음부터 엄청 친하지 않아도 괜찮아요.

같이 지도 보고, 메뉴 고르고, 다음 약속까지 자연스럽게 이어지는 친구.
그런 연결을 만들고 싶어서 OnlyFriends를 만들고 있어요.

#일본친구 #한일친구 #언어교환 #서울카페 #OnlyFriends

👉 앱스토어에서 OnlyFriends 검색하고 일본 친구 만들기""",
    ("kr", "threads"): """일본 친구랑 카페에서 지도 보다가
갑자기 다음 약속 얘기까지 나오는 순간이 좋다.

친구는 대단한 이벤트보다
이런 작은 대화에서 시작되는 것 같음.

👉 일본 친구 진짜 만들어보고 싶으면 → https://onlyfriends.tryproo.com/""",
    ("kr", "x"): """일본 친구랑 카페에서 지도 보다가 다음 약속 얘기까지 나오는 순간. 친구는 생각보다 작은 대화에서 시작됨.

👉 일본 친구 진짜 만들어보고 싶으면 → https://onlyfriends.tryproo.com/""",
    ("jp", "instagram"): """カフェで韓国の友達とスマホを見ながら、
行きたい街や次に会う場所の話で自然に笑ってしまう瞬間。

韓国語が完璧じゃなくても大丈夫。
最初から深い話をしなくても大丈夫。

一緒に地図を見て、メニューを選んで、
また会いたいと思える友達づくり。
そのきっかけをOnlyFriendsで作りたいです。

#韓国人友達 #日韓交流 #言語交換 #韓国旅行 #OnlyFriends

👉 App StoreでOnlyFriendsを検索して、韓国の友達作り""",
    ("jp", "threads"): """韓国の友達とカフェで地図を見ていたら、
いつの間にか次に会う約束の話になっていた。

友達づくりって、大きなイベントより
こういう小さな会話から始まる気がする。

👉 韓国の友達を本当に作ってみたいなら → https://onlyfriends.tryproo.com/""",
    ("jp", "x"): """韓国の友達とカフェで地図を見ながら、次に会う約束の話まで自然に進む瞬間。友達づくりは小さな会話から始まる。

👉 韓国の友達を本当に作ってみたいなら → https://onlyfriends.tryproo.com/""",
}


def _write_draft(path: str, meta: dict[str, str], body: str) -> None:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n" + body.strip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-url", required=True)
    ap.add_argument("--image-local-path", required=True)
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()

    env = _load_env_file(os.path.join(ROOT, ".env"))
    results = []
    for account in ("kr", "jp"):
        lang = "ko" if account == "kr" else "ja"
        for platform in ("instagram", "threads", "x"):
            body = COPY[(account, platform)]
            draft_path = os.path.join(
                DRAFTS_DIR,
                f"{platform}-{args.stamp}-{account}-gpt55-preview.md",
            )
            meta = {
                "status": "awaiting_approval" if platform != "x" else "manual_upload_required",
                "platform": platform,
                "account": account,
                "lang": lang,
                "theme": "gpt55-krjp-friendship-preview",
                "hook": body.splitlines()[0],
                "hashtags": "#OnlyFriends",
                "media_type": "image",
                "image_url": args.image_url,
                "image_local_path": os.path.abspath(args.image_local_path),
                "image_keyword": "GPT-5.5 photorealistic Korea-Japan cafe friendship",
                "style_source": "manual_gpt55_preview_after_cleanup",
                "created_at": args.stamp,
                "source": "manual_gpt55_preview_after_cleanup",
            }
            _write_draft(draft_path, meta, body)
            mode = "manual" if platform == "x" else "approval"
            proc = subprocess.run(
                [
                    sys.executable,
                    SLACK_NOTIFIER,
                    "--draft-path",
                    draft_path,
                    "--platform",
                    platform,
                    "--account",
                    account,
                    "--mode",
                    mode,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=60,
            )
            results.append({
                "draft_path": draft_path,
                "platform": platform,
                "account": account,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            })
            if proc.returncode != 0:
                print({"failed": results[-1]}, file=sys.stderr)
                return proc.returncode
    for item in results:
        print(item["stdout"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
