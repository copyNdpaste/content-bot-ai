#!/usr/bin/env python3
# version: content_pipeline_v1
"""박재범 자율 컨텐츠 파이프라인 (메인 오케스트레이터).

흐름:
  1) 트렌드 수집 (간단 RSS·Google Trends RSS — 외부 의존성 0)
  2) 박재범 페르소나로 컨텐츠 초안 생성 (Claude CLI subprocess, --model claude-opus-4-7)
  3) draft .md 저장 (_company/_agents/instagram/tools/drafts/)
  4) 각 draft 마다 slack_notifier.py 호출 → 승인 카드 게시

CLI:
  python3 content_pipeline.py --platform threads --account jp --theme "K-뷰티 트렌드"
  python3 content_pipeline.py --platform all --account all          # 6 채널 × 계정
  python3 content_pipeline.py --platform x --account kr --dry-run   # Slack 게시 X

환경변수 (.env 또는 launchd):
  ROUTINE_PLATFORMS  threads,instagram,x   (--platform all 일 때 사용)
  ROUTINE_ACCOUNTS   jp,kr                 (--account all 일 때 사용)
  ROUTINE_LANGS      ko,ja                 (계정 기본 언어 매핑이 미정일 때 폴백)
  SLACK_BOT_TOKEN    Slack 게시용 (없으면 fallback → Telegram → stdout)
  TELEGRAM_*         Slack 폴백용

stdout JSON:
  {"status":"completed", "drafts_created": N, "slack_notified": N, "errors": [...]}
"""
import argparse
import json
import os
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# content-bot-ai 신규 레이아웃: src/workflow/ → 두 단계 위가 repo root
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.application import draft_documents  # noqa: E402
from src.domain import content_rules  # noqa: E402

SLACK_NOTIFIER = os.path.join(REPO_ROOT, "src", "slack", "slack_notifier.py")
THREADS_UPLOADER = os.path.join(REPO_ROOT, "src", "uploaders", "threads_uploader.py")
INSTAGRAM_UPLOADER = os.path.join(REPO_ROOT, "src", "uploaders", "instagram_uploader.py")
X_UPLOADER = os.path.join(REPO_ROOT, "src", "uploaders", "x_uploader.py")
# drafts 는 repo 안 drafts/ (gitignored). tokens.json 은 src/auth/ 에.
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
# .env 는 repo root 에. (옛 money-ai 의 _company/_agents/instagram/.env 도 폴백)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
ENV_PATH_LEGACY = os.path.join(REPO_ROOT, "_company", "_agents", "instagram", ".env")

CLAUDE_TIMEOUT_SEC = 180
CODEX_TIMEOUT_SEC = 180
PYTHON_BIN = sys.executable or "/opt/homebrew/bin/python3"

PLATFORM_LIMITS = content_rules.PLATFORM_LIMITS

STYLE_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "content_styles.json")
_STYLE_CACHE = None

ACCOUNT_LANG_DEFAULT = {
    "jp": "ja",
    "kr": "ko",
}

# ─── .env 로더 (외부 의존성 0) ────────────────────────────────────────────

