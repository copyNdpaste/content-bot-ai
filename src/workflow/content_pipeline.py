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
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
# content-bot-ai 신규 레이아웃: src/workflow/ → 두 단계 위가 repo root
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SLACK_NOTIFIER = os.path.join(REPO_ROOT, "src", "slack", "slack_notifier.py")
# drafts 는 repo 안 drafts/ (gitignored). tokens.json 은 src/auth/ 에.
DRAFTS_DIR = os.path.join(REPO_ROOT, "drafts")
# .env 는 repo root 에. (옛 money-ai 의 _company/_agents/instagram/.env 도 폴백)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
ENV_PATH_LEGACY = os.path.join(REPO_ROOT, "_company", "_agents", "instagram", ".env")

CLAUDE_MODEL = "claude-opus-4-7"  # 박재범 heavy tier
CLAUDE_TIMEOUT_SEC = 180
PYTHON_BIN = sys.executable or "/opt/homebrew/bin/python3"

PLATFORM_LIMITS = {
    "x": 280,
    "threads": 500,
    "instagram": 2200,  # 캡션
}

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


# ─── Claude CLI 호출 ─────────────────────────────────────────────────────

BRAND_NAME = "OnlyFriends"
LANDING_URL = "https://onlyfriends.tryproo.com/"

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


def _pick_soft_mention(lang: str) -> str:
    """자연스러운 brand mention 1개 — 광고 톤 X, 글 끝에 슬쩍."""
    pool = SOFT_MENTION_KO if lang == "ko" else SOFT_MENTION_JA
    return random.choice(pool)


