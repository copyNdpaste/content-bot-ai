#!/usr/bin/env python3
"""Generate a high-quality GPT image from a dynamic prompt and save it locally."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


IMAGE_API_URL = "https://api.openai.com/v1/images/generations"


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


def _normalize_size(width: int, height: int) -> str:
    if width == height:
        return "1024x1024"
    if width > height:
        return "1536x1024"
    return "1024x1536"


def _quality_prompt(prompt: str) -> str:
    return (
        "Use GPT image generation, not SVG illustration. "
        "Generate a high-quality AI image similar to ChatGPT image generation quality. "
        "Create a fully photorealistic Instagram lifestyle photo, never anime, manga, cartoon, "
        "illustration, vector art, webtoon, or 3D render. "
        "Use detailed lighting, real skin texture, rich textures, atmospheric depth, "
        "realistic material detail, and natural camera composition. "
        "When people fit the prompt, show fictional adult Korean or Japanese women who look "
        "late 20s to early 30s, exceptionally beautiful, stylish, eye-catching, and socially current. "
        "Avoid ordinary casual fashion. Use idol airport fashion, off-duty K-pop idol styling, "
        "fashion-model street editorial styling, statement outfits, luxury accessories, trendy hair, "
        "and polished glam details without resembling any real celebrity. "
        "Use idol/model-inspired styling without resembling any real celebrity: striking realistic faces, "
        "bright fair skin tone, clean natural complexion, natural glam makeup, contemporary hair, "
        "slim elegant proportions, and attention-grabbing candid Instagram composition. "
        "Prefer sunny clear daytime, bright outdoor natural light, fresh spring/summer atmosphere, "
        "clean white-balanced color, and airy Instagram editorial photography. "
        "Avoid rainy weather, cloudy weather, yellowish skin, muddy color grading, dull gray lighting, "
        "or orange indoor lighting. "
        "Avoid minors, teenage appearance, celebrity likeness, plastic-perfect faces, "
        "over-smoothed AI skin, ID-photo portraits, extreme close-up headshots, or direct model-stare poses. "
        "Do not generate flat vector graphics, icons, logos, text, watermark, infographic, or clip art. "
        f"{prompt}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    args = ap.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_env_file(os.path.join(repo_root, ".env"))

    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        sys.stderr.write("OPENAI_API_KEY missing\n")
        return 2

    model = (os.environ.get("OPENAI_IMAGE_MODEL") or "gpt-image-1").strip()
    quality = (os.environ.get("OPENAI_IMAGE_QUALITY") or "high").strip()
    output_format = (os.environ.get("OPENAI_IMAGE_OUTPUT_FORMAT") or "png").strip()
    size = (os.environ.get("OPENAI_IMAGE_SIZE") or _normalize_size(args.width, args.height)).strip()

    payload = {
        "model": model,
        "prompt": _quality_prompt(args.prompt),
        "size": size,
        "quality": quality,
        "output_format": output_format,
        "n": 1,
    }
    req = urllib.request.Request(
        IMAGE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"OpenAI image HTTP {e.code}: {err[:800]}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"OpenAI image request failed: {e}\n")
        return 1

    try:
        parsed = json.loads(body)
        data = parsed.get("data") or []
        first = data[0] if data else {}
        b64 = first.get("b64_json") or ""
    except Exception as e:
        sys.stderr.write(f"OpenAI image response parse failed: {e}\n")
        return 1

    if not b64:
        sys.stderr.write(f"OpenAI image response missing b64_json: {body[:500]}\n")
        return 1

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "wb") as f:
        f.write(base64.b64decode(b64))

    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
