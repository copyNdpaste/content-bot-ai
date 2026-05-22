# 콘텐츠 학습 루프

현재 `content-bot-ai`는 생성된 본문, 이미지 프롬프트, 이미지 URL, Slack 전송 결과를
Supabase의 `content_generation_artifacts` 테이블에 저장하도록 구성되어 있다.

먼저 `content-bot` 프로젝트의 아래 SQL을 Supabase SQL Editor에서 한 번 실행해야 한다.

```text
/Users/hoony/projects/content-bot/sql/migrations/20260522_content_learning_artifacts.sql
```

저장되는 핵심 데이터:

- 생성 본문, hook, hashtags
- 본문 생성 프롬프트
- 이미지 생성 프롬프트
- 이미지 모델, quality, size
- Cloudinary image URL
- persona/audience/concept/style 선택값
- Slack 채널/ts/upload 상태

성과 피드백은 수동 입력 또는 추후 수집기로 `content_feedback_events`에 저장한다.

```bash
.venv/bin/python scripts/record_feedback_event.py \
  --draft-path drafts/instagram-YYYYMMDD-HHMMSS-kr.md \
  --platform instagram \
  --views 1000 \
  --likes 120 \
  --comments 8 \
  --shares 3 \
  --saves 20 \
  --comment-samples '[{"text":"사진 예쁘다"},{"text":"어디야?"}]'
```

이 데이터가 쌓이면 persona, concept, image prompt, 모델 설정별 성과를 비교해서 다음 생성
프롬프트에 반영할 수 있다.
