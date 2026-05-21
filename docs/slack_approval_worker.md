# Slack 승인 워커 (Socket Mode)

`slack_notifier.py` 가 보낸 카드의 ✅/❌/📝 버튼을 처리하는 백그라운드 데몬.

## 동작

| 버튼 | 동작 |
|------|------|
| ✅ 승인 → 업로드 | draft 의 platform/account 확인 → 해당 uploader.py 호출 (실제 게시) → 메시지에 permalink 표시 |
| ❌ 거절 | 모달로 사유 입력 → frontmatter 에 기록 → `drafts/rejected/` 로 파일 이동 |
| 📝 수정 요청 | 모달로 본문 수정 → frontmatter 의 `status: awaiting_approval` 유지 → ✅/❌ 버튼 다시 표시 |

## 의존성

```bash
/opt/homebrew/bin/python3 -m pip install --user slack-sdk
```

## 환경변수

```bash
SLACK_BOT_TOKEN=xoxb-...    # 봇 OAuth 토큰
SLACK_APP_TOKEN=xapp-...    # App-Level Token (connections:write)
SLACK_CHANNEL_ID=C0...      # (선택, payload 채널 우선)
```

## 백그라운드 실행 (launchd)

권장 — `~/Library/LaunchAgents/com.moneyai.slack-worker.plist` 사용.

```bash
launchctl unload ~/Library/LaunchAgents/com.moneyai.slack-worker.plist 2>/dev/null
launchctl load   ~/Library/LaunchAgents/com.moneyai.slack-worker.plist
tail -f /tmp/moneyai-slack-worker.log
```

## 디버깅

| 증상 | 확인 |
|------|------|
| 워커 안 뜸 | `launchctl list \| grep moneyai`, `/tmp/moneyai-slack-worker.err` |
| 메시지 안 옴 | `slack_notifier.py` 출력 JSON 확인 |
| 버튼 클릭해도 반응 X | Slack App → Socket Mode 활성? → App-Level Token `connections:write` 스코프? |
| 업로드 실패 | `tail /tmp/moneyai-slack-worker.err`, `token_manager.py --status` |

전체 셋업: `assets/tool-seeds/instagram/slack_setup.md`
