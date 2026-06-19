#!/usr/bin/env python3
"""Delete recent Instagram/Threads posts tracked by local draft files.

Safety defaults:
  - dry-run unless --execute is supplied
  - actual deletion also requires --confirm DELETE
  - targets only posted drafts with platform_post_id from the last N days
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
TOKEN_MANAGER_PATH = os.path.join(REPO_ROOT, "src", "auth", "token_manager.py")
ENV_PATH = os.path.join(REPO_ROOT, ".env")
ENV_PATH_LEGACY = os.path.join(REPO_ROOT, "_company", "_agents", "instagram", ".env")

IG_API_BASE = "https://graph.instagram.com/v18.0"
THREADS_API_BASE = "https://graph.threads.net/v1.0"
REFRESH_THRESHOLD_DAYS = 7


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
            value = value.strip()
            if value and not (value.startswith('"') or value.startswith("'")):
                hash_idx = value.find("#")
                if hash_idx >= 0:
                    value = value[:hash_idx].rstrip()
            value = value.strip('"').strip("'")
            if key and not os.environ.get(key):
                os.environ[key] = value


def _parse_draft(path: str) -> tuple[dict, str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def _write_draft(path: str, meta: dict, body: str) -> None:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    out = "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


def _parse_time(value: str):
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y%m%d-%H%M%S"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _load_tokens() -> dict:
    if not os.path.isfile(TOKENS_PATH):
        return {}
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _days_until(iso: str):
    parsed = _parse_time(iso)
    if not parsed:
        return None
    return (parsed - dt.datetime.utcnow()).total_seconds() / 86400.0


def _try_auto_refresh() -> None:
    if not os.path.isfile(TOKEN_MANAGER_PATH):
        return
    try:
        subprocess.run(
            [sys.executable, TOKEN_MANAGER_PATH, "--refresh"],
            timeout=60,
            capture_output=True,
        )
    except Exception:
        pass


def _resolve_credentials(platform: str, account: str) -> tuple[str, str, str]:
    tokens = _load_tokens()
    section = "instagram" if platform == "instagram" else "threads"
    info = (tokens.get(section) or {}).get(account) or {}
    if info.get("access_token"):
        days = _days_until(info.get("expires_at", ""))
        if days is not None and days <= REFRESH_THRESHOLD_DAYS:
            _try_auto_refresh()
            tokens = _load_tokens()
            info = (tokens.get(section) or {}).get(account) or info
        return info.get("access_token", ""), info.get("user_id", ""), "tokens.json"

    if platform == "instagram":
        token = (os.environ.get("META_IG_ACCESS_TOKEN") or "").strip()
        user_id = (os.environ.get("META_IG_USER_ID") or "").strip()
    else:
        token = (os.environ.get("META_THREADS_ACCESS_TOKEN") or "").strip()
        user_id = (os.environ.get("META_THREADS_USER_ID") or "").strip()
    if token:
        return token, user_id, "env"
    return "", "", "none"


def _http_delete_json(url: str, access_token: str) -> dict:
    query = urllib.parse.urlencode({"access_token": access_token})
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}{query}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}")
    if not raw.strip():
        return {"success": True}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:500]}


def _delete_post(platform: str, post_id: str, access_token: str) -> dict:
    base = IG_API_BASE if platform == "instagram" else THREADS_API_BASE
    url = f"{base}/{urllib.parse.quote(post_id)}"
    return _http_delete_json(url, access_token)


def _iter_candidates(
    *,
    days: int,
    from_dt,
    until_dt,
    platforms: set[str],
    accounts: set[str],
):
    cutoff = from_dt or (dt.datetime.now() - dt.timedelta(days=days))
    if not os.path.isdir(DRAFTS_DIR):
        return
    for name in sorted(os.listdir(DRAFTS_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(DRAFTS_DIR, name)
        try:
            meta, body = _parse_draft(path)
        except Exception as e:
            yield {"path": path, "skip": f"parse failed: {e}"}
            continue
        platform = (meta.get("slack_platform") or meta.get("platform") or meta.get("target") or "").lower()
        account = (meta.get("slack_account") or meta.get("account") or "default").lower()
        if platform not in {"instagram", "threads"}:
            continue
        if platforms and platform not in platforms:
            continue
        if accounts and account not in accounts:
            continue
        if meta.get("status") != "posted":
            continue
        post_id = (meta.get("platform_post_id") or "").strip()
        if not post_id:
            continue
        posted_at = _parse_time(meta.get("posted_at", ""))
        if not posted_at or posted_at < cutoff:
            continue
        if until_dt and posted_at >= until_dt:
            continue
        yield {
            "path": path,
            "meta": meta,
            "body": body,
            "platform": platform,
            "account": account,
            "post_id": post_id,
            "posted_at": posted_at.isoformat(),
            "permalink": meta.get("permalink", ""),
        }


def _mark_deleted(item: dict, result: dict) -> None:
    meta = dict(item["meta"])
    meta["status"] = "deleted"
    meta["deleted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["deleted_result"] = json.dumps(result, ensure_ascii=False)[:500].replace("\n", " ")
    _write_draft(item["path"], meta, item["body"])


def main() -> int:
    _load_env_file(ENV_PATH)
    _load_env_file(ENV_PATH_LEGACY)

    ap = argparse.ArgumentParser(
        description="Delete recent Instagram/Threads posts tracked by draft metadata."
    )
    ap.add_argument("--days", type=int, default=14, help="posted_at window in days")
    ap.add_argument(
        "--from",
        dest="from_date",
        default="",
        help="inclusive posted_at lower bound, e.g. 2026-05-22T18:01:32",
    )
    ap.add_argument(
        "--until",
        default="",
        help="exclusive posted_at upper bound, e.g. 2026-06-13T23:49:00",
    )
    ap.add_argument(
        "--platforms",
        default="instagram,threads",
        help="comma-separated: instagram,threads",
    )
    ap.add_argument("--accounts", default="jp,kr", help="comma-separated accounts")
    ap.add_argument("--execute", action="store_true", help="actually delete posts")
    ap.add_argument("--confirm", default="", help="must be DELETE when --execute is used")
    ap.add_argument("--limit", type=int, default=0, help="optional max count")
    args = ap.parse_args()

    platforms = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
    accounts = {a.strip().lower() for a in args.accounts.split(",") if a.strip()}
    if not platforms <= {"instagram", "threads"}:
        sys.stderr.write("--platforms must contain only instagram,threads\n")
        return 2
    if args.days <= 0:
        sys.stderr.write("--days must be positive\n")
        return 2
    from_dt = _parse_time(args.from_date)
    until_dt = _parse_time(args.until)
    if args.from_date and not from_dt:
        sys.stderr.write("--from must be a valid datetime\n")
        return 2
    if args.until and not until_dt:
        sys.stderr.write("--until must be a valid datetime\n")
        return 2
    if from_dt and until_dt and until_dt <= from_dt:
        sys.stderr.write("--until must be later than --from\n")
        return 2
    if args.execute and args.confirm != "DELETE":
        sys.stderr.write("Refusing to delete: pass --execute --confirm DELETE\n")
        return 2

    items = [
        x
        for x in _iter_candidates(
            days=args.days,
            from_dt=from_dt,
            until_dt=until_dt,
            platforms=platforms,
            accounts=accounts,
        )
        if not x.get("skip")
    ]
    if args.limit > 0:
        items = items[: args.limit]

    summary = {
        "mode": "execute" if args.execute else "dry-run",
        "days": args.days,
        "from": from_dt.isoformat() if from_dt else "",
        "until": until_dt.isoformat() if until_dt else "",
        "platforms": sorted(platforms),
        "accounts": sorted(accounts),
        "count": len(items),
        "items": [
            {
                "platform": item["platform"],
                "account": item["account"],
                "post_id": item["post_id"],
                "posted_at": item["posted_at"],
                "permalink": item["permalink"],
                "draft_path": item["path"],
            }
            for item in items
        ],
    }

    if not args.execute:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    results = []
    for item in items:
        token, _user_id, source = _resolve_credentials(item["platform"], item["account"])
        row = {
            "platform": item["platform"],
            "account": item["account"],
            "post_id": item["post_id"],
            "permalink": item["permalink"],
            "draft_path": item["path"],
            "token_source": source,
        }
        if not token:
            row.update({"ok": False, "error": "missing access token"})
            results.append(row)
            continue
        try:
            result = _delete_post(item["platform"], item["post_id"], token)
            row.update({"ok": True, "result": result})
            _mark_deleted(item, result)
        except Exception as e:
            row.update({"ok": False, "error": str(e)[:800]})
        results.append(row)

    print(json.dumps({**summary, "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
