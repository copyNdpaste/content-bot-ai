#!/usr/bin/env python3
"""Upload a local image to Cloudinary and print the public HTTPS URL.

Supports either:
  - unsigned upload: CLOUDINARY_CLOUD_NAME + CLOUDINARY_UPLOAD_PRESET
  - signed upload: CLOUDINARY_CLOUD_NAME + CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
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
            value = value.strip()
            if value and not (value.startswith('"') or value.startswith("'")):
                hash_idx = value.find("#")
                if hash_idx >= 0:
                    value = value[:hash_idx].rstrip()
            value = value.strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _multipart(fields: dict, file_field: str, file_path: str) -> tuple[bytes, str]:
    boundary = f"----contentbot{int(time.time() * 1000)}{random.randint(0, 9999):04d}"
    body = []
    for key, value in fields.items():
        body.append(f"--{boundary}\r\n".encode())
        body.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.append(str(value).encode())
        body.append(b"\r\n")

    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        blob = f.read()
    body.append(f"--{boundary}\r\n".encode())
    body.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        .encode()
    )
    body.append(b"Content-Type: image/png\r\n\r\n")
    body.append(blob)
    body.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(body), boundary


def _signature(params: dict, api_secret: str) -> str:
    pairs = []
    for key in sorted(params):
        value = params[key]
        if value in (None, ""):
            continue
        pairs.append(f"{key}={value}")
    raw = "&".join(pairs) + api_secret
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="local image path")
    ap.add_argument("--folder", default=os.environ.get("CLOUDINARY_FOLDER", "content-bot-ai"))
    args = ap.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_env_file(os.path.join(repo_root, ".env"))

    if not os.path.isfile(args.file):
        sys.stderr.write(f"file not found: {args.file}\n")
        return 2

    cloud_name = (os.environ.get("CLOUDINARY_CLOUD_NAME") or "").strip()
    upload_preset = (os.environ.get("CLOUDINARY_UPLOAD_PRESET") or "").strip()
    api_key = (os.environ.get("CLOUDINARY_API_KEY") or "").strip()
    api_secret = (os.environ.get("CLOUDINARY_API_SECRET") or "").strip()
    if not cloud_name:
        sys.stderr.write("CLOUDINARY_CLOUD_NAME missing\n")
        return 2

    fields = {"folder": args.folder}
    if api_key and api_secret:
        fields["api_key"] = api_key
        fields["timestamp"] = int(time.time())
        fields["signature"] = _signature(
            {"folder": fields["folder"], "timestamp": fields["timestamp"]},
            api_secret,
        )
    elif upload_preset:
        fields["upload_preset"] = upload_preset
    else:
        sys.stderr.write(
            "Set CLOUDINARY_UPLOAD_PRESET or CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET\n"
        )
        return 2

    body, boundary = _multipart(fields, "file", args.file)
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"Cloudinary HTTP {e.code}: {err[:500]}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"Cloudinary upload failed: {e}\n")
        return 1

    try:
        parsed = json.loads(resp)
    except json.JSONDecodeError:
        sys.stderr.write(f"Cloudinary response parse failed: {resp[:300]}\n")
        return 1

    public_url = parsed.get("secure_url") or parsed.get("url") or ""
    if not public_url.startswith("https://"):
        sys.stderr.write(f"Cloudinary response missing secure_url: {resp[:300]}\n")
        return 1

    print(public_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
