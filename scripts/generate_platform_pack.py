#!/usr/bin/env python3
"""Generate one round-level image and platform-specific drafts.

For each scheduler round:
  1. Prepare platform-specific text drafts for every requested account.
  2. Generate exactly one image from the round's combined concept/text.
  3. Write every draft with the same round image URL.
  4. Send Slack review cards; upload only after Slack approval.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.workflow import content_pipeline as p  # noqa: E402
from src.domain import content_rules, publication_targets  # noqa: E402

ROUND_VISUAL_VARIATIONS = [
    "sunny Tokyo cafe terrace, two stylish Korean/Japanese friends laughing over drinks",
    "bright Seoul street corner, two stylish Korean/Japanese friends checking a phone map",
    "clean daytime train-station meet-up, two stylish Korean/Japanese friends with travel bags",
    "sunny dessert cafe table, two stylish Korean/Japanese friends arranging cake and coffee",
    "bright shopping street snapshot, two stylish Korean/Japanese friends mid-conversation",
]


def _disabled_targets() -> set[tuple[str, str]]:
    return publication_targets.disabled_target_set(os.environ.get("ROUTINE_DISABLED_TARGETS", ""))


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


def _cta_for(platform: str, account: str, lang: str) -> str:
    return content_rules.required_landing_cta(account, lang, platform)


def _fit_platform_limit(text: str, platform: str, account: str, lang: str) -> str:
    return content_rules.fit_platform_limit(text, platform, account, lang)


def _adapt_for_platform(base: dict, platform: str, account: str, lang: str,
                        style_context: dict) -> dict:
    if platform == "instagram":
        out = dict(base)
        out["raw"] = base.get("raw", "")
        return out

    limit = p.PLATFORM_LIMITS.get(platform, 500)
    cta = _cta_for(platform, account, lang)
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
    p._ensure_required_landing_cta(out, account, lang, platform)
    out["text"] = _fit_platform_limit(out["text"], platform, account, lang)
    return out


def _prepare_account_payloads(account: str, theme: str, platforms: list[str]) -> dict:
    lang = p.ACCOUNT_LANG_DEFAULT.get(account.lower(), "ko")
    disabled = _disabled_targets()
    platforms = publication_targets.filter_disabled_targets(platforms, account, disabled)
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
    p._ensure_required_landing_cta(base, account, lang, "instagram")

    payloads = {}
    for platform in platforms:
        if platform == "instagram":
            payload = dict(base)
            payload["raw"] = base.get("raw", "")
        else:
            payload = _adapt_for_platform(base, platform, account, lang, style_context)
        p._attach_style_meta(payload, style_context)
        p._ensure_required_landing_cta(payload, account, lang, platform)
        payload["text"] = _fit_platform_limit(payload.get("text", ""), platform, account, lang)
        payloads[platform] = payload

    return {
        "ok": True,
        "account": account,
        "lang": lang,
        "platforms": platforms,
        "prompt": prompt,
        "style_context": style_context,
        "payloads": payloads,
        "trends_fetched": len(trends),
    }


def _concept_id(style_context: dict, payload: dict) -> str:
    concept = style_context.get("concept") or {}
    return concept.get("id") or concept.get("format_type") or payload.get("concept_id") or ""


def _round_image_source_text(prepared_accounts: list[dict], theme: str,
                             visual_variation: str) -> str:
    parts = []
    if theme:
        parts.append(f"Round theme: {theme}")
    parts.append(f"Round visual variation: {visual_variation}")

    for prepared in prepared_accounts:
        account = prepared.get("account", "")
        style_context = prepared.get("style_context") or {}
        payloads = prepared.get("payloads") or {}
        for platform in prepared.get("platforms") or []:
            payload = payloads.get(platform) or {}
            concept_id = _concept_id(style_context, payload)
            parts.append(
                "\n".join([
                    f"Draft: {account}/{platform}",
                    f"Concept id: {concept_id}",
                    f"Hook: {payload.get('hook', '')}",
                    "Post:",
                    payload.get("text", ""),
                ])
            )
    return "\n\n---\n\n".join(part for part in parts if part)


def _first_payload(prepared_accounts: list[dict]) -> tuple[dict, dict]:
    for prepared in prepared_accounts:
        payloads = prepared.get("payloads") or {}
        for platform in prepared.get("platforms") or []:
            payload = payloads.get(platform)
            if payload:
                return prepared, payload
    return {}, {}


def _generate_round_media(prepared_accounts: list[dict], theme: str) -> dict:
    prepared, payload = _first_payload(prepared_accounts)
    style_context = prepared.get("style_context") or {}
    visual_variation = random.choice(ROUND_VISUAL_VARIATIONS)
    img_prompt = p._call_codex_image_prompt(
        "instagram",
        "ko/ja",
        _round_image_source_text(prepared_accounts, theme, visual_variation),
        payload.get("hook", ""),
        payload.get("hashtags") or [],
        style_context,
    )
    if not img_prompt.get("ok"):
        return {"ok": False, "error": img_prompt.get("error", "image prompt failed")}

    image_keyword = img_prompt["image_keyword"]
    local_path = p._generate_codex_image(image_keyword)
    if not local_path:
        return {"ok": False, "error": "image generation failed"}

    image_url = p._publish_image_url(local_path)
    if not image_url:
        return {"ok": False, "error": "image URL publish failed"}

    return {
        "ok": True,
        "image_keyword": image_keyword,
        "image_prompt_raw": img_prompt.get("raw", ""),
        "image_local_path": local_path,
        "image_url": image_url,
        "round_visual_variation": visual_variation,
    }


def _write_account_pack(prepared: dict, theme: str, media: dict) -> dict:
    if prepared.get("skipped"):
        return prepared
    if not prepared.get("ok"):
        return prepared

    account = prepared["account"]
    lang = prepared["lang"]
    prompt = prepared["prompt"]
    results = []
    for platform in prepared["platforms"]:
        payload = dict(prepared["payloads"][platform])
        payload.update({
            "image_keyword": media.get("image_keyword", ""),
            "image_prompt_raw": media.get("image_prompt_raw", ""),
            "image_local_path": media.get("image_local_path", ""),
            "image_url": media.get("image_url", ""),
            "round_visual_variation": media.get("round_visual_variation", ""),
        })

        draft_path = p._write_draft(platform, account, lang, theme, payload)
        artifact_id = p._insert_generation_artifact(
            platform, account, lang, theme, draft_path, payload, prompt
        )
        # Instagram/Threads must wait for Slack approval; X remains manual.
        slack_mode = "manual" if platform == "x" else "approval"
        slack = p._notify_slack(draft_path, platform, account, mode=slack_mode)
        if isinstance(slack, dict):
            slack_result = slack.get("result") if isinstance(slack.get("result"), dict) else {}
            p._update_generation_artifact(draft_path, {
                "slack_channel": slack_result.get("channel"),
                "slack_ts": slack_result.get("ts"),
                "slack_upload_ok": True,
            })
        auto_upload = {"skipped": "requires_slack_approval"}
        results.append({
            "platform": platform,
            "draft_path": draft_path,
            "artifact_id": artifact_id,
            "image_url": payload.get("image_url", ""),
            "image_local_path": payload.get("image_local_path", ""),
            "slack": slack,
            "auto_upload": auto_upload,
        })

    return {
        "ok": True,
        "account": account,
        "image_url": media.get("image_url", ""),
        "image_local_path": media.get("image_local_path", ""),
        "round_visual_variation": media.get("round_visual_variation", ""),
        "items": results,
        "drafts_created": len(results),
        "slack_notified": sum(
            1 for item in results
            if isinstance(item.get("slack"), dict) and item["slack"].get("ok")
        ),
    }


def generate_round_pack(accounts: list[str], theme: str, platforms: list[str]) -> dict:
    prepared = [_prepare_account_payloads(account, theme, platforms) for account in accounts]
    active = [item for item in prepared if item.get("ok") and not item.get("skipped")]
    failures = [item for item in prepared if not item.get("ok")]

    media = {}
    if active:
        media = _generate_round_media(active, theme)
        if not media.get("ok"):
            return {
                "status": "partial",
                "accounts": len(accounts),
                "images_created": 0,
                "drafts_created": 0,
                "slack_notified": 0,
                "error": media.get("error", "image generation failed"),
                "results": prepared,
            }

    results = [
        _write_account_pack(item, theme, media) if item.get("ok") and not item.get("skipped") else item
        for item in prepared
    ]
    status = "completed" if not failures and all(item.get("ok") for item in results) else "partial"
    return {
        "status": status,
        "accounts": len(accounts),
        "images_created": 1 if media.get("image_url") else 0,
        "round_image_url": media.get("image_url", ""),
        "round_image_local_path": media.get("image_local_path", ""),
        "round_visual_variation": media.get("round_visual_variation", ""),
        "drafts_created": sum(int(item.get("drafts_created") or 0) for item in results),
        "slack_notified": sum(int(item.get("slack_notified") or 0) for item in results),
        "results": results,
    }


def generate_account_pack(account: str, theme: str, platforms: list[str]) -> dict:
    """Backward-compatible single-account entrypoint."""
    summary = generate_round_pack([account], theme, platforms)
    result = summary.get("results", [{}])[0]
    if result.get("ok"):
        result = dict(result)
        result["images_created"] = summary.get("images_created", 0)
    return result


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
    summary = generate_round_pack(accounts, args.theme, platforms)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