def _load_env_file(path: str) -> None:
    """KEY=VAL 형식 .env 를 os.environ 에 머지 (기존 값 우선 — launchd 가 이김)."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                # 인라인 주석 제거 (단, 따옴표 안의 # 은 보존)
                if v and not (v.startswith('"') or v.startswith("'")):
                    hash_idx = v.find("#")
                    if hash_idx >= 0:
                        v = v[:hash_idx].rstrip()
                v = v.strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


# ─── 트렌드 수집 (외부 의존성 0) ──────────────────────────────────────────

GOOGLE_TRENDS_RSS = {
    "kr": "https://trends.google.com/trending/rss?geo=KR",
    "jp": "https://trends.google.com/trending/rss?geo=JP",
}


def _fetch_trends(lang: str, limit: int = 8) -> list:
    """Google Trends RSS 에서 키워드 추출. 실패 시 [] 반환."""
    geo_key = "jp" if lang == "ja" else "kr"
    url = GOOGLE_TRENDS_RSS.get(geo_key)
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 moneyai-bot"})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    # 매우 단순한 <title> 추출 (1번째는 채널 타이틀 → 건너뜀)
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?([^<\]]+?)(?:\]\]>)?</title>", body)
    if len(titles) <= 1:
        return []
    return [t.strip() for t in titles[1:1 + limit] if t.strip()]


# ─── DB 기반 스타일 컨텍스트 ──────────────────────────────────────────────

def _supabase_env() -> tuple[str, str]:
    url = (
        os.environ.get("CONTENT_STYLE_SUPABASE_URL")
        or os.environ.get("SUPABASE_URL")
        or ""
    ).strip().rstrip("/")
    key = (
        os.environ.get("CONTENT_STYLE_SUPABASE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_KEY")
        or ""
    ).strip()
    return url, key


def _fetch_supabase_table(table: str, *, limit: int = 200) -> list:
    url, key = _supabase_env()
    if not url or not key:
        return []
    query = urllib.parse.urlencode({"select": "*", "limit": str(limit)})
    req = urllib.request.Request(
        f"{url}/rest/v1/{urllib.parse.quote(table)}?{query}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "content-bot-ai-style-loader",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        sys.stderr.write(f"⚠️ Supabase 스타일 로드 실패({table}): {str(e)[:160]}\n")
        return []


def _supabase_request(method: str, table: str, payload=None, query: str = ""):
    url, key = _supabase_env()
    if not url or not key:
        return None
    endpoint = f"{url}/rest/v1/{urllib.parse.quote(table)}"
    if query:
        endpoint += f"?{query}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
            "User-Agent": "content-bot-ai-learning-logger",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else None


def _json_or_text(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except Exception:
            return {"raw": value[:12000]}
    return {"raw": str(value)}


def _learning_log_enabled() -> bool:
    return (os.environ.get("CONTENT_LEARNING_LOG_ENABLED") or "true").lower() not in {
        "0", "false", "no", "off"
    }


def _insert_generation_artifact(platform: str, account: str, lang: str, theme: str,
                                draft_path: str, payload: dict,
                                content_prompt: str) -> str:
    if not _learning_log_enabled():
        return ""
    row = {
        "draft_path": draft_path,
        "platform_id": platform,
        "account": account,
        "language": lang,
        "theme": theme or "",
        "style_source": payload.get("style_source") or "",
        "persona_id": payload.get("persona_id") or None,
        "audience_id": payload.get("audience_id") or None,
        "concept_id": payload.get("concept_id") or None,
        "text": payload.get("text") or "",
        "hook_text": payload.get("hook") or "",
        "hashtags": payload.get("hashtags") or [],
        "content_prompt": content_prompt,
        "content_raw": _json_or_text(payload.get("raw")),
        "image_prompt": payload.get("image_keyword") or "",
        "image_prompt_raw": _json_or_text(payload.get("image_prompt_raw")),
        "image_model": os.environ.get("OPENAI_IMAGE_MODEL") or "",
        "image_quality": os.environ.get("OPENAI_IMAGE_QUALITY") or "",
        "image_size": os.environ.get("OPENAI_IMAGE_SIZE") or "",
        "image_output_format": os.environ.get("OPENAI_IMAGE_OUTPUT_FORMAT") or "",
        "image_url": payload.get("image_url") or "",
        "image_local_path": payload.get("image_local_path") or "",
        "image_error": payload.get("image_error") or "",
        "approval_status": "pending",
    }
    try:
        query = urllib.parse.urlencode({"on_conflict": "draft_path"})
        res = _supabase_request("POST", "content_generation_artifacts", [row], query)
        if isinstance(res, list) and res:
            artifact_id = str(res[0].get("id") or "")
            sys.stderr.write(f"✅ 생성 아티팩트 DB 저장 완료 → {artifact_id}\n")
            return artifact_id
    except Exception as e:
        sys.stderr.write(f"⚠️ 생성 아티팩트 DB 저장 스킵: {str(e)[:220]}\n")
    return ""


def _update_generation_artifact(draft_path: str, updates: dict) -> None:
    if not _learning_log_enabled() or not draft_path or not updates:
        return
    clean = {k: v for k, v in updates.items() if v is not None}
    if not clean:
        return
    try:
        query = urllib.parse.urlencode({"draft_path": f"eq.{draft_path}"})
        _supabase_request("PATCH", "content_generation_artifacts", clean, query)
    except Exception as e:
        sys.stderr.write(f"⚠️ 생성 아티팩트 DB 업데이트 스킵: {str(e)[:220]}\n")


def _load_json_style_config() -> dict:
    if not os.path.isfile(STYLE_CONFIG_PATH):
        return {}
    try:
        with open(STYLE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        sys.stderr.write(f"⚠️ 로컬 스타일 config 로드 실패: {str(e)[:160]}\n")
        return {}


def _load_style_data() -> dict:
    """Supabase DB 스타일 정의를 우선 로드하고, 로컬 JSON은 변주 보조로 사용."""
    global _STYLE_CACHE
    if _STYLE_CACHE is not None:
        return _STYLE_CACHE

    source = (os.environ.get("CONTENT_STYLE_SOURCE") or "auto").strip().lower()
    local_config = _load_json_style_config()
    data = {"source": "none", "local_config": local_config}

    if source in {"auto", "supabase"}:
        tables = {
            "personas": _fetch_supabase_table("personas"),
            "audiences": _fetch_supabase_table("audiences"),
            "content_concepts": _fetch_supabase_table("content_concepts"),
            "platforms": _fetch_supabase_table("platforms"),
            "persona_audience_mapping": _fetch_supabase_table("persona_audience_mapping"),
            "time_strategies": _fetch_supabase_table("time_strategies"),
        }
        if any(tables.values()):
            data.update(tables)
            data["source"] = "supabase"
            _STYLE_CACHE = data
            return data
        if source == "supabase":
            _STYLE_CACHE = data
            return data

    if local_config:
        data["source"] = "json"
    _STYLE_CACHE = data
    return data


def _by_id(rows: list, row_id: str) -> dict:
    for row in rows or []:
        if isinstance(row, dict) and str(row.get("id", "")) == row_id:
            return row
    return {}


def _persona_id_for(account: str, lang: str) -> str:
    if account.lower() == "jp" or lang == "ja":
        return "JP_female"
    return "KR_female"


def _choose_persona_id(personas: list, account: str, lang: str) -> str:
    nationality = "JP" if account.lower() == "jp" or lang == "ja" else "KR"
    candidates = [
        p for p in personas or []
        if isinstance(p, dict)
        and p.get("is_active", True)
        and p.get("gender") == "female"
        and p.get("nationality") == nationality
        and (not p.get("primary_language") or p.get("primary_language") == lang)
    ]
    expanded = [
        p for p in candidates
        if str(p.get("id", "")) not in {"KR_female", "JP_female"}
    ]
    pool = expanded or candidates
    if not pool:
        return _persona_id_for(account, lang)
    return str(random.choice(pool).get("id"))


def _hour_kst() -> int:
    return int(time.strftime("%H", time.gmtime(time.time() + 9 * 3600)))


def _closest_strategy(strategies: list, persona_id: str, platform: str) -> dict:
    candidates = [
        s for s in strategies or []
        if isinstance(s, dict)
        and s.get("persona_id") == persona_id
        and s.get("is_active", True)
    ]
    if not candidates:
        return {}

    platform = platform.lower()
    platform_candidates = [
        s for s in candidates
        if platform in {str(x).lower() for x in (s.get("platform_pool") or [])}
    ]
    if platform_candidates:
        candidates = platform_candidates

    now_hour = _hour_kst()

    def distance(row: dict) -> int:
        h = int(row.get("hour_kst") or 0)
        raw = abs(h - now_hour)
        return min(raw, 24 - raw)

    return sorted(candidates, key=distance)[0]


def _choose_mapping(mappings: list, persona_id: str, audience_id: str = "") -> dict:
    candidates = [
        m for m in mappings or []
        if isinstance(m, dict) and m.get("persona_id") == persona_id
    ]
    if audience_id:
        exact = [m for m in candidates if m.get("audience_id") == audience_id]
        if exact:
            candidates = exact
    if not candidates:
        return {}
    weighted = []
    for m in candidates:
        weight = max(1, int(m.get("priority_weight") or 1))
        weighted.extend([m] * weight)
    return random.choice(weighted)


def _load_style_context(platform: str, account: str, lang: str) -> dict:
    data = _load_style_data()
    local = data.get("local_config") or {}

    if data.get("source") == "supabase":
        persona_id = _choose_persona_id(data.get("personas") or [], account, lang)
        strategy = _closest_strategy(data.get("time_strategies"), persona_id, platform)
        audience_id = random.choice(strategy.get("target_audience_ids") or [""])
        mapping = _choose_mapping(data.get("persona_audience_mapping"), persona_id, audience_id)
        if not audience_id:
            audience_id = str(mapping.get("audience_id", "") or "")

        concept_pool = list(strategy.get("concept_pool") or mapping.get("recommended_concepts") or [])
        forbidden_concepts = set(mapping.get("forbidden_concepts") or [])
        concept_pool = [c for c in concept_pool if c and c not in forbidden_concepts]
        concept_id = random.choice(concept_pool) if concept_pool else ""

        return {
            "source": "supabase",
            "persona": _by_id(data.get("personas"), persona_id),
            "audience": _by_id(data.get("audiences"), audience_id),
            "mapping": mapping,
            "concept": _by_id(data.get("content_concepts"), concept_id),
            "strategy": strategy,
            "variation": _pick_local_variation(local, platform),
        }

    return {
        "source": data.get("source", "none"),
        "persona": random.choice((local.get("personas") or {}).get(account, []) or [{}]),
        "audience": {},
        "mapping": {},
        "concept": {},
        "strategy": {},
        "variation": _pick_local_variation(local, platform),
    }


def _pick_local_variation(local: dict, platform: str) -> dict:
    return {
        "content_angle": random.choice(local.get("content_angles") or [""]),
        "mood": random.choice(local.get("moods") or [""]),
        "visual_style": random.choice(local.get("visual_styles") or [""]),
        "platform_variation": random.choice(
            (local.get("platform_variations") or {}).get(platform, []) or [""]
        ),
    }


def _format_list(values, limit: int = 6) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        return values
    return ", ".join(str(x) for x in list(values)[:limit] if str(x).strip())


def _build_style_context_block(ctx: dict, lang: str, platform: str) -> str:
    if not ctx or ctx.get("source") == "none":
        return ""
    persona = ctx.get("persona") or {}
    audience = ctx.get("audience") or {}
    mapping = ctx.get("mapping") or {}
    concept = ctx.get("concept") or {}
    strategy = ctx.get("strategy") or {}
    variation = ctx.get("variation") or {}

    lines = [
        "🧬 DB 스타일 컨텍스트:",
        f"  - source: {ctx.get('source')}",
    ]
    if persona:
        lines += [
            f"  - persona: {persona.get('name') or persona.get('label') or persona.get('id')}",
            f"    background: {persona.get('background_story') or persona.get('background') or ''}",
            f"    speaking_style: {persona.get('speaking_style') or ''}",
            f"    signature_phrases: {_format_list(persona.get('signature_phrases'))}",
            f"    forbidden_phrases: {_format_list(persona.get('forbidden_phrases'))}",
            f"    expertise: {_format_list(persona.get('expertise_areas'))}",
        ]
    if audience:
        lines += [
            f"  - audience: {audience.get('name') or audience.get('id')}",
            f"    interests: {_format_list(audience.get('interests'))}",
            f"    desires: {_format_list(audience.get('desires'))}",
            f"    pain_points: {_format_list(audience.get('pain_points'))}",
            f"    preferred_tone: {_format_list(audience.get('preferred_tone'))}",
        ]
    if mapping:
        lines += [
            f"  - matching_strategy: {mapping.get('matching_strategy') or ''}",
            f"    tone_guideline: {mapping.get('tone_guideline') or ''}",
            f"    topic_guideline: {mapping.get('topic_guideline') or ''}",
            f"    recommended_concepts: {_format_list(mapping.get('recommended_concepts'))}",
        ]
    if concept:
        lines += [
            f"  - concept: {concept.get('id') or concept.get('format_type')}",
            f"    goal: {concept.get('goal') or ''}",
            f"    structure: {concept.get('structure_template') or ''}",
            f"    hook_pattern: {concept.get('hook_pattern') or ''}",
            f"    caution: {concept.get('cautions') or ''}",
        ]
    if strategy:
        lines += [
            f"  - time_strategy: {strategy.get('hour_kst')}시 / {strategy.get('tone') or ''}",
            f"    tone_description: {strategy.get('tone_description') or ''}",
            f"    topic_priority: {_format_list(strategy.get('topic_priority'))}",
        ]
    if variation:
        lines += [
            f"  - variation_for_this_post: {variation.get('platform_variation') or ''}",
            f"    content_angle: {variation.get('content_angle') or ''}",
            f"    mood: {variation.get('mood') or ''}",
            f"    visual_style: {variation.get('visual_style') or ''}",
        ]
    lines += [
        f"  - 적용 규칙: 위 컨텍스트를 {platform}/{lang} 글의 화자, 청자, 컨셉, 이미지 분위기에 반영.",
        "  - 단, DB 문장을 그대로 복붙하지 말고 실제 사람이 쓴 것처럼 자연스럽게 변형.",
    ]
    return "\n".join(lines) + "\n"


def _attach_style_meta(payload: dict, ctx: dict) -> None:
    """draft frontmatter 에 추적 가능한 스타일 선택값만 남긴다."""
    if not isinstance(payload, dict) or not isinstance(ctx, dict):
        return
    payload["style_source"] = ctx.get("source", "")
    persona = ctx.get("persona") or {}
    audience = ctx.get("audience") or {}
    concept = ctx.get("concept") or {}
    payload["persona_id"] = persona.get("id", "")
    payload["audience_id"] = audience.get("id", "")
    payload["concept_id"] = concept.get("id", "") or concept.get("format_type", "")


# ─── 본문 생성 LLM 호출 ──────────────────────────────────────────────────

BRAND_NAME = content_rules.BRAND_NAME
LANDING_URL = content_rules.LANDING_URL

# 자연스러운 brand mention 예시 — "광고 카피" 가 아니라 "친구한테 말하듯" 톤.
# LLM 이 이걸 참고해서 더 자연스럽게 변형하길 기대 (그대로 베껴도 OK).
# 핵심: 본문보다 존재감 약하게, 글 흐름 끝부분에 슬쩍.
SOFT_MENTION_KO = [
    "👉 OnlyFriends 에서 한일 친구 만들어보세요 https://onlyfriends.tryproo.com/",
    "👉 일본 친구 매칭 받고 싶으면 우리 OnlyFriends 한 번 들러요 https://onlyfriends.tryproo.com/",
    "👉 한일 친구 매칭 → OnlyFriends https://onlyfriends.tryproo.com/",
    "👉 일본 친구 진짜 만들어보고 싶으면 → https://onlyfriends.tryproo.com/",
    "🇰🇷🇯🇵 OnlyFriends — 한일 친구 매칭 https://onlyfriends.tryproo.com/",
    "프로필 링크 → 한일 친구 매칭 시작 https://onlyfriends.tryproo.com/",
    "👉 우리 서비스에서 일본 친구 매칭 받을 수 있어요 https://onlyfriends.tryproo.com/",
    "한일 친구 진짜로 만들고 싶다 → OnlyFriends https://onlyfriends.tryproo.com/",
]
SOFT_MENTION_JA = [
    "👉 OnlyFriends で韓国の友達できます https://onlyfriends.tryproo.com/",
    "👉 韓国の友達ほしいなら OnlyFriends に来てね https://onlyfriends.tryproo.com/",
    "🇰🇷🇯🇵 日韓フレンドマッチング → OnlyFriends https://onlyfriends.tryproo.com/",
    "👉 韓国人の友達マッチングしたいなら → https://onlyfriends.tryproo.com/",
    "プロフィールリンクから日韓友達マッチ → https://onlyfriends.tryproo.com/",
    "👉 私たちのサービスで韓国友達できますよ https://onlyfriends.tryproo.com/",
    "本当に韓国の友達ほしい人へ → OnlyFriends https://onlyfriends.tryproo.com/",
    "韓国の友達 → OnlyFriends で会えます https://onlyfriends.tryproo.com/",
]


def _required_landing_cta(account: str, lang: str) -> str:
    """계정별 필수 랜딩 CTA. 최종 본문 끝에 코드로 강제한다."""
    return content_rules.required_landing_cta(account, lang)


def _ensure_required_landing_cta(payload: dict, account: str, lang: str) -> None:
    content_rules.enforce_payload_cta(payload, account, lang)


def _pick_soft_mention(lang: str) -> str:
    """자연스러운 brand mention 1개 — 광고 톤 X, 글 끝에 슬쩍."""
    pool = SOFT_MENTION_KO if lang == "ko" else SOFT_MENTION_JA
    return random.choice(pool)


def _build_persona_prompt(platform: str, account: str, lang: str,
                          theme: str, trends: list,
                          style_context: dict | None = None) -> str:
    """실사용자 시뮬레이션 프롬프트 — 광고 카피 X, 진짜 사람 글 O.

    구조 핵심:
      - 박재범 (운영자/에이전트) 이 아니라 '실제 2030 한일 여성 SNS 유저' 1인칭으로 LLM 가 ' 빙의'
      - CTA·브랜드는 본문보다 약하게, 글 끝에 자연스럽게 슬쩍
      - 결론·기승전결·완벽한 문장 금지. 흐름 끊겨도 OK
    """
    limit = PLATFORM_LIMITS.get(platform, 500)
    trend_block = ""
    if trends:
        trend_block = "최근 회자되는 키워드 (참고용 — 베끼지 말고 자연스럽게 녹임):\n" + \
            "\n".join(f"  - {t}" for t in trends[:6])

    lang_label = {"ko": "한국어", "ja": "일본어"}.get(lang, "한국어")
    soft_mention = _pick_soft_mention(lang)

    # 페르소나 — 광고 카피라이터 X. 운영자 관점은 갖되 글은 일반 SNS처럼 자연스럽게.
    persona_ko = (
        "당신은 '광고 카피라이터' 가 아닙니다.\n"
        "당신의 역할은 — **OnlyFriends (한일 친구 매칭 서비스) 의 20대 후반 한국인 운영팀 멤버** 로서, "
        "본인 일상·매칭 사례·여행 이야기를 Threads / X / Instagram 에 올리는 것입니다.\n"
        "\n"
        "📌 핵심 정체성:\n"
        "  - 운영자이지만 운영자 티를 과하게 내지 않음\n"
        "  - 친구한테 카톡하듯, 오늘 있었던 일처럼 씀\n"
        "  - 매칭 사례·후기·여행기·일상이 자연스럽게 소재가 됨\n"
        "  - 브랜드/URL 은 매번 넣지 말고, 필요할 때만 끝에 아주 작게\n"
    )
    persona_ja = (
        "あなたは「広告コピーライター」ではありません。\n"
        "あなたの役割は — **OnlyFriends (日韓フレンドマッチング) の 20代後半・日本人運営チームメンバー** "
        "として、自分の日常・マッチング事例・旅行話を Threads / X / Instagram に投稿することです。\n"
        "\n"
        "📌 核心アイデンティティ:\n"
        "  - 運営側だけど、運営感を強く出しすぎない\n"
        "  - 友達にLINEするように、今日あった話みたいに書く\n"
        "  - 毎日のマッチング事例・口コミ・旅行記・日常が自然に話題になる\n"
        "  - ブランド/URL は毎回入れず、必要な時だけ最後に小さく\n"
    )

    # 핵심 목표 — 감정 유도 (서비스 소개 X)
    goal_ko = (
        "🎯 핵심 목표:\n"
        "글을 읽은 사람이 다음 같은 감정을 자연스럽게 느끼게 해야 합니다 —\n"
        "  - '나도 일본 친구 있었으면 좋겠다'\n"
        "  - '일본인이랑 카페 가고 싶다'\n"
        "  - '여행 같이 다닐 일본 친구 만들고 싶다'\n"
        "  - '언어교환 친구 있으면 재밌겠다'\n"
        "\n"
        "**서비스 소개가 목적이 아닙니다.** '한일 친구 만들고 싶은 감정' 유도가 목적.\n"
    )
    goal_ja = (
        "🎯 核心目標:\n"
        "投稿を読んだ人が以下のような感情を自然に感じるように —\n"
        "  - '私も韓国の友達ほしいな'\n"
        "  - '韓国人とカフェ行きたい'\n"
        "  - '旅行一緒に行ける韓国の友達ほしい'\n"
        "  - '言語交換の友達できたら楽しそう'\n"
        "\n"
        "**サービス紹介が目的じゃないです。** '韓国の友達ほしい感情' を引き出すのが目的。\n"
    )

    # 금지 사항
    forbidden_ko = (
        "🚫 절대 금지:\n"
        "  - 광고 카피처럼 쓰기\n"
        "  - 문장을 너무 완벽하게 정리\n"
        "  - 감성문구 연속 사용 (인스타 광고체)\n"
        "  - 억지 공감 / 과한 CTA\n"
        "  - 번역투\n"
        "  - AI 느낌 나는 지나치게 깔끔한 문장\n"
        "  - '~하고 싶지 않아?' '여러분도 한번' 같은 노골적 유도\n"
        "  - 브랜드를 중심으로 글 쓰기\n"
        "  - 기승전결 완벽\n"
        "  - 해시태그 도배\n"
    )
    forbidden_ja = (
        "🚫 絶対禁止:\n"
        "  - 広告コピー的に書く\n"
        "  - 文章を完璧に整える\n"
        "  - 感性フレーズ連発 (インスタ広告体)\n"
        "  - わざとらしい共感 / 過剰な CTA\n"
        "  - 翻訳調\n"
        "  - AI っぽい綺麗すぎる文\n"
        "  - '〜したくない?' '皆さんも一度' のような露骨な誘導\n"
        "  - ブランド中心の文章構成\n"
        "  - 起承転結を完璧に\n"
        "  - ハッシュタグ多用\n"
    )

    # 실제 SNS 문체 가이드
    style_ko = (
        "✍️ 실제 SNS 문체:\n"
        "  - 혼잣말 느낌 OK\n"
        "  - 약간 흐름 끊겨도 OK\n"
        "  - 짧은 문장 섞기\n"
        "  - 감정 여백 남기기 (전부 설명하지 X)\n"
        "  - 'ㅋㅋ', '괜히', '뭐랄까', '암튼', '~인 듯' 같은 실제 말투\n"
        "  - 매번 패턴/구조 바꾸기 (같은 구조 반복 X)\n"
        "  - 결론 명확히 안 내도 됨\n"
        "  - 감정이 애매해도 됨\n"
    )
    style_ja = (
        "✍️ 実際の SNS 文体:\n"
        "  - 独り言っぽくて OK\n"
        "  - 流れがちょっと飛んでも OK\n"
        "  - 短い文混ぜる\n"
        "  - 感情の余白を残す (全部説明しない)\n"
        "  - 'なんか', 'ちょっと', '笑', 'てか', '〜かも' のような実際の言い方\n"
        "  - 毎回パターン/構成を変える (同じ構成繰り返さない)\n"
        "  - 結論を明確に出さなくて OK\n"
        "  - 感情が曖昧でも OK\n"
    )

    # 채널별 톤
    channel_tone = {
        "x": (
            f"X — 짧게 툭. 한 문장짜리 혼잣말도 OK. 농담/관찰/여운 중심. "
            f"해시태그 0개 권장, URL 은 거의 넣지 않음. **{limit}자 이내**."
        ),
        "threads": (
            f"Threads — 3~6줄 정도의 자연스러운 짧은 이야기. "
            f"첫 줄은 공감 가능한 관찰, 마지막은 댓글 달고 싶게 살짝 열린 문장. "
            f"{limit}자 이내. 해시태그 0~1개."
        ),
        "instagram": (
            f"Instagram — 사진 밑에 붙는 자연스러운 일상 캡션. "
            f"문장은 너무 시처럼 쓰지 말고, 실제 사람이 올린 짧은 기록처럼. "
            f"본문 {limit}자 이내. 해시태그 4~7개 (도배 X, 자연스럽게). "
            f"본문 분위기에 어울리는 **AI 이미지 생성용 영문 프롬프트** 도 image_keyword 에 함께 출력. "
            f"인물은 반드시 성인 20대 후반~30대 초반 한국/일본 여성처럼 매우 세련되고 압도적으로 예쁘게 보이게. "
            f"일반인 패션 금지. 아이돌 공항패션, K-pop 무대 밖 사복, 패션모델 스트릿 화보처럼 튀는 스타일링과 밝고 깨끗한 fair skin 톤을 선호. "
            f"비 오는 날씨, 흐린 날씨, 노란 조명, 누런 피부톤은 금지. 화창한 낮 자연광으로. "
            f"얼굴을 무조건 숨기지 말고, 카페/거리/여행 사진처럼 자연스러운 거리감의 장면으로. "
            f"실존 인물처럼 특정 유명인을 닮게 하지 말고, 과한 정면 클로즈업/증명사진 느낌은 피함. "
            f"만화풍/일러스트/애니풍은 금지. 완전 실사 인스타 라이프스타일 사진만 허용. "
            f"flat vector, 로고, 텍스트, 워터마크는 금지."
        ),
    }.get(platform, "")

    # CTA — 자연스럽게, 광고 X
    required_cta = _required_landing_cta(account, lang)
    cta_rule_ko = (
        "💬 CTA 규칙:\n"
        "  - CTA 는 선택이 아니라 필수. 게시글 마지막 줄에 정확히 아래 문장을 넣음.\n"
        f"  - 필수 마지막 문장: '{required_cta}'\n"
        f"  - 참고 변형 예시는 쓰지 말고, 이번 실행에서는 필수 문장을 그대로 사용.\n"
        "  - 나쁜 예: '지금 가입하세요' '친구 만들고 싶다면?' '당신도 원하지 않나요?'\n"
        "  - 본문은 자연스럽게 쓰되 마지막 CTA 는 반드시 유지\n"
    )
    cta_rule_ja = (
        "💬 CTA ルール:\n"
        "  - CTA は任意ではなく必須。投稿の最後の行に正確に下の文を入れる。\n"
        f"  - 必須の最後の文: '{required_cta}'\n"
        f"  - 参考例は使わず、今回の実行では必須文をそのまま使う。\n"
        "  - 悪い例: '今すぐ登録' '友達作りたい人は?' 'あなたも欲しくないですか?'\n"
        "  - 本文は自然に書くが、最後の CTA は必ず残す\n"
    )

    # 타겟 분위기
    audience_ko = (
        "👥 타겟: 20~30대 한국 여성.\n"
        "관심사: 카페·여행·K-pop·J-pop·맛집·패션·혼자 여행·전시회·사진·"
        "언어교환·감성 일상 브이로그 톤.\n"
        "감성 핵심: '외국인 친구' 가 아니라 **'일본인 친구'**.\n"
    )
    audience_ja = (
        "👥 ターゲット: 20〜30代の日本人女性。\n"
        "興味: カフェ・旅行・K-POP・J-POP・グルメ・ファッション・一人旅・展覧会・写真・"
        "言語交換・感性日常 vlog のトーン。\n"
        "感情の核: '外国人の友達' ではなく **'韓国人の友達'**。\n"
    )

    persona = persona_ko if lang == "ko" else persona_ja
    goal = goal_ko if lang == "ko" else goal_ja
    forbidden = forbidden_ko if lang == "ko" else forbidden_ja
    style = style_ko if lang == "ko" else style_ja
    cta_rule = cta_rule_ko if lang == "ko" else cta_rule_ja
    audience = audience_ko if lang == "ko" else audience_ja

    theme_block = ""
    if theme:
        theme_block = f"오늘 떠올린 주제: {theme}\n"
    style_context_block = _build_style_context_block(style_context or {}, lang, platform)

    return (
        f"{persona}\n"
        f"{style_context_block}\n"
        f"{goal}\n"
        f"{forbidden}\n"
        f"{style}\n"
        f"{audience}\n"
        f"{cta_rule}\n"
        "중요: 문구가 조금 덜 완성돼 보여도 실제 사람 같으면 그쪽을 선택. "
        "광고 문장, 캠페인 문장, 서비스 소개문처럼 보이면 실패.\n"
        f"📱 채널·언어: {platform.upper()} (@{account}) / {lang_label}\n"
        f"형식: {channel_tone}\n"
        "\n"
        f"{theme_block}{trend_block}\n"
        "\n"
        "이제 위 인격으로 빙의해서 글 1개 작성하세요. "
        "최종 점검: 읽었을 때 '광고 같은데?' 가 아니라 "
        "**'이 사람 진짜 일본/한국 친구 좋아하나 보다'** 느낌 나야 성공.\n"
        "\n"
        "출력 형식 (JSON only, 다른 텍스트 X):\n"
        + (
            '{"text": "<게시될 본문 전체>", '
            '"hook": "<첫 한 줄>", '
            '"hashtags": ["<있으면 태그>"], '
            '"image_keyword": "<IG 일 때만 — 영문 20~45단어, AI 이미지 생성 프롬프트 (20~30대 한국/일본인 분위기 + 장면·조명·분위기 + no text, no watermark),'
            "예: 'tokyo cafe friends', 'seoul night market'>\"}\n"
            if platform == "instagram"
            else '{"text": "<게시될 본문 전체>", '
                 '"hook": "<첫 한 줄>", '
                 '"hashtags": ["<있으면 태그>"]}\n'
        )
    )


def _call_claude(prompt: str) -> dict:
    """claude -p <prompt> --model ... --output-format json 호출.

    반환:
      {"ok": True, "text": "...", "hook": "...", "hashtags": [...], "raw": "..."}
      {"ok": False, "error": "..."}
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--model", os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
        "--output-format", "json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI 미설치 (which claude 확인)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"claude CLI 타임아웃 ({CLAUDE_TIMEOUT_SEC}s)"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"claude exit {proc.returncode}: {(proc.stderr or '')[:300]}",
        }

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return {"ok": False, "error": "claude stdout 비어있음"}

    # claude --output-format json → {"type":"result","result":"<text>", ...}
    outer = None
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        # 혹시 평문이면 그대로 사용
        outer = {"result": stdout}

    inner_text = outer.get("result") if isinstance(outer, dict) else None
    if not inner_text:
        inner_text = stdout

    # 모델이 ```json ... ``` 코드블록으로 감쌀 수 있음 → 벗기기
    cleaned = inner_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    # JSON 추출 시도
    parsed = None
    m = re.search(r"\{.*\}", cleaned, re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None

    if isinstance(parsed, dict) and parsed.get("text"):
        return {
            "ok": True,
            "text": str(parsed.get("text", "")).strip(),
            "hook": str(parsed.get("hook", "")).strip(),
            "hashtags": parsed.get("hashtags") or [],
            "image_keyword": str(parsed.get("image_keyword", "") or "").strip(),
            "raw": inner_text,
        }

    # JSON 추출 실패 → 평문 그대로 본문으로
    return {
        "ok": True,
        "text": cleaned,
        "hook": cleaned.split("\n", 1)[0][:120],
        "hashtags": [],
        "image_keyword": "",
        "raw": inner_text,
    }


def _parse_content_json(content: str) -> dict:
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict) or not parsed.get("text"):
        return {"ok": False, "error": f"JSON text 없음: {cleaned[:200]}"}

    return {
        "ok": True,
        "text": str(parsed.get("text", "")).strip(),
        "hook": str(parsed.get("hook", "")).strip(),
        "hashtags": parsed.get("hashtags") or [],
        "image_keyword": str(parsed.get("image_keyword", "") or "").strip(),
        "raw": content,
    }


