#!/usr/bin/env python3
"""Generate an image through local Codex GPT-5.5 image generation.

This intentionally does not use OPENAI_API_KEY or the OpenAI Images API.
It shells out to local `codex exec --model gpt-5.5`, asks Codex to use the
built-in image generation tool, and saves the PNG returned by Codex's JSON
image generation event.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time


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


def _is_png(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _generated_images_dir() -> str:
    codex_home = (os.environ.get("CODEX_HOME") or "").strip()
    if not codex_home:
        codex_home = os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(codex_home, "generated_images")


def _codex_sessions_dir() -> str:
    codex_home = (os.environ.get("CODEX_HOME") or "").strip()
    if not codex_home:
        codex_home = os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(codex_home, "sessions")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _png_files(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower().endswith(".png"):
                matches.append(os.path.join(dirpath, filename))
    return matches


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_generated_images(root: str) -> dict[str, tuple[int, int, str]]:
    snapshot = {}
    for path in _png_files(root):
        try:
            stat = os.stat(path)
            snapshot[path] = (stat.st_mtime_ns, stat.st_size, _sha256(path))
        except OSError:
            continue
    return snapshot


def _fresh_generated_png(
    root: str,
    before: dict[str, tuple[int, int, str]],
    started_ns: int,
) -> tuple[str, str]:
    previous_hashes = {item[2] for item in before.values()}
    candidates = []
    for path in _png_files(root):
        try:
            stat = os.stat(path)
            digest = _sha256(path)
        except OSError:
            continue
        prior = before.get(path)
        changed = prior is None or prior != (stat.st_mtime_ns, stat.st_size, digest)
        if not changed:
            continue
        if stat.st_mtime_ns < started_ns - 2_000_000_000:
            continue
        if digest in previous_hashes:
            continue
        candidates.append((stat.st_mtime_ns, path, digest))
    if not candidates:
        return "", ""
    _mtime, path, digest = sorted(candidates)[-1]
    return path, digest


def _jsonl_files(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    matches = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower().endswith(".jsonl"):
                matches.append(os.path.join(dirpath, filename))
    return matches


def _snapshot_files(root: str) -> dict[str, tuple[int, int]]:
    snapshot = {}
    for path in _jsonl_files(root):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        snapshot[path] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _changed_jsonl_files(
    root: str,
    before: dict[str, tuple[int, int]],
    started_ns: int,
) -> list[str]:
    candidates = []
    for path in _jsonl_files(root):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_mtime_ns < started_ns - 2_000_000_000:
            continue
        prior = before.get(path)
        if prior is not None and prior == (stat.st_mtime_ns, stat.st_size):
            continue
        candidates.append((stat.st_mtime_ns, path))
    return [path for _mtime, path in sorted(candidates, reverse=True)]


def _json_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _find_image_generation_result(value: object) -> str:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            event_type = item.get("type")
            result = item.get("result")
            if (
                event_type == "image_generation_end"
                and isinstance(result, str)
                and result.strip()
            ):
                return result
            if (
                isinstance(event_type, str)
                and "image" in event_type
                and isinstance(result, str)
                and result.strip()
            ):
                return result
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def _decode_png_result(result: str) -> bytes:
    raw = result.strip()
    if "," in raw and raw.lower().startswith("data:image/"):
        raw = raw.split(",", 1)[1]
    raw = "".join(raw.split())
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return b""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b""
    return data


def _write_png_data(
    data: bytes,
    output: str,
    previous_hashes: set[str] | None = None,
) -> str:
    digest = _sha256_bytes(data)
    if previous_hashes and digest in previous_hashes:
        return ""
    with open(output, "wb") as f:
        f.write(data)
    return digest


def _write_image_from_codex_json(
    stdout: str,
    output: str,
    previous_hashes: set[str] | None = None,
) -> str:
    for event in _json_events(stdout):
        result = _find_image_generation_result(event)
        if not result:
            continue
        data = _decode_png_result(result)
        if not data:
            continue
        digest = _write_png_data(data, output, previous_hashes)
        if digest:
            return digest
    return ""


def _write_image_from_codex_sessions(
    sessions_root: str,
    before: dict[str, tuple[int, int]],
    started_ns: int,
    output: str,
    previous_hashes: set[str] | None = None,
) -> str:
    for path in _changed_jsonl_files(sessions_root, before, started_ns):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    result = _find_image_generation_result(event)
                    if not result:
                        continue
                    data = _decode_png_result(result)
                    if not data:
                        continue
                    digest = _write_png_data(data, output, previous_hashes)
                    if digest:
                        return digest
        except OSError:
            continue
    return ""


def _instructions(prompt: str, output: str, width: int, height: int) -> str:
    return f"""
