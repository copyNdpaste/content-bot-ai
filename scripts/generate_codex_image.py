#!/usr/bin/env python3
"""Generate a social image without OPENAI_API_KEY.

The content pipeline passes a dynamic prompt derived from the finished post.
This script asks local Codex CLI for a compact visual direction JSON, then
renders a polished PNG locally with Pillow.
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter


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


def _hex_to_rgb(value: str, fallback=(240, 232, 218)):
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _clamp(v: int) -> int:
    return max(0, min(255, int(v)))


def _mix(a, b, t: float):
    return tuple(_clamp(a[i] * (1 - t) + b[i] * t) for i in range(3))


def _call_codex_direction(prompt: str, repo_root: str) -> dict:
    codex_bin = (os.environ.get("CODEX_BIN") or "/Users/hoony/.local/bin/codex").strip()
    if not os.path.isfile(codex_bin):
        codex_bin = "codex"

    timeout = int(os.environ.get("CODEX_IMAGE_PLANNER_TIMEOUT_SEC") or "90")
    model = (os.environ.get("CODEX_IMAGE_PLANNER_MODEL") or "").strip()
    instructions = f"""
Return only one valid JSON object. No markdown. No explanation.
You are planning a visual for a social post image.
The renderer is local and stylized, so provide a simple art direction, not code.

Required JSON keys:
- scene: short English scene name
- mood: one of cozy, rainy, night, travel, cafe, street, gallery, minimal
- palette: array of 4 hex colors
- elements: array of 4 to 7 nouns to visually include, no text/logos

Rules:
- Match the prompt.
- No text in the image, no watermark, no logo.
- If people are present, faces must be hidden by phone/menu/camera/back view/silhouette.

Prompt:
{prompt}
"""
    fd, output_path = tempfile.mkstemp(prefix="contentbot-image-plan-", suffix=".txt")
    os.close(fd)
    cmd = [
        codex_bin,
        "exec",
        "--sandbox", "read-only",
        "-C", repo_root,
        "--output-last-message", output_path,
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(instructions)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return {}

    if proc.returncode != 0:
        return {}

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        raw = proc.stdout or ""
    finally:
        try:
            os.unlink(output_path)
        except Exception:
            pass

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {}
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _fallback_direction(prompt: str) -> dict:
    lower = prompt.lower()
    if "rain" in lower or "비" in lower:
        mood = "rainy"
        palette = ["#d8dee6", "#f2ede3", "#41556b", "#b98f76"]
    elif "night" in lower or "tokyo" in lower or "도쿄" in lower:
        mood = "night"
        palette = ["#202733", "#f4d58d", "#8fb3ff", "#f2efe8"]
    elif "gallery" in lower or "전시" in lower:
        mood = "gallery"
        palette = ["#ede7dc", "#263238", "#d6bfa7", "#9bb7aa"]
    else:
        mood = "cafe"
        palette = ["#f5efe4", "#263238", "#d9eadf", "#f3c6a8"]
    return {
        "scene": "candid social cafe moment",
        "mood": mood,
        "palette": palette,
        "elements": ["cafe table", "coffee", "phone", "menu", "window", "two friends"],
    }


def _gradient_background(w, h, top, bottom):
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        c = _mix(top, bottom, t)
        for x in range(w):
            px[x, y] = c
    return img


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _draw_window(draw, w, h, palette, rng):
    x0, y0 = int(w * 0.10), int(h * 0.10)
    x1, y1 = int(w * 0.90), int(h * 0.43)
    _rounded(draw, (x0, y0, x1, y1), 34, _mix(palette[0], (255, 255, 255), 0.45),
             outline=palette[1], width=4)
    for i in range(4):
        x = x0 + int((x1 - x0) * (i + 1) / 5)
        draw.line((x, y0 + 20, x, y1 - 20), fill=_mix(palette[1], palette[0], 0.55), width=2)
    for _ in range(40):
        x = rng.randint(x0 + 20, x1 - 20)
        y = rng.randint(y0 + 20, y1 - 20)
        draw.line((x, y, x - 8, y + 18), fill=_mix(palette[1], palette[0], 0.6), width=1)


def _draw_people(draw, w, h, palette):
    y = int(h * 0.50)
    for cx, accent in [(int(w * 0.34), palette[2]), (int(w * 0.66), palette[3])]:
        draw.ellipse((cx - 58, y - 84, cx + 58, y + 32), fill=accent, outline=palette[1], width=4)
        _rounded(draw, (cx - 76, y + 10, cx + 76, y + 220), 38, _mix(accent, palette[0], 0.35),
                 outline=palette[1], width=4)
        # Phone/menu blocks the face.
        _rounded(draw, (cx - 48, y - 56, cx + 48, y + 42), 16, _mix(palette[1], (255, 255, 255), 0.18),
                 outline=palette[1], width=3)


def _draw_table(draw, w, h, palette):
    y = int(h * 0.72)
    _rounded(draw, (int(w * 0.12), y, int(w * 0.88), int(h * 0.91)), 42,
             _mix(palette[3], (255, 255, 255), 0.35), outline=palette[1], width=4)
    for cx in [int(w * 0.36), int(w * 0.63)]:
        draw.ellipse((cx - 46, y + 36, cx + 46, y + 126), fill="#f8f4ed", outline=palette[1], width=3)
        draw.ellipse((cx - 30, y + 48, cx + 30, y + 108), fill=_mix(palette[1], palette[3], 0.45))
    _rounded(draw, (int(w * 0.46), y + 42, int(w * 0.55), y + 132), 14,
             _mix(palette[1], (255, 255, 255), 0.3), outline=palette[1], width=3)
    _rounded(draw, (int(w * 0.22), y + 118, int(w * 0.78), y + 148), 12,
             _mix(palette[0], (255, 255, 255), 0.35), outline=palette[1], width=2)


def _render(prompt: str, direction: dict, output: str, width: int, height: int):
    seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    fallback = _fallback_direction(prompt)
    palette_values = direction.get("palette") or fallback["palette"]
    palette = [_hex_to_rgb(c) for c in palette_values[:4]]
    while len(palette) < 4:
        palette.append(_hex_to_rgb(fallback["palette"][len(palette)]))

    img = _gradient_background(width, height, palette[0], _mix(palette[0], palette[2], 0.28))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for _ in range(26):
        r = rng.randint(18, 56)
        x = rng.randint(-r, width)
        y = rng.randint(-r, height)
        color = (*_mix(palette[2], palette[3], rng.random()), rng.randint(18, 42))
        draw.ellipse((x, y, x + r, y + r), fill=color)

    _draw_window(draw, width, height, palette, rng)
    _draw_people(draw, width, height, palette)
    _draw_table(draw, width, height, palette)

    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=115, threshold=3))
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    img.convert("RGB").save(output, quality=95)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    args = ap.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_env_file(os.path.join(repo_root, ".env"))

    direction = _call_codex_direction(args.prompt, repo_root) or _fallback_direction(args.prompt)
    _render(args.prompt, direction, args.output, args.width, args.height)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