def _parse_image_prompt_json(content: str) -> dict:
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict) or not parsed.get("image_keyword"):
        return {"ok": False, "error": f"image_keyword 없음: {cleaned[:200]}"}
    return {
        "ok": True,
        "image_keyword": str(parsed.get("image_keyword", "")).strip(),
        "raw": content,
    }


def _run_codex_json(instructions: str, *, timeout_env: str = "CODEX_TEXT_TIMEOUT_SEC") -> dict:
    codex_bin = (os.environ.get("CODEX_BIN") or "/Users/hoony/.local/bin/codex").strip()
    if not os.path.isfile(codex_bin):
        codex_bin = "codex"

    model = (os.environ.get("CODEX_TEXT_MODEL") or "").strip()
    timeout = int(os.environ.get(timeout_env) or str(CODEX_TIMEOUT_SEC))
    fd, output_path = tempfile.mkstemp(prefix="contentbot-codex-", suffix=".txt")
    os.close(fd)
    cmd = [
        codex_bin,
        "exec",
        "--sandbox", "read-only",
        "-C", REPO_ROOT,
        "--output-last-message", output_path,
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(instructions)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "codex CLI 미설치"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"codex CLI 타임아웃 ({timeout}s)"}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": f"codex exit {proc.returncode}: {err[:300]}"}

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        content = proc.stdout or ""
    finally:
        try:
            os.unlink(output_path)
        except Exception:
            pass
    return {"ok": True, "content": content}


