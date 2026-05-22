#!/usr/bin/env python3
"""쿨다운 큐 자동 업로드 워커.

Instagram/Threads 자동 업로드 중 플랫폼 쿨다운이 감지되면 draft 상태가
`queued` 로 바뀐다. 이 워커는 queued_until 이 지난 draft 를 찾아 재시도한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.workflow import content_pipeline as pipeline  # noqa: E402

LOG_PATH = "/tmp/contentbot-queued-upload-worker.log"
DEFAULT_INTERVAL_SECONDS = 300
_STOP = False


def _log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _sig_handler(signum, _frame):
    global _STOP
    _STOP = True
    _log(f"signal {signum} 수신")


def _parse_utc(value: str):
    if not value:
        return None
    try:
        s = value[:-1] if value.endswith("Z") else value
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _due(meta: dict) -> bool:
    until = _parse_utc(meta.get("queued_until", ""))
    if not until:
        return True
    return until <= _now_utc()


def _disabled_targets() -> set[tuple[str, str]]:
    raw = (os.environ.get("ROUTINE_DISABLED_TARGETS") or "").strip()
    out = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item or ":" not in item:
            continue
        platform, account = [x.strip() for x in item.split(":", 1)]
        if platform and account:
            out.add((platform, account))
    return out


def _iter_queued_drafts():
    if not os.path.isdir(pipeline.DRAFTS_DIR):
        return
    for name in sorted(os.listdir(pipeline.DRAFTS_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(pipeline.DRAFTS_DIR, name)
        try:
            meta, body = pipeline._parse_draft(path)
        except Exception as e:
            _log(f"draft 파싱 실패 {path}: {e}")
            continue
        if meta.get("status") != "queued":
            continue
        platform = meta.get("slack_platform") or meta.get("platform") or ""
        if platform == "x":
            continue
        yield path, meta, body


def run_once() -> dict:
    attempted = 0
    posted = 0
    requeued = 0
    failed = 0
    skipped = 0

    disabled = _disabled_targets()
    for path, meta, _body in _iter_queued_drafts() or []:
        if not _due(meta):
            skipped += 1
            continue
        platform = meta.get("slack_platform") or meta.get("platform") or ""
        account = meta.get("slack_account") or meta.get("account") or "default"
        if (platform.lower(), account.lower()) in disabled:
            skipped += 1
            _log(f"비활성 타겟 스킵: {platform}/{account} {os.path.basename(path)}")
            continue
        attempted += 1
        _log(f"재시도 시작: {platform}/{account} {os.path.basename(path)}")
        result = pipeline._auto_upload_after_slack(path, platform, account)
        if result.get("ok"):
            posted += 1
        elif result.get("queued"):
            requeued += 1
        else:
            failed += 1

    return {
        "attempted": attempted,
        "posted": posted,
        "requeued": requeued,
        "failed": failed,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="queued draft 자동 업로드 워커")
    parser.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                        help="반복 모드 확인 주기(초)")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    pipeline._load_env_file(pipeline.ENV_PATH)
    pipeline._load_env_file(pipeline.ENV_PATH_LEGACY)

    while not _STOP:
        try:
            summary = run_once()
            if summary["attempted"] or args.once:
                _log(f"큐 확인 완료: {summary}")
        except Exception as e:
            _log(f"큐 워커 예외: {e}\n{traceback.format_exc()}")
        if args.once:
            break
        slept = 0
        interval = max(30, int(args.interval))
        while slept < interval and not _STOP:
            time.sleep(1)
            slept += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
