#!/usr/bin/env python3
# version: scheduler_v2_random_interval
"""박재범 자율 회차 스케줄러 데몬.

ROUTINE_MIN_HOURS ~ ROUTINE_MAX_HOURS 사이 **랜덤** 간격 (분 단위 정밀도) 으로
generate_platform_pack.py 를 호출 — 회차별 이미지 1장과 플랫폼별 문구를 생성한다.
launchd 가 RunAtLoad=true, KeepAlive=true 로 띄우는 것을 전제.

env (.env 또는 launchd):
  ROUTINE_MIN_HOURS       기본 2.0  (랜덤 하한)
  ROUTINE_MAX_HOURS       기본 3.0  (랜덤 상한)
  ROUTINE_INTERVAL_HOURS  (legacy 폴백 — 있으면 min=max 로 사용)
  ROUTINE_ACTIVE_START_HOUR 기본 9   (KST, 이 시각부터 회차 시작)
  ROUTINE_ACTIVE_END_HOUR   기본 23  (KST, 이 시각부터 다음날까지 대기)
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
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.domain import scheduling as scheduling_rules  # noqa: E402

PACK_GENERATOR = os.path.join(REPO_ROOT, "scripts", "generate_platform_pack.py")
ENV_PATH = os.path.join(REPO_ROOT, ".env")
ENV_PATH_LEGACY = os.path.join(REPO_ROOT, "_company", "_agents", "instagram", ".env")

PYTHON_BIN = sys.executable or "/opt/homebrew/bin/python3"
LOG_PATH = "/tmp/contentbot-content-scheduler.log"
STATE_PATH = os.path.join(REPO_ROOT, "var", "content-scheduler-state.json")

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
    min_h, max_h = _interval_bounds()
    hours = random.uniform(min_h, max_h)
    seconds = int(hours * 3600)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return seconds, f"{h}시간 {m}분"


def _interval_bounds() -> tuple[float, float]:
    return scheduling_rules.interval_bounds(
        legacy_hours=os.environ.get("ROUTINE_INTERVAL_HOURS") or "",
        min_hours=os.environ.get("ROUTINE_MIN_HOURS") or "2.0",
        max_hours=os.environ.get("ROUTINE_MAX_HOURS") or "3.0",
    )


def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(patch: dict) -> None:
    data = _read_state()
    data.update(patch)
    data["updated_at"] = int(time.time())
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        _log(f"상태 파일 저장 실패: {e}")


def _seconds_until_min_interval_elapsed() -> int:
    last_started = _read_state().get("last_run_started_at")
    min_h, _max_h = _interval_bounds()
    return scheduling_rules.seconds_until_min_interval_elapsed(
        now=time.time(),
        last_started_at=last_started,
        min_hours=min_h,
    )


def _sleep_interruptibly(seconds: int) -> None:
    slept = 0
    seconds = max(0, int(seconds))
    while slept < seconds and not _STOP:
        time.sleep(1)
        slept += 1


def _active_hours() -> tuple[int, int]:
    try:
        start = int((os.environ.get("ROUTINE_ACTIVE_START_HOUR") or "9").strip())
    except ValueError:
        start = 9
    try:
        end = int((os.environ.get("ROUTINE_ACTIVE_END_HOUR") or "23").strip())
    except ValueError:
        end = 23
    start = max(0, min(start, 23))
    end = max(1, min(end, 24))
    if end <= start:
        end = min(start + 1, 24)
    return start, end


def _kst_now() -> tuple[int, int]:
    t = time.gmtime(time.time() + 9 * 3600)
    return t.tm_hour, t.tm_min


def _seconds_until_next_active_start() -> tuple[int, str]:
    start, end = _active_hours()
    now = time.time()
    kst = time.gmtime(now + 9 * 3600)
    hour = kst.tm_hour
    minute = kst.tm_min
    second = kst.tm_sec

    if hour < start:
        delta = ((start - hour) * 3600) - (minute * 60) - second
    else:
        delta = ((24 - hour + start) * 3600) - (minute * 60) - second
    delta = max(60, int(delta))
    next_label = time.strftime("%Y-%m-%d %H:%M KST", time.gmtime(now + delta + 9 * 3600))
    return delta, next_label


def _within_active_window() -> bool:
    start, end = _active_hours()
    hour, _minute = _kst_now()
    return start <= hour < end


def _run_pipeline_once() -> dict:
    platforms = os.environ.get("ROUTINE_PLATFORMS", "threads,instagram,x").strip() or "all"
    accounts = os.environ.get("ROUTINE_ACCOUNTS", "jp,kr").strip() or "all"

    cmd = [
        PYTHON_BIN, PACK_GENERATOR,
        "--platforms", platforms,
        "--accounts", accounts,
    ]
    _log(f"회차 시작: {' '.join(cmd)}")
    _push_telegram(f"🚀 박재범 회차 시작\nplatforms={platforms} accounts={accounts}")

    timeout = int(os.environ.get("ROUTINE_RUN_TIMEOUT_SEC") or "4200")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"회차 타임아웃 ({timeout}초 초과)")
        _push_telegram(f"⏱️ 박재범 회차 타임아웃 ({timeout}초 초과)")
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
    _load_env_file(ENV_PATH_LEGACY)
    _log(f"스케줄러 부팅 — pack_generator={PACK_GENERATOR}")
    start, end = _active_hours()
    min_h, max_h = _interval_bounds()
    _push_telegram(
        f"🛰️ 박재범 스케줄러 부팅 — {start}:00~{end}:00 KST, "
        f"회차마다 {min_h:g}~{max_h:g}시간 사이 랜덤 간격"
    )

    while not _STOP:
        if not _within_active_window():
            wait, next_label = _seconds_until_next_active_start()
            _log(f"운영 시간 외 — 다음 시작 {next_label}")
            _sleep_interruptibly(wait)
            continue

        remaining = _seconds_until_min_interval_elapsed()
        if remaining > 0:
            next_at = time.strftime("%H:%M", time.localtime(time.time() + remaining))
            _log(f"최소 2시간 간격 보호 — {remaining // 60}분 후 재개 ({next_at} 예정)")
            _sleep_interruptibly(remaining)
            continue

        try:
            _write_state({"last_run_started_at": int(time.time())})
            _run_pipeline_once()
        except Exception as e:
            _log(f"메인 루프 예외: {e}\n{traceback.format_exc()}")
            _push_telegram(f"💥 박재범 스케줄러 예외: {e}")

        if _STOP:
            break

        # 다음 회차까지 대기 — 매번 새 랜덤 간격 (봇 패턴 회피)
        interval, label = _next_interval_seconds()
        if not _within_active_window():
            interval, next_label = _seconds_until_next_active_start()
            label = f"운영 시간 외 대기 → {next_label}"
        else:
            start, end = _active_hours()
            next_hour = time.gmtime(time.time() + interval + 9 * 3600).tm_hour
            if next_hour < start or next_hour >= end:
                interval, next_label = _seconds_until_next_active_start()
                label = f"다음 운영 시간까지 대기 → {next_label}"
        next_at = time.strftime("%H:%M", time.localtime(time.time() + interval))
        _log(f"다음 회차: {label} 후 ({next_at} 예정)")
        _push_telegram(f"⏳ 다음 회차: {label} 후 ({next_at})")

        _sleep_interruptibly(interval)

    _log("스케줄러 종료")
    _push_telegram("🛑 박재범 스케줄러 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