def _call_codex_content(prompt: str) -> dict:
    """Codex CLI 로 채널별 SNS 문구 JSON 생성. OpenAI API 키를 직접 쓰지 않는다."""
    instructions = (
        "Return only one valid JSON object. No markdown. No explanation.\n"
        "Required keys: text, hook, hashtags.\n"
        "Write like an actual social media user, not a marketer.\n\n"
        + prompt
    )
    res = _run_codex_json(instructions)
    if not res.get("ok"):
        return res
    return _parse_content_json(res.get("content", ""))


def _call_codex_image_prompt(platform: str, lang: str, post_text: str,
                             hook: str = "", hashtags=None,
                             style_context: dict | None = None) -> dict:
    """생성된 게시글 본문을 기반으로 이미지 생성 프롬프트를 만든다."""
    tag_text = ", ".join(hashtags or [])
    style_block = _build_style_context_block(style_context or {}, lang, platform)
    instructions = f"""
Return only one valid JSON object. No markdown. No explanation.
Required key: image_keyword.

Create an English image-generation prompt that matches this {platform} post.
Use GPT image generation, not SVG illustration.
Generate a high-quality AI image similar to ChatGPT image generation quality.
Create a fully photorealistic Instagram lifestyle photo. Never use anime, manga, illustration, cartoon, or vector style.
Create a trendy, candid 2020s social-media photo with detailed lighting, real skin texture, natural lens depth, and believable environment.
The main subject should look like an exceptionally beautiful, stylish adult woman in her late 20s to early 30s from Korea or Japan when people fit the post.
Avoid ordinary casual fashion. Use eye-catching idol airport fashion, off-duty K-pop idol styling, fashion-model street editorial styling, statement outfit, luxury accessories, trendy hair, and polished glam details without resembling any real celebrity.
Use idol/model-inspired styling without resembling any real celebrity: natural glam makeup, striking but realistic facial features, symmetrical photogenic face, contemporary hair, fashionable statement outfit, slim elegant proportions, and a polished influencer/editorial look.
Use bright fair skin tones and clean natural complexion. Avoid yellowish skin, muddy color grading, orange indoor lighting, dull gray lighting, and rainy or cloudy weather.
Prefer sunny clear daytime, bright outdoor natural light, fresh spring/summer atmosphere, clean white-balanced color, and airy Instagram editorial photography.
Do not generate flat vector graphics, anime, manga, webtoon, 3D render, doll-like skin, or over-smoothed AI faces.
The image must feel like an attention-grabbing candid social media visual, not an ad.
No text in the image, no logos, no watermark.
Show exceptionally attractive late-20s to early-30s Korean and Japanese adult women when it fits the post.
Faces may be visible if they look like fictional, natural social-media people.
Avoid minors, teenage appearance, celebrity likeness, plastic-perfect faces, ID-photo portraits, extreme close-up headshots, or direct model-stare poses.
Use a natural candid distance, cafe/travel/street/stadium composition, and contemporary Korean/Japanese styling.
For baseball-related posts, lean into the trendy beautiful woman at baseball stadium vibe: stylish jersey, idol-like hair and makeup, bright daytime stadium, cheering crowd, food in hand, candid friend-taken photo.
Use concrete scene details from the post: place, mood, weather, objects, time of day.
Use the DB style context below for persona, mood, and visual variation when present.
Keep it 30-55 English words.

{style_block}
Post language: {lang}
Hook: {hook}
Hashtags: {tag_text}
Post:
{post_text}
"""
    res = _run_codex_json(instructions)
    if not res.get("ok"):
        return res
    return _parse_image_prompt_json(res.get("content", ""))


