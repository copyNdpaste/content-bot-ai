"""Pure publication rules shared by workflow scripts and tests."""
from __future__ import annotations

import re

BRAND_NAME = "OnlyFriends"
LANDING_URL = "https://onlyfriends.tryproo.com/"
INSTAGRAM_APP_STORE_CTA_KO = "👉 앱스토어에서 OnlyFriends 검색하고 일본 친구 만들기"
INSTAGRAM_APP_STORE_CTA_JA = "👉 App StoreでOnlyFriendsを検索して、韓国の友達作り"

PLATFORM_LIMITS = {
    "x": 280,
    "threads": 500,
    "instagram": 2200,
}


def required_landing_cta(account: str, lang: str, platform: str = "") -> str:
    """Return the mandatory account/language-specific landing CTA."""
    if (platform or "").lower() == "instagram":
        if (account or "").lower() == "jp" or lang == "ja":
            return INSTAGRAM_APP_STORE_CTA_JA
        return INSTAGRAM_APP_STORE_CTA_KO
    if (account or "").lower() == "jp" or lang == "ja":
        return f"👉 韓国の友達を本当に作ってみたいなら → {LANDING_URL}"
    return f"👉 일본 친구 진짜 만들어보고 싶으면 → {LANDING_URL}"


def ensure_required_landing_cta(
    text: str,
    account: str,
    lang: str,
    platform: str = "",
) -> str:
    """Normalize generated text so it ends with exactly one required CTA."""
    body = (text or "").rstrip()
    if not body:
        return body
    cta = required_landing_cta(account, lang, platform)
    body = re.sub(
        r"\n*👉\s*(?:일본|한국|韓国|日本)[^\n]*tryproo\.com/?\s*$",
        "",
        body,
    ).rstrip()
    body = re.sub(
        r"\n*https://onlyfriends\.tryproo\.com/?\s*$",
        "",
        body,
    ).rstrip()
    body = re.sub(
        r"\n*👉\s*(?:앱스토어|App Store)[^\n]*(?:친구 만들기|友達作り)\s*$",
        "",
        body,
    ).rstrip()
    return f"{body}\n\n{cta}"


def enforce_payload_cta(
    payload: dict,
    account: str,
    lang: str,
    platform: str = "",
) -> None:
    """Mutate a content payload in place to satisfy the CTA invariant."""
    if not isinstance(payload, dict):
        return
    text = str(payload.get("text") or "")
    if not text.strip():
        return
    payload["text"] = ensure_required_landing_cta(text, account, lang, platform)


def fit_platform_limit(text: str, platform: str, account: str, lang: str) -> str:
    """Trim text to the platform limit while preserving the required CTA."""
    limit = PLATFORM_LIMITS.get((platform or "").lower())
    if not limit or len(text or "") <= limit:
        return text or ""
    cta = required_landing_cta(account, lang, platform)
    body = text or ""
    if cta in body:
        body = body.replace(cta, "").strip()
    room = max(0, limit - len(cta) - 2)
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return f"{body}\n\n{cta}".strip()


def media_type_for(platform: str, image_url: str) -> str:
    """Return frontmatter media_type from platform and public image URL."""
    if not image_url:
        return "text"
    return "IMAGE" if (platform or "").lower() == "instagram" else "image"
