#!/usr/bin/env python3
"""Generate one account-level image and platform-specific drafts.

For each account:
  1. Write platform-specific text drafts for instagram/threads/x.
  2. Generate exactly one shared image from the combined platform text.
  3. Notify Slack with every text/image.
  4. Auto-upload Instagram/Threads, keep X manual.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.workflow import content_pipeline as p  # noqa: E402
from src.domain import content_rules  # noqa: E402


def _disabled_targets() -> set[tuple[str, str]]:
    raw = (os.environ.get("ROUTINE_DISABLED_TARGETS") or "").strip()
    out = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item or ":" not in item:
            continue
        platform, account = [x.strip() for x in item.split(":", 1)]
        if platform and account:
            out.add((platform, account))
    return out


def _parse_json_content(content: str) -> dict:
    parsed = p._parse_content_json(content)
    if parsed.get("ok"):
        return parsed
    return {
        "ok": True,
        "text": (content or "").strip(),
        "hook": (content or "").strip().split("\n", 1)[0][:120],
        "hashtags": [],
        "image_keyword": "",
        "raw": content,
    }


def _cta_for(account: str, lang: str) -> str:
    return content_rules.required_landing_cta(account, lang)


def _fit_platform_limit(text: str, platform: str, account: str, lang: str) -> str:
    return content_rules.fit_platform_limit(text, platform, account, lang)


def _adapt_for_platform(base: dict, platform: str, account: str, lang: str,
                        style_context: dict) -> dict:
    if platform == "instagram":
        out = dict(base)
        out["raw"] = base.get("raw", "")
        return out

    limit = p.PLATFORM_LIMITS.get(platform, 500)
    cta = _cta_for(account, lang)
    style_block = p._build_style_context_block(style_context, lang, platform)
    instructions = f"""
Return only one valid JSON object. No markdown. No explanation.
Required keys: text, hook, hashtags.

Adapt the base Instagram post below for {platform.upper()}.
Keep the same topic, same scene, same emotion, and same image relevance.
Write naturally for the platform, not like an ad.
The last line is mandatory and must be exactly:
{cta}

Character limit including CTA: {limit}
Platform rules:
- x: one sharp short observation or mini story, no hashtags unless essential.
- threads: 3-6 short lines, conversational, slightly open-ended.