# ─── 외부 이미지 생성 커맨드 (Codex/자동화 훅) ─────────────────────────────


def _image_prompt_with_safety(prompt: str) -> str:
    """SNS 게시용 이미지 프롬프트에 품질/금지 가이드를 보강."""
    p = (prompt or "").strip()
    if not p:
        return ""
    safety_suffix = (
        ", fully photorealistic Instagram lifestyle photo, exceptionally beautiful adult Korean or Japanese woman "
        "in her late 20s to early 30s, eye-catching idol airport fashion, off-duty K-pop idol styling, "
        "fashion-model street editorial outfit, statement accessories, natural glam makeup, striking realistic face, "
        "idol/model-inspired styling without celebrity likeness, bright fair skin tone, clean natural complexion, "
        "sunny clear daytime, bright outdoor natural light, clean white-balanced color, no rainy weather, no cloudy weather, "
        "no yellowish skin, no orange indoor lighting, candid social media scene, cinematic lighting, detailed textures, natural skin texture, "
        "no anime, no manga, no illustration, no cartoon, no vector, no 3D render, no text, no watermark, no logo, "
        "no minors, no teenage appearance, no celebrity likeness, no ID photo, no extreme close-up"
    )
    lower = p.lower()
    if "no text" not in lower or "no watermark" not in lower:
        p = p + safety_suffix
    return p


