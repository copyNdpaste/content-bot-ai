#!/usr/bin/env python3
"""Bootstrap X OAuth 2.0 user tokens with PKCE and save to src/auth/tokens.json."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV_PATH = os.path.join(REPO_ROOT, ".env")
TOKENS_PATH = os.path.join(REPO_ROOT, "src", "auth", "tokens.json")
PENDING_PATH = "/tmp/content-bot-ai-x-oauth-pending.json"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
AUTH_URL = "https://x.com/i/oauth2/authorize"
ME_URL = "https://api.x.com/2/users/me"


def load_env() -> None:
    if not os.path.isfile(ENV_PATH):
        return
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_tokens() -> dict:
    if not os.path.isfile(TOKENS_PATH):
        return {"threads": {}, "instagram": {}, "x": {}}
    with open(TOKENS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    data.setdefault("threads", {})
    data.setdefault("instagram", {})
    data.setdefault("x", {})
    return data


def save_tokens(data: dict) -> None:
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, TOKENS_PATH)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def exchange_code(code: str, verifier: str, redirect_uri: str,
                  client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def me(access_token: str) -> dict:
    req = urllib.request.Request(ME_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}
    expected_state: str = ""

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]
        if state != self.expected_state:
            self.result = {"error": "state mismatch"}
        elif error:
            self.result = {"error": error}
        elif not code:
            self.result = {"error": "missing code"}
        else:
            self.result = {"code": code}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body><h3>X OAuth complete. You can close this tab.</h3></body></html>"
            .encode("utf-8")
        )


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="kr | jp | ...")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--open-browser", action="store_true")
    ap.add_argument("--manual", action="store_true",
                    help="Print auth URL and save PKCE state, then exit.")
    ap.add_argument("--complete-url", default="",
                    help="Complete a manual flow using the final callback URL.")
    args = ap.parse_args()

    client_id = (os.environ.get("X_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("X_CLIENT_SECRET") or "").strip()
    if not client_id:
        print(json.dumps({"ok": False, "error": "X_CLIENT_ID missing"}, ensure_ascii=False))
        return 1

    if args.complete_url:
        if not os.path.isfile(PENDING_PATH):
            print(json.dumps({"ok": False, "error": "pending OAuth state not found"}, ensure_ascii=False), flush=True)
            return 1
        pending = json.load(open(PENDING_PATH, "r", encoding="utf-8"))
        parsed = urllib.parse.urlparse(args.complete_url)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not code:
            print(json.dumps({"ok": False, "error": "callback URL missing code"}, ensure_ascii=False), flush=True)
            return 1
        if state != pending.get("state"):
            print(json.dumps({"ok": False, "error": "state mismatch"}, ensure_ascii=False), flush=True)
            return 1
        try:
            token = exchange_code(
                code,
                pending["verifier"],
                pending["redirect_uri"],
                client_id,
                client_secret,
            )
            access_token = token.get("access_token") or ""
            refresh_token = token.get("refresh_token") or ""
            expires_in = int(token.get("expires_in") or 7200)
            user = me(access_token) if access_token else {}
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False), flush=True)
            return 1

        tokens = load_tokens()
        acct = pending.get("account") or args.account.lower()
        tokens.setdefault("x", {})[acct] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": str(((user.get("data") or {}).get("id")) or ""),
            "username": str(((user.get("data") or {}).get("username")) or ""),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in)),
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_tokens(tokens)
        try:
            os.unlink(PENDING_PATH)
        except Exception:
            pass
        print(json.dumps({
            "ok": True,
            "account": acct,
            "user_id": tokens["x"][acct]["user_id"],
            "username": tokens["x"][acct]["username"],
            "expires_in_min": expires_in // 60,
            "saved": TOKENS_PATH,
        }, ensure_ascii=False), flush=True)
        return 0

    redirect_uri = f"http://{args.host}:{args.port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    scopes = "tweet.read tweet.write users.read media.write offline.access"
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    url = f"{AUTH_URL}?{params}"

    if args.manual:
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "account": args.account.lower(),
                "verifier": verifier,
                "state": state,
                "redirect_uri": redirect_uri,
                "created_at": time.time(),
            }, f)
        print("Open this URL and authorize the X account:", flush=True)
        print(url, flush=True)
        print("\nAfter approval, copy the full callback URL and run:", flush=True)
        print(
            f".venv/bin/python scripts/bootstrap_x_oauth.py --account {args.account.lower()} "
            "--complete-url '<PASTE_CALLBACK_URL>'",
            flush=True,
        )
        return 0

    CallbackHandler.result = {}
    CallbackHandler.expected_state = state
    server = http.server.HTTPServer((args.host, args.port), CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this URL and authorize the X account:", flush=True)
    print(url, flush=True)
    if args.open_browser:
        webbrowser.open(url)

    deadline = time.time() + 300
    while time.time() < deadline and not CallbackHandler.result:
        time.sleep(0.2)
    server.server_close()

    result = CallbackHandler.result
    if not result:
        print(json.dumps({"ok": False, "error": "timeout waiting for callback"}, ensure_ascii=False), flush=True)
        return 1
    if result.get("error"):
        print(json.dumps({"ok": False, "error": result["error"]}, ensure_ascii=False), flush=True)
        return 1

    try:
        token = exchange_code(result["code"], verifier, redirect_uri, client_id, client_secret)
        access_token = token.get("access_token") or ""
        refresh_token = token.get("refresh_token") or ""
        expires_in = int(token.get("expires_in") or 7200)
        user = me(access_token) if access_token else {}
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:500]}, ensure_ascii=False), flush=True)
        return 1

    tokens = load_tokens()
    tokens.setdefault("x", {})[args.account.lower()] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": str(((user.get("data") or {}).get("id")) or ""),
        "username": str(((user.get("data") or {}).get("username")) or ""),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in)),
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_tokens(tokens)
    print(json.dumps({
        "ok": True,
        "account": args.account.lower(),
        "user_id": tokens["x"][args.account.lower()]["user_id"],
        "username": tokens["x"][args.account.lower()]["username"],
        "expires_in_min": expires_in // 60,
        "saved": TOKENS_PATH,
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