{style_block}
Base post:
{base.get("text", "")}
"""
    res = p._run_codex_json(instructions)
    if not res.get("ok"):
        return {
            "ok": True,
            "text": _fit_platform_limit(base.get("text", ""), platform, account, lang),
            "hook": base.get("hook", ""),
            "hashtags": [],
            "raw": "",
        }
    out = _parse_json_content(res.get("content", ""))
    p._ensure_required_landing_cta(out, account, lang)
    out["text"] = _fit_platform_limit(out["text"], platform, account, lang)
    return out


def generate_account_pack(account: str, theme: str, platforms: list[str]) -> dict:
    lang = p.ACCOUNT_LANG_DEFAULT.get(account.lower(), "ko")
    disabled = _disabled_targets()
    platforms = [
        platform for platform in platforms
        if (platform.lower(), account.lower()) not in disabled
    ]
    if not platforms:
        return {"ok": True, "account": account, "skipped": "all targets disabled", "items": []}

    trends = p._fetch_trends(lang)
    style_context = p._load_style_context("instagram", account, lang)
    prompt = p._build_persona_prompt("instagram", account, lang, theme, trends, style_context)

    base = p._call_codex_content(prompt)
    if not base.get("ok") and os.environ.get("CONTENT_LLM_FALLBACK", "claude") == "claude":
        base = p._call_claude(prompt)
    if not base.get("ok"):
        return {"ok": False, "account": account, "error": base.get("error", "content failed")}

    p._attach_style_meta(base, style_context)
    p._ensure_required_landing_cta(base, account, lang)

    adapted_payloads = {}
    combined_text_parts = [f"instagram:\n{base.get('text', '')}"]
    for platform in platforms:
        if platform == "instagram":
            payload = dict(base)
            payload["raw"] = base.get("raw", "")
        else:
            payload = _adapt_for_platform(base, platform, account, lang, style_context)
        p._attach_style_meta(payload, style_context)
        p._ensure_required_landing_cta(payload, account, lang)
        payload["text"] = _fit_platform_limit(payload.get("text", ""), platform, account, lang)
        adapted_payloads[platform] = payload
        if platform != "instagram":
            combined_text_parts.append(f"{platform}:\n{payload.get('text', '')}")

    combined_for_image = "\n\n---\n\n".join(combined_text_parts)
    img_prompt = p._call_codex_image_prompt(
        "instagram",
        lang,
        combined_for_image,
        base.get("hook", ""),
        base.get("hashtags") or [],
        style_context,
    )
    if img_prompt.get("ok"):
        base["image_keyword"] = img_prompt["image_keyword"]
        base["image_prompt_raw"] = img_prompt.get("raw", "")
    else:
        return {"ok": False, "account": account, "error": img_prompt.get("error", "image prompt failed")}

    local_path = p._generate_codex_image(base["image_keyword"])
    if not local_path:
        return {"ok": False, "account": account, "error": "image generation failed"}
    image_url = p._publish_image_url(local_path)
    if not image_url:
        return {"ok": False, "account": account, "error": "image URL publish failed"}

    shared_media = {
        "image_keyword": base.get("image_keyword", ""),
        "image_prompt_raw": base.get("image_prompt_raw", ""),
        "image_local_path": local_path,
        "image_url": image_url,
    }

    results = []
    for platform in platforms:
        payload = dict(adapted_payloads[platform])
        payload.update(shared_media)

        draft_path = p._write_draft(platform, account, lang, theme, payload)
        artifact_id = p._insert_generation_artifact(
            platform, account, lang, theme, draft_path, payload, prompt
        )
        slack_mode = "manual" if platform == "x" else "auto"
        slack = p._notify_slack(draft_path, platform, account, mode=slack_mode)
        if isinstance(slack, dict):
            slack_result = slack.get("result") if isinstance(slack.get("result"), dict) else {}
            p._update_generation_artifact(draft_path, {
                "slack_channel": slack_result.get("channel"),
                "slack_ts": slack_result.get("ts"),
                "slack_upload_ok": True,
            })
        auto_upload = {"skipped": "slack_not_failed"}
        if slack.get("ok"):
            auto_upload = p._auto_upload_after_slack(draft_path, platform, account)
        results.append({
            "platform": platform,
            "draft_path": draft_path,
            "artifact_id": artifact_id,
            "slack": slack,
            "auto_upload": auto_upload,
        })

    return {
        "ok": True,
        "account": account,
        "image_url": image_url,
        "image_local_path": local_path,
        "items": results,
        "drafts_created": len(results),
        "slack_notified": sum(
            1 for item in results
            if isinstance(item.get("slack"), dict) and item["slack"].get("ok")
        ),
    }


def main() -> int:
    p._load_env_file(p.ENV_PATH)
    p._load_env_file(p.ENV_PATH_LEGACY)
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="kr,jp")
    ap.add_argument("--platforms", default="instagram,threads,x")
    ap.add_argument("--theme", default="")
    args = ap.parse_args()

    accounts = [x.strip() for x in args.accounts.split(",") if x.strip()]
    platforms = [x.strip() for x in args.platforms.split(",") if x.strip()]
    summary = [generate_account_pack(a, args.theme, platforms) for a in accounts]
    status = "completed" if all(x.get("ok") for x in summary) else "partial"
    print(json.dumps({
        "status": status,
        "accounts": len(accounts),
        "images_created": sum(1 for x in summary if x.get("image_url")),
        "drafts_created": sum(int(x.get("drafts_created") or 0) for x in summary),
        "slack_notified": sum(int(x.get("slack_notified") or 0) for x in summary),
        "results": summary,
    }, ensure_ascii=False))
    return 0 if status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