def _format_command_template(template: str, values: dict) -> list:
    """환경변수 커맨드 템플릿을 shell 없이 argv 로 변환."""
    quoted = {k: shlex.quote(str(v)) for k, v in values.items()}
    return shlex.split(template.format(**quoted))


def _generate_codex_image(prompt: str, *, width: int = 1024, height: int = 1024) -> str | None:
    """CODEX_IMAGE_COMMAND 실행 → /tmp/codex-image-*.png 경로 반환.

    이 프로젝트는 이미지 생성 API 를 직접 호출하지 않는다. 외부 커맨드가
    `{prompt}` 와 `{output}` 을 받아 이미지 파일을 만들어야 한다.
    """
    template = (os.environ.get("CODEX_IMAGE_COMMAND") or "").strip()
    if not template:
        sys.stderr.write("ℹ️ CODEX_IMAGE_COMMAND 미설정 — 이미지 생성 스킵\n")
        return None

    p = _image_prompt_with_safety(prompt)
    if not p:
        return None

    ts = int(time.time())
    out_path = f"/tmp/codex-image-{ts}-{random.randint(1000, 9999)}.png"
    timeout = int(os.environ.get("CODEX_IMAGE_TIMEOUT_SEC") or "600")

    try:
        cmd = _format_command_template(template, {
            "prompt": p,
            "output": out_path,
            "width": width,
            "height": height,
        })
        sys.stderr.write(
            f"🎨 외부 이미지 생성 시작 ({width}x{height}) → {out_path}\n"
        )
        sys.stderr.flush()
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - t0
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"❌ 이미지 생성 커맨드 타임아웃 ({timeout}s)\n")
        return None
    except Exception as e:
        sys.stderr.write(f"❌ 이미지 생성 커맨드 실행 실패: {e}\n")
        return None

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        sys.stderr.write(f"❌ 이미지 생성 커맨드 실패: {err[:500]}\n")
        return None

    if not os.path.isfile(out_path):
        sys.stderr.write(f"⚠️ 이미지 생성 커맨드 출력 파일 없음: {out_path}\n")
        return None

    sys.stderr.write(f"✅ 외부 이미지 생성 완료 ({elapsed:.1f}s) → {out_path}\n")
    sys.stderr.flush()
    return out_path


def _publish_image_url(image_path: str) -> str | None:
    """IMAGE_PUBLIC_URL_COMMAND 실행 → Instagram API 용 공개 HTTPS URL 반환."""
    template = (os.environ.get("IMAGE_PUBLIC_URL_COMMAND") or "").strip()
    if not template:
        sys.stderr.write("ℹ️ IMAGE_PUBLIC_URL_COMMAND 미설정 — IG 이미지 URL 생성 스킵\n")
        return None
    if not image_path or not os.path.isfile(image_path):
        return None

    timeout = int(os.environ.get("IMAGE_PUBLIC_URL_TIMEOUT_SEC") or "180")
    try:
        cmd = _format_command_template(template, {"file": image_path})
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"❌ 이미지 URL 커맨드 타임아웃 ({timeout}s)\n")
        return None
    except Exception as e:
        sys.stderr.write(f"❌ 이미지 URL 커맨드 실행 실패: {e}\n")
        return None

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        sys.stderr.write(f"❌ 이미지 URL 커맨드 실패: {err[:500]}\n")
        return None

    url = (proc.stdout or "").strip().splitlines()[-1].strip()
    if not url.startswith("https://"):
        sys.stderr.write(f"❌ 이미지 URL 커맨드가 HTTPS URL 을 반환하지 않음: {url[:200]}\n")
        return None

    sys.stderr.write(f"✅ 이미지 공개 URL 생성 완료 → {url}\n")
    sys.stderr.flush()
    return url


# ─── draft 저장 ───────────────────────────────────────────────────────────

def _ensure_drafts_dir() -> None:
    os.makedirs(DRAFTS_DIR, exist_ok=True)


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _escape_fm(v) -> str:
    """frontmatter 안전 직렬화 (단일 라인)."""
    return draft_documents.escape_frontmatter_value(v)


