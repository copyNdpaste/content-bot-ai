# Content Bot AI

한일 친구 매칭 서비스 **OnlyFriends**의 SNS 콘텐츠를 생성, 검수, 게시, 성과 기록까지 자동화하는 Python 워크플로우입니다.

대상 채널은 Threads, Instagram, X이고 계정은 `kr`, `jp`를 기본으로 합니다. 생성된 글은 광고 카피처럼 보이지 않도록 2030 한일 여성 사용자의 일상형 톤으로 작성하며, 게시 전후로 필수 랜딩 CTA와 플랫폼별 글자 수 제한을 코드에서 다시 보정합니다.

## 현재 동작 흐름

### 자동 회차

`src/workflow/scheduler.py`가 KST 활성 시간대에 2~3시간 랜덤 간격으로 `scripts/generate_platform_pack.py`를 실행합니다.

1. 계정별로 Instagram용 기준 글을 생성합니다.
2. 같은 주제와 감정선을 유지한 채 Threads/X용 문구로 변환합니다.
3. 회차별 공유 이미지 1장을 생성합니다.
4. 이미지를 공개 HTTPS URL로 업로드합니다.
5. 플랫폼별 draft를 `drafts/`에 저장합니다.
6. Slack에 검수 카드와 이미지 미리보기를 올립니다.
7. Instagram/Threads는 토큰이 있으면 자동 게시하고, X는 기본적으로 수동 검수 모드로 둡니다.

### 단건 실행

`src/workflow/content_pipeline.py`는 플랫폼/계정 단위로 한 장씩 생성합니다. `scripts/test-kr.sh`, `scripts/test-jp.sh`가 이 경로를 감싼 빠른 테스트 진입점입니다.

## 빠른 시작

```bash
cd /Users/hoony/projects/content-bot-ai

python3 -m venv .venv
.venv/bin/pip install -e .

cp .env.example .env
# .env에 Meta, X, Slack, Codex, OpenAI, Cloudinary 값을 채웁니다.
```

최초 Meta 토큰 부트스트랩:

```bash
.venv/bin/python src/auth/token_manager.py --bootstrap
.venv/bin/python src/auth/token_manager.py --status
```

한 번만 생성해 보기:

```bash
./scripts/test-kr.sh instagram
./scripts/test-jp.sh threads

# LLM/Slack 호출 없이 draft 형식만 점검
./scripts/test-kr.sh x --dry-run
```

계정별 플랫폼 묶음 생성:

```bash
.venv/bin/python scripts/generate_platform_pack.py \
  --accounts kr,jp \
  --platforms instagram,threads,x
```

## 주요 환경변수

`.env.example`를 기준으로 `.env`를 작성합니다. 실제 `.env`, `tokens.json`, `drafts/`, `.runtime/`, `var/`는 커밋하지 않습니다.

| 영역 | 변수 |
| --- | --- |
| Meta 토큰 | `META_APP_ID`, `META_APP_SECRET`, `META_THREADS_SHORT_TOKEN_*`, `META_IG_SHORT_TOKEN_*` |
| X OAuth | `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_OAUTH_TOKEN_*`, `X_OAUTH_REFRESH_TOKEN_*` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID` |
| 본문 생성 | `CODEX_BIN`, `CODEX_TEXT_MODEL`, `CODEX_TEXT_TIMEOUT_SEC`, `CONTENT_LLM_FALLBACK` |
| 스타일 소스 | `CONTENT_STYLE_SOURCE`, `CONTENT_STYLE_SUPABASE_URL`, `CONTENT_STYLE_SUPABASE_KEY` |
| 이미지 생성 | `CODEX_IMAGE_COMMAND`, `CODEX_IMAGE_TIMEOUT_SEC`, `IMAGE_PLATFORMS` |
| 이미지 URL | `IMAGE_PUBLIC_URL_COMMAND`, `CLOUDINARY_*` |
| 자동 회차 | `ROUTINE_MIN_HOURS`, `ROUTINE_MAX_HOURS`, `ROUTINE_ACTIVE_START_HOUR`, `ROUTINE_ACTIVE_END_HOUR` |
| 대상 제어 | `ROUTINE_PLATFORMS`, `ROUTINE_ACCOUNTS`, `ROUTINE_DISABLED_TARGETS`, `ROUTINE_LANGS` |

`CONTENT_STYLE_SOURCE=auto`이면 Supabase 설정이 있을 때 DB 스타일 데이터를 쓰고, 없으면 `config/content_styles.json`으로 폴백합니다.

## launchd 데몬

macOS 백그라운드 실행은 `launchd/`의 plist를 `~/Library/LaunchAgents/`에 복사해서 사용합니다.

```bash
cp launchd/com.contentbot.*.plist ~/Library/LaunchAgents/

