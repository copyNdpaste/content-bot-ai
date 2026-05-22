"""Draft document assembly use cases.

The workflow layer owns file paths and timestamps. This module only builds the
markdown document content from already prepared metadata/body values.
"""
from __future__ import annotations

from src.domain.content_rules import media_type_for


def escape_frontmatter_value(value) -> str:
    """Serialize a single-line frontmatter value."""
    if isinstance(value, list):
        return ", ".join(str(item).replace("\n", " ") for item in value)
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def build_draft_frontmatter(
    *,
    platform: str,
    account: str,
    lang: str,
    theme: str,
    payload: dict,
    created_at: str,
) -> dict:
    """Create frontmatter for a generated draft."""
    image_url = str(payload.get("image_url", "") or "").strip()
    return {
        "status": "pending",
        "platform": platform,
        "account": account,
        "lang": lang,
        "theme": escape_frontmatter_value(theme or ""),
        "hook": escape_frontmatter_value(payload.get("hook", "")),
        "hashtags": escape_frontmatter_value(payload.get("hashtags") or []),
        "media_type": media_type_for(platform, image_url),
        "image_url": escape_frontmatter_value(image_url),
        "image_local_path": escape_frontmatter_value(payload.get("image_local_path", "")),
        "image_keyword": escape_frontmatter_value(payload.get("image_keyword", "")),
        "style_source": escape_frontmatter_value(payload.get("style_source", "")),
        "persona_id": escape_frontmatter_value(payload.get("persona_id", "")),
        "audience_id": escape_frontmatter_value(payload.get("audience_id", "")),
        "concept_id": escape_frontmatter_value(payload.get("concept_id", "")),
        "created_at": created_at,
        "source": "content_pipeline_v1",
    }


def build_draft_markdown(frontmatter: dict, body: str) -> str:
    """Render draft frontmatter and body to markdown."""
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", "", (body or "").strip(), ""])
    return "\n".join(lines)

