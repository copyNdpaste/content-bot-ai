"""Draft parsing and status transition use cases."""
from __future__ import annotations

from src.application.draft_documents import escape_frontmatter_value


def parse_draft_markdown(raw: str) -> tuple[dict, str]:
    """Parse a draft markdown document into frontmatter and body."""
    meta: dict[str, str] = {}
    body = raw or ""
    if (raw or "").startswith("---"):
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


def render_draft_markdown(meta: dict, body: str) -> str:
    """Render frontmatter and body without changing key order."""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {escape_frontmatter_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")


def mark_manual_upload_required(meta: dict) -> dict:
    """Return metadata for a draft that must be posted manually."""
    updated = dict(meta)
    updated["status"] = "manual_upload_required"
    return updated


def mark_posted(meta: dict, *, posted_at: str, permalink: str = "", post_id: str = "") -> dict:
    """Return metadata for a successfully posted draft."""
    updated = dict(meta)
    updated["status"] = "posted"
    updated["posted_at"] = posted_at
    if permalink:
        updated["permalink"] = permalink
    if post_id:
        updated["platform_post_id"] = post_id
    return updated


def mark_queued(
    meta: dict,
    *,
    queued_until: str,
    error: str,
    queued_at: str,
) -> dict:
    """Return metadata for a draft waiting for platform cooldown."""
    updated = dict(meta)
    updated["status"] = "queued"
    updated["queued_until"] = queued_until
    updated["queued_reason"] = "cooldown"
    updated["last_error"] = escape_frontmatter_value((error or "")[:500])
    updated["queued_at"] = queued_at
    return updated


def mark_failed(meta: dict, *, error: str, failed_at: str) -> dict:
    """Return metadata for a draft whose automatic upload failed."""
    updated = dict(meta)
    updated["status"] = "failed"
    updated["last_error"] = escape_frontmatter_value((error or "")[:500])
    updated["failed_at"] = failed_at
    return updated
