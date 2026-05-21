#!/usr/bin/env python3
# version: scheduler_v1
"""박재범 자율 회차 스케줄러 데몬.

매 ROUTINE_INTERVAL_HOURS 시간마다 content_pipeline.py 를 호출한다.
launchd 가 RunAtLoad=true, KeepAlive=true 로 띄우는 것을 전제.

env (.env 또는 launchd):
  ROUTINE_INTERVAL_HOURS  기본 4
  ROUTINE_PLATFORMS       기본 threads,instagram,x
  ROUTINE_ACCOUNTS        기본 jp,kr
  TELEGRAM_BOT_TOKEN/CHAT_ID  알림 폴백
  SLACK_BOT_TOKEN/CHANNEL_ID  Slack 알림용 (notifier 에 위임)

로그: /tmp/moneyai-content-scheduler.log
"""
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PIPELINE = os.path.join(HERE, "content_pipeline.py")
ENV_PATH = os.path.join(REPO_ROOT, "_company", "_agents", "instagram", ".env")

PYTHON_BIN = sys.executable or "/opt/homebrew/bin/python3"
LOG_PATH = "/tmp/moneyai-content-scheduler.log"

_STOP = False


def _sig_handler(signum, _frame):
    global _STOP
    _STOP = True
    _log(f"signal {signum} 수신 → 종료 준비")


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    # launchd StandardOutPath 와 별개로 stdout 도 흘림
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _load_env_file(path: str) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if v and not (v.startswith('"') or v.startswith("'")):
                    hash_idx = v.find("#")
                    if hash_idx >= 0:
                        v = v[:hash_idx].rstrip()
                v = v.strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


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


def _interval_seconds() -> int:
    raw = (os.environ.get("ROUTINE_INTERVAL_HOURS") or "4").strip()
    try:
        hours = float(raw)
    except ValueError:
        hours = 4.0
    hours = max(0.25, min(hours, 24.0))  # 15분 ~ 24h
    return int(hours * 3600)


def _run_pipeline_once() -> dict:
    platforms = os.environ.get("ROUTINE_PLATFORMS", "threads,instagram,x").strip() or "all"
    accounts = os.environ.get("ROUTINE_ACCOUNTS", "jp,kr").strip() or "all"

    cmd = [
        PYTHON_BIN, PIPELINE,
        "--platform", platforms,
        "--account", accounts,
    ]
    _log(f"회차 시작: {' '.join(cmd)}")
    _push_telegram(f"🚀 박재범 회차 시작\nplatforms={platforms} accounts={accounts}")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        _log("회차 타임아웃 (15분 초과)")
        _push_telegram("⏱️ 박재범 회차 타임아웃 (15분 초과)")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        _log(f"회차 subprocess 실패: {e}")
        _push_telegram(f"❌ 박재범 회차 실패: {e}")
        return {"ok": False, "error": str(e)}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    summary = {}
    if out:
        try:
            summary = json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            summary = {"raw": out[:500]}

    if proc.returncode == 0:
        _log(f"회차 완료: {summary}")
        msg = (
            f"✅ 박재범 회차 완료\n"
            f"drafts={summary.get('drafts_created', '?')} "
            f"slack={summary.get('slack_notified', '?')}"
        )
        if summary.get("errors"):
            msg += f"\n⚠️ {len(summary['errors'])}건 부분 실패"
        _push_telegram(msg)
        return {"ok": True, "summary": summary}

    _log(f"회차 비정상 종료 (exit {proc.returncode}): {err[:300]}")
    _push_telegram(f"❌ 박재범 회차 실패 (exit {proc.returncode}): {err[:200]}")
    return {"ok": False, "error": err[:300] or f"exit {proc.returncode}",
            "summary": summary}


def main() -> int:
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    _load_env_file(ENV_PATH)
    interval = _interval_seconds()
    _log(f"스케줄러 부팅 — interval={interval}s pipeline={PIPELINE}")
    _push_telegram(f"🛰️ 박재범 스케줄러 부팅 (회차 주기 {interval // 60}분)")

    # 시작 즉시 1회 실행 (launchd RunAtLoad 와 자연스럽게 결합)
    first = True
    while not _STOP:
        if first:
            first = False
        try:
            _run_pipeline_once()
        except Exception as e:
            _log(f"메인 루프 예외: {e}\n{traceback.format_exc()}")
            _push_telegram(f"💥 박재범 스케줄러 예외: {e}")

        # 다음 회차까지 대기 (1초씩 끊어서 종료 신호 빠르게 반영)
        slept = 0
        while slept < interval and not _STOP:
            time.sleep(1)
            slept += 1

    _log("스케줄러 종료")
    _push_telegram("🛑 박재범 스케줄러 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
