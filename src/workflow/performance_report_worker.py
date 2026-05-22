#!/usr/bin/env python3
"""게시 성과 수집 + Slack 브리핑 워커.

역할:
  - posted draft 의 platform_post_id(media_id/thread_id)를 기준으로 Meta insights 수집
  - 24시간 지난 게시물은 1일 브리핑
  - 7일마다 최근 7일 종합 브리핑
  - Supabase 테이블이 있으면 snapshot/report 저장
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.workflow import content_pipeline as pipeline  # noqa: E402

TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
STATE_PATH = os.path.join(REPO_ROOT, ".runtime", "performance_report_state.json")
LOG_PATH = "/tmp/contentbot-performance-report-worker.log"

IG_API_BASE = "https://graph.instagram.com/v18.0"
THREADS_API_BASE = "https://graph.threads.net/v1.0"
DEFAULT_INTERVAL_SECONDS = 3600
DAILY_REPORT_AFTER_HOURS = 24
DAILY_REPORT_GRACE_HOURS = 72
WEEKLY_REPORT_HOUR_KST = 10

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


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _now_kst() -> dt.datetime:
    return _now_utc() + dt.timedelta(hours=9)


def _parse_time(value: str):
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y%m%d-%H%M%S"):
        try:
            parsed = dt.datetime.strptime(text[:len(fmt)], fmt)
            # draft timestamps are KST local time.
            return parsed - dt.timedelta(hours=9)
        except Exception:
            pass
    try:
        s = text[:-1] if text.endswith("Z") else text
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _load_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _tokens() -> dict:
    return _load_json(TOKENS_PATH)


def _access_token(platform: str, account: str) -> str:
    data = _tokens()
    info = (data.get(platform) or {}).get(account) or {}
    return str(info.get("access_token") or "")


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "content-bot-ai-metrics"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"네트워크 실패: {e.reason}")


def _insights(base: str, media_id: str, token: str, metric_groups: list[list[str]]) -> tuple[dict, list[str]]:
    values = {}
    errors = []
    for metrics in metric_groups:
        query = urllib.parse.urlencode({
            "metric": ",".join(metrics),
            "access_token": token,
        })
        url = f"{base}/{urllib.parse.quote(media_id)}/insights?{query}"
        try:
            data = _http_json(url)
        except Exception as e:
            errors.append(f"{','.join(metrics)}: {e}")
            if len(metrics) > 1:
                for metric in metrics:
                    one, errs = _insights(base, media_id, token, [[metric]])
                    values.update(one)
                    errors.extend(errs)
            continue
        for row in data.get("data") or []:
            name = row.get("name")
            raw_values = row.get("values") or []
            val = None
            if raw_values:
                val = raw_values[-1].get("value")
            if isinstance(val, dict):
                for k, v in val.items():
                    values[f"{name}_{k}"] = v
            elif name:
                values[name] = val
    return values, errors


def _normalize_metrics(platform: str, raw: dict) -> dict:
    if platform == "instagram":
        views = raw.get("views") or raw.get("impressions") or raw.get("plays") or 0
        comments = raw.get("comments") or raw.get("comments_count") or 0
        likes = raw.get("likes") or raw.get("like_count") or 0
        shares = raw.get("shares") or 0
        saves = raw.get("saved") or 0
        reach = raw.get("reach") or 0
        reposts = raw.get("reposts") or raw.get("threads_reposts") or 0
        quotes = raw.get("quotes") or 0
    elif platform == "threads":
        views = raw.get("views") or raw.get("threads_views") or raw.get("content_views") or 0
        comments = raw.get("replies") or raw.get("thread_replies") or 0
        likes = raw.get("likes") or 0
        shares = raw.get("shares") or raw.get("thread_shares") or 0
        saves = raw.get("saved") or 0
        reach = raw.get("reach") or 0
        reposts = raw.get("reposts") or raw.get("threads_reposts") or 0
        quotes = raw.get("quotes") or 0
    else:
        views = comments = likes = shares = saves = reach = reposts = quotes = 0

    interactions = likes + comments + shares + saves + reposts + quotes
    denom = views or reach or 0
    engagement_rate = round((interactions / denom) * 100, 3) if denom else 0
    return {
        "views": int(views or 0),
        "reach": int(reach or 0),
        "likes": int(likes or 0),
        "comments": int(comments or 0),
        "shares": int(shares or 0),
        "saves": int(saves or 0),
        "reposts": int(reposts or 0),
        "quotes": int(quotes or 0),
        "interactions": int(interactions or 0),
        "engagement_rate": engagement_rate,
    }


def _fetch_instagram_metrics(media_id: str, account: str) -> dict:
    token = _access_token("instagram", account)
    if not token:
        return {"ok": False, "error": "instagram token 없음"}
    metric_groups = [
        ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions"],
        ["impressions", "plays"],
    ]
    raw, errors = _insights(IG_API_BASE, media_id, token, metric_groups)
    return {
        "ok": bool(raw),
        "raw": raw,
        "metrics": _normalize_metrics("instagram", raw),
        "errors": errors[:8],
    }


def _fetch_threads_metrics(thread_id: str, account: str) -> dict:
    token = _access_token("threads", account)
    if not token:
        return {"ok": False, "error": "threads token 없음"}
    metric_groups = [
        ["views", "likes", "replies", "reposts", "quotes", "shares"],
        ["threads_views", "thread_replies", "threads_reposts", "thread_shares"],
    ]
    raw, errors = _insights(THREADS_API_BASE, thread_id, token, metric_groups)
    return {
        "ok": bool(raw),
        "raw": raw,
        "metrics": _normalize_metrics("threads", raw),
        "errors": errors[:8],
    }


def _iter_posted_drafts():
    if not os.path.isdir(pipeline.DRAFTS_DIR):
        return
    for name in sorted(os.listdir(pipeline.DRAFTS_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(pipeline.DRAFTS_DIR, name)
        try:
            meta, body = pipeline._parse_draft(path)
        except Exception:
            continue
        if meta.get("status") != "posted":
            continue
        platform = meta.get("slack_platform") or meta.get("platform") or ""
        if platform not in {"instagram", "threads"}:
            continue
        post_id = meta.get("platform_post_id") or meta.get("media_id") or meta.get("thread_id") or ""
        if not post_id:
            continue
        posted_at = _parse_time(meta.get("posted_at") or meta.get("created_at") or "")
        if not posted_at:
            continue
        yield path, meta, body, platform, meta.get("slack_account") or meta.get("account") or "default", post_id, posted_at


def _score(metrics: dict) -> float:
    return (
        metrics.get("views", 0) * 0.02
        + metrics.get("likes", 0) * 1
        + metrics.get("comments", 0) * 4
        + metrics.get("shares", 0) * 5
        + metrics.get("saves", 0) * 4
        + metrics.get("reposts", 0) * 5
        + metrics.get("quotes", 0) * 5
    )


def _record_snapshot(row: dict) -> None:
    try:
        pipeline._supabase_request(
            "POST",
            "content_performance_snapshots",
            [row],
            urllib.parse.urlencode({"on_conflict": "draft_path,snapshot_type"}),
        )
    except Exception as e:
        _log(f"성과 snapshot DB 저장 스킵: {str(e)[:180]}")


def _record_report(row: dict) -> None:
    try:
        pipeline._supabase_request(
            "POST",
            "content_performance_reports",
            [row],
            urllib.parse.urlencode({"on_conflict": "report_key"}),
        )
    except Exception as e:
        _log(f"성과 report DB 저장 스킵: {str(e)[:180]}")


def _slack_post(text: str) -> None:
    channel = (os.environ.get("SLACK_CHANNEL_ID") or "").strip()
    if not channel:
        return
    pipeline._slack_api_post("chat.postMessage", {
        "channel": channel,
        "text": text[:3900],
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": text[:2900]},
        }],
    })


def _fetch_metrics(platform: str, post_id: str, account: str) -> dict:
    if platform == "instagram":
        return _fetch_instagram_metrics(post_id, account)
    if platform == "threads":
        return _fetch_threads_metrics(post_id, account)
    return {"ok": False, "error": f"unsupported platform {platform}"}


def _daily_due(state: dict, path: str, posted_at: dt.datetime) -> bool:
    if path in (state.get("daily_reported") or {}):
        return False
    age_h = (_now_utc() - posted_at).total_seconds() / 3600
    return DAILY_REPORT_AFTER_HOURS <= age_h <= DAILY_REPORT_GRACE_HOURS


def _brief_line(item: dict) -> str:
    m = item["metrics"]
    return (
        f"`{item['platform']}/{item['account']}` "
        f"조회 {m['views']} · 좋아요 {m['likes']} · 댓글 {m['comments']} · "
        f"공유 {m['shares']} · 저장 {m['saves']} · ER {m['engagement_rate']}%"
    )


def _make_daily_report(item: dict) -> str:
    meta = item["meta"]
    body = item["body"].strip().splitlines()[0][:120] if item["body"].strip() else ""
    return (
        "*📊 24시간 성과 브리핑*\n"
        f"{_brief_line(item)}\n"
        f"점수: `{item['score']:.1f}`\n"
        f"훅: {meta.get('hook') or body}\n"
        f"콘셉트: `{meta.get('persona_id', '')}` / `{meta.get('concept_id', '')}`\n"
        f"링크: {meta.get('permalink', '') or '(없음)'}"
    )


def _make_weekly_report(items: list[dict], key: str) -> str:
    if not items:
        return f"*📈 7일 종합 브리핑* `{key}`\n최근 7일 내 수집 가능한 게시물 데이터가 없습니다."
    ranked = sorted(items, key=lambda x: x["score"], reverse=True)
    totals = {
        "views": sum(x["metrics"]["views"] for x in items),
        "likes": sum(x["metrics"]["likes"] for x in items),
        "comments": sum(x["metrics"]["comments"] for x in items),
        "shares": sum(x["metrics"]["shares"] for x in items),
        "saves": sum(x["metrics"]["saves"] for x in items),
        "reposts": sum(x["metrics"]["reposts"] for x in items),
        "quotes": sum(x["metrics"]["quotes"] for x in items),
    }
    platform_totals = {}
    for item in items:
        platform_totals.setdefault(item["platform"], 0)
        platform_totals[item["platform"]] += item["score"]
    best_platform = max(platform_totals.items(), key=lambda x: x[1])[0]
    top_lines = []
    for idx, item in enumerate(ranked[:5], 1):
        hook = item["meta"].get("hook") or item["body"].strip().splitlines()[0][:80]
        top_lines.append(f"{idx}. {_brief_line(item)} · `{item['score']:.1f}`\n   {hook}")
    return (
        f"*📈 7일 종합 브리핑* `{key}`\n"
        f"게시물: {len(items)}개 · 조회 {totals['views']} · 좋아요 {totals['likes']} · "
        f"댓글 {totals['comments']} · 공유 {totals['shares']} · 저장 {totals['saves']}\n"
        f"가장 반응 좋은 플랫폼: `{best_platform}`\n\n"
        "*Top 콘텐츠*\n" + "\n".join(top_lines) + "\n\n"
        "*다음 주 개선 방향*\n"
        "- 점수 높은 훅/장면/persona 조합을 더 자주 사용\n"
        "- 공유·저장 비율이 높은 이미지 콘셉트를 우선 재사용\n"
        "- 낮은 ER 콘텐츠는 CTA는 유지하되 첫 문장과 이미지 장면을 더 강하게 변주"
    )


def _collect_post(path: str, meta: dict, body: str, platform: str,
                  account: str, post_id: str, snapshot_type: str) -> dict | None:
    res = _fetch_metrics(platform, post_id, account)
    if not res.get("ok"):
        _log(f"성과 수집 실패 {platform}/{account}/{post_id}: {res.get('error') or res.get('errors')}")
        return None
    metrics = res.get("metrics") or {}
    item = {
        "path": path,
        "meta": meta,
        "body": body,
        "platform": platform,
        "account": account,
        "post_id": post_id,
        "metrics": metrics,
        "raw_metrics": res.get("raw") or {},
        "score": _score(metrics),
    }
    _record_snapshot({
        "draft_path": path,
        "platform_id": platform,
        "account": account,
        "platform_post_id": post_id,
        "snapshot_type": snapshot_type,
        "snapshot_at": _now_utc().replace(microsecond=0).isoformat() + "Z",
        **metrics,
        "score": item["score"],
        "raw_metrics": res.get("raw") or {},
        "metric_errors": res.get("errors") or [],
    })
    return item


def run_once(force_weekly: bool = False) -> dict:
    state = _load_json(STATE_PATH)
    state.setdefault("daily_reported", {})
    state.setdefault("weekly_reported", {})

    daily_count = 0
    weekly_items = []
    now = _now_utc()
    week_start = now - dt.timedelta(days=7)

    for path, meta, body, platform, account, post_id, posted_at in _iter_posted_drafts() or []:
        if posted_at >= week_start:
            item = _collect_post(path, meta, body, platform, account, post_id, "latest")
            if item:
                weekly_items.append(item)
        if _daily_due(state, path, posted_at):
            item = _collect_post(path, meta, body, platform, account, post_id, "24h")
            if not item:
                continue
            _slack_post(_make_daily_report(item))
            state["daily_reported"][path] = _now_utc().replace(microsecond=0).isoformat() + "Z"
            daily_count += 1

    kst = _now_kst()
    iso_year, iso_week, _weekday = kst.isocalendar()
    weekly_key = f"{iso_year}-W{iso_week:02d}"
    weekly_due = force_weekly or (
        kst.weekday() == 0
        and kst.hour >= WEEKLY_REPORT_HOUR_KST
        and weekly_key not in state["weekly_reported"]
    )
    weekly_sent = False
    if weekly_due:
        report_text = _make_weekly_report(weekly_items, weekly_key)
        _slack_post(report_text)
        _record_report({
            "report_key": weekly_key,
            "report_type": "weekly",
            "period_start": (kst.date() - dt.timedelta(days=7)).isoformat(),
            "period_end": kst.date().isoformat(),
            "summary_text": report_text,
            "raw_items": [
                {
                    "draft_path": x["path"],
                    "platform": x["platform"],
                    "account": x["account"],
                    "post_id": x["post_id"],
                    "metrics": x["metrics"],
                    "score": x["score"],
                    "hook": x["meta"].get("hook", ""),
                    "persona_id": x["meta"].get("persona_id", ""),
                    "concept_id": x["meta"].get("concept_id", ""),
                }
                for x in weekly_items
            ],
        })
        state["weekly_reported"][weekly_key] = _now_utc().replace(microsecond=0).isoformat() + "Z"
        weekly_sent = True

    _save_json(STATE_PATH, state)
    return {
        "daily_reports": daily_count,
        "weekly_sent": weekly_sent,
        "weekly_items": len(weekly_items),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SNS 성과 수집/브리핑 워커")
    parser.add_argument("--once", action="store_true", help="한 번만 실행")
    parser.add_argument("--force-weekly", action="store_true", help="7일 리포트 즉시 발송")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    pipeline._load_env_file(pipeline.ENV_PATH)
    pipeline._load_env_file(pipeline.ENV_PATH_LEGACY)

    while not _STOP:
        try:
            summary = run_once(force_weekly=args.force_weekly)
            _log(f"성과 확인 완료: {summary}")
        except Exception as e:
            _log(f"성과 워커 예외: {e}\n{traceback.format_exc()}")
        if args.once:
            break
        slept = 0
        interval = max(300, int(args.interval))
        while slept < interval and not _STOP:
            time.sleep(1)
            slept += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