def _write_draft(platform: str, account: str, lang: str, theme: str,
                 payload: dict) -> str:
    _ensure_drafts_dir()
    ts = _now_stamp()
    filename = f"{platform}-{ts}-{account}.md"
    path = os.path.join(DRAFTS_DIR, filename)

    fm = draft_documents.build_draft_frontmatter(
        platform=platform,
        account=account,
        lang=lang,
        theme=theme,
        payload=payload,
        created_at=ts,
    )
    body = payload.get("text", "").strip() + "\n"
    content = draft_documents.build_draft_markdown(fm, body)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _parse_draft(path: str) -> tuple[dict, str]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    meta = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def _rewrite_draft(path: str, meta: dict, body: str) -> None:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    out = "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


# ─── Slack 알림 호출 ──────────────────────────────────────────────────────

def _slack_upload_preview(image_local_path: str, platform: str, account: str) -> dict:
    """Slack files_upload_v2 로 로컬 이미지 미리보기 게시.
       (생성된 로컬 이미지 결과를 채널에서 검토 가능.)
       slack_notifier.py 의 chat.postMessage (본문 + ✅/❌ 버튼) 직전에 호출."""
    if not image_local_path or not os.path.isfile(image_local_path):
        return {"ok": False, "skipped": "no local image"}
    bot_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    channel_id = (os.environ.get("SLACK_CHANNEL_ID") or "").strip()
    if not bot_token or not channel_id:
        return {"ok": False, "skipped": "no slack token/channel"}
    try:
        from slack_sdk import WebClient
    except ImportError:
        return {"ok": False, "error": "slack-sdk 미설치"}
    try:
        client = WebClient(token=bot_token)
        client.files_upload_v2(
            channel=channel_id,
            file=image_local_path,
            title=f"{platform} draft preview ({account})",
            initial_comment=(
                f"🖼️ 이미지 생성 미리보기 — {platform}/{account}\n"
                f"(아래 본문 카드에서 ✅/❌ 결정)"
            ),
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _notify_slack(draft_path: str, platform: str, account: str,
                  mode: str = "approval") -> dict:
    """slack_notifier.py subprocess. 결과 dict 반환."""
    if not os.path.isfile(SLACK_NOTIFIER):
        return {"ok": False, "error": f"slack_notifier 없음: {SLACK_NOTIFIER}"}
    cmd = [
        PYTHON_BIN, SLACK_NOTIFIER,
        "--draft-path", draft_path,
        "--platform", platform,
        "--account", account,
        "--mode", mode,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"slack_notifier subprocess 실패: {e}"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": err[:300] or f"exit {proc.returncode}"}

    try:
        parsed = json.loads(out.splitlines()[-1]) if out else {}
    except json.JSONDecodeError:
        parsed = {"raw": out}
    return {"ok": True, "result": parsed}


def _slack_api_post(method: str, payload: dict) -> dict:
    bot_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if not bot_token:
        return {"ok": False, "error": "no slack token"}
    url = f"https://slack.com/api/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {bot_token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="replace")
        res = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "unknown")}
    return res


def _slack_update(channel: str, ts: str, header: str, detail: str) -> None:
    if not channel or not ts:
        return
    _slack_api_post("chat.update", {
        "channel": channel,
        "ts": ts,
        "text": f"{header}: {detail[:120]}",
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{header}*\n{detail[:2900]}"},
        }],
    })


def _cooldown_until_from_error(error: str) -> str:
    m = re.search(r"COOLDOWN_UNTIL=([0-9T:\-]+Z)", error or "")
    if m:
        return m.group(1)
    m = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z)", error or "")
    return m.group(1) if m else ""


def _run_uploader_for_draft(path: str, platform: str, account: str,
                            meta: dict, body: str) -> dict:
    if platform == "x":
        return {"ok": False, "manual": True, "error": "X는 수동 업로드 모드입니다."}

    if platform == "threads":
        uploader = THREADS_UPLOADER
        cmd = [PYTHON_BIN, uploader, "--text", body, "--account", account]
        if meta.get("image_url"):
            cmd += ["--image-url", meta["image_url"], "--media-type", "image"]
    elif platform == "instagram":
        uploader = INSTAGRAM_UPLOADER
        cmd = [PYTHON_BIN, uploader, "--caption", body, "--account", account]
        if meta.get("image_url"):
            cmd += ["--image-url", meta["image_url"]]
        media_type = meta.get("media_type") or "IMAGE"
        cmd += ["--media-type", media_type]
    else:
        return {"ok": False, "error": f"알 수 없는 플랫폼: {platform}"}

    if not os.path.isfile(uploader):
        return {"ok": False, "error": f"uploader 없음: {uploader}"}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "uploader 타임아웃"}
    except Exception as e:
        return {"ok": False, "error": f"uploader subprocess 실패: {e}"}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        cooldown_until = _cooldown_until_from_error(err)
        return {
            "ok": False,
            "error": err or f"exit {proc.returncode}",
            "cooldown_until": cooldown_until,
        }

    try:
        parsed = json.loads(out.splitlines()[-1]) if out else {}
    except Exception:
        parsed = {}
    return {
        "ok": True,
        "permalink": parsed.get("permalink", "") or "",
        "post_id": (
            parsed.get("media_id")
            or parsed.get("thread_id")
            or parsed.get("tweet_id")
            or ""
        ),
        "raw_result": parsed,
        "raw": out[-400:],
    }


