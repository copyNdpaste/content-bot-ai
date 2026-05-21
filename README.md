# Content Bot AI 🇰🇷🇯🇵

> 한일 SNS 자동 컨텐츠 봇 — Threads · Instagram · X 멀티 계정.
> 2030 한국·일본 여성 타겟. 광고 카피 X, 실사용자 시뮬레이션 톤.

## 🔁 흐름

```
[ 트렌드 수집 ] → [ Claude Opus 4.7 컨텐츠 생성 ] → [ draft 저장 ]
                          ↓
[ Slack ✅/❌ 카드 ] ← [ 이미지 자동 첨부 (Pollinations·Fal) ]
        ↓ 승인
[ Threads / IG / X 자동 게시 (jp/kr 계정 분기) ]
```

## ⚡ 빠른 시작

```bash
# 0) 의존성 (Python 3.10+)
python3 -m venv .venv
.venv/bin/pip install slack-sdk

# 1) 환경변수 작성
cp .env.example .env
# .env 열어서 토큰 채우기 (가이드: docs/token_manager.md, docs/x_uploader.md, docs/slack_setup.md)

# 2) 토큰 부트스트랩 (60일 장기 토큰 자동 발급)
.venv/bin/python3 src/auth/token_manager.py --bootstrap

# 3) 즉시 한 회차 테스트
./scripts/test-jp.sh threads
./scripts/test-kr.sh instagram

# 4) 자율 회차 + 토큰 자동 갱신 + Slack 워커 백그라운드 데몬
cp launchd/com.contentbot.*.plist ~/Library/LaunchAgents/
for n in token-refresh slack-worker content-scheduler; do
  launchctl load ~/Library/LaunchAgents/com.contentbot.${n}.plist
done
```

## 📁 구조

```
src/
├── workflow/   — content_pipeline (트렌드+Claude+draft+Slack 통합 오케스트레이터)
├── uploaders/  — threads / instagram / x 각 플랫폼 API
├── slack/      — notifier (draft → Slack 카드) + approval_worker (버튼 → 자동 게시)
└── auth/       — token_manager (60일 토큰 자동 발급·갱신)

docs/          — 토큰 발급 가이드 (Meta·X·Slack·Pexels) + 도구별 README
scripts/       — test-jp.sh / test-kr.sh (한 회차 수동 트리거)
launchd/       — macOS 백그라운드 데몬 plist 3개
```

## 🎯 컨텐츠 톤

광고 카피 X. 2030 한일 여성 SNS 유저 1인칭 빙의:
- 결론 명확히 안 냄 OK
- 감정 애매 OK
- 흐름 약간 끊김 OK
- "한국 친구 / 일본 친구" 만들고 싶은 감정 자연스럽게 유도
- CTA 는 본문보다 약하게, 글 끝에 슬쩍

자세한 프롬프트 규칙: `src/workflow/content_pipeline.py` 의 `_build_persona_prompt`.

## 🔒 보안

- `.env`, `tokens.json`, `drafts/` 모두 `.gitignore`
- 토큰은 OAuth 후 한 번만 입력 → 60일 자동 갱신 (launchd 매일 04:00)
- 모든 시크릿은 평문 stdout/log 노출 X

## 🤝 Companion Repo

운영자 도구 (VS Code 확장 + 9 에이전트 회사) 는 별도:
- 👉 https://github.com/copyNdpaste/agent-os-ai
