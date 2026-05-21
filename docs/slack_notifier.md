# Slack 콘텐츠 승인 노티파이어

draft `.md` 파일을 Slack 채널에 카드(Block Kit + ✅/❌/📝 버튼)로 게시한다.
승인 버튼을 누르면 `slack_approval_worker.py` 가 자동으로 적절한 uploader 를 실행한다.

## 기본 워크플로우

```bash
# 1) draft 생성 (기존 도구)
python3 assets/tool-seeds/instagram/threads_uploader.py --text "안녕 일본" --account jp
#   → drafts/threads-jp-20260521-101010.md 저장됨

# 2) Slack 으로 승인 요청 카드 보내기
python3 assets/tool-seeds/instagram/slack_notifier.py \
    --draft-path assets/tool-seeds/instagram/drafts/threads-jp-20260521-101010.md \
    --platform threads \
    --account jp

# 3) Slack 채널에서 ✅ 버튼 클릭 → 워커가 실제 업로드
```

## 환경변수

`_company/_agents/instagram/.env` 에 추가:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL_ID=C0...
```

설정 가이드: `assets/tool-seeds/instagram/slack_setup.md`

## 폴백 동작

- `SLACK_BOT_TOKEN` 미설정 → `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 있으면 텔레그램으로 발송
- 텔레그램도 없으면 stdout 에 안내만 출력 후 exit 0 (워크플로우는 안 끊김)

## 출력 (JSON)

성공:
```json
{"status":"posted","channel":"C0...","ts":"1700000000.000000","draft_path":"...","platform":"threads","account":"jp"}
```

폴백:
```json
{"status":"fallback","reason":"...","draft_path":"...","telegram_sent":false,"message":"Slack 미설정 — 콘텐츠를 직접 검토하세요"}
```