Use the imagegen skill and the built-in image generation tool. Do not use
OpenAI API scripts, Pillow, SVG, HTML, canvas, or local drawing.

Generate exactly one high-quality photorealistic social media image from this
production prompt:

{prompt}

Requirements:
- Generate a brand-new image now. Do not reuse, copy, or reference any existing
  image file under $CODEX_HOME/generated_images or /tmp.
- Do not copy any image into this output path yourself:
  {output}
  The parent script will save the generated PNG from Codex's image generation
  event.
- Target aspect: {width}x{height}; if the built-in tool chooses a nearby
  supported size, keep a social-media-ready crop.
- Fully photorealistic, natural lifestyle/editorial image.
- No text, no logos, no watermark, no cartoon, no anime, no illustration.
- Do not edit source code or project configuration.

Final response must be exactly one line:
NEW IMAGE GENERATED
""".strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    args = ap.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_env_file(os.path.join(repo_root, ".env"))

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    try:
        if os.path.exists(output):
            os.unlink(output)
    except OSError as e:
        sys.stderr.write(f"failed to remove stale output: {e}\n")
        return 1

    codex_bin = (
        os.environ.get("LOCAL_GPT_IMAGE_CODEX_BIN")
        or os.environ.get("CODEX_BIN")
        or "codex"
    ).strip()
    model = (os.environ.get("LOCAL_GPT_IMAGE_MODEL") or "gpt-5.5").strip()
    timeout = int(os.environ.get("LOCAL_GPT_IMAGE_TIMEOUT_SEC") or "3300")
    generated_root = _generated_images_dir()
    before = _snapshot_generated_images(generated_root)
    previous_hashes = {item[2] for item in before.values()}
    sessions_root = _codex_sessions_dir()
    sessions_before = _snapshot_files(sessions_root)
    started_ns = time.time_ns()

    stamp = f"{int(time.time())}"
    job_dir = os.path.join(repo_root, "var", "gpt55-image-jobs")
    os.makedirs(job_dir, exist_ok=True)
    last_message = os.path.join(job_dir, f"last-message-{stamp}.txt")

    cmd = [
        codex_bin,
        "exec",
        "--json",
        "-C",
        repo_root,
        "--model",
        model,
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        last_message,
        _instructions(args.prompt, output, args.width, args.height),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_root,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"local GPT image generation timed out after {timeout}s\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"local GPT image generation failed to start: {e}\n")
        return 1

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        sys.stderr.write(f"local GPT image generation failed: {detail[-2000:]}\n")
        return 1

    output_hash = _write_image_from_codex_json(proc.stdout, output, previous_hashes)
    if not output_hash:
        output_hash = _write_image_from_codex_sessions(
            sessions_root,
            sessions_before,
            started_ns,
            output,
            previous_hashes,
        )
    if output_hash:
        if not _is_png(output):
            sys.stderr.write(f"local GPT image generation wrote an invalid PNG at {output}\n")
            return 1
        print(output)
        return 0

    fresh_path, fresh_hash = _fresh_generated_png(generated_root, before, started_ns)
    if not fresh_path:
        detail = ""
        try:
            with open(last_message, "r", encoding="utf-8") as f:
                detail = f.read().strip()
        except OSError:
            pass
        try:
            if os.path.exists(output):
                os.unlink(output)
        except OSError:
            pass
        sys.stderr.write(
            "local GPT image generation did not return a PNG in Codex JSON output "
            f"and did not create a fresh PNG under {generated_root}; "
            "refusing to reuse stale image. "
            f"Last message: {detail[-500:]}\n"
        )
        return 1

    shutil.copyfile(fresh_path, output)

    if not _is_png(output):
        detail = ""
        try:
            with open(last_message, "r", encoding="utf-8") as f:
                detail = f.read().strip()
        except OSError:
            pass
        sys.stderr.write(
            "local GPT image generation did not produce a valid PNG at "
            f"{output}\n{detail[-1000:]}\n"
        )
        return 1

    if _sha256(output) != fresh_hash:
        sys.stderr.write("copied PNG hash mismatch\n")
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