def _queue_draft(path: str, meta: dict, body: str, until: str, error: str) -> None:
    meta["status"] = "queued"
    meta["queued_until"] = until
    meta["queued_reason"] = "cooldown"
    meta["last_error"] = _escape_fm(error[:500])
    meta["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _rewrite_draft(path, meta, body)


def _auto_upload_after_slack(path: str, platform: str, account: str) -> dict:
    if platform == "x":
        meta, body = _parse_draft(path)
        meta["status"] = "manual_upload_required"
        _rewrite_draft(path, meta, body)
        _slack_update(
            meta.get("slack_channel", ""),
            meta.get("slack_ts", ""),
            "𝕏 수동 업로드 필요",
            "X API 크레딧 문제로 자동 업로드하지 않습니다. Slack의 문구와 이미지를 X에 직접 업로드하세요.",
        )
        return {"ok": True, "manual": True}

    meta, body = _parse_draft(path)
    _slack_update(
        meta.get("slack_channel", ""),
        meta.get("slack_ts", ""),
        "⏳ 자동 업로드 중",
        f"`{platform}` / `{account}`",
    )
    result = _run_uploader_for_draft(path, platform, account, meta, body)
    if result.get("ok"):
        permalink = result.get("permalink") or "(permalink 없음)"
        meta["status"] = "posted"
        meta["posted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if result.get("permalink"):
            meta["permalink"] = result["permalink"]
        if result.get("post_id"):
            meta["platform_post_id"] = result["post_id"]
        _rewrite_draft(path, meta, body)
        _slack_update(
            meta.get("slack_channel", ""),
            meta.get("slack_ts", ""),
            "✅ 자동 업로드 완료",
            f"`{platform}` / `{account}`\n🔗 {permalink}",
        )
        _update_generation_artifact(path, {
            "approval_status": "posted",
            "posted_at": meta["posted_at"],
            "permalink": result.get("permalink") or "",
            "platform_post_id": result.get("post_id") or "",
        })
        return result

    cooldown_until = result.get("cooldown_until") or ""
    if cooldown_until:
        _queue_draft(path, meta, body, cooldown_until, result.get("error", ""))
        _slack_update(
            meta.get("slack_channel", ""),
            meta.get("slack_ts", ""),
            "⏳ 쿨다운 큐 등록",
            (
                f"`{platform}` / `{account}`\n"
                f"Meta/X 제한 때문에 `{cooldown_until}` 이후 자동 재시도합니다."
            ),
        )
        _update_generation_artifact(path, {
            "approval_status": "queued",
            "last_error": result.get("error", "")[:500],
        })
        return {**result, "queued": True}

    meta["status"] = "failed"
    meta["last_error"] = _escape_fm(result.get("error", "")[:500])
    meta["failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _rewrite_draft(path, meta, body)
    _slack_update(
        meta.get("slack_channel", ""),
        meta.get("slack_ts", ""),
        "❌ 자동 업로드 실패",
        result.get("error", "unknown"),
    )
    return result


# ─── 회차 실행 ────────────────────────────────────────────────────────────

def _expand(value: str, env_key: str, default: list) -> list:
    if value == "all":
        raw = (os.environ.get(env_key) or ",".join(default)).strip()
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def _image_enabled_for(platform: str) -> bool:
    raw = (os.environ.get("IMAGE_PLATFORMS") or "instagram,threads,x").strip()
    enabled = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return platform.lower() in enabled or "all" in enabled


def run_round(platform: str, account: str, theme: str,
              dry_run: bool = False) -> dict:
    """단일 (platform, account) 회차 1번 실행."""
    lang = ACCOUNT_LANG_DEFAULT.get(account.lower(), "ko")

    trends = _fetch_trends(lang)
    style_context = _load_style_context(platform, account, lang)
    prompt = _build_persona_prompt(platform, account, lang, theme, trends, style_context)

    if dry_run:
        # 실제 LLM 호출 없이 더미 draft + Slack 알림 스킵.
        payload = {
            "text": f"[DRY-RUN {platform}/{account}] 테마={theme or '자동'} 트렌드={len(trends)}개 수집됨.",
            "hook": "[DRY-RUN]",
            "hashtags": [],
            "image_keyword": "",
        }
        _attach_style_meta(payload, style_context)
        _ensure_required_landing_cta(payload, account, lang)
        if _image_enabled_for(platform):
            img_prompt = _call_codex_image_prompt(
                platform, lang, payload["text"], payload["hook"], payload["hashtags"], style_context
            )
            if img_prompt.get("ok"):
                payload["image_keyword"] = img_prompt["image_keyword"]
                payload["image_prompt_raw"] = img_prompt.get("raw", "")
            else:
                payload["image_keyword"] = (
                    "cozy cafe table with coffee, travel notebook and phone, "
                    "soft natural light, candid social media photo, no text, no watermark"
                )
            local_path = _generate_codex_image(payload["image_keyword"])
            if local_path:
                payload["image_local_path"] = local_path
                image_url = _publish_image_url(local_path)
                if image_url:
                    payload["image_url"] = image_url
        path = _write_draft(platform, account, lang, theme, payload)
        artifact_id = _insert_generation_artifact(
            platform, account, lang, theme, path, payload, prompt
        )
        return {
            "ok": True,
            "draft_path": path,
            "artifact_id": artifact_id,
            "slack": {"skipped": "dry-run"},
            "trends_fetched": len(trends),
            "image_attached": bool(payload.get("image_local_path")),
            "dry_run": True,
        }

    result = _call_codex_content(prompt)
    if not result.get("ok") and os.environ.get("CONTENT_LLM_FALLBACK", "claude") == "claude":
        sys.stderr.write(f"⚠️ Codex 본문 생성 실패 — Claude 폴백: {result.get('error', '')}\n")
        result = _call_claude(prompt)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "본문 생성 실패"),
                "platform": platform, "account": account}
    _attach_style_meta(result, style_context)
    _ensure_required_landing_cta(result, account, lang)

    # 이미지 자동 생성:
    #   게시글 본문을 기반으로 이미지 프롬프트를 별도 생성한 뒤 이미지를 만든다.
    if _image_enabled_for(platform):
        img_prompt = _call_codex_image_prompt(
            platform,
            lang,
            result.get("text", ""),
            result.get("hook", ""),
            result.get("hashtags") or [],
            style_context,
        )
        if img_prompt.get("ok"):
            result["image_keyword"] = img_prompt["image_keyword"]
            result["image_prompt_raw"] = img_prompt.get("raw", "")
            local_path = _generate_codex_image(result["image_keyword"])
            if local_path:
                result["image_local_path"] = local_path
                image_url = _publish_image_url(local_path)
                if image_url:
                    result["image_url"] = image_url
            else:
                result["image_error"] = "external_image_generation_failed"
        else:
            result["image_error"] = img_prompt.get("error", "image_prompt_failed")

    path = _write_draft(platform, account, lang, theme, result)
    artifact_id = _insert_generation_artifact(
        platform, account, lang, theme, path, result, prompt
    )

    # IG + 로컬 이미지 있으면 Slack files_upload_v2 로 네이티브 미리보기 먼저 게시.
    # files.upload 와 interactive blocks (✅/❌) 동시 사용 X → 두 단계 분리.
    slack_upload = {"skipped": "no local image"}
    local_path = result.get("image_local_path", "")
    if _image_enabled_for(platform) and local_path:
        slack_upload = _slack_upload_preview(local_path, platform, account)

    slack_mode = "manual" if platform == "x" else "auto"
    slack = _notify_slack(path, platform, account, mode=slack_mode)
    if isinstance(slack, dict):
        slack["upload"] = slack_upload
        slack_result = slack.get("result") if isinstance(slack.get("result"), dict) else {}
        _update_generation_artifact(path, {
            "slack_channel": slack_result.get("channel"),
            "slack_ts": slack_result.get("ts"),
            "slack_upload_ok": bool(slack_upload.get("ok")),
        })
    auto_upload = {"skipped": "slack_not_failed"}
    if slack.get("ok"):
        auto_upload = _auto_upload_after_slack(path, platform, account)
    return {
        "ok": True,
        "draft_path": path,
        "artifact_id": artifact_id,
        "slack": slack,
        "auto_upload": auto_upload,
        "trends_fetched": len(trends),
        "image_attached": bool(result.get("image_url")),
        "image_local_path": local_path,
        "platform": platform,
        "account": account,
    }


def main() -> int:
    _load_env_file(ENV_PATH)
    _load_env_file(ENV_PATH_LEGACY)  # 옛 money-ai 위치 폴백

    ap = argparse.ArgumentParser(description="박재범 자율 컨텐츠 파이프라인")
    ap.add_argument("--platform", required=True,
                    help="threads | instagram | x | all (콤마 구분 가능)")
    ap.add_argument("--account", required=True,
                    help="jp | kr | all (콤마 구분 가능)")
    ap.add_argument("--theme", default="",
                    help="(선택) 강제 테마. 없으면 트렌드에서 자동")
    ap.add_argument("--dry-run", action="store_true",
                    help="LLM·Slack 호출 없이 더미 draft 만 생성")
    args = ap.parse_args()

    platforms = _expand(
        args.platform, "ROUTINE_PLATFORMS",
        ["threads", "instagram", "x"],
    )
    accounts = _expand(
        args.account, "ROUTINE_ACCOUNTS",
        ["jp", "kr"],
    )

    # 화이트리스트 필터
    valid_platforms = {"threads", "instagram", "x"}
    platforms = [p for p in platforms if p in valid_platforms]
    if not platforms:
        print(json.dumps({"status": "error",
                          "reason": "no valid platforms"}, ensure_ascii=False))
        return 1
    if not accounts:
        print(json.dumps({"status": "error",
                          "reason": "no accounts"}, ensure_ascii=False))
        return 1

    results = []
    errors = []
    notified = 0
    for platform in platforms:
        for account in accounts:
            r = run_round(platform, account, args.theme, dry_run=args.dry_run)
            results.append({
                "platform": platform,
                "account": account,
                "ok": r.get("ok"),
                "draft_path": r.get("draft_path"),
                "artifact_id": r.get("artifact_id"),
                "slack": r.get("slack"),
                "auto_upload": r.get("auto_upload"),
                "error": r.get("error"),
            })
            if not r.get("ok"):
                errors.append(f"{platform}/{account}: {r.get('error')}")
            elif r.get("slack", {}).get("ok"):
                notified += 1

    summary = {
        "status": "completed" if not errors else "partial",
        "drafts_created": sum(1 for x in results if x["ok"]),
        "slack_notified": notified,
        "total_attempts": len(results),
        "errors": errors,
        "details": results,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
