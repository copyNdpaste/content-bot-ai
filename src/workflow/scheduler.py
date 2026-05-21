#!/usr/bin/env python3
# version: scheduler_v2_random_interval
"""박재범 자율 회차 스케줄러 데몬.

ROUTINE_MIN_HOURS ~ ROUTINE_MAX_HOURS 사이 **랜덤** 간격 (분 단위 정밀도) 으로
content_pipeline.py 를 호출 — 매 회차마다 다른 시간차로 게시해서 봇 패턴 회피.
launchd 가 RunAtLoad=true, KeepAlive=true 로 띄우는 것을 전제.

env (.env 또는 launchd):
  ROUTINE_MIN_HOURS       기본 2.0  (랜덤 하한)
  ROUTINE_MAX_HOURS       기본 3.0  (랜덤 상한)
  ROUTINE_INTERVAL_HOURS  (legacy 폴백 — 있으면 min=max 로 사용)
  ROUTINE_PLATFORMS       기본 threads,instagram,x
  ROUTINE_ACCOUNTS        기본 jp,kr
  TELEGRAM_BOT_TOKEN/CHAT_ID  알림 폴백
  SLACK_BOT_TOKEN/CHANNEL_ID  Slack 알림용 (notifier 에 위임)

로그: /tmp/contentbot-content-scheduler.log
"""
import json
import os
import random
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
LOG_PATH = "/tmp/contentbot-content-scheduler.log"

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


def _next_interval_seconds() -> tuple:
    """매 회차마다 호출 — ROUTINE_MIN_HOURS ~ MAX_HOURS 사이 랜덤 초.
       반환: (seconds, human_label)  예: (8521, "2시간 22분")
       legacy ROUTINE_INTERVAL_HOURS 가 있으면 min=max 로 처리 (옛 동작 호환)."""
    legacy = (os.environ.get("ROUTINE_INTERVAL_HOURS") or "").strip()
    if legacy:
        try:
            h = float(legacy)
            min_h = max_h = max(0.1, min(h, 24.0))
        except ValueError:
            min_h, max_h = 2.0, 3.0
    else:
        try:
            min_h = float((os.environ.get("ROUTINE_MIN_HOURS") or "2.0").strip())
        except ValueError:
            min_h = 2.0
        try:
            max_h = float((os.environ.get("ROUTINE_MAX_HOURS") or "3.0").strip())
        except ValueError:
            max_h = 3.0
    # 안전: 0.1h(6분) ~ 24h, min<=max 보장
    min_h = max(0.1, min(min_h, 24.0))
    max_h = max(min_h, min(max_h, 24.0))
    hours = random.uniform(min_h, max_h)
    seconds = int(hours * 3600)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return seconds, f"{h}시간 {m}분"


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
    _log(f"스케줄러 부팅 — pipeline={PIPELINE}")
    _push_telegram("🛰️ 박재범 스케줄러 부팅 — 회차마다 2~3시간 사이 랜덤 간격")

    # 시작 즉시 1회 실행 (launchd RunAtLoad 와 자연스럽게 결합)
    while not _STOP:
        try:
            _run_pipeline_once()
        except Exception as e:
            _log(f"메인 루프 예외: {e}\n{traceback.format_exc()}")
            _push_telegram(f"💥 박재범 스케줄러 예외: {e}")

        if _STOP:
            break

        # 다음 회차까지 대기 — 매번 새 랜덤 간격 (봇 패턴 회피)
        interval, label = _next_interval_seconds()
        next_at = time.strftime("%H:%M", time.localtime(time.time() + interval))
        _log(f"다음 회차: {label} 후 ({next_at} 예정)")
        _push_telegram(f"⏳ 다음 회차: {label} 후 ({next_at})")

        slept = 0
        while slept < interval and not _STOP:
            time.sleep(1)
            slept += 1

    _log("스케줄러 종료")
    _push_telegram("🛑 박재범 스케줄러 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
