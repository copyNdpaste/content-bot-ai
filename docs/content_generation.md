# 콘텐츠 문구 생성

content pipeline 은 본문 생성에 로컬 Codex CLI 를 먼저 사용한다.
Codex 호출이 실패하면 기본값으로 기존 Claude CLI 를 폴백으로 사용한다.

## 설정

```dotenv
CODEX_BIN=/Users/hoony/.local/bin/codex
CODEX_TEXT_MODEL=
CODEX_TEXT_TIMEOUT_SEC=180
CONTENT_LLM_FALLBACK=claude
```

## DB 기반 스타일 변주

`content-bot` Supabase DB의 `personas`, `audiences`, `content_concepts`,
`persona_audience_mapping`, `time_strategies`를 읽어 페르소나/청자/컨셉/시간대 톤을
프롬프트에 주입한다. 실제 키는 `.env`에만 둔다.

```dotenv
CONTENT_STYLE_SOURCE=auto
CONTENT_STYLE_SUPABASE_URL=
CONTENT_STYLE_SUPABASE_KEY=
```

Supabase 설정이 없거나 읽기에 실패하면 `config/content_styles.json`의 로컬 변주값만
사용한다.

폴백을 끄려면:

```dotenv
CONTENT_LLM_FALLBACK=none
```

## 채널 톤

- X: 짧게 툭 던지는 관찰/혼잣말. 해시태그와 URL은 거의 쓰지 않음.
- Threads: 3~6줄 정도의 짧은 이야기. 마지막은 댓글이 붙을 수 있게 살짝 열어둠.
- Instagram: 사진 밑에 붙는 자연스러운 일상 캡션. 해시태그는 4~7개 정도.

모든 채널에서 광고 문장, 캠페인 문장, 서비스 소개문처럼 보이면 실패로 본다.