for name in token-refresh slack-worker content-scheduler queued-upload-worker performance-report-worker; do
  launchctl load ~/Library/LaunchAgents/com.contentbot.${name}.plist
done
```

역할:

| 데몬 | 역할 |
| --- | --- |
| `token-refresh` | Meta 장기 토큰 만료 전 자동 갱신 |
| `slack-worker` | Slack Socket Mode 승인/반려/수정 버튼 처리 |
| `content-scheduler` | KST 활성 시간대에 콘텐츠 회차 자동 실행 |
| `queued-upload-worker` | 플랫폼 쿨다운으로 `queued` 된 draft 재시도 |
| `performance-report-worker` | posted draft의 Instagram/Threads 성과 수집과 Slack 브리핑 |

로그는 주로 `/tmp/contentbot-*.log`에 남습니다.

## 디렉터리 구조

```text
src/
  domain/       순수 비즈니스 규칙: CTA, 글자 수, 스케줄 간격
  application/  draft markdown 조립 같은 유스케이스 보조
  workflow/     콘텐츠 생성, 스케줄러, 큐 워커, 성과 리포트
  slack/        Slack 알림과 인터랙티브 승인 워커
  uploaders/    Threads, Instagram, X 업로더
  auth/         Meta 토큰 발급/갱신

scripts/        수동 실행, 이미지 생성, Cloudinary 업로드, 피드백 기록
launchd/        macOS LaunchAgents plist
docs/           세부 운영 문서
config/         로컬 스타일 변주 설정
tests/          BDD 성격의 publication guardrail 테스트
```

## 운영 규칙

- 필수 CTA는 `src/domain/content_rules.py`에서 강제합니다.
- X는 280자, Threads는 500자, Instagram은 2200자 제한을 코드에서 보정합니다.
- Instagram/Threads 업로드 중 쿨다운이 감지되면 draft가 `queued` 상태로 남고 큐 워커가 재시도합니다.
- 생성 본문, 이미지 프롬프트, 이미지 URL, Slack 결과는 Supabase `content_generation_artifacts`에 저장할 수 있습니다.
- 수동 성과 피드백은 `scripts/record_feedback_event.py`로 `content_feedback_events`에 기록할 수 있습니다.

## 테스트

기본 테스트는 외부 서비스 없이 실행됩니다.

```bash
PYTHONPYCACHEPREFIX=/tmp/contentbot-ai-pycache \
  python3 -m unittest discover -s tests -p "test_*.py"
```

현재 테스트는 필수 CTA 정규화, 플랫폼 글자 수 제한, 스케줄러 최소 간격, application layer draft 조립 규칙을 확인합니다.

## 참고 문서

- `docs/token_manager.md`: Meta 토큰 부트스트랩과 갱신
- `docs/image_generation.md`: GPT Image와 Cloudinary 연결
- `docs/slack_setup.md`: Slack 앱/Socket Mode 설정
- `docs/instagram_uploader.md`, `docs/threads_uploader.md`, `docs/x_uploader.md`: 플랫폼별 업로더
- `docs/content_learning.md`: 생성 아티팩트와 성과 피드백 저장