def _build_persona_prompt(platform: str, account: str, lang: str,
                          theme: str, trends: list) -> str:
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

    # 페르소나 — 광고 카피라이터 X. 'OnlyFriends 운영팀의 2030 여성 직원' 1인칭 빙의.
    # 핵심: 본인이 OnlyFriends (한일 친구 매칭) 의 일부라서 자연스럽게 "우리 서비스" 라고 말함.
    persona_ko = (
        "당신은 '광고 카피라이터' 가 아닙니다.\n"
        "당신의 역할은 — **OnlyFriends (한일 친구 매칭 서비스) 의 20대 후반 한국인 운영팀 멤버** 로서, "
        "본인 일상·매칭 사례·여행 이야기를 Threads / X / Instagram 에 올리는 것입니다.\n"
        "\n"
        "📌 핵심 정체성:\n"
        "  - 본인 계정 = OnlyFriends 공식 (jp 계정은 일본팀 / kr 계정은 한국팀)\n"
        "  - 일반 사용자가 추천하는 톤 X — '우리 서비스에 이런 일 있었음' 1인칭 운영자 톤 O\n"
        "  - 매일 매칭 사례·후기·여행기·일상이 자연스럽게 소재가 됨\n"
        "  - 광고체 X, 친구한테 회사 얘기 하는 느낌\n"
    )
    persona_ja = (
        "あなたは「広告コピーライター」ではありません。\n"
        "あなたの役割は — **OnlyFriends (日韓フレンドマッチング) の 20代後半・日本人運営チームメンバー** "
        "として、自分の日常・マッチング事例・旅行話を Threads / X / Instagram に投稿することです。\n"
        "\n"
        "📌 核心アイデンティティ:\n"
        "  - 自分のアカウント = OnlyFriends 公式 (jp = 日本チーム / kr = 韓国チーム)\n"
        "  - 一般ユーザーが推薦するトーン X — '私たちのサービスにこんな話があったよ' 一人称運営者トーン O\n"
        "  - 毎日のマッチング事例・口コミ・旅行記・日常が自然に話題になる\n"
        "  - 広告調 X、友達に会社の話する感じ\n"
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
            f"X — 짧고 툭 던지는 느낌. 혼잣말 많이. 해시태그 거의 없음. "
            f"**{limit}자 이내 (URL 포함)**."
        ),
        "threads": (
            f"Threads — 살짝 스토리텔링. 공감형. 댓글 달고 싶게 끝맺음. "
            f"{limit}자 이내. 해시태그 0~2개."
        ),
        "instagram": (
            f"Instagram — 감성 일상 느낌. 사진 설명 같은 톤. 정보 전달형 금지. "
            f"본문 {limit}자 이내. 해시태그 5~10개 (도배 X, 자연스럽게). "
            f"본문 분위기에 어울리는 **AI 이미지 생성용 영문 프롬프트** 도 image_keyword 에 함께 출력. "
            f"\n"
            f"🚨 매우 중요 — 사람은 OK, 단 얼굴 자체는 반드시 가려져 있어야 함:\n"
            f"  FLUX 무료가 사람 얼굴·이목구비 그리면 80% 망가짐. 사람 등장은 일상감 살리되 "
            f"얼굴은 항상 어떤 식으로든 가려서 안전하게.\n"
            f"\n"
            f"좋은 얼굴 가림 방법 (다양하게 섞기):\n"
            f"  - 폰으로 얼굴 가리기 (셀카 찍는 거울 셀카, 폰이 얼굴 가림)\n"
            f"  - 손으로 얼굴 가리기 (수줍게, 햇빛 가리는 척, 입가에 손)\n"
            f"  - 모자·후드·마스크로 얼굴 가림 (yk2k 패션, 비니 + 마스크)\n"
            f"  - 머리카락이 옆모습 가림 (긴 머리 늘어뜨림)\n"
            f"  - 카메라·책·메뉴판·꽃다발이 얼굴 일부 가림\n"
            f"  - 뒷모습·옆모습 (얼굴 안 보임)\n"
            f"  - 실루엣 (역광·창가)\n"
            f"  - 거울 셀카 (카메라가 얼굴 자리)\n"
            f"  - 음식·커피 컵이 화면 전면, 사람 손만 보임\n"
            f"\n"
            f"형식: 영문 20~35 단어 + 끝에 반드시 'face hidden, no visible face, "
            f"face out of frame, film grain, 35mm, instagram aesthetic, no text, no watermark' 첨부. "
            f"\n"
            f"좋은 예시:\n"
            f"  - 'young korean woman taking mirror selfie in cozy cafe, phone covering her face "
            f"completely, oversized hoodie, k-beauty aesthetic, soft afternoon light, film grain, "
            f"35mm, face hidden by phone, no visible face, instagram aesthetic, no text, no watermark'\n"
            f"  - 'girl from behind, long hair, sitting at tokyo cafe window, holding matcha "
            f"latte, only back of head visible, soft window light, film grain, 35mm, no visible "
            f"face, instagram aesthetic, no text, no watermark'\n"
            f"  - 'two friends seoul cafe, both holding cameras covering their faces, oversized "
            f"sweaters, candid laughter moment, soft morning light, film grain, 35mm, faces "
            f"hidden by cameras, no visible faces, instagram aesthetic, no text, no watermark'\n"
            f"  - 'silhouette of girl walking through shinjuku at night, neon lights backlit, "
            f"only outline visible, cinematic, 35mm film, face out of frame, no visible face, "
            f"instagram aesthetic, no text, no watermark'\n"
            f"  - 'hands holding korean snack and matcha latte on rustic table, only forearms "
            f"and hands visible, cropped composition, soft morning light, film grain, no face, "
            f"instagram aesthetic, no text, no watermark'\n"
            f"\n"
            f"❌ 절대 금지 키워드: 'portrait', 'close-up face', 'smiling face', "
            f"'eye contact', 'looking at camera' (얼굴 정면), 'two girls smiling at camera'"
        ),
    }.get(platform, "")

    # CTA — 자연스럽게, 광고 X
    cta_rule_ko = (
        "💬 CTA 규칙:\n"
        "  - CTA 는 '광고' 가 아니라 **글 흐름상 자연스럽게 마지막에 슬쩍**\n"
        f"  - 좋은 예 (참고): '{soft_mention}'\n"
        "  - 나쁜 예: '지금 가입하세요' '친구 만들고 싶다면?' '당신도 원하지 않나요?'\n"
        f"  - URL ({LANDING_URL}) 은 본문 톤에 어울리면 살짝 첨부, 어색하면 생략 OK\n"
        "  - 브랜드 언급은 최소화. 본문보다 존재감 강하면 실패\n"
    )
    cta_rule_ja = (
        "💬 CTA ルール:\n"
        "  - CTA は「広告」ではなく **流れ的に自然に最後にそっと**\n"
        f"  - 良い例 (参考): '{soft_mention}'\n"
        "  - 悪い例: '今すぐ登録' '友達作りたい人は?' 'あなたも欲しくないですか?'\n"
        f"  - URL ({LANDING_URL}) は本文に馴染めばそっと、不自然なら省略 OK\n"
        "  - ブランド言及は最小限。本文より存在感強かったら失敗\n"
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

    return (
        f"{persona}\n"
        f"{goal}\n"
        f"{forbidden}\n"
        f"{style}\n"
        f"{audience}\n"
        f"{cta_rule}\n"
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
            '"image_keyword": "<IG 일 때만 — 영문 15~30단어, AI 이미지 생성 프롬프트 (사람·장면·조명·분위기 + no text, no watermark),'
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
        "--model", CLAUDE_MODEL,
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


# ─── AI 이미지 생성 (Pollinations.ai — 무료, 키 0) ──────────────────────
# Pollinations 는 GET 요청 자체가 이미지 생성·반환. URL 을 그대로 IG 업로더에
# 넘기면 Meta API 가 그 URL 에서 다운로드해 게시. seed 로 재현성 보장 가능.
# Fal.ai 폴백은 다음 라운드에 추가 예정.

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def _generate_pollinations_image(prompt: str, *, width: int = 1080, height: int = 1080,
                                 model: str = "flux", seed: int = None) -> str | None:
    """Pollinations.ai 로 AI 이미지 URL 생성.

    - 프롬프트 비어있음 → None
    - URL 자체가 GET 호출 시 이미지 binary 반환 (Meta API 가 받아서 게시 가능)
    - seed 지정 가능 (None 이면 자동 랜덤)
    - 모델: flux (FLUX.1-schnell, 기본 추천), turbo (더 빠르지만 품질 낮음)
    - 호출 X (URL 만 빌드) — 실제 이미지 생성은 Meta 가 다운로드 시 일어남
    - 프롬프트에 사람 회피·negative 가이드 자동 첨부 (FLUX 무료가 사람 얼굴 망침 회피)
    """
    p = (prompt or "").strip()
    if not p:
        return None
    # 자동 safety suffix — 사람 OK 단 얼굴 가려야 (FLUX 가 얼굴 잘 못 그려서)
    safety_suffix = (
        ", face hidden, no visible face, face out of frame, "
        "no eye contact, film grain, 35mm, instagram aesthetic, "
        "no text, no watermark, no logo"
    )
    if "face hidden" not in p.lower() and "no visible face" not in p.lower() \
            and "no people" not in p.lower():
        p = p + safety_suffix
    if seed is None:
        seed = random.randint(1, 1_000_000_000)
    qs = urllib.parse.urlencode({
        "width": width,
        "height": height,
        "model": model,
        "seed": seed,
        "nologo": "true",
        "enhance": "true",
    })
    encoded = urllib.parse.quote(p, safe="")
    return f"{POLLINATIONS_BASE}/{encoded}?{qs}"


# ─── draft 저장 ───────────────────────────────────────────────────────────

def _ensure_drafts_dir() -> None:
    os.makedirs(DRAFTS_DIR, exist_ok=True)


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _escape_fm(v) -> str:
    """frontmatter 안전 직렬화 (단일 라인)."""
    if isinstance(v, list):
        return ", ".join(str(x).replace("\n", " ") for x in v)
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s


def _write_draft(platform: str, account: str, lang: str, theme: str,
                 payload: dict) -> str:
    _ensure_drafts_dir()
    ts = _now_stamp()
    filename = f"{platform}-{ts}-{account}.md"
    path = os.path.join(DRAFTS_DIR, filename)

    # IG 일 때만 이미지 첨부 흐름 작동. 다른 채널은 text 유지.
    image_url = str(payload.get("image_url", "") or "").strip()
    image_keyword = str(payload.get("image_keyword", "") or "").strip()
    if platform == "instagram" and image_url:
        media_type = "IMAGE"
    else:
        media_type = "text"

    fm = {
        "status": "pending",
        "platform": platform,
        "account": account,
        "lang": lang,
        "theme": _escape_fm(theme or ""),
        "hook": _escape_fm(payload.get("hook", "")),
        "hashtags": _escape_fm(payload.get("hashtags") or []),
        "media_type": media_type,
        "image_url": _escape_fm(image_url),
        "image_keyword": _escape_fm(image_keyword),
        "created_at": ts,
        "source": "content_pipeline_v1",
    }
    body = payload.get("text", "").strip() + "\n"

    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ─── Slack 알림 호출 ──────────────────────────────────────────────────────

def _notify_slack(draft_path: str, platform: str, account: str) -> dict:
    """slack_notifier.py subprocess. 결과 dict 반환."""
    if not os.path.isfile(SLACK_NOTIFIER):
        return {"ok": False, "error": f"slack_notifier 없음: {SLACK_NOTIFIER}"}
    cmd = [
        PYTHON_BIN, SLACK_NOTIFIER,
        "--draft-path", draft_path,
        "--platform", platform,
        "--account", account,
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


# ─── 회차 실행 ────────────────────────────────────────────────────────────

def _expand(value: str, env_key: str, default: list) -> list:
    if value == "all":
        raw = (os.environ.get(env_key) or ",".join(default)).strip()
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def run_round(platform: str, account: str, theme: str,
              dry_run: bool = False) -> dict:
    """단일 (platform, account) 회차 1번 실행."""
    lang = ACCOUNT_LANG_DEFAULT.get(account.lower(), "ko")

    trends = _fetch_trends(lang)
    prompt = _build_persona_prompt(platform, account, lang, theme, trends)

    if dry_run:
        # 실제 LLM 호출 없이 더미 draft + Slack 알림 스킵
        # IG 일 때 image_keyword 추출 흐름은 실제로 시도 (Pexels 키 있으면 호출됨)
        dummy_keyword = ""
        if platform == "instagram":
            # 트렌드 첫 키워드 (있으면) 또는 기본값을 영문 키워드처럼 사용
            dummy_keyword = "tokyo cafe friends" if lang == "ja" else "seoul cafe friends"
        payload = {
            "text": f"[DRY-RUN {platform}/{account}] 테마={theme or '자동'} 트렌드={len(trends)}개 수집됨.",
            "hook": "[DRY-RUN]",
            "hashtags": [],
            "image_keyword": dummy_keyword,
        }
        if platform == "instagram" and dummy_keyword:
            img = _generate_pollinations_image(dummy_keyword)
            if img:
                payload["image_url"] = img
        path = _write_draft(platform, account, lang, theme, payload)
        return {
            "ok": True,
            "draft_path": path,
            "slack": {"skipped": "dry-run"},
            "trends_fetched": len(trends),
            "image_attached": bool(payload.get("image_url")),
            "dry_run": True,
        }

    result = _call_claude(prompt)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "claude 실패"),
                "platform": platform, "account": account}

    # IG 일 때만 Pexels 로 이미지 자동 첨부
    if platform == "instagram":
        kw = result.get("image_keyword", "") or ""
        if kw:
            img_url = _generate_pollinations_image(kw)
            if img_url:
                result["image_url"] = img_url

    path = _write_draft(platform, account, lang, theme, result)
    slack = _notify_slack(path, platform, account)
    return {
        "ok": True,
        "draft_path": path,
        "slack": slack,
        "trends_fetched": len(trends),
        "image_attached": bool(result.get("image_url")),
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
                "slack": r.get("slack"),
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
