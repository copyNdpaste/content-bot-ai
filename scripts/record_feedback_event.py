#!/usr/bin/env python3
"""Record platform performance/comment feedback for a generated draft artifact."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def _load_env_file(path: str) -> None:
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _request(method: str, table: str, payload=None, query: str = ""):
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL/SUPABASE_KEY missing")
    endpoint = f"{url}/rest/v1/{urllib.parse.quote(table)}"
    if query:
        endpoint += f"?{query}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else None


def _artifact_id_from_draft(draft_path: str) -> str:
    query = urllib.parse.urlencode({
        "select": "id",
        "draft_path": f"eq.{draft_path}",
        "limit": "1",
    })
    rows = _request("GET", "content_generation_artifacts", query=query) or []
    if not rows:
        raise RuntimeError(f"artifact not found for draft_path: {draft_path}")
    return str(rows[0]["id"])


def _parse_comments(raw: str):
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except Exception:
        return [{"text": raw}]


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_env_file(os.path.join(repo_root, ".env"))

    ap = argparse.ArgumentParser()
    ap.add_argument("--draft-path", required=True)
    ap.add_argument("--platform", required=True, choices=["instagram", "threads", "x"])
    ap.add_argument("--external-post-id", default="")
    ap.add_argument("--hours-since-post", type=int, default=24)
    ap.add_argument("--views", type=int, default=0)
    ap.add_argument("--likes", type=int, default=0)
    ap.add_argument("--comments", type=int, default=0)
    ap.add_argument("--shares", type=int, default=0)
    ap.add_argument("--quotes", type=int, default=0)
    ap.add_argument("--saves", type=int, default=0)
    ap.add_argument("--profile-visits", type=int, default=0)
    ap.add_argument("--follows", type=int, default=0)
    ap.add_argument("--comment-samples", default="", help="JSON list or plain text")
    ap.add_argument("--comment-summary", default="")
    ap.add_argument("--sentiment", default="")
    ap.add_argument("--source", default="manual")
    args = ap.parse_args()

    artifact_id = _artifact_id_from_draft(args.draft_path)
    row = {
        "artifact_id": artifact_id,
        "platform_id": args.platform,
        "external_post_id": args.external_post_id,
        "hours_since_post": args.hours_since_post,
        "views": args.views,
        "likes": args.likes,
        "comments": args.comments,
        "shares": args.shares,
        "quotes": args.quotes,
        "saves": args.saves,
        "profile_visits": args.profile_visits,
        "follows": args.follows,
        "comment_samples": _parse_comments(args.comment_samples),
        "comment_summary": args.comment_summary,
        "sentiment": args.sentiment or None,
        "source": args.source,
        "raw_payload": vars(args),
    }
    res = _request("POST", "content_feedback_events", [row])
    print(json.dumps({"ok": True, "inserted": len(res or []), "artifact_id": artifact_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
