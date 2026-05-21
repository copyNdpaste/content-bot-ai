# content_pipeline.py — 박재범 자율 컨텐츠 파이프라인

박재범 에이전트가 한 회차 동안 수행하는 전 과정의 메인 오케스트레이터.

## 흐름

1. **트렌드 수집** — Google Trends RSS (KR/JP) 에서 최근 키워드 추출. 외부 의존성 0 (urllib).
2. **초안 생성** — Claude CLI subprocess (`--model claude-opus-4-7`, 박재범 heavy tier) 로 박재범 페르소나 프롬프트 + 채널별 형식 가이드 + 트렌드를 합쳐 JSON 응답 받음.
3. **draft 저장** — `_company/_agents/instagram/tools/drafts/{platform}-{ts}-{account}.md` (frontmatter `status: pending`).
4. **Slack 알림** — 각 draft 마다 `slack_notifier.py` 호출 → 승인 카드 게시 → 사장님이 ✅/❌ 누름.

## 사용법

```bash
# 단일 (platform, account)
python3 assets/tool-seeds/workflow/content_pipeline.py \
  --platform threads --account jp

# 전체 회차 (3 채널 × 2 계정 = 6장)
python3 assets/tool-seeds/workflow/content_pipeline.py \
  --platform all --account all

# 테마 강제 지정
python3 assets/tool-seeds/workflow/content_pipeline.py \
  --platform instagram --account kr --theme "벚꽃 시즌 캡션"

# dry-run (LLM·Slack 호출 X, 더미 draft 만 생성)
python3 assets/tool-seeds/workflow/content_pipeline.py \
  --platform x --account jp --dry-run
```

## 환경변수

`_company/_agents/instagram/.env` 에서 자동 로딩 (기존 launchd env 가 더 강함).

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `ROUTINE_PLATFORMS` | `threads,instagram,x` | `--platform all` 일 때 펼쳐질 목록 |
| `ROUTINE_ACCOUNTS` | `jp,kr` | `--account all` 일 때 펼쳐질 목록 |
| `ROUTINE_LANGS` | `ko,ja` | 계정 기본 언어 폴백 |
| `SLACK_BOT_TOKEN` | — | 있으면 Slack 카드, 없으면 Telegram 폴백 |
| `TELEGRAM_*` | — | Slack 폴백용 알림 |

## 출력 (stdout JSON)

```json
{
  "status": "completed",
  "drafts_created": 6,
  "slack_notified": 6,
  "total_attempts": 6,
  "errors": [],
  "details": [...]
}
```

`partial` = 일부 실패, `error` = 인자 오류.

## 트러블슈팅

- **`claude CLI 미설치`** → `which claude` 확인. Claude Code (CLI) 가 PATH 에 있어야 함.
- **`claude CLI 타임아웃`** → 기본 180s. 모델 지연시 잠시 후 재시도.
- **`slack_notifier 없음`** → `assets/tool-seeds/instagram/slack_notifier.py` 존재 확인.
- **trends 0개** → Google Trends RSS 일시 차단 가능. 무시하고 진행 (테마 자유 모드로 폴백).
- **빈 stdout** → claude 응답 없음. `claude -p "test"` 로 직접 확인.
- **frontmatter 깨짐** → `_escape_fm` 단일 라인 강제. 본문 내 `---` 는 그대로 두니 안전.
